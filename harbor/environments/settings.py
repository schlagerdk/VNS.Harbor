"""The environments of a site and their settings, and running commands where one is. From lib/prod.sh: prod_targets,
load_main_site, load_prod_settings, prod_exec and prod_run.

Each environment has its settings in <site>/scripts/targets/<id>/publish_prod.sh, or in <site>/scripts/publish_prod.sh
for one made before several were possible (id "prod"). Only the PROD_* assignments in it are read, as the script does.
"""
from dataclasses import dataclass, field, fields
import os
import json
import re
import shlex

TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ASSIGNMENT = re.compile(r"PROD_[A-Z_]+=")


from harbor.core.system import Stop  # noqa: E402


class SettingsError(Stop, ValueError):
    """Settings that cannot be used: the command stops with the message, as the script did"""


@dataclass
class Environment:
    """An environment's settings. What the script does not set is empty, and the SSH port 22."""
    id: str = "prod"
    script: str = ""
    LOCAL_PATH: str = ""
    IP: str = ""
    USER: str = ""
    PASSWORD: str = field(default="", repr=False)
    SSH_PORT: str = "22"
    FOLDER: str = ""
    BINDING: str = ""
    PORT_HTTP: str = ""
    ADMIN_MAIL: str = ""
    HTTPS: str = ""
    TARGET: str = ""
    WINDOWS_SHARE: str = ""
    SMB_USER: str = ""
    SMB_PASSWORD: str = field(default="", repr=False)
    SMB_DOMAIN: str = ""
    other: dict = field(default_factory=dict)

    @property
    def windows(self):
        return self.TARGET == "windows"

    @property
    def local(self):
        return not self.windows and bool(self.LOCAL_PATH)

    @property
    def description(self):
        """Where it is, as the script's _target says"""
        if self.windows:
            return self.WINDOWS_SHARE
        if self.LOCAL_PATH:
            return "this server"
        return f"{self.USER}@{self.IP}:{self.SSH_PORT}"

    @property
    def root(self):
        """Its folder where it runs, as the script's _root"""
        if self.windows:
            return self.WINDOWS_SHARE
        if self.LOCAL_PATH:
            # As ${PROD_LOCAL_PATH%/}: one trailing slash goes
            return self.LOCAL_PATH[:-1] if self.LOCAL_PATH.endswith("/") else self.LOCAL_PATH
        return f"/var/www/{self.FOLDER}"


SETTINGS = {item.name for item in fields(Environment)} - {"id", "script", "other"}


def script_ids(system, site_root):
    """The environments that have a script in the site folder: the older single one first, then scripts/targets"""
    root = system.path(site_root)
    ids = ["prod"] if (root / "scripts/publish_prod.sh").is_file() else []
    targets = root / "scripts/targets"
    if targets.is_dir():
        ids += sorted(path.name for path in targets.iterdir() if path.is_dir() and (path / "publish_prod.sh").is_file())
    return ids


def target_ids(system, site_root):
    """The id of every environment of a site. While the site folder has any of their scripts it is the truth;
    without, Harbor's copy is (harbor/state)."""
    ids = script_ids(system, site_root)
    if ids:
        return ids
    copy = system.path(f"{STATE}/{site_name(site_root)}/environments")
    found = sorted(path.stem for path in copy.glob("*.json")) if copy.is_dir() else []
    return (["prod"] if "prod" in found else []) + [item for item in found if item != "prod" and TARGET.fullmatch(item)]


def settings_script(site_root, target="prod"):
    if target != "prod":
        if not TARGET.fullmatch(target):
            raise SettingsError(f"Invalid environment: {target}")
        return f"{site_root}/scripts/targets/{target}/publish_prod.sh"
    return f"{site_root}/scripts/publish_prod.sh"


def parse(text):
    """The PROD_* values a script sets, as bash reads them: quotes, escapes and comments as in the shell"""
    values = {}
    for line in text.splitlines():
        if not ASSIGNMENT.match(line):
            continue
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise SettingsError(f"Could not read the line {line.split('=', 1)[0]}: {error}") from None
        for word in words:
            name, equals, value = word.partition("=")
            if equals and re.fullmatch(r"PROD_[A-Z_]+", name):
                values[name[len("PROD_"):]] = value
    return values


# Harbor's copy of the state (harbor/state/store.py): read when the site folder no longer has the script
STATE = "/var/lib/harbor/sites"
SECRETS = "/etc/harbor/secrets/sites"


def site_name(site_root):
    return str(site_root).rstrip("/").rsplit("/", 1)[-1]


def migrated(system, site_root):
    """A site migrated (phase 6): Harbor's copy is its truth, its folder has no scripts or .vns-publish any more"""
    return system.path(f"{STATE}/{site_name(site_root)}/migrated.json").is_file()


SECRET_KEYS = ("PASSWORD", "SMB_PASSWORD")


def quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def write_copy(system, site_root, target, found):
    """An environment's settings into Harbor's copy: the passwords apart, only root may read them"""
    from harbor.state.store import write_json
    name = site_name(site_root)
    write_json(system.path(f"{STATE}/{name}/environments/{target}.json"), {key: value for key, value in found.items() if key not in SECRET_KEYS})
    hidden = {key: found[key] for key in SECRET_KEYS if found.get(key)}
    secret = system.path(f"{SECRETS}/{name}/{target}.json")
    if hidden:
        write_json(secret, hidden, private=True)
        for place in (system.path("/etc/harbor/secrets"), system.path(SECRETS), secret.parent):
            place.chmod(0o700)
    else:
        secret.unlink(missing_ok=True)


def update(system, site_root, target, changes):
    """Some of an environment's settings changed, such as {"HTTPS": "y"}: in its script while the site folder has it,
    else in Harbor's copy. A setting the script does not have yet is added after its other settings."""
    if not TARGET.fullmatch(target):
        raise SettingsError(f"Invalid environment: {target}")
    path = system.path(settings_script(site_root, target))
    if path.is_file():
        text = path.read_text()
        for key, value in changes.items():
            line = f"PROD_{key}={quote(value)}"
            pattern = re.compile(rf"(?m)^PROD_{re.escape(key)}=\S*")
            if pattern.search(text):
                text = pattern.sub(lambda match: line, text, count=1)
            else:
                last = list(re.finditer(r"(?m)^PROD_[A-Z_]+=.*$", text))
                at = last[-1].end() if last else 0
                text = text[:at] + ("\n" if at else "") + line + ("" if at else "\n") + text[at:]
        path.write_text(text)
        return
    found = values(system, site_root, target)
    if found is None:
        raise SettingsError(f"No environment {target} in {site_name(site_root)}.")
    write_copy(system, site_root, target, {**found, **{key: str(value) for key, value in changes.items()}})


def remove(system, site_root, target):
    """An environment's settings gone, from Harbor's copy (its script goes with its folder)"""
    name = site_name(site_root)
    system.path(f"{STATE}/{name}/environments/{target}.json").unlink(missing_ok=True)
    system.path(f"{SECRETS}/{name}/{target}.json").unlink(missing_ok=True)


def values(system, site_root, target="prod"):
    """An environment's settings, its passwords too: from its script in the site folder while that is there, else from
    Harbor's copy. None when it has neither."""
    path = system.path(settings_script(site_root, target))
    if path.is_file():
        return parse(path.read_text())
    name = site_name(site_root)
    copy = system.path(f"{STATE}/{name}/environments/{target}.json")
    if not copy.is_file():
        return None
    try:
        found = json.loads(copy.read_text())
        secret = system.path(f"{SECRETS}/{name}/{target}.json")
        if secret.is_file():
            found.update(json.loads(secret.read_text()))
    except (OSError, ValueError) as error:
        raise SettingsError(f"Could not read Harbor's settings of {name} {target}: {error}") from None
    return {key: str(value) for key, value in found.items()}


def load(system, site_root, target="prod"):
    """An environment's settings, as load_prod_settings reads them"""
    script = settings_script(site_root, target)
    found = values(system, site_root, target)
    if found is None:
        raise SettingsError(f"This site has no production site (missing {script}).")
    environment = Environment(id=target, script=script)
    for name, value in found.items():
        if name in SETTINGS:
            setattr(environment, name, value)
        else:
            environment.other[name] = value
    if not environment.FOLDER or not environment.BINDING:
        raise SettingsError(f"PROD_FOLDER and PROD_BINDING must be set in {script}")
    return environment


HARBOR_KEY = "/etc/harbor/ssh/id_ed25519"


def harbor_key():
    """ssh's options for Harbor's own key (harbor servers), offered besides root's own: an environment on a server that
    was added in Servers needs no password of its own"""
    return ["-i", HARBOR_KEY] if os.path.isfile(HARBOR_KEY) else []


def ssh_command(environment, tty=False):
    """The ssh to an environment's server, and the variables it needs: the password goes in SSHPASS, never in argv"""
    argv = ["ssh", "-p", environment.SSH_PORT, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    if tty:
        argv.append("-tt")
    env = {}
    if environment.PASSWORD:
        env["SSHPASS"] = environment.PASSWORD
        argv = ["sshpass", "-e", *argv]
    else:
        argv += ["-o", "BatchMode=yes", *harbor_key()]
    return argv, env


def remote_exec(system, environment, command, tty=False, input=None, check=True, capture=False):
    """Run a shell command as root where the environment is: here with bash, else over SSH (with sudo -n when not root)"""
    if environment.windows:
        raise SettingsError("This action is not available for a Windows production site. It has no SSH access: use IIS on the server.")
    if environment.LOCAL_PATH:
        return system.run("sudo", "bash", "-c", command, check=check, capture=capture, input=input)
    argv, env = ssh_command(environment, tty)
    sudo = "" if environment.USER == "root" else "sudo -n"
    return system.run(*argv, f"{environment.USER}@{environment.IP}", f"{sudo} {command}", check=check, capture=capture,
                      input=input, env=env)


def remote_run(system, environment, variables, script, check=True):
    """Run a bash script as root where the environment is, with the given variables (prod_run)"""
    return remote_exec(system, environment, f"env {variables} bash -s", input=script, check=check)


@dataclass
class MainSite:
    """What create_prod.sh is rendered with, read from the main site (load_main_site)"""
    project_name: str
    binding: str
    admin_mail: str
    publish_folder: str
    csharp_kind: str


def load_main_site(system, site):
    projects = sorted(system.path(site.site_root).glob("*.csproj"))
    if len(projects) != 1:
        raise SettingsError(f"{site.name} needs exactly one project.")
    config = system.path(site.config_path).read_text() if system.path(site.config_path).is_file() else ""
    binding = next((line.split()[1] for line in config.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")
    if not binding:
        raise SettingsError(f"{site.name} has no ServerName.")
    mail = next((line.split()[1] for line in config.splitlines() if line.split()[:1] == ["ServerAdmin"] and len(line.split()) > 1), "")
    service = system.path(site.service_path)
    working = ""
    if service.is_file():
        working = next((line[len("WorkingDirectory="):] for line in service.read_text().splitlines() if line.startswith("WorkingDirectory=")), "")
    working = working.rstrip("/")
    publish = "." if not working or working == site.site_root else working.rsplit("/", 1)[-1]
    kind = "umbraco" if "Umbraco.Cms" in projects[0].read_text(errors="replace") else "empty"
    return MainSite(projects[0].stem, binding, mail or site.admin_mail, publish, kind)


def environment_size(system, site_root, target):
    """How big an environment's folder is now, in MB, here (du) or on its server over SSH; None on Windows (IIS has no
    du) or when it cannot be told. For the usage limits (1.0.94)."""
    import shlex
    environment = load(system, site_root, target)
    if environment.windows:
        return None
    result = remote_exec(system, environment, f"du -sk {shlex.quote(environment.root)} 2>/dev/null", check=False, capture=True)
    try:
        return round(int((result.stdout or "").split()[0]) / 1024, 1)
    except (IndexError, ValueError):
        return None


def environment_sizes(system, web_root="/var/www", parallel=8):
    """Every environment of every site here, how big its folder is now and where it runs: measured where each runs, here
    or on its server over SSH, several at once. [{site, target, environment, host, size_mb}] (size_mb None on Windows
    or when not reached). For Usage (1.0.94)."""
    from concurrent.futures import ThreadPoolExecutor
    web = system.path(web_root)
    found = []
    for folder in sorted(web.iterdir()) if web.is_dir() else []:
        if not folder.is_dir() or folder.is_symlink():
            continue
        root = f"{web_root}/{folder.name}"
        for target in target_ids(system, root):
            try:
                environment = load(system, root, target)
            except SettingsError:
                continue
            found.append((folder.name, root, target, environment))

    def one(entry):
        site, root, target, environment = entry
        try:
            size = environment_size(system, root, target)
        except Exception:  # noqa: BLE001  (an environment not reached has no size now)
            size = None
        return {"site": site, "target": target, "environment": environment.other.get("ENVIRONMENT", ""), "binding": environment.BINDING,
                "local": environment.local, "windows": environment.windows, "folder": environment.FOLDER, "size_mb": size,
                # In the site's folder here: then it is in the site's size, and in its limit, as well (1.0.95)
                "in_site": bool(environment.local and not environment.windows and str(environment.root).rstrip("/").startswith(root + "/"))}
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(found) or 1))) as executor:
        return list(executor.map(one, found))
