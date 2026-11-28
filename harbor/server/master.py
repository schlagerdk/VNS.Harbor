"""A master Harbor over many servers (phase 13 of dev/HARBOR.md): one Harbor, such as harbor.vns.dk, from which every
server is administered. The others are its members: servers in Servers, joined with harbor servers join.

The master reaches a member as it reaches any server in Servers: over SSH, with Harbor's own key. There it runs
harbor master call, which asks the member's own admin at 127.0.0.1 with a token only root there can read, so the
master is answered by the member's API itself, as the member's admin page is: nothing of it is kept on the master,
where it would go stale.

  /etc/harbor/secrets/master-token   on a member: the token its admin takes from harbor master call (root only)
  /var/lib/harbor/servers.json       on the master: a member is a server with "member": true
"""
import base64
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
# A path of the admin's API (or its terminals), and its query string: nothing that leaves it
PATH = re.compile(r"/(api|ws)/[A-Za-z0-9._~%@/-]{1,400}(\?[A-Za-z0-9._~%&=+,:@!*()/-]{0,2000})?")
CONTENT_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}(;[ -~]{0,200})?")
REMOTE_HARBOR = "python3 /opt/harbor/current/bin/harbor"
# The master keeps one SSH connection to a member open a while, so a page of calls does not log in for each
CONTROL_DIR = "/run/harbor-ssh"


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


USER_ID = re.compile(r"u_[0-9a-f]{6,32}")


def client_address(value):
    """The address one on the master's page comes from, as the master sees it: a plain IP address, or None"""
    import ipaddress
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return None


def call(system, method, path, body=None, opener=None, raw=None, as_user=None, client=None):
    """One call to the admin here, as the master: {status, type, disposition, data (base64), body (its JSON, when it is)}.
    body is JSON to send; raw is what a page sent as it is: {type, data (base64)}, an upload with its files"""
    method = str(method or "").upper()
    if method not in METHODS or not PATH.fullmatch(path or "") or not path.startswith("/api/"):
        raise Stop("Give a method and a path of the admin's API, such as GET /api/sites.")
    headers = {HEADER: token(system), "Accept": "application/json"}
    # For one of the master's users: the admin here answers as that user
    if as_user:
        if not USER_ID.fullmatch(as_user):
            raise Stop("Not a user.")
        headers["X-Harbor-As"] = as_user
    # Where the one on the master's page is (the admin here sees only this server's own address): Your address now
    if client_address(client):
        headers["X-Harbor-Client"] = client_address(client)
    data = None
    if raw is not None:
        sent_type = str(raw.get("type") or "")
        if sent_type and not CONTENT_TYPE.fullmatch(sent_type):
            raise Stop("Not a content type.")
        data = base64.b64decode(raw.get("data") or "") or None
        if sent_type:
            headers["Content-Type"] = sent_type
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{admin_port(system)}{path}", method=method, data=data, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=600) as response:
            status, content, kind, disposition = response.status, response.read(), response.headers.get("Content-Type", ""), response.headers.get("Content-Disposition", "")
    except urllib.error.HTTPError as error:
        status, content, kind, disposition = error.code, error.read(), error.headers.get("Content-Type", ""), error.headers.get("Content-Disposition", "")
    except OSError as error:
        raise Stop(f"The admin here did not answer: {error}") from None
    answer = {"status": status, "type": kind or "", "disposition": disposition or "", "data": base64.b64encode(content).decode()}
    if "json" in (kind or "") or not kind:
        try:
            answer["body"] = json.loads(content) if content.strip() else None
        except ValueError:
            answer["body"] = {"error": content.decode("utf-8", "replace").strip()[:500]}
    return answer


VENV_PYTHON = "/opt/harbor/venv/bin/python3"


def bridge(system, path, as_user=None):
    """(on a member) the admin's terminal or task at path, for the master: run as the admin's Python (master_ws), which
    has the websocket the system's Python has not. Lines of JSON both ways"""
    import os
    from pathlib import Path
    if not PATH.fullmatch(path or "") or not path.startswith("/ws/"):
        raise Stop("Give a path of the admin's terminals, such as /ws/term/<id>.")
    port = admin_port(system)
    os.environ["HARBOR_MASTER_TOKEN"] = token(system)
    if as_user:
        if not USER_ID.fullmatch(as_user):
            raise Stop("Not a user.")
        os.environ["HARBOR_MASTER_AS"] = as_user
    script = str(Path(__file__).with_name("master_ws.py"))
    os.execv(VENV_PYTHON, [VENV_PYTHON, script, str(port), path])


# ---- on the master
def members(system):
    """The servers that are members: {name: entry}"""
    from harbor.server.servers import servers
    return {name: entry for name, entry in servers(system, "linux").items() if entry.get("member")}


def ssh(system, entry):
    """The ssh to a member, its connection kept open a while and used again"""
    from harbor.server.servers import ssh as plain
    folder = system.path(CONTROL_DIR)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
    except OSError:
        return plain(entry)
    return [*plain(entry), "-o", "ControlMaster=auto", "-o", f"ControlPath={CONTROL_DIR}/%C", "-o", "ControlPersist=120"]


def on(entry, command):
    """A command as root on the member (sudo -n when the login is not root)"""
    return f"{entry['user']}@{entry['host']}", ("" if entry["user"] == "root" else "sudo -n ") + command


def ask(system, name, entry, method, path, body=None, raw=None, as_user=None, client=None):
    """A member's admin asked over SSH (harbor master call there): {status, body, …}, or Stop with why it did not answer.
    as_user: one of the master's users, as whom the member answers"""
    flag = " --raw" if raw is not None else " --body" if body is not None else ""
    if as_user:
        if not USER_ID.fullmatch(as_user):
            raise Stop("Not a user.")
        flag += f" --as {as_user}"
    if client_address(client):
        flag += f" --client {client_address(client)}"
    command = f"{REMOTE_HARBOR} master call {shlex.quote(method)} {shlex.quote(path)}{flag}"
    sent = json.dumps(raw) if raw is not None else json.dumps(body) if body is not None else None
    result = system.run(*ssh(system, entry), *on(entry, command), input=sent, check=False, capture=True)
    if not result.ok:
        said = (result.stderr or result.stdout).strip().splitlines()
        if any("invalid choice: 'master'" in line or "argument <group>" in line or "unrecognized arguments" in line or "invalid choice: 'ws'" in line for line in said):
            raise Stop(f"{name} runs a Harbor without the master's calls: update it (Servers, Update).")
        raise Stop(f"{name} did not answer: {said[-1] if said else 'no answer'}")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Stop(f"{name} answered something Harbor does not understand.") from None


MEMBERSHIP = "/var/lib/harbor/master.json"


def join(system, name):
    """A server in Servers made a member: it must answer the master's calls first. Only a server that is no one's member
    makes members (a member keeps servers in Servers for its connections and copies, never members of its own), and a
    master is never made another's member"""
    from harbor.server.servers import SERVERS, server
    if system.path(MEMBERSHIP).exists():
        raise Stop("This server is a member of a master: only the master makes members.")
    entry = server(system, name)
    system.plan([f"Ask {name}'s admin", f"Make {name} a member"])
    system.step(f"Ask {name}'s admin")
    answer = ask(system, name, entry, "GET", "/api/me")
    if answer.get("status") != 200:
        raise Stop(f"{name}'s admin did not take the master's call ({answer.get('status')}): update Harbor there.")
    told = answer.get("body") or {}
    if told.get("master"):
        raise Stop(f"{name} is a master itself, with members of its own: a master is no one's member.")
    if told.get("member_of") and (told["member_of"].get("name") or told["member_of"].get("address")):
        mine = json.loads(base64.b64decode(membership_body(system)["data"]))
        if told["member_of"].get("address") != mine.get("address"):
            raise Stop(f"{name} is a member of {told['member_of'].get('address')} already: it leaves that master first.")
    system.step(f"Make {name} a member")
    # It is told whose member it is: its own users are made here from now on
    told = ask(system, name, entry, "PUT", "/api/membership", raw=membership_body(system))
    if told.get("status") != 200:
        raise Stop(f"{name} could not be told it is a member: {(told.get('body') or {}).get('error') or told.get('status')}")
    found = read_json(system.path(SERVERS)) or {}
    found[name] = {**found[name], "member": True}
    write_json(system.path(SERVERS), found)
    # The members know each other: a site is copied or moved from one to another without a password
    introduce(system, only=name)
    # The master's notifications are its too (its mail server, the site report, the usage, health and job alerts),
    # until it keeps one its own: sent by the master's admin, which has them
    try:
        sent = call(system, "POST", "/api/master/notifications/push", body={"member": name})
        if sent.get("status") != 200:
            system.say(f"{name} did not take the master's notifications: {(sent.get('body') or {}).get('error') or sent.get('status')}")
    except (Stop, OSError) as exc:
        system.say(f"{name} did not take the master's notifications: {exc}")
    system.say(f"{name} is a member: its sites are shown here, and its users are made here.")


def membership_body(system):
    """What a member is told of its master: its name and the address of its admin"""
    from harbor.install.admin import admin_sites
    sites = admin_sites(system)
    body = {"name": "Harbor Master", "address": f"https://{sites[0]}" if sites else ""}
    return {"type": "application/json", "data": base64.b64encode(json.dumps(body).encode()).decode()}


def reached_at(host):
    """The address the master reaches a server at: its name as the master's own DNS says it, else as it is given"""
    import ipaddress
    import socket
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


KEY_OF = ("cd /opt/harbor/current && python3 -c 'from harbor.core.system import System; "
          "from harbor.server.servers import key; print(key(System()))'")


def introduce(system, only=None):
    """The members made known to each other, as the master knows them: each gets the others in its own Servers (Harbor's
    key of it put on them by the master, which reaches them all), so a site there is copied or moved to another member
    from there, with no password asked. One that knows another already is left as it is. only: a member just joined"""
    from harbor.server.servers import remote
    found = members(system)
    said = []
    for name, entry in sorted(found.items()):
        others = {other: item for other, item in found.items() if other != name and (only is None or only in (name, other))}
        if not others:
            continue
        listed = remote(system, entry, f"{REMOTE_HARBOR} servers list", check=False)
        try:
            known = json.loads(listed.stdout or "{}") if listed.ok else {}
        except ValueError:
            known = {}
        hosts = {str(item.get("host", "")).lower() for item in known.values() if isinstance(item, dict)}
        public = ""
        for other, item in sorted(others.items()):
            if other in known or str(item.get("host", "")).lower() in hosts:
                continue
            if not public:
                got = remote(system, entry, KEY_OF, check=False)
                public = (got.stdout or "").strip().splitlines()[-1] if got.ok and got.stdout.strip() else ""
                if not public.startswith("ssh-"):
                    said.append(f"{name}: its Harbor key could not be read")
                    break
            system.step(f"Make {other} known to {name}")
            put = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                   f"(grep -qxF {shlex.quote(public)} ~/.ssh/authorized_keys || echo {shlex.quote(public)} >> ~/.ssh/authorized_keys)")
            if not remote(system, item, put, check=False).ok:
                said.append(f"{name}'s key could not be put on {other}")
                continue
            # The address the master reaches it at: a name may be another server there (linux-02 is its public address
            # on linux-01, its internal one here)
            address = reached_at(item["host"])
            added = remote(system, entry, f"{REMOTE_HARBOR} servers add {shlex.quote(other)} {shlex.quote(address)} --user {shlex.quote(item.get('user', 'root'))} "
                                          f"--port {shlex.quote(str(item.get('port', 22)))}", check=False)
            if not added.ok:
                said.append(f"{other} could not be added on {name}: {((added.stderr or added.stdout).strip().splitlines() or ['no answer'])[-1]}")
            elif item.get("no_web") or item.get("closed"):
                # One that hosts no sites, or takes no new one, is no place for one there either
                remote(system, entry, f"{REMOTE_HARBOR} servers web {shlex.quote(other)} {'off' if item.get('no_web') else 'closed'} --here", check=False)
            if added.ok and item.get("sql_closed"):
                # One whose SQL Server keeps its databases is no place to convert, move or copy one to there either
                remote(system, entry, f"{REMOTE_HARBOR} servers sql {shlex.quote(other)} closed --here", check=False)
    for line in said:
        system.warn(line)
    return said


def leave(system, name):
    """A member back to an ordinary server in Servers: its users are made on it again; nothing else changes there"""
    from harbor.server.servers import SERVERS, server
    entry = server(system, name)
    try:
        ask(system, name, entry, "DELETE", "/api/membership")
    except Stop as error:
        system.warn(f"{name} could not be told it is no longer a member: {error}")
    found = read_json(system.path(SERVERS)) or {}
    found[name] = {key: value for key, value in found[name].items() if key != "member"}
    write_json(system.path(SERVERS), found)
    # Its access goes from the master's users and templates (the admin here keeps them)
    try:
        said = call(system, "POST", "/api/master/forget-member", body={"member": name})
        if said.get("status") != 200:
            system.warn(f"The access on {name} could not be taken off the users here: {(said.get('body') or {}).get('error') or said.get('status')}")
    except (Stop, OSError) as error:
        system.warn(f"The access on {name} could not be taken off the users here: {error}")
    system.say(f"{name} is no longer a member: the master's users are gone from it, and its access from them.")


# What of a user goes to a member: who they are, and what they may there (an administrator everywhere, with the
# master's password, so they can log in there should the master be down)
USER_SENT = ("id", "username", "name", "email", "role", "disabled", "version")


def adds_environments(user):
    """A site given them that lets them add environments (Add environments), here or on any member: without one, no
    server makes environments for them, whatever was saved before"""
    grants = [user.get("sites") or {}] + [(access or {}).get("sites") or {} for access in (user.get("servers") or {}).values() if isinstance(access, dict)]
    return any("create_env" in (grant.get("actions") or []) for sites in grants for grant in sites.values() if isinstance(grant, dict))


def tell(system, name, own, others):
    """A member told something of itself (own, a harbor command run there), and every other member what it marks of it
    (others, run there): [what could not be told]"""
    found = members(system)
    said = []

    def why(result):
        """What a member that could not be told said: one on an older Harbor does not know the command"""
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        if any("invalid choice" in line or "argument" in line for line in lines):
            return "its Harbor is older: update it (Servers, Update all), then set this again"
        return lines[-1] if lines else "no answer"
    told = system.run(*ssh(system, found[name]), *on(found[name], f"{REMOTE_HARBOR} {own}"), check=False, capture=True)
    if not told.ok:
        said.append(f"{name} itself could not be told: {why(told)}")
    for other, entry in found.items():
        if other == name:
            continue
        sent = system.run(*ssh(system, entry), *on(entry, f"{REMOTE_HARBOR} {others}"), check=False, capture=True)
        if not sent.ok:
            said.append(f"{other} could not be told: {why(sent)}")
    return said


def sql(system, name, state):
    """A member's SQL Server set to take new databases (on), or to keep its own (closed): marked here, told to the member
    itself (its own connections are then no place to convert, move or copy a database to) and to every other member
    (whose connections to it, or that it lends, are not either)"""
    from harbor.server.servers import set_entry_sql
    if name not in members(system):
        raise Stop(f"{name} is not a member.")
    set_entry_sql(system, name, state)
    for line in tell(system, name, f"server sql {state}", f"servers sql {shlex.quote(name)} {state} --here"):
        system.warn(line)
    system.say(f"{name}'s SQL Server takes new databases." if state == "on"
               else f"{name}'s SQL Server keeps its databases: none is converted, moved or copied there.")


def web(system, name, state):
    """A member set to host sites (on), to keep its own and take no new one (closed), or to host none (off, a SQL
    Server's): marked here, told to the member itself (its admin makes no site then), and to every other member, whose
    dialogs then leave it out as a place for a site or an environment. The users are sent again: on one that is not
    on they make no site or environment there, and on one that is off they have no Web Server there"""
    from harbor.server.servers import set_entry_web
    found = members(system)
    if name not in found:
        raise Stop(f"{name} is not a member.")
    set_entry_web(system, name, state)
    said = []

    def why(result):
        """What a member that could not be told said: one on an older Harbor does not know the command"""
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        if any("invalid choice" in line or "argument" in line for line in lines):
            return "its Harbor is older: update it (Servers, Update all), then set this again"
        return lines[-1] if lines else "no answer"
    told = system.run(*ssh(system, found[name]), *on(found[name], f"{REMOTE_HARBOR} server web {state}"), check=False, capture=True)
    if not told.ok:
        said.append(f"{name} itself could not be told: {why(told)}")
    for other, entry in found.items():
        if other == name:
            continue
        sent = system.run(*ssh(system, entry), *on(entry, f"{REMOTE_HARBOR} servers web {shlex.quote(name)} {state} --here"), check=False, capture=True)
        if not sent.ok:
            said.append(f"{other} could not be told: {why(sent)}")
    try:
        call(system, "POST", "/api/master/push-users", body={})
    except (Stop, OSError):
        said.append("the users could not be sent again")
    for line in said:
        system.warn(line)
    system.say({"on": f"{name} hosts sites.",
                "closed": f"{name} keeps its sites: no new site or environment is made there.",
                "off": f"{name} hosts no sites: no site or environment is made there, and no user has a Web Server there."}[state])


def users_for(users, name, no_web=(), closed=()):
    """The master's users that have something on the member, as it is sent them. On a member that hosts no sites
    (no_web: the members that do not), no Web Server, and it is no place for their sites or environments; on one that
    keeps its sites (closed), their sites there stay theirs, and they make no new one or environment there"""
    shut = set(no_web) | set(closed)
    found = []
    for user in users:
        admin = user.get("role") == "admin"
        access = (user.get("servers") or {}).get(name)
        if not admin and not access:
            continue
        item = {key: user[key] for key in USER_SENT if key in user}
        item.update(access or {})
        if name in no_web:
            item.update(web=False, create_sites=False, create_envs=False, sftp=False, sites={})
        elif name in closed:
            item.update(create_sites=False, create_envs=False)
        # The other members they may make sites on: an environment of a site here may go there, and nowhere else
        item["create_on"] = sorted(other for other, given in (user.get("servers") or {}).items()
                                   if other != name and other not in shut and isinstance(given, dict) and given.get("create_sites"))
        # Where their environments may be made: the other members that host them for this user
        item["envs_on"] = sorted(other for other, given in (user.get("servers") or {}).items()
                                 if other != name and other not in shut and isinstance(given, dict) and given.get("create_envs"))
        if not adds_environments(user):
            item["envs_on"] = []
            item["create_envs"] = False
        if admin:
            item["password_hash"] = user.get("password_hash", "")
        found.append(item)
    return found


def push_users(system, users):
    """The master's users sent to each member, each with what it may there: {member: {users, skipped} or {error}}"""
    found = members(system)
    no_web = {name for name, entry in found.items() if entry.get("no_web")}
    closed = {name for name, entry in found.items() if entry.get("closed")}

    def send(item):
        name, entry = item
        listed = users_for(users, name, no_web, closed)
        raw = {"type": "application/json", "data": base64.b64encode(json.dumps({"users": listed}).encode()).decode()}
        try:
            answer = ask(system, name, entry, "PUT", "/api/membership/users", raw=raw)
        except Stop as error:
            return name, {"error": str(error)}
        body = answer.get("body") or {}
        if answer.get("status") != 200:
            return name, {"error": (body.get("error") if isinstance(body, dict) else "") or f"It answered {answer.get('status')}."}
        return name, {"users": body.get("users", 0), "skipped": body.get("skipped") or []}
    with ThreadPoolExecutor(max_workers=8) as executor:
        return dict(executor.map(send, sorted(found.items())))


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


# What of a SQL connection the master shows: never its login's password
CONNECTION_SHOWN = ("id", "name", "engine", "host", "port", "username")
# What of a database: its name, size and state (SQL Server's own and PostgreSQL's have an id of 4 or less: not shown)
DATABASE_SHOWN = ("name", "size_mb", "state", "owner")


def addresses(entry):
    """A server's addresses as Servers has them: its host, what that name resolves to, and its public address"""
    import socket
    found = {entry.get("host", ""), entry.get("public", "")}
    try:
        found.add(socket.gethostbyname(entry.get("host", "")))
    except OSError:
        pass
    return found - {""}


LOCAL = {"localhost", "127.0.0.1", "::1"}


def member_databases(system, name, entry, others=None):
    """A member's own SQL Server (Microsoft SQL Server, PostgreSQL or both): its databases, as its own admin lists them.
    Only the connections to itself: what it reaches elsewhere is another server's"""
    own = LOCAL | {address.lower() for address in addresses(entry)}
    base = {"server": name, "host": entry.get("host", "")}
    try:
        answer = ask(system, name, entry, "GET", "/api/sql/connections")
    except Stop as error:
        return {**base, "reachable": False, "error": str(error), "connections": []}
    body = answer.get("body") or {}
    if answer.get("status") != 200:
        return {**base, "reachable": False, "error": (body.get("error") if isinstance(body, dict) else "") or f"It answered {answer.get('status')}.", "connections": []}
    shown = []
    for item in body.get("connections") or []:
        if not isinstance(item, dict) or str(item.get("host", "")).lower() not in own:
            continue
        connection = {key: item[key] for key in CONNECTION_SHOWN if key in item}
        try:
            found = ask(system, name, entry, "GET", f"/api/sql/{item.get('id', '')}")
            listed = (found.get("body") or {}).get("databases") if found.get("status") == 200 else None
            connection["error"] = "" if listed is not None else ((found.get("body") or {}).get("error") or "Its databases could not be read.")
        except Stop as error:
            listed, connection["error"] = None, str(error)
        connection["databases"] = [{key: database[key] for key in DATABASE_SHOWN if key in database}
                                   for database in listed or [] if isinstance(database, dict) and (database.get("id") or 0) > 4]
        shown.append(connection)
    return {**base, "reachable": True, "error": "", "connections": shown}


def connections(system):
    """Every member's own SQL Server and its databases, the members asked at once"""
    found = members(system)
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(lambda item: member_databases(system, *item), sorted(found.items())))


def admin_address(entry):
    """Where a member's own admin is, to open a site there: admin.<its name>, as Servers learnt it"""
    names = entry.get("names") or []
    return f"https://admin.{names[0]}" if names else ""


def sites(system):
    """Every member's sites, a member at a time, asked at once"""
    found = members(system)
    with ThreadPoolExecutor(max_workers=8) as executor:
        return list(executor.map(lambda item: member_sites(system, *item), sorted(found.items())))
