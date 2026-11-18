"""Add an environment to a site: a remote Linux server (SSH), a local folder, or an IIS site on a Windows server (SMB).
Was the site template csharp/scripts/create_prod.sh.

The settings come from the admin's dialog (VNS_PROD_MODAL=1 and VNS_PROD_*), or are asked for. They are saved in
Harbor (its state, the passwords in its secrets), and the environment is published and its content synced when that
is possible. A remote or Windows destination that already holds a site is adopted as it is: its settings are read
from the server, it must run the dll this site builds, and nothing is published, synced or changed there.
VNS_PROD_NO_PUBLISH=1 (adopt_site, move_env, register_env) only saves the settings.
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from harbor.core.bindings import validate_binding
from harbor.core.names import normalize_dns_name, normalize_site_name, service_name
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.sites.create import ask
from harbor.core.paths import BIN, LIB

# The settings an environment has, as its publish_prod.sh held them: rendered with the values, then read as bash reads
# them (settings.parse), so a value is quoted and kept exactly as before
LINUX_SETTINGS = """PROD_LOCAL_PATH=@PROD_LOCAL_PATH@
PROD_IP=@PROD_IP@
PROD_USER=@PROD_USER@
PROD_PASSWORD=@PROD_PASSWORD@
PROD_SSH_PORT=@PROD_SSH_PORT@
PROD_FOLDER=@PROD_FOLDER@
PROD_BINDING=@PROD_BINDING@
PROD_HTTPS=@PROD_HTTPS@
PROD_ENVIRONMENT="@PROD_ENVIRONMENT@"
PROD_ADMIN_MAIL="@ADMIN_MAIL@"
"""
WINDOWS_SETTINGS = """PROD_TARGET=windows
PROD_WINDOWS_SHARE=@PROD_WINDOWS_SHARE@
PROD_SMB_USER=@PROD_SMB_USER@
PROD_SMB_PASSWORD=@PROD_SMB_PASSWORD@
PROD_SMB_DOMAIN=@PROD_SMB_DOMAIN@
PROD_FOLDER=@PROD_FOLDER@
PROD_BINDING=@PROD_BINDING@
PROD_HTTPS=@PROD_HTTPS@
PROD_ENVIRONMENT="@PROD_ENVIRONMENT@"
PROD_OFFLINE_WAIT=5
"""
ENVIRONMENTS = ("Development", "Test", "Staging", "Production")


def quoted(value):
    """Single quotes, as %q writes non-ASCII names differently depending on the locale"""
    return "'" + value.replace("'", "'\\''") + "'"


def render(text, values):
    """@NAME@ for every R_NAME given, as create_prod.sh rendered with perl; what is not given stays"""
    return re.sub(r"@([A-Z_]+)@", lambda match: values.get(match[1], match[0]), text)


class Request:
    """What the environment is to be: the admin's dialog, or the answers in a terminal"""

    def __init__(self, system, main, target_arg=""):
        self.system = system
        env = os.environ
        self.local_path = self.ip = self.user = self.password = self.ssh_port = ""
        self.smb_share = self.smb_user = self.smb_password = self.smb_domain = ""
        self.https = ""
        if env.get("VNS_PROD_MODAL") == "1":
            self.target = env.get("VNS_PROD_TARGET", "")
            self.local_path = env.get("VNS_PROD_LOCAL_PATH", "")
            self.ip = env.get("VNS_PROD_IP", "")
            self.user = env.get("VNS_PROD_USER", "")
            self.password = env.get("VNS_PROD_PASSWORD", "")
            self.ssh_port = env.get("VNS_PROD_SSH_PORT") or "22"
            self.folder = env.get("VNS_PROD_FOLDER", "")
            self.binding = env.get("VNS_PROD_BINDING", "")
            self.environment = env.get("VNS_PROD_ENVIRONMENT") or "Production"
            self.smb_share = env.get("VNS_PROD_SMB_SHARE", "")
            self.smb_user = env.get("VNS_PROD_SMB_USER", "")
            self.smb_password = env.get("VNS_PROD_SMB_PASSWORD", "")
            self.smb_domain = env.get("VNS_PROD_SMB_DOMAIN", "")
            self.https = env.get("VNS_PROD_HTTPS") or "y"
            return
        self.target = ask(system, target_arg, "Production target (remote/local/windows)", "remote")
        default_folder = ""
        if self.target == "remote":
            self.ip = ask(system, "", "Production server IP or hostname")
            if not self.ip:
                raise Stop("IP is required.")
            self.user = ask(system, "", "SSH login", "root")
            self.password = system.ask("SSH password (empty = use an SSH key): ", secret=True)
            self.ssh_port = ask(system, "", "SSH port", "22")
            if not self.ssh_port.isdigit():
                raise Stop("SSH port must be a number.")
            default_folder = main.name
        elif self.target == "local":
            self.local_path = ask(system, "", "Local production folder (absolute path)", f"{main.site_root}/_prod")
            if not self.local_path.startswith("/"):
                raise Stop("The path must be absolute.")
            default_folder = f"prod-{main.name}"
        elif self.target == "windows":
            system.say("The IIS site and its folder must already exist on the Windows server.")
            self.smb_share = ask(system, "", "Folder of the IIS site (e.g. //server/share/site-folder)")
            self.smb_user = ask(system, "", "SMB username", "Administrator")
            self.smb_password = system.ask("SMB password: ", secret=True)
            self.smb_domain = ask(system, "", "SMB domain (empty = the server is not on a domain)")
        else:
            raise Stop("Target must be remote, local or windows.")
        if self.target == "windows":
            self.binding = ask(system, "", "Production binding (the domain of the IIS site)", main.binding)
            self.folder = self.binding
        else:
            self.folder = ask(system, "", "Production site name (folder /var/www/<name>, service and Apache config)", default_folder)
            self.binding = ask(system, "", "Production binding (domain)", self.folder)
        self.environment = ask(system, "", "Environment (Development/Test/Staging/Production)", "Production")
        # Follow this site: an HTTP-only site usually has an HTTP-only production site
        config = system.path(f"/etc/apache2/sites-available/{main.name}.conf")
        https_default = "y" if config.is_file() and re.search(r"(?m)^[ \t]*SSLEngine[ \t]+On([ \t]|$)", config.read_text(errors="replace")) else "n"
        if self.target == "windows":
            self.https = ask(system, "", "Does the IIS site use HTTPS? (y/n)", https_default)
        else:
            self.https = ask(system, "", "Use HTTPS? y issues a certificate, n runs on plain HTTP (y/n)", https_default)


class Main:
    """The values the template was rendered with: the site the environment belongs to"""

    def __init__(self, name, site_root, binding, project, publish_folder, admin_mail, kind):
        self.name, self.site_root, self.binding, self.project = name, site_root, binding, project
        self.publish_folder, self.admin_mail, self.kind = publish_folder, admin_mail, kind


def create_environment(system, main, target_arg=""):
    scripts = f"{main.site_root}/scripts"
    # The environments' settings are in Harbor: a site that still has its scripts is migrated first
    if not settings.migrated(system, main.site_root):
        raise Stop(f"{main.name} still has its scripts: migrate it first with: harbor migrate {main.name}")
    request = Request(system, main, target_arg)
    no_publish = os.environ.get("VNS_PROD_NO_PUBLISH") == "1"

    if request.environment not in ENVIRONMENTS:
        raise Stop("Environment must be Development, Test, Staging or Production.")
    if request.https.lower() in ("y", "yes"):
        request.https = "y"
    elif request.https.lower() in ("n", "no"):
        request.https = "n"
    else:
        raise Stop("HTTPS must be y or n.")
    if request.target not in ("remote", "local", "windows"):
        raise Stop("Target must be remote, local or windows.")
    try:
        request.folder = normalize_site_name(request.folder)
        request.binding = normalize_dns_name(request.binding, fully_qualified=True)
    except ValueError as error:
        raise Stop(str(error)) from None
    try:
        validate_binding(request.binding)
    except ValueError as error:
        raise Stop(str(error)) from None
    root_path = main.site_root.rsplit("/", 1)[0]
    if request.target == "remote":
        if not (request.ip and request.user and request.ssh_port.isdigit() and 1 <= int(request.ssh_port) <= 65535):
            raise Stop("Remote target requires a host, SSH user and valid SSH port.")
    elif request.target == "windows":
        share = request.smb_share.replace("\\", "/").rstrip("/")
        request.smb_share = share
        # A folder below the share is required, so a typo cannot mirror files into the root of a drive
        if not re.fullmatch(r"//[A-Za-z0-9][A-Za-z0-9.-]*/[^/]+(/[^/]+)+", share) or "/../" in f"/{share}/" or "/./" in f"/{share}/":
            raise Stop("The folder must look like //server/share/folder.")
        if not request.smb_user or not request.smb_password:
            raise Stop("Windows target requires an SMB username and password.")
        if not re.fullmatch(r"[A-Za-z0-9._-]*", request.smb_domain):
            raise Stop("Invalid SMB domain.")
    else:
        if not request.local_path.startswith("/"):
            raise Stop("The local production folder must be an absolute path.")
        if service_name(request.folder) == service_name(main.name) or request.binding == main.binding:
            raise Stop("A local production site needs a different name and binding than this site.")
        local = request.local_path.rstrip("/") if request.local_path != "/" else "/"
        # Outside this site's folder it is a site of its own: its folder, service and Apache config share one name
        if not local.startswith(f"{main.site_root}/"):
            if not no_publish and local != f"{root_path}/{request.folder}":
                raise Stop(f"A folder outside this site must be {root_path}/{request.folder} ({root_path}/<production site name>).")
            if not no_publish and os.path.lexists(system.path(request.local_path)):
                raise Stop(f"{request.local_path} already exists.")

    # The saved settings of the site's other environments, read without running their scripts
    if request.target == "local":
        for other_id in settings.target_ids(system, main.site_root):
            try:
                other = settings.values(system, main.site_root, other_id) or {}
            except settings.SettingsError:
                continue
            if not other.get("LOCAL_PATH"):
                continue
            if other["LOCAL_PATH"].rstrip("/") == request.local_path.rstrip("/") or service_name(other.get("FOLDER") or "x") == service_name(request.folder):
                raise Stop("Another environment on this server already uses that folder or site name.")

    # The folder is named after the environment: production, production-2, ...
    target_id = os.environ.get("VNS_PROD_ID") or request.environment.lower()
    number = 2
    taken = set(settings.target_ids(system, main.site_root))
    while os.path.lexists(system.path(f"{scripts}/targets/{target_id}")) or target_id in taken:
        target_id = f"{request.environment.lower()}-{number}"
        number += 1

    where = (f" ({request.user}@{request.ip}:{request.ssh_port})" if request.ip else "") + (f" ({request.local_path})" if request.local_path else "") \
        + (f" ({request.smb_user}@{request.smb_share})" if request.smb_share else "")
    system.say("")
    system.say(f"Target:   {request.target}{where}")
    system.say(f"Site:     {request.folder}")
    system.say(f"Binding:  {request.binding}")
    system.say(f"Environment: {request.environment}")
    system.say(f"Protocol: {'HTTPS' if request.https == 'y' else 'HTTP'}")
    if os.environ.get("VNS_PROD_MODAL") != "1":
        answer = system.ask("Add the environment? [Y/n]: ").strip() or "y"
        if not answer.lower().startswith("y"):
            system.say("Cancelled.")
            raise Stop("", 1)

    # Whether the destination already holds a site, for instance an environment that was removed here but kept on its
    # server. Then it is only registered: publishing would overwrite its files.
    has_files = False
    if request.target == "windows":
        has_files, request.environment = adopt_windows(system, main, request, no_publish)
    elif request.target == "remote":
        environment = settings.Environment(IP=request.ip, USER=request.user, PASSWORD=request.password, SSH_PORT=request.ssh_port)
        found = settings.remote_exec(system, environment, f"find {shlex.quote(f'/var/www/{request.folder}')} -mindepth 1 -maxdepth 1 -print -quit",
                                     check=False, capture=True)
        has_files = found.ok and bool(found.stdout.strip())
        # A site on a Linux server is adopted with register_env: it reads the binding, protocol and environment from the
        # server, checks that the site runs the dll this site builds, and publishes nothing
        if has_files and not no_publish:
            system.say(f"/var/www/{request.folder} on {request.ip} already holds a site: it is adopted as it is, with its settings read from the server.")
            from harbor.environments.register import register_environment
            saved = dict(os.environ)
            os.environ.update(VNS_PROD_IP=request.ip, VNS_PROD_USER=request.user, VNS_PROD_PASSWORD=request.password,
                              VNS_PROD_SSH_PORT=request.ssh_port, VNS_PROD_FOLDER=request.folder)
            try:
                register_environment(system, main.name)
            finally:
                os.environ.clear()
                os.environ.update(saved)
            return
    if has_files and not no_publish:
        system.say("")
        system.say(f"{request.smb_share} already holds this site. It is adopted as it is, with its environment ({request.environment}) read")
        system.say("from its web.config: nothing is published, synced or changed on the server. Publish it when you want to update it.")

    values = {"PROD_LOCAL_PATH": quoted(request.local_path), "PROD_IP": quoted(request.ip), "PROD_USER": quoted(request.user),
              "PROD_PASSWORD": quoted(request.password), "PROD_SSH_PORT": quoted(request.ssh_port), "PROD_FOLDER": quoted(request.folder),
              "PROD_BINDING": quoted(request.binding), "PROD_ENVIRONMENT": request.environment, "ADMIN_MAIL": main.admin_mail,
              "PROJECT_NAME": main.project, "PUBLISH_FOLDER": main.publish_folder, "SITE": main.name, "PROD_HTTPS": request.https,
              "PROD_WINDOWS_SHARE": quoted(request.smb_share), "PROD_SMB_USER": quoted(request.smb_user),
              "PROD_SMB_PASSWORD": quoted(request.smb_password), "PROD_SMB_DOMAIN": quoted(request.smb_domain),
              }
    # Its settings into Harbor's copy, as the script held them; Harbor publishes and syncs
    found = settings.parse(render(WINDOWS_SETTINGS if request.target == "windows" else LINUX_SETTINGS, values))
    settings.write_copy(system, main.site_root, target_id, found)
    system.say(f"Environment {target_id} saved in Harbor.")
    finish_migrated(system, main, request, target_id, no_publish, has_files)


def finish_migrated(system, main, request, target_id, no_publish, has_files):
    """A new environment of a migrated site published and synced by harbor, as its scripts did for the others"""
    if no_publish:
        return
    if has_files:
        system.say(f"Environment {target_id} is the site that was already on the server. Nothing was published.")
        return
    for key in ("VNS_PROD_PASSWORD", "VNS_PROD_SMB_PASSWORD"):
        os.environ.pop(key, None)
    system.plan([f"Publish to {request.environment}", *([f"Sync content to {request.environment}"] if main.kind == "umbraco" else [])])
    system.step(f"Publish to {request.environment}")
    published = system.run("python3", str(BIN), "env", "publish", main.name, env={"VNS_TARGET": target_id}, check=False)
    if not published.ok:
        raise Stop("", published.returncode)
    if main.kind == "umbraco":
        system.step(f"Sync content to {request.environment}")
        synced = system.run("python3", str(BIN), "env", "sync", main.name, "main", target_id, check=False)
        if not synced.ok:
            raise Stop("", synced.returncode)
    system.say(f"Environment {target_id} is ready.")


def adopt_windows(system, main, request, no_publish):
    """(whether the share holds a site, the environment): the share and login are checked before anything is changed"""
    from harbor.environments.windows import Share
    environment = settings.Environment(TARGET="windows", WINDOWS_SHARE=request.smb_share, SMB_USER=request.smb_user,
                                       SMB_PASSWORD=request.smb_password, SMB_DOMAIN=request.smb_domain)
    try:
        share = Share(system, environment).connect()
    except RuntimeError as error:
        raise Stop(str(error)) from None
    listing = share.rclone("lsf", share.target, "--max-depth", "1", capture=True).stdout.splitlines()
    files = share.rclone("lsf", share.target, "--max-depth", "1", "--files-only", capture=True).stdout.splitlines()
    name = next((line for line in files if line.lower() == "web.config"), "")
    with tempfile.TemporaryDirectory() as folder:
        web_config = Path(folder, "web.config")
        if name:
            share.rclone("copyto", f"{share.target}/{name}", str(web_config))
        has_web_config = web_config.is_file() and web_config.stat().st_size > 0
        # A site is there when it has a web.config or a dll; IIS's own start page alone is not a site
        has_files = has_web_config or any(line.lower().endswith(".dll") for line in listing)
        if not has_files or no_publish:
            return has_files, request.environment
        if not has_web_config:
            raise Stop(f"{request.smb_share} holds a site without a web.config, so it cannot be adopted. Empty the folder, or choose another.")
        described = system.run("python3", str(LIB / "windows_prod.py"), "describe", str(web_config), capture=True, check=False)
        if not described.ok:
            if described.stderr.strip():
                system.warn(described.stderr.rstrip("\n"))
            raise Stop("", described.returncode)
        fields = dict(line.split("=", 1) for line in described.stdout.splitlines() if "=" in line)
        project = system.path(f"{main.site_root}/{main.project}.csproj")
        assembly = next(iter(re.findall(r"<AssemblyName>(.*)</AssemblyName>", project.read_text(errors="replace"))), "") if project.is_file() else ""
        assembly = assembly or main.project
        if fields.get("dll", "").lower() != assembly.lower():
            element = re.search(r"<aspNetCore[^>]*>", web_config.read_text(errors="replace"), re.I)
            system.warn(f"{request.smb_share} runs {fields.get('dll') or 'an unknown'}.dll, but {main.name} builds {assembly}.dll, so it is not this site. "
                        "Nothing was changed.")
            raise Stop(f"Its web.config starts the site with: {element[0] if element else 'no aspNetCore element'}")
        environment_name = fields.get("environment", "")
        return True, environment_name if environment_name in ENVIRONMENTS else request.environment
