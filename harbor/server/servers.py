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
    if match:
        admin = match.group(1)
found["admin"] = admin
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


def remove(system, name, uninstall=False, revoke_key=False):
    """A server forgotten here. With uninstall Harbor goes from it too (only /opt/harbor and its command), with
    revoke_key Harbor's key is taken out of authorized_keys there, so this server can no longer log in."""
    found = servers(system)
    entry = found.get(name)
    if entry is None:
        raise Stop(f"No server {name}.")
    if entry.get("kind") == "windows":
        del found[name]
        write_json(system.path(SERVERS), found)
        system.path(f"{SECRETS}/{name}.json").unlink(missing_ok=True)
        system.say(f"{name} is removed here, with its password. Nothing on it was changed.")
        return
    if uninstall or revoke_key:
        if not remote(system, entry, "true", check=False).ok:
            raise Stop(f"{name} does not answer, so Harbor cannot be uninstalled or its key taken out there. Remove it without them.")
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
    left = [] if revoke_key else [f"Harbor's key stays in ~/.ssh/authorized_keys on {entry['host']}"]
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
        base = {"name": name, "kind": "windows", "share": share, "user": user, "domain": domain}
        try:
            open_share(system, share, user, password, domain)
        except Stop as error:
            return {**base, "reachable": False, "error": [str(error) or "the share could not be opened"]}
        return {**base, "reachable": True}
    entry = server(system, name)
    result = remote(system, entry, "python3 -", input=STATUS, check=False)
    if not result.ok:
        return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "reachable": False,
                "error": (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no answer"]}
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {}
    remember_public(system, name, found.get("public", ""), found.get("fqdn"), found.get("admin_names"))
    return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "reachable": True, **judged(found)}


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
