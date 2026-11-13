"""The admin site, run from the Harbor release in use (cutover step 4a of dev/HARBOR.md).

  /opt/harbor/current/admin   its code: the release's, so the admin and harbor are one version
  /opt/harbor/venv                                      its Python (Flask, gunicorn), the same for every release
  /var/www/<admin>/app                                  what it writes: config.json (the login), audit.log,
                                                        sql-connections.json, system-update.*, static/vendor

The service names the data folder in HARBOR_ADMIN_DATA. A rollback of Harbor is a rollback of the admin too.
Was install_admin_site.sh, update_admin_site.sh, set_password.sh and uninstall_admin_site.sh.
"""
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.request

from harbor.core.bindings import site_binding
from harbor.core.names import normalize_site_name
from harbor.core.ports import allocate_port
from harbor.core.site_config import install_apache_config, issue_certificate
from harbor.core.system import Stop
from harbor.core.templates import Site, render
from harbor.install.release import CURRENT, ROOT
from harbor.server.init import INITIALIZED

VENV = f"{ROOT}/venv"
APP = f"{CURRENT}/admin"
TEMPLATES = Path(__file__).resolve().parents[2] / "templates/admin"
MIN_PASSWORD = 12
USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")
ADDRESS = re.compile(r"[0-9A-Fa-f:.]+(/[0-9]{1,3})?")

# The front-end libraries, downloaded into the data folder (not stored in the repository)
XTERM = "5.3.0"
XTERM_FIT = "0.8.0"
MONACO = "0.52.2"
CDN = "https://cdn.jsdelivr.net/npm"


def web_root():
    return os.environ.get("VNS_WEB_ROOT") or "/var/www"


def is_admin_site(path):
    """A site folder that is the admin's: its app folder holds the admin's login (config.json), whether the admin's code
    is there (VNS 1.x) or in the release (Harbor)"""
    app = Path(path) / "app"
    if (app / "manage.py").exists() and (app / "config.json").exists():
        return True
    try:
        config = json.loads((app / "config.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and bool(config.get("secret_key"))


def admin_sites(system):
    """The admin sites here"""
    root = system.path(web_root())
    return [path.name for path in sorted(root.iterdir()) if path.is_dir() and is_admin_site(path)] if root.is_dir() else []


def choose(system, name):
    found = admin_sites(system)
    if not name:
        if not found:
            raise Stop(f"No admin site found in {web_root()}. Use harbor admin install.")
        if len(found) > 1:
            system.say(f"Admin sites: {' '.join(found)}")
            name = system.ask("Enter admin site folder name: ").strip()
        else:
            name = found[0]
    if name not in found:
        raise Stop(f"{name} is not an admin site.")
    return name


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "VNS-Harbor"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def download_vendor(system, app):
    """xterm.js and Monaco in <app>/static/vendor; what is there is kept"""
    vendor = system.path(f"{app}/static/vendor")
    vendor.mkdir(parents=True, exist_ok=True)
    files = {"xterm.js": f"{CDN}/xterm@{XTERM}/lib/xterm.js", "xterm.css": f"{CDN}/xterm@{XTERM}/css/xterm.css",
             "xterm-addon-fit.js": f"{CDN}/xterm-addon-fit@{XTERM_FIT}/lib/xterm-addon-fit.js"}
    if any(not (vendor / name).is_file() for name in files):
        system.say(f"Download the terminal library (xterm.js {XTERM})")
        for name, url in files.items():
            (vendor / name).write_bytes(fetch(url))
    if not (vendor / "monaco/vs/editor/editor.main.js").is_file():
        system.say(f"Download the code editor (Monaco {MONACO})")
        try:
            archive = fetch(f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{MONACO}.tgz")
            with tempfile.TemporaryDirectory() as folder, tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                members = [member for member in tar.getmembers() if member.name.startswith("package/min/vs/")
                           and (member.isfile() or member.isdir())]
                tar.extractall(folder, members=members, filter="data")
                shutil.rmtree(vendor / "monaco", ignore_errors=True)
                (vendor / "monaco").mkdir()
                shutil.move(f"{folder}/package/min/vs", vendor / "monaco/vs")
        except (OSError, tarfile.TarError) as error:
            system.warn(f"Could not download Monaco ({error}); the admin site falls back to a plain text editor.")


def prepare_python(system):
    """/opt/harbor/venv with what the admin of the release in use needs"""
    if not system.path(f"{VENV}/bin/python").is_file():
        system.say(f"Create the admin's Python environment in {VENV}")
        system.run("python3", "-m", "venv", VENV)
    system.run(f"{VENV}/bin/pip", "install", "--quiet", "--disable-pip-version-check", "-r", f"{APP}/requirements.txt")


def install_service(system, site):
    """The admin's service, running the release in use with its data in the site's app folder"""
    text = render((TEMPLATES / "service.tpl").read_text(), site).replace("@HARBOR@", ROOT)
    system.path(site.service_path).write_text(text)
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "enable", site.service_name)
    # The service uses KillMode=process, so terminals opened in the admin site keep running
    system.run("systemctl", "restart", site.service_name)


def service_port(system, site):
    try:
        text = system.path(site.service_path).read_text()
    except OSError:
        text = ""
    match = re.search(r"--bind 127\.0\.0\.1:([0-9]+)", text)
    if not match:
        raise Stop(f"The port of the admin site {site.name} could not be read from {site.service_path}.")
    return match[1]


def write_config(system, path, config):
    path = system.path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(config, stream, indent=2)
    os.chmod(path, 0o600)


def update(system, name=""):
    """The admin site moved to (or kept on) the release in use: its Python, its libraries and its service. Its login and
    the rest of its data stay where they are."""
    name = choose(system, name)
    site = Site(name)
    data = f"{site.site_root}/app"
    system.step("Update the admin's Python packages")
    prepare_python(system)
    download_vendor(system, data)
    config_path = f"{data}/config.json"
    config = json.loads(system.path(config_path).read_text())
    # The admin finds Harbor beside itself: the folder VNS 1.x named here is not used
    if "apache_dir" in config:
        del config["apache_dir"]
        write_config(system, config_path, config)
    site.port_http = service_port(system, site)
    system.step(f"Restart the admin site {name}")
    install_service(system, site)
    system.say(f"Admin site '{name}' runs Harbor from {CURRENT}.")


def ask_password(system):
    password = os.environ.get("VNS_ADMIN_PASSWORD", "")
    if not password:
        password = system.ask(f"Admin password (min {MIN_PASSWORD} characters): ", secret=True)
        if password != system.ask("Repeat password: ", secret=True):
            raise Stop("The passwords do not match.")
    if len(password) < MIN_PASSWORD:
        raise Stop(f"The password must be at least {MIN_PASSWORD} characters.")
    return password


def install(system, name="", binding="", user="", https="", allowed=None):
    """A new admin site. The password comes from VNS_ADMIN_PASSWORD, or is asked for."""
    from harbor.server.apache import set_servername
    from harbor.sites.create import ask
    if not system.path(INITIALIZED).is_file():
        raise Stop("The server is not set up yet. Run harbor server init first.")
    try:
        name = normalize_site_name(ask(system, name, "Enter admin site folder name"))
    except ValueError as error:
        raise Stop(str(error)) from None
    binding = ask(system, binding, "Enter admin site URL binding", name if "." in name else "")
    if not binding:
        raise Stop("No binding given.")
    # A certificate needs a public name with a dot. A name without one can only be served on HTTP.
    if "." not in binding and not https:
        system.say(f"{binding} has no dot, so it cannot get a certificate: the admin site will use HTTP.")
        https = "n"
    https = ask(system, https, "Use HTTPS with a Let's Encrypt certificate? (y/n)", "y").lower()
    if https not in ("y", "yes", "n", "no"):
        raise Stop("Answer y or n.")
    https = https[0] == "y"
    try:
        binding = site_binding(binding, http_only=not https)
    except ValueError as error:
        raise Stop(str(error)) from None
    user = ask(system, user, "Admin username", "admin")
    if not USER.fullmatch(user):
        raise Stop("Invalid username. Use letters, numbers and . _ @ - (max 64).")
    password = ask_password(system)

    system.say("The admin site gives root access to this server. Restricting it to your own IP addresses is strongly recommended.")
    if not https:
        system.say("Without HTTPS the password and everything you do are sent unencrypted. Restrict it to addresses on a network you trust.")
    if allowed is None:
        allowed = system.ask("Allowed IP addresses or CIDR ranges, separated by spaces (empty = everyone): ")
    addresses = allowed.replace(",", " ").split()
    for address in addresses:
        if not ADDRESS.fullmatch(address):
            raise Stop(f"Invalid IP address or range: {address}")

    site = Site(name, binding=binding, access_rules="Require ip " + " ".join(addresses) if addresses else "")
    site.doc_root = f"{site.site_root}/wwwroot"
    if system.path(site.site_root).exists():
        raise Stop(f"{site.site_root} already exists.")
    reservation = allocate_port(system, name)
    try:
        site.port_http = str(reservation.port)
        data = f"{site.site_root}/app"
        system.path(f"{data}/static/vendor").mkdir(parents=True)
        system.path(site.doc_root).mkdir(parents=True)
        system.run("a2enmod", "-q", "proxy", "proxy_http", "proxy_wstunnel", "rewrite", "headers")
        set_servername(system)
        if https:
            system.run("a2enmod", "-q", "ssl")
            system.step(f"Issue a certificate for {binding}")
            issue_certificate(system, site, binding)
        system.step("Prepare the admin's Python")
        prepare_python(system)
        download_vendor(system, data)
        system.step("Create the login")
        system.run(f"{VENV}/bin/python", f"{APP}/manage.py", "init", "--user", user, "--admin-site", name,
                   "--web-root", site.root_path, *([] if https else ["--insecure-cookies"]),
                   env={"VNS_ADMIN_PASSWORD": password, "HARBOR_ADMIN_DATA": str(system.path(data))}, cwd=APP)
        system.run("chown", "-R", "root:root", data)
        system.run("chmod", "700", data)
        install_apache_config(system, site, TEMPLATES / "apache.conf.tpl", certificate=https)
        system.step("Start the admin site")
        install_service(system, site)
    finally:
        reservation.release()
    system.say(f"Admin site created: {'https' if https else 'http'}://{binding}/ (user: {user}, folder: {name})")
    system.say("Change the password later with: harbor admin set-password")


def set_password(system, name=""):
    """A new login password; it also signs out every session"""
    name = choose(system, name)
    site = Site(name)
    system.run(f"{VENV}/bin/python", f"{APP}/manage.py", "set-password",
               env={"HARBOR_ADMIN_DATA": str(system.path(f"{site.site_root}/app"))}, cwd=APP)
    system.run("systemctl", "restart", site.service_name)
    system.say("Password changed. Log in again.")


def uninstall(system, name="", delete_certificate=""):
    """The admin site removed: files, login, service and Apache config. Other sites are not touched."""
    from harbor.sites.delete import delete_site
    name = choose(system, name)
    system.say(f"This removes the admin site '{name}' completely (files, login, service and Apache config).")
    system.say("If you run this from the admin site's own terminal, the terminal will end when the site is removed.")
    if system.ask("Type the folder name to confirm: ").strip() != name:
        raise Stop("Cancelled.")
    if not delete_certificate:
        delete_certificate = system.ask("Also revoke and remove the certificate? [y/N]: ")
    delete_site(system, name, "y" if delete_certificate.strip().lower() in ("y", "yes") else "n")
