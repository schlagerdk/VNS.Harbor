"""Other servers Harbor works with (phase 5 of dev/HARBOR.md): added once, then reached with Harbor's own SSH key.

  /etc/harbor/ssh/id_ed25519      Harbor's key (root only); its .pub is put on each server when it is added
  /etc/harbor/ssh/known_hosts     the servers' host keys, as first seen
  /var/lib/harbor/servers.json    {name: {host, user, port, added}}

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
print(json.dumps(found))
'''


def servers(system):
    found = read_json(system.path(SERVERS))
    return found if isinstance(found, dict) else {}


def server(system, name):
    found = servers(system).get(name)
    if not found:
        raise Stop(f"No server {name}. Add it with: harbor servers add {name} <host>")
    return found


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


def forget_host_key(system, host, port=22):
    """The host key Harbor saw for a server, forgotten (ssh-keygen -R): it is learnt again at the next login"""
    if not system.path(KNOWN_HOSTS).is_file():
        return
    for known in (host, f"[{host}]:{port}"):
        system.run("ssh-keygen", "-f", KNOWN_HOSTS, "-R", known, check=False, capture=True)


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
    if not any(other["host"] == host and int(other["port"]) == int(port) for other in servers(system).values()):
        forget_host_key(system, host, port)
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
    system.say(f"{name} ({user}@{host}) is added. Harbor reaches it with its own key; no password is kept.")


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
    if uninstall or revoke_key:
        if not remote(system, entry, "true", check=False).ok:
            raise Stop(f"{name} does not answer, so Harbor cannot be uninstalled or its key taken out there. Remove it without them.")
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
    if not any(other["host"] == entry["host"] and int(other["port"]) == int(entry["port"]) for other in found.values()):
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
    """The version handshake: what the server runs, and whether this Harbor can work with it"""
    entry = server(system, name)
    result = remote(system, entry, "python3 -", input=STATUS, check=False)
    if not result.ok:
        return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "reachable": False,
                "error": (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no answer"]}
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {}
    return {"name": name, **{key: entry[key] for key in ("host", "user", "port")}, "reachable": True, **judged(found)}


def rollback(system, name):
    """The other server goes back to the release installed there before the one in use (harbor self rollback there)"""
    entry = server(system, name)
    found = status(system, name)
    if not found["reachable"]:
        raise Stop(f"{name} does not answer: {' '.join(found['error'])}")
    if not found.get("rollback"):
        raise Stop(f"{name} has no earlier release of VNS Harbor to go back to.")
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
    if release.endswith("-dev") or not remote(system, entry, f"test -d {there}", check=False).ok:
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
