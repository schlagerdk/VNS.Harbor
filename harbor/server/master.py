"""A master Harbor over many servers (phase 13 of dev/HARBOR.md): one Harbor, such as harbor.vns.dk, from which every
server is administered. The others are its members: servers in Servers, joined with harbor servers join.

The master reaches a member as it reaches any server in Servers: over SSH, with Harbor's own key. There it runs
harbor master call, which asks the member's own admin at 127.0.0.1 with a token only root there can read, so the
master is answered by the member's API itself, as the member's admin page is: nothing of it is kept on the master,
where it would go stale.

  /etc/harbor/secrets/master-token   on a member: the token its admin takes from harbor master call (root only)
  /var/lib/harbor/servers.json       on the master: a member is a server with "member": true
"""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import secrets
import shlex
import urllib.error
import urllib.request

from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

TOKEN = "/etc/harbor/secrets/master-token"
HEADER = "X-Harbor-Master"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# A path of the admin's API, and its query string: nothing that leaves it
PATH = re.compile(r"/api/[A-Za-z0-9._~%/-]{1,400}(\?[A-Za-z0-9._~%&=+,:-]{0,800})?")
REMOTE_HARBOR = "python3 /opt/harbor/current/bin/harbor"


def token(system):
    """This server's token for the master's calls, made the first time"""
    path = system.path(TOKEN)
    try:
        found = path.read_text().strip()
    except OSError:
        found = ""
    if len(found) >= 32:
        return found
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    found = secrets.token_urlsafe(32)
    path.write_text(found + "\n")
    path.chmod(0o600)
    return found


def admin_port(system):
    """The port the admin here listens on, at 127.0.0.1"""
    from harbor.core.templates import Site
    from harbor.install.admin import admin_sites, service_port
    found = admin_sites(system)
    if not found:
        raise Stop("There is no admin site here to answer the master.")
    return service_port(system, Site(found[0]))


def call(system, method, path, body=None, opener=None):
    """One call to the admin here, as the master: {status, body}"""
    method = str(method or "").upper()
    if method not in METHODS or not PATH.fullmatch(path or ""):
        raise Stop("Give a method and a path of the admin's API, such as GET /api/sites.")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{admin_port(system)}{path}", method=method, data=data,
                                     headers={HEADER: token(system), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=120) as response:
            status, text = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode("utf-8", "replace")
    except OSError as error:
        raise Stop(f"The admin here did not answer: {error}") from None
    try:
        answer = json.loads(text) if text.strip() else None
    except ValueError:
        answer = {"error": text.strip()[:500]}
    return {"status": status, "body": answer}


# ---- on the master
def members(system):
    """The servers that are members: {name: entry}"""
    from harbor.server.servers import servers
    return {name: entry for name, entry in servers(system, "linux").items() if entry.get("member")}


def ask(system, name, entry, method, path, body=None):
    """A member's admin asked over SSH (harbor master call there): {status, body}, or Stop with why it did not answer"""
    from harbor.server.servers import remote
    command = f"{REMOTE_HARBOR} master call {shlex.quote(method)} {shlex.quote(path)}" + (" --body" if body is not None else "")
    result = remote(system, entry, command, input=json.dumps(body) if body is not None else None, check=False)
    if not result.ok:
        said = (result.stderr or result.stdout).strip().splitlines()
        if any("invalid choice: 'master'" in line or "argument <group>" in line for line in said):
            raise Stop(f"{name} runs a Harbor without the master's calls: update it (Servers, Update).")
        raise Stop(f"{name} did not answer: {said[-1] if said else 'no answer'}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Stop(f"{name} answered something Harbor does not understand.") from None


def join(system, name):
    """A server in Servers made a member: it must answer the master's calls first"""
    from harbor.server.servers import SERVERS, server
    entry = server(system, name)
    system.plan([f"Ask {name}'s admin", f"Make {name} a member"])
    system.step(f"Ask {name}'s admin")
    answer = ask(system, name, entry, "GET", "/api/me")
    if answer.get("status") != 200:
        raise Stop(f"{name}'s admin did not take the master's call ({answer.get('status')}): update Harbor there.")
    system.step(f"Make {name} a member")
    found = read_json(system.path(SERVERS)) or {}
    found[name] = {**found[name], "member": True}
    write_json(system.path(SERVERS), found)
    system.say(f"{name} is a member: its sites are shown here.")


def leave(system, name):
    """A member back to an ordinary server in Servers: nothing changes on it"""
    from harbor.server.servers import SERVERS, server
    server(system, name)
    found = read_json(system.path(SERVERS)) or {}
    found[name] = {key: value for key, value in found[name].items() if key != "member"}
    write_json(system.path(SERVERS), found)
    system.say(f"{name} is no longer a member.")


def member_sites(system, name, entry):
    """A member's sites as its own admin lists them, or why they could not be read"""
    base = {"server": name, "host": entry.get("host", ""), "admin": admin_address(entry)}
    try:
        answer = ask(system, name, entry, "GET", "/api/sites")
    except Stop as error:
        return {**base, "reachable": False, "error": str(error), "sites": []}
    body = answer.get("body") or {}
    if answer.get("status") != 200:
        return {**base, "reachable": False, "error": (body.get("error") if isinstance(body, dict) else "") or f"It answered {answer.get('status')}.", "sites": []}
    return {**base, "reachable": True, "error": "", "sites": body.get("sites") or [], "version": body.get("version", "")}


def admin_address(entry):
    """Where a member's own admin is, to open a site there: admin.<its name>, as Servers learnt it"""
    names = entry.get("names") or []
    return f"https://admin.{names[0]}" if names else ""


def sites(system):
    """Every member's sites, a member at a time, asked at once"""
    found = members(system)
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(lambda item: member_sites(system, *item), sorted(found.items())))
