"""A site's connection to Harbor through the VNS.Harbor package (Umbraco 17+), and the backoffice login from the admin.
Phase 9a of dev/HARBOR.md.

  /etc/harbor/secrets/sites/<site>/connection/<environment>.json   the site's token for Harbor's API and its login key
  /var/lib/harbor/sites/<site>/backoffice.json                     the Umbraco user the Backend button logs in as, per environment
  /var/lib/harbor/sites/<site>/backoffice-logins.log              who logged in from the admin, when, as whom
  vns-harbor.json in the folder the site runs from                 what the package reads (root:www-data 0640)

The environment "main" is the site itself. A login link is a one-time token: base64url(JSON) "." base64url(HMAC-SHA256 of
the first part with the login key), the JSON naming the site, the environment, the user, the expiry and a nonce.
"""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import time

from harbor.core.system import Stop
from harbor.environments import settings
from harbor.sites.actions import site_named
from harbor.sites.tools import working_folder
from harbor.state.store import SECRETS, STATE, read_json, write_json

LIFETIME = 60
PACKAGE = re.compile(r'<PackageReference\s+Include="VNS\.Harbor"', re.I)


def uses_package(system, site):
    return any(PACKAGE.search(path.read_text(errors="replace")) for path in system.path(site.site_root).glob("*.csproj"))


def connection(system, site, environment="main"):
    """The site's token and login key for an environment, made the first time"""
    path = system.path(f"{SECRETS}/{site.name}/connection/{environment}.json")
    found = read_json(path)
    if isinstance(found, dict) and found.get("token") and found.get("login_key"):
        return found
    found = {"token": secrets.token_hex(32), "login_key": secrets.token_hex(32)}
    write_json(path, found, private=True)
    for folder in (system.path("/etc/harbor/secrets"), system.path(SECRETS), path.parent.parent, path.parent):
        folder.chmod(0o700)
    return found


def running_folder(system, site, environment):
    """Where the site of an environment runs, on this server: the site's own service folder, or a local environment's"""
    if environment == "main":
        return working_folder(system, site)
    found = settings.load(system, site.site_root, environment)
    if not found.local:
        raise Stop("Only a site on this server can be connected yet: its environment is on another server.")
    return found.root


def admin_url(system):
    """Harbor's own address: the admin site on this server (the service that runs /var/www/<admin>/app), as its Apache
    config names it. Empty when there is none."""
    services = system.path("/etc/systemd/system")
    for service in sorted(services.glob("*.service")) if services.is_dir() else []:
        try:
            match = re.search(r"(?m)^WorkingDirectory=/var/www/([^/\n]+)/app$", service.read_text(errors="replace"))
        except OSError:
            continue
        if match:
            return address(system, site_named(system, match[1]), "main")
    return ""


def connect(system, name, environment="main", harbor_url=""):
    """vns-harbor.json where the site runs, so the package knows its Harbor, its token and its login key"""
    site = site_named(system, name)
    keys = connection(system, site, environment)
    path = system.path(f"{running_folder(system, site, environment)}/vns-harbor.json")
    data = {"harbor": harbor_url or (read_json(path) or {}).get("harbor", "") or admin_url(system), "site": site.name, "environment": environment,
            "token": keys["token"], "loginKey": keys["login_key"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o640)
    system.run("chown", "root:www-data", str(path), check=False, capture=True)
    system.say(f"{site.name} ({environment}) is connected to Harbor.")


def backoffice_user(system, name, environment="main"):
    """The Umbraco user for an environment's Backend button: its own, else the site's (an environment's database
    usually has the same users). The package checks that the user is there."""
    users = read_json(system.path(f"{STATE}/{site_named(system, name).name}/backoffice.json")) or {}
    if not isinstance(users, dict):
        return ""
    return users.get(environment) or users.get("main", "")


def set_backoffice_user(system, name, user, environment="main"):
    """The Umbraco user the Backend button logs in as (an email or a login name); empty turns it off"""
    site = site_named(system, name)
    if user and not re.fullmatch(r"[^\s/\\]{1,254}", user):
        raise Stop("Enter the Umbraco user's email or login name.")
    path = system.path(f"{STATE}/{site.name}/backoffice.json")
    users = read_json(path) or {}
    users = users if isinstance(users, dict) else {}
    if user:
        users[environment] = user
    else:
        users.pop(environment, None)
    write_json(path, users)
    system.say(f"The Backend button of {site.name} ({environment}) logs in as {user}." if user else f"The Backend button of {site.name} ({environment}) opens the login.")


def address(system, site, environment):
    """The site's own address: its binding, with https when its Apache config has a certificate"""
    if environment == "main":
        config = system.path(site.config_path)
        text = config.read_text(errors="replace") if config.is_file() else ""
        binding = next((line.split()[1] for line in text.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), site.name)
        # No backslash in an f-string: harbor runs on Python 3.10
        scheme = "https" if re.search(r"(?m)^[ \t]*SSLEngine[ \t]+On", text) else "http"
        return f"{scheme}://{binding}"
    found = settings.load(system, site.site_root, environment)
    return f"{'http' if found.HTTPS == 'n' else 'https'}://{found.BINDING}"


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def login_link(system, name, environment="main", who="", now=None):
    """A one-time link that logs the environment's chosen Umbraco user in to the backoffice"""
    site = site_named(system, name)
    user = backoffice_user(system, site.name, environment)
    if not user:
        raise Stop("No Umbraco user is chosen for the Backend button of this site.")
    # Where it runs on this server, it must run the package: published before the package was added, the login link
    # would lead nowhere, and the Backend button opens the backoffice's own login instead
    folder = running_folder(system, site, environment)
    if not system.path(f"{folder}/VNS.Harbor.dll").is_file():
        raise Stop(f"{site.name} ({environment}) does not run the VNS.Harbor package yet: publish it.")
    keys = connection(system, site, environment)
    # The site where it runs must know the key: connected now when it is not yet (an environment on this server)
    written = read_json(system.path(f"{running_folder(system, site, environment)}/vns-harbor.json")) or {}
    if written.get("loginKey") != keys["login_key"]:
        connect(system, site.name, environment)
    now = time.time() if now is None else now
    payload = encode(json.dumps({"site": site.name, "environment": environment, "user": user, "exp": int(now) + LIFETIME,
                                 "nonce": secrets.token_hex(16)}, separators=(",", ":")).encode())
    signature = encode(hmac.new(keys["login_key"].encode(), payload.encode(), hashlib.sha256).digest())
    log = system.path(f"{STATE}/{site.name}/backoffice-logins.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as stream:
        stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "who": who, "environment": environment,
                                 "user": user}) + "\n")
    return f"{address(system, site, environment)}/umbraco/vns-harbor/login?token={payload}.{signature}"
