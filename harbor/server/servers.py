"""Other servers Harbor works with (phase 5 of dev/HARBOR.md): added once, then reached with Harbor's own SSH key.

  /etc/harbor/ssh/id_ed25519      Harbor's key (root only); its .pub is put on each server when it is added
  /etc/harbor/ssh/known_hosts     the servers' host keys, as first seen
  /var/lib/harbor/servers.json    {name: {host, user, port, added}}; a Windows server {kind: windows, share, user, domain, added}
  /etc/harbor/secrets/servers/<name>.json   a Windows server's password (root only)

A Windows server is only kept with its share and login, so Import from Windows can choose it: Harbor runs nothing there.

A password is used once, to put the key on the server, and is never stored. status() is the version handshake: what the
server runs, and whether this Harbor can work with it.
"""
from datetime import datetime, timezone
import json
import re
import shlex

from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

SERVERS = "/var/lib/harbor/servers.json"
SSH_DIR = "/etc/harbor/ssh"
KEY = f"{SSH_DIR}/id_ed25519"
KNOWN_HOSTS = f"{SSH_DIR}/known_hosts"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# What the other server tells about itself: read there with its own python3, sent back as JSON
STATUS = r'''
import json, os, platform, re, subprocess
found = {"python": platform.python_version()}
try:
    release = dict(line.rstrip("\n").split("=", 1) for line in open("/etc/os-release") if "=" in line)
    found["os"] = release.get("PRETTY_NAME", "").strip('"')
    found["os_id"] = release.get("ID", "").strip('"')
except OSError:
    pass
try:
    found["harbor"] = json.load(open("/opt/harbor/current/release.json"))
    found["harbor"]["name"] = os.path.basename(os.path.realpath("/opt/harbor/current"))
except (OSError, ValueError):
    found["harbor"] = None
# The release a rollback there goes back to: the one installed before the one in use, from the same repository
def release(name):
    folder = os.path.join("/opt/harbor/releases", name)
    try:
        info = json.load(open(os.path.join(folder, "release.json")))
    except (OSError, ValueError):
        info = {}
    try:
        repository = open(os.path.join(folder, "REPOSITORY")).read().strip()
    except OSError:
        repository = ""
    return {"name": name, "version": info.get("version", ""), "installed": info.get("installed", ""), "repository": repository}
try:
    names = [name for name in os.listdir("/opt/harbor/releases") if not name.startswith(".")]
except OSError:
    names = []
found_releases = sorted((release(name) for name in names), key=lambda item: (item["installed"], item["name"]), reverse=True)
found["rollback"] = None
if found["harbor"]:
    index = next((index for index, item in enumerate(found_releases) if item["name"] == found["harbor"]["name"]), None)
    if index is not None and index + 1 < len(found_releases) and found_releases[index + 1]["repository"] == found_releases[index]["repository"]:
        found["rollback"] = {key: found_releases[index + 1][key] for key in ("name", "version")}
try:
    found["vns"] = open("/vns/VERSION").read().strip()
except OSError:
    found["vns"] = ""
admin = ""
for name in sorted(os.listdir("/etc/systemd/system")) if os.path.isdir("/etc/systemd/system") else []:
    try:
        text = open(os.path.join("/etc/systemd/system", name)).read()
    except OSError:
        continue
    # The admin site: its code in its folder (VNS 1.x), or its data there and its code in the release (Harbor)
    match = re.search(r"(?m)^(?:WorkingDirectory|Environment=HARBOR_ADMIN_DATA)=/var/www/([^/\n]+)/app$", text)
    # A Python site runs from its app folder too: the admin's holds its login (config.json with a secret key)
    try:
        login = json.load(open(f"/var/www/{match.group(1)}/app/config.json")) if match else {}
    except (OSError, ValueError):
        login = {}
    if isinstance(login, dict) and login.get("secret_key"):
        admin = match.group(1)
found["admin"] = admin
# Its own sites: the folders in /var/www that are not the admin, nor an environment managed from another server (a
# note in /var/lib/vns-managed). While it has any, Harbor is needed there.
try:
    managed = set(os.listdir("/var/lib/vns-managed"))
except OSError:
    managed = set()
own = []
for name in sorted(os.listdir("/var/www")) if os.path.isdir("/var/www") else []:
    path = os.path.join("/var/www", name)
    if name in (admin, "html") or name.startswith(".") or name in managed or os.path.islink(path) or not os.path.isdir(path):
        continue
    own.append(name)
found["own_sites"] = own
# The names the admin is reached by (its ServerName and aliases): a site's temporary alias goes under the one that
# reaches the server now
found["admin_names"] = []
try:
    text = open(f"/etc/apache2/sites-available/{admin}.conf").read() if admin else ""
    for line in re.findall(r"(?mi)^\s*Server(?:Name|Alias)\s+(.+)$", text):
        for item in line.split():
            item = item.lower().split(":")[0]
            if item.startswith("admin.") and item not in found["admin_names"]:
                found["admin_names"].append(item)
except OSError:
    pass
# Its address as the internet sees it: a server behind a router knows only its internal one
found["public"] = ""
try:
    import urllib.request
    text = urllib.request.urlopen("https://1.1.1.1/cdn-cgi/trace", timeout=4).read(4096).decode("ascii", "replace")
    match = re.search(r"(?m)^ip=([0-9.]+)$", text)
    found["public"] = match.group(1) if match else ""
except Exception:
    pass
# Its full name: a temporary alias for a site moved there is named under it ({binding}.<name>)
import socket
found["fqdn"] = socket.getfqdn().lower().rstrip(".")
print(json.dumps(found))
'''


SECRETS = "/etc/harbor/secrets/servers"
SHARE = re.compile(r"//[A-Za-z0-9][A-Za-z0-9.-]{0,252}/[^\x00-\x1f\"<>|]{1,400}")
WINDOWS_USER = re.compile(r"[A-Za-z0-9._@\\-]{1,128}")
DOMAIN = re.compile(r"[A-Za-z0-9._-]{0,128}")


def servers(system, kind=""):
    """Every server Harbor knows; with kind, only the Linux ones (linux) or the Windows ones (windows)"""
    found = read_json(system.path(SERVERS))
    found = found if isinstance(found, dict) else {}
    if kind:
        found = {name: entry for name, entry in found.items() if (entry.get("kind") or "linux") == kind}
    return found


def server(system, name):
    found = servers(system).get(name)
    if not found:
        raise Stop(f"No server {name}. Add it with: harbor servers add {name} <host>")
    if found.get("kind") == "windows":
        raise Stop(f"{name} is a Windows server: Harbor only keeps its share and login.")
    return found


def add_windows(system, name, share, user, password, domain=""):
    """A Windows server's share and login, kept for Import from Windows. The login is tried first."""
    from harbor.sites.windows_import import share as open_share
    share = (share or "").strip().replace("\\", "/").rstrip("/")
    if not NAME.fullmatch(name or ""):
        raise Stop("A server's name is lower case letters, digits and -, such as windows-01.")
    if name in servers(system):
        raise Stop(f"There is a server {name} already.")
    if not SHARE.fullmatch(share) or not WINDOWS_USER.fullmatch(user or "") or not DOMAIN.fullmatch(domain or "") or not password:
        raise Stop("Give the share as //server/share/folder, the login and its password.")
    system.plan([f"Log in to {share}"])
    system.step(f"Log in to {share}")
    open_share(system, share, user, password, domain)
    found = servers(system)
    found[name] = {"kind": "windows", "share": share, "user": user, "domain": domain,
                   "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    secrets = system.path(SECRETS)
    secrets.mkdir(parents=True, exist_ok=True)
    secrets.chmod(0o700)
    write_json(secrets / f"{name}.json", {"password": password}, private=True)
    write_json(system.path(SERVERS), found)
    system.say(f"{name} ({share}) is added. Import from Windows can choose it; its password is kept for root only.")


def windows_login(system, name):
    """A Windows server's share and login, with its password: (share, user, password, domain)"""
    entry = servers(system).get(name)
    if not entry or entry.get("kind") != "windows":
        raise Stop(f"No Windows server {name}.")
    secret = read_json(system.path(f"{SECRETS}/{name}.json")) or {}
    return entry["share"], entry["user"], secret.get("password", ""), entry.get("domain", "")


def key(system):
    """Harbor's own SSH key, made the first time"""
    folder = system.path(SSH_DIR)
    if not system.path(KEY).is_file():
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        hostname = system.output("hostname", check=False) or "harbor"
        system.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"harbor@{hostname}", "-f", KEY, capture=True)
    return system.path(f"{KEY}.pub").read_text().strip()


def ssh(entry, *, password=False):
    """The ssh to a server: with Harbor's key, or (adding it) the password in SSHPASS"""
    argv = ["ssh", "-p", str(entry["port"]), "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    if password:
        return ["sshpass", "-e", *argv, "-o", "PubkeyAuthentication=no"]
    return [*argv, "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]


def remote(system, entry, command, input=None, check=True, capture=True):
    """A command as root on the server (with sudo -n when the login is not root)"""
    sudo = "" if entry["user"] == "root" else "sudo -n "
    return system.run(*ssh(entry), f"{entry['user']}@{entry['host']}", f"{sudo}{command}", input=input, check=check, capture=capture)


# The environments reach their servers as root does (its known_hosts), the servers Harbor knows with Harbor's own
ROOT_KNOWN_HOSTS = "/root/.ssh/known_hosts"


def forget_host_key(system, host, port=22):
    """The host key seen for a server, forgotten (ssh-keygen -R) in Harbor's known_hosts and root's: it is learnt again
    at the next login. Else a server set up again at the address stops every environment on it"""
    for path in (KNOWN_HOSTS, ROOT_KNOWN_HOSTS):
        if not system.path(path).is_file():
            continue
        for known in (host, f"[{host}]:{port}"):
            system.run("ssh-keygen", "-f", path, "-R", known, check=False, capture=True)


def add(system, name, host, user="root", port="22", password=""):
    if not NAME.fullmatch(name or ""):
        raise Stop("A server's name is lower case letters, digits and -, such as linux-02.")
    if not HOST.fullmatch(host or "") or not USER.fullmatch(user or "") or not str(port).isdigit():
        raise Stop("Give the server's address, login and SSH port.")
    entry = {"host": host, "user": user, "port": int(port)}
    public = key(system)
    system.path(SSH_DIR).mkdir(parents=True, exist_ok=True)
    # A server none of Harbor's servers is at: a key seen there before belongs to what was there then (a server set up
    # again has a new one), and this one is learnt as it logs in now
    if not any(other["host"] == host and int(other["port"]) == int(port) for other in servers(system, "linux").values()):
        forget_host_key(system, host, port)
    system.plan([*([f"Put Harbor's key on {host}"] if password else []), f"Log in to {host} with Harbor's key"])
    if password:
        system.step(f"Put Harbor's key on {host}")
        command = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                   f"(grep -qxF '{public}' ~/.ssh/authorized_keys || echo '{public}' >> ~/.ssh/authorized_keys)")
        result = system.run(*ssh(entry, password=True), f"{user}@{host}", command, env={"SSHPASS": password}, check=False, capture=True)
        if not result.ok:
            raise Stop(f"Could not log in to {user}@{host}: {(result.stderr or result.stdout).strip() or 'the password was not accepted'}")
    system.step(f"Log in to {host} with Harbor's key")
    if not remote(system, entry, "true", check=False).ok:
        raise Stop(f"Harbor's key does not let it in as root on {user}@{host} (a login other than root needs passwordless "
                   f"sudo). Add the server with its password, or put {KEY}.pub in ~/.ssh/authorized_keys there.")
    entry["added"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found = servers(system)
    found[name] = entry
    write_json(system.path(SERVERS), found)
    try:
        status(system, name)
    except Stop:
        pass
    system.say(f"{name} ({user}@{host}) is added. Harbor reaches it with its own key; no password is kept.")


def rename(system, name, new):
    """A server given another name in Servers: its password goes with it (a Windows server), and the environments copied
    from Windows that name it follow. Nothing changes on the server, and its environments are reached by address."""
    found = servers(system)
    if name not in found:
        raise Stop(f"No server {name}.")
    if not NAME.fullmatch(new or ""):
        raise Stop("A server's name is lower case letters, digits and -, such as linux-02.")
    if new == name:
        return
    if new in found:
        raise Stop(f"There is a server called {new} already.")
    found = {new if key == name else key: value for key, value in found.items()}
    write_json(system.path(SERVERS), found)
    secret = system.path(f"{SECRETS}/{name}.json")
    if secret.is_file():
        secret.rename(system.path(f"{SECRETS}/{new}.json"))
    # Copy from Windows keeps the server's name with each copy (harbor/environments/windows_move.py)
    from harbor.environments.settings import STATE
    state = system.path(STATE)
    for path in sorted(state.glob("*/windows-move.json")) if state.is_dir() else []:
        links = read_json(path)
        if isinstance(links, dict) and any(isinstance(entry, dict) and entry.get("server") == name for entry in links.values()):
            write_json(path, {key: {**entry, "server": new} if isinstance(entry, dict) and entry.get("server") == name else entry
                              for key, entry in links.items()})
    system.say(f"{name} is called {new} now.")


def set_host(system, name, host):
    """The server has a new address (it was given another IP): kept in Servers, and in every environment here that was
    on the old one, so publish, refresh and take over reach it there. Nothing changes on the server itself."""
    entry = server(system, name)
    if not HOST.fullmatch(host or ""):
        raise Stop("Give the server's new address.")
    old = entry["host"]
    if host == old:
        system.say(f"{name} is at {host} already.")
        return []
    trial = {**entry, "host": host}
    # A key seen at the new address belongs to what was there before (such as the server it takes over from)
    if not any(other["host"] == host and int(other["port"]) == int(entry["port"]) for other in servers(system, "linux").values()):
        forget_host_key(system, host, entry["port"])
    system.plan([f"Log in to {host} with Harbor's key"])
    system.step(f"Log in to {host} with Harbor's key")
    if not remote(system, trial, "true", check=False).ok:
        raise Stop(f"Harbor cannot log in to {entry['user']}@{host}. Has the server got its new address yet?")
    found = servers(system)
    found[name] = {key: value for key, value in {**found[name], "host": host}.items() if key not in ("public", "fqdn")}
    write_json(system.path(SERVERS), found)
    moved = environments_at(system, old, entry["port"], host)
    for site, target in moved:
        system.say(f"{site} ({target}): {old} -> {host}")
    try:
        status(system, name)
    except Stop:
        pass
    system.say(f"{name} is at {host} now, with {len(moved)} environment(s).")
    return moved


def environments_at(system, old, port, host):
    """Every environment here on the server at old (and its SSH port): its address changed to host"""
    from harbor.core.templates import Site
    from harbor.environments import settings
    from harbor.install.admin import web_root
    root = system.path(web_root())
    moved = []
    for folder in sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")) if root.is_dir() else []:
        site = Site(folder)
        try:
            targets = settings.target_ids(system, site.site_root)
        except (settings.SettingsError, OSError):
            continue
        for target in targets:
            try:
                environment = settings.load(system, site.site_root, target)
            except settings.SettingsError:
                continue
            if environment.windows or environment.IP != old or str(environment.SSH_PORT or "22") != str(port):
                continue
            settings.update(system, site.site_root, target, {"IP": host})
            moved.append((folder, target))
    return moved


# Only what Harbor installed: its releases and its command when it points there. The sites and the admin stay.
UNINSTALL = """set -e
rm -rf /opt/harbor
if [ -L /usr/local/bin/harbor ] && [ "$(readlink /usr/local/bin/harbor)" = /opt/harbor/current/bin/harbor ]; then
	rm -f /usr/local/bin/harbor
fi
echo "Harbor is uninstalled. The sites are not touched."
"""


def same_share(share, under):
    """Whether a folder in a Windows share is in another share (//host/share$/sites/shop in //host/share$/sites)"""
    norm = lambda text: str(text or "").replace("\\", "/").strip().rstrip("/").lower()
    return bool(norm(under)) and (norm(share) == norm(under) or norm(share).startswith(norm(under) + "/"))


def environments_on(system, host, share=""):
    """The environments of sites here that run on a server: a Linux one by its address (they publish and sync with
    Harbor's key, so it stays there while they are), a Windows one by its share (each its folder in it)"""
    import glob
    import json
    import re as _re
    from harbor.environments.settings import parse
    found = []
    for path in sorted(glob.glob(str(system.path("/var/lib/harbor/sites")) + "/*/environments/*.json")):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and ((host and data.get("IP") == host) or (share and same_share(data.get("WINDOWS_SHARE"), share))):
            parts = path.split("/")
            found.append(f"{parts[-3]} ({parts[-1][:-len('.json')]})")
    # Sites not migrated yet: the settings in their publish script
    for path in sorted(glob.glob(str(system.path("/var/www")) + "/*/scripts/publish_prod.sh") + glob.glob(str(system.path("/var/www")) + "/*/scripts/targets/*/publish_prod.sh")):
        try:
            values = parse(open(path, encoding="utf-8", errors="replace").read())
        except (OSError, ValueError):
            continue
        if (host and values.get("IP") == host) or (share and same_share(values.get("WINDOWS_SHARE"), share)):
            site = _re.search(r"/var/www/([^/]+)/scripts", path.replace(str(system.path("/")), "/", 1))
            label = site.group(1) if site else path
            if not any(item.startswith(label + " ") for item in found):
                found.append(label)
    return found


def sql_connections_to(system, entry, name):
    """The SQL connections of the admin here that reach a server (by its address, names or SSH): they use it"""
    import glob
    addresses = {value.lower() for value in (entry.get("host"), entry.get("public"), entry.get("fqdn"), name, *(entry.get("names") or [])) if value}
    found = []
    for path in glob.glob(str(system.path("/var/www")) + "/*/app/sql-connections.json"):
        try:
            items = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        for item in items if isinstance(items, list) else []:
            hosts = {str(item.get(key, "")).lower().split(",")[0].split("\\")[0] for key in ("host", "ssh_host") if item.get(key)}
            if hosts & addresses:
                found.append(item.get("name") or item.get("host"))
    return found


def uninstall_blockers(system, name, entry, found):
    """Why Harbor must stay on a server: sites of its own (not environments managed from another server), or an SQL
    connection here that reaches it"""
    reasons = []
    own = found.get("own_sites") or []
    if own:
        reasons.append(f"it has sites of its own: {', '.join(own[:5])}{' and more' if len(own) > 5 else ''}")
    connections = sql_connections_to(system, entry, name)
    if connections:
        reasons.append(f"SQL connections here use it: {', '.join(connections)}")
    return reasons


def adopt_environment_servers(system):
    """The servers the environments here run on, put in Servers where they are not: an environment made before a server
    had to be chosen from Servers (or by an older release) is then known by it, and can be chosen and removed as the
    others. Nothing is done on those servers: a Linux one is named by its own hostname when Harbor's key reaches it,
    else by its address; a Windows one gets the share and login its environment has. Run by the update."""
    from harbor.environments import settings
    found = servers(system)
    added = []

    def free(name):
        name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:56] or "server"
        candidate, number = name, 2
        while candidate in found:
            candidate, number = f"{name}-{number}", number + 1
        return candidate

    web = system.path("/var/www")
    state = system.path(settings.STATE)
    names = sorted({path.name for folder in (web, state) if folder.is_dir() for path in folder.iterdir() if path.is_dir()})
    for site in names:
        root = f"/var/www/{site}"
        for target in settings.target_ids(system, root):
            try:
                values = settings.values(system, root, target) or {}
            except settings.SettingsError:
                continue
            if values.get("TARGET") == "windows":
                share = values.get("WINDOWS_SHARE", "").replace("\\", "/").rstrip("/").rsplit("/", 1)[0]
                if not SHARE.fullmatch(share) or any(same_share(values.get("WINDOWS_SHARE"), entry.get("share")) for entry in found.values() if entry.get("kind") == "windows"):
                    continue
                name = free("windows-" + share.lstrip("/").split("/")[0])
                found[name] = {"kind": "windows", "share": share, "user": values.get("SMB_USER", ""), "domain": values.get("SMB_DOMAIN", ""),
                               "added": datetime.now(timezone.utc).isoformat(timespec="seconds"), "adopted": True}
                if values.get("SMB_PASSWORD"):
                    secrets = system.path(SECRETS)
                    secrets.mkdir(parents=True, exist_ok=True)
                    secrets.chmod(0o700)
                    write_json(secrets / f"{name}.json", {"password": values["SMB_PASSWORD"]}, private=True)
                added.append(f"{name} ({share})")
                continue
            host = values.get("IP", "")
            if not host or not HOST.fullmatch(host) or any(entry.get("host") == host for entry in found.values() if entry.get("kind") != "windows"):
                continue
            user = values.get("USER") or "root"
            port = int(values.get("SSH_PORT") or 22) if str(values.get("SSH_PORT") or "22").isdigit() else 22
            entry = {"host": host, "user": user if USER.fullmatch(user) else "root", "port": port}
            known = remote(system, entry, "hostname -s", check=False)
            hostname = known.stdout.strip().splitlines()[-1] if known.ok and known.stdout.strip() else ""
            name = free(hostname if NAME.fullmatch(hostname.lower()) else "server-" + host.replace(".", "-").replace(":", "-"))
            found[name] = {**entry, "added": datetime.now(timezone.utc).isoformat(timespec="seconds"), "adopted": True}
            added.append(f"{name} ({user}@{host})")
    if added:
        write_json(system.path(SERVERS), found)
        system.say(f"Servers the environments here run on, added to Servers: {', '.join(added)}.")
    else:
        system.say("Every server the environments here run on is in Servers.")
    return added


def remove(system, name, uninstall=False):
    """A server forgotten here. Harbor's key goes from it too (authorized_keys there), so this server cannot log in
    there any more unseen: always, unless an environment of a site here runs there (it publishes and syncs with the
    key), or the server does not answer. With uninstall Harbor goes from it too (only /opt/harbor and its command),
    unless it is used there (uninstall_blockers)."""
    found = servers(system)
    entry = found.get(name)
    if entry is None:
        raise Stop(f"No server {name}.")
    if entry.get("kind") == "windows":
        # An environment on it is made from it: it stays in Servers while there is one, so what runs where is known
        using = environments_on(system, "", entry.get("share", ""))
        if using:
            raise Stop(f"Environments here run on {name}: {', '.join(using)}. It stays in Servers while they are there.")
        del found[name]
        write_json(system.path(SERVERS), found)
        system.path(f"{SECRETS}/{name}.json").unlink(missing_ok=True)
        system.say(f"{name} is removed here, with its password. Nothing on it was changed.")
        return
    # An environment here runs on it: it stays in Servers (and its key there) while it does, so what runs where is known
    using = environments_on(system, entry["host"])
    if using:
        raise Stop(f"Environments here run on {name}: {', '.join(using)}. It stays in Servers while they are there.")
    answers = remote(system, entry, "true", check=False).ok
    if uninstall and not answers:
        raise Stop(f"{name} does not answer, so Harbor cannot be uninstalled there. Remove it without that.")
    revoke_key = answers
    if uninstall:
        result = remote(system, entry, "python3 -", input=STATUS, check=False)
        try:
            there = json.loads(result.stdout.strip().splitlines()[-1]) if result.ok else {}
        except (ValueError, IndexError):
            there = {}
        reasons = uninstall_blockers(system, name, entry, there)
        if reasons:
            raise Stop(f"Harbor stays on {name}: {'; '.join(reasons)}.")
    system.plan([*([f"Uninstall Harbor on {name}"] if uninstall else []), *([f"Take Harbor's key off {name}"] if revoke_key else [])])
    if uninstall:
        system.step(f"Uninstall Harbor on {name}")
        remote(system, entry, "sh -s", input=UNINSTALL, capture=False)
    if revoke_key:
        system.step(f"Take Harbor's key off {name}")
        public = system.path(f"{KEY}.pub").read_text().strip()
        # The last thing done there: after it this server cannot log in again
        command = (f"f=~{entry['user']}/.ssh/authorized_keys; [ -f \"$f\" ] || exit 0; "
                   f"grep -vxF '{public}' \"$f\" > \"$f.harbor\" || true; cat \"$f.harbor\" > \"$f\"; rm -f \"$f.harbor\"")
        remote(system, entry, f"sh -c {shlex.quote(command)}")
    del found[name]
    write_json(system.path(SERVERS), found)
    # Its host key goes too, unless another server here is at the same address
    if not any(other.get("host") == entry["host"] and int(other.get("port", 0)) == int(entry["port"]) for other in found.values()):
        forget_host_key(system, entry["host"], entry["port"])
    left = [] if revoke_key else [f"Harbor's key stays on {entry['host']}, as it does not answer"]
    if not uninstall:
        left.append("Harbor stays installed there")
    system.say(f"{name} is removed here." + (" " + "; ".join(left) + "." if left else ""))


def judged(found):
    """What would keep a server from running Harbor"""
    python = tuple(int(part) for part in re.findall(r"\d+", found.get("python", "0.0"))[:2])
    problems = []
    if found.get("os_id") != "ubuntu":
        problems.append(f"Ubuntu is needed (it runs {found.get('os') or 'something else'})")
    if python < (3, 10):
        problems.append(f"Python 3.10 or newer is needed (it has {found.get('python') or 'none'})")
    return {**found, "compatible": not problems, "problems": problems}


def here(system):
    """This server, as status() tells another one"""
    result = system.run("python3", "-", input=STATUS, check=False, capture=True)
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {}
    addresses = system.output("hostname", "-I", check=False).split()
    return {"name": system.output("hostname", check=False) or "this server", "host": addresses[0] if addresses else "", "user": "", "port": 22,
            "this": True, "reachable": True, **judged(found)}


def status(system, name):
    """The version handshake: what the server runs, and whether this Harbor can work with it. A Windows server: whether
    its share opens with the login kept for it."""
    found = servers(system).get(name)
    if found and found.get("kind") == "windows":
        from harbor.sites.windows_import import share as open_share
        share, user, password, domain = windows_login(system, name)
        base = {"name": name, "kind": "windows", "share": share, "user": user, "domain": domain,
                # Its sites listed on Web Server, and the folders left out (harbor servers windows-sites)
                "list_sites": bool(found.get("list_sites")), "exclude": found.get("exclude") or []}
        try:
            open_share(system, share, user, password, domain)
        except Stop as error:
            return {**base, "reachable": False, "error": [str(error) or "the share could not be opened"]}
        return {**base, "reachable": True}
    entry = server(system, name)
    result = remote(system, entry, "python3 -", input=STATUS, check=False)
    if not result.ok:
        return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "member": bool(entry.get("member")), "no_web": bool(entry.get("no_web")), "closed": bool(entry.get("closed")), "sql_closed": bool(entry.get("sql_closed")), "reachable": False,
                "error": (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no answer"]}
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {}
    remember_public(system, name, found.get("public", ""), found.get("fqdn"), found.get("admin_names"))
    # Why Harbor must stay there, for Remove: sites of its own, or SQL connections here that reach it
    return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "member": bool(entry.get("member")), "no_web": bool(entry.get("no_web")), "closed": bool(entry.get("closed")), "sql_closed": bool(entry.get("sql_closed")), "reachable": True, **judged(found),
            "keep_harbor": uninstall_blockers(system, name, entry, found)}


def remember_public(system, name, public, fqdn=None, admin_names=None):
    """The server's public address, kept with it: DNS for a site there is checked against it (its own may be internal).
    And its full name, when it has one with a domain ("" when not, so it is not asked again)"""
    found = servers(system)
    if name not in found:
        return
    entry = dict(found[name])
    if public and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", public):
        entry["public"] = public
    if fqdn is not None:
        entry["fqdn"] = fqdn if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", fqdn or "") else ""
    if admin_names is not None:
        # admin.<name>: the names under which its sites can be reached for a test
        entry["names"] = [item[len("admin."):] for item in admin_names if re.fullmatch(r"admin(?:\.[a-z0-9-]+){2,}", item)]
    if entry != found[name]:
        found[name] = entry
        write_json(system.path(SERVERS), found)


def rollback(system, name):
    """The other server goes back to the release installed there before the one in use (harbor self rollback there)"""
    entry = server(system, name)
    found = status(system, name)
    if not found["reachable"]:
        raise Stop(f"{name} does not answer: {' '.join(found['error'])}")
    if not found.get("rollback"):
        raise Stop(f"{name} has no earlier release of VNS Harbor to go back to.")
    system.plan([f"Roll {name} back to Harbor {found['rollback']['name']}"])
    system.step(f"Roll {name} back to Harbor {found['rollback']['name']}")
    remote(system, entry, "python3 /opt/harbor/current/bin/harbor self rollback", capture=False)
    system.say(f"{name} runs Harbor {found['rollback']['name']} again.")


def install(system, name):
    """This server's Harbor release on the other server, made current there: an agent, the harbor command. The same
    again updates it. A release that is there already is only made current (a working copy, -dev, is copied again)."""
    import os
    from harbor.install.release import CURRENT, RELEASES
    entry = server(system, name)
    here = system.path(CURRENT)
    if not os.path.islink(here):
        raise Stop("This server runs no Harbor release to install there (harbor self install).")
    release = os.path.basename(os.readlink(here))
    found = status(system, name)
    if not found["reachable"]:
        raise Stop(f"{name} does not answer: {' '.join(found['error'])}")
    if not found["compatible"]:
        raise Stop(f"{name} cannot run Harbor: " + "; ".join(found["problems"]))
    there = f"{RELEASES}/{release}"
    copy = release.endswith("-dev") or not remote(system, entry, f"test -d {there}", check=False).ok
    system.plan([*([f"Copy Harbor {release} to {name}"] if copy else []), f"Use Harbor {release} on {name}", f"Bring {name} up to Harbor {release}"])
    if copy:
        system.step(f"Copy Harbor {release} to {name}")
        if not remote(system, entry, "command -v rsync", check=False).ok:
            remote(system, entry, "env DEBIAN_FRONTEND=noninteractive apt-get install -y -q rsync")
        remote(system, entry, f"mkdir -p {RELEASES}")
        shell = " ".join(ssh(entry))
        system.run("rsync", "-a", "--delete", "-e", shell, f"{system.path(there)}/", f"{entry['user']}@{entry['host']}:{RELEASES}/.{release}.new/",
                   capture=True)
        remote(system, entry, f"sh -c 'rm -rf {there} && mv {RELEASES}/.{release}.new {there}'")
    system.step(f"Use Harbor {release} on {name}")
    remote(system, entry, f"python3 {there}/bin/harbor self use {release}", capture=False)
    # Then the server is brought up to the release, as an update there does: its sites, its admin, and the old
    # installation removed when nothing uses it
    system.step(f"Bring {name} up to Harbor {release}")
    remote(system, entry, f"python3 {CURRENT}/bin/harbor self after-update", capture=False)
    system.say(f"{name} runs Harbor {release}.")


# A server that hosts no sites (a SQL Server's, say): no site is made there, nor an environment of one elsewhere, and
# its users have no Web Server there. One that keeps its sites (closed) runs and shows the sites it has, and takes no
# new one: no new site, environment, copy or import lands there. On the server itself a marker its admin reads; in each
# Servers that knows it, a flag on its entry, so the dialogs that choose a server leave it out
NO_WEB = "/var/lib/harbor/no-web"
CLOSED = "/var/lib/harbor/no-new-sites"
WEB_STATES = ("on", "closed", "off")


def web_state(system):
    """Whether this server hosts sites: on, closed (keeps its sites, takes no new one) or off"""
    return "off" if system.path(NO_WEB).exists() else "closed" if system.path(CLOSED).exists() else "on"


def hosts_sites(system):
    """Whether this server hosts sites (it does unless it was set not to)"""
    return web_state(system) != "off"


def set_hosts_sites(system, state):
    """This server set to host sites (on), keep those it has and take no new one (closed), or host none (off): its
    admin makes none then, and with none its Web Server is off in its menu"""
    if state not in WEB_STATES:
        raise Stop(f"Choose {', '.join(WEB_STATES)}.")
    for path, wanted in ((NO_WEB, state == "off"), (CLOSED, state == "closed")):
        marker = system.path(path)
        if wanted:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"This server {'hosts no sites' if state == 'off' else 'takes no new sites'} (Harbor: harbor server web on to change it).\n")
        else:
            marker.unlink(missing_ok=True)
    system.say({"on": "This server hosts sites.", "closed": "This server keeps its sites and takes no new one.",
                "off": "This server hosts no sites: none is made here."}[state])


def entry_web_state(entry):
    """A server in Servers: on, closed or off, as its entry is marked"""
    return "off" if entry.get("no_web") else "closed" if entry.get("closed") else "on"


def set_entry_web(system, name, state):
    """A server in Servers marked as hosting sites (on), keeping its own (closed), or hosting none (off), here"""
    if state not in WEB_STATES:
        raise Stop(f"Choose {', '.join(WEB_STATES)}.")
    found = read_json(system.path(SERVERS)) or {}
    if name not in found:
        raise Stop(f"No server {name}.")
    entry = {key: value for key, value in found[name].items() if key not in ("no_web", "closed")}
    found[name] = {**entry, **({"no_web": True} if state == "off" else {"closed": True} if state == "closed" else {})}
    write_json(system.path(SERVERS), found)


# A server's SQL Server (Microsoft SQL Server and PostgreSQL) that keeps its databases and takes no new one from a
# conversion, a move or a copy: a database never ends up on a server it does not belong on. On the server itself a
# marker its admin reads; in each Servers that knows it, a flag on its entry (sql_closed). Create database on its SQL
# Server page is still there: one made by hand is meant to be there
SQL_CLOSED = "/var/lib/harbor/no-new-databases"
SQL_STATES = ("on", "closed")


def sql_state(system):
    """Whether this server's SQL Server takes new databases (on) or keeps its own only (closed)"""
    return "closed" if system.path(SQL_CLOSED).exists() else "on"


def set_sql_state(system, state):
    if state not in SQL_STATES:
        raise Stop(f"Choose {', '.join(SQL_STATES)}.")
    marker = system.path(SQL_CLOSED)
    if state == "closed":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("This server's SQL Server takes no new database from Convert, Move or Copy (Harbor: harbor server sql on to change it).\n")
        system.say("This server's SQL Server keeps its databases: none is converted, moved or copied here.")
    else:
        marker.unlink(missing_ok=True)
        system.say("This server's SQL Server takes new databases.")


def set_entry_sql(system, name, state):
    """A server in Servers marked as taking new databases (on) or keeping its own (closed), here"""
    if state not in SQL_STATES:
        raise Stop(f"Choose {', '.join(SQL_STATES)}.")
    found = read_json(system.path(SERVERS)) or {}
    if name not in found:
        raise Stop(f"No server {name}.")
    entry = {key: value for key, value in found[name].items() if key != "sql_closed"}
    found[name] = {**entry, "sql_closed": True} if state == "closed" else entry
    write_json(system.path(SERVERS), found)
