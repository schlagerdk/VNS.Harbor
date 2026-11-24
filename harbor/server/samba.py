"""The whole server shared over Samba as root, for root with a Samba password of its own: turned on, given a new
password or turned off from Server tools (harbor server samba). It was a question in harbor server init."""
import json
import os
import re

from harbor.core.system import Stop
from harbor.sql.server import installed

SMB_CONF = "/etc/samba/smb.conf"
PASSWORD_VARIABLE = "HARBOR_SAMBA_PASSWORD"


def share_name(system):
    """The server's short name, as the share is called: linux-03 on linux-03.vns.dk"""
    from harbor.server.apache import own_name
    return own_name(system).split(".")[0]


def sections(text):
    """smb.conf's sections: (name, its text from the header to the next one)"""
    found = list(re.finditer(r"(?m)^[ \t]*\[([^\]]+)\][ \t]*$", text))
    return [(match.group(1), text[match.start():found[index + 1].start() if index + 1 < len(found) else len(text)])
            for index, match in enumerate(found)]


def root_share(system):
    """The share of the whole server: the one by the server's name, or one with path = / by another name (an older
    server's, as [kikayo/]), or None"""
    conf = system.path(SMB_CONF)
    text = conf.read_text() if conf.is_file() else ""
    name = share_name(system).lower()
    found = [(section, body) for section, body in sections(text) if section.lower() not in ("global", "printers", "print$")]
    for section, body in found:
        if section.lower() == name:
            return section
    for section, body in found:
        if re.search(r"(?im)^\s*path\s*=\s*/\s*$", body):
            return section
    return None


def has_share(system):
    return root_share(system) is not None


def has_password(system):
    return system.which("pdbedit") is not None and system.run("pdbedit", "-u", "root", check=False, capture=True).ok


SYSTEM_SECTIONS = ("global", "printers", "print$")
# The settings a share has in Server tools; others in its section stay as they are
SETTINGS = {"path": "path", "read only": "read_only", "browseable": "browseable", "guest ok": "guest_ok", "force user": "force_user",
            "valid users": "valid_users", "comment": "comment"}
# A slash is allowed, also first and alone: older servers named their root share as [kikayo/], and [/] works too
SHARE_NAME = re.compile(r"[A-Za-z0-9/][A-Za-z0-9._ $/-]{0,79}")


def is_yes(value):
    return str(value).strip().lower() in ("yes", "true", "1")


def shares(system):
    """smb.conf's shares (not [global] and the printers), each with its settings as Server tools shows them"""
    conf = system.path(SMB_CONF)
    text = conf.read_text() if conf.is_file() else ""
    found = []
    for name, body in sections(text):
        if name.lower() in SYSTEM_SECTIONS:
            continue
        values = {}
        for line in body.splitlines()[1:]:
            key, separator, value = line.strip().partition("=")
            if separator and not line.strip().startswith(("#", ";")):
                values[" ".join(key.strip().lower().split())] = value.strip()
        read_only = values.get("read only", "yes" if not is_yes(values.get("writable", values.get("writeable", "no"))) else "no")
        found.append({"name": name, "path": values.get("path", ""), "read_only": is_yes(read_only),
                      "browseable": is_yes(values.get("browseable", values.get("browsable", "yes"))), "guest_ok": is_yes(values.get("guest ok", "no")),
                      "force_user": values.get("force user", ""), "valid_users": values.get("valid users", ""), "comment": values.get("comment", "")})
    return found


USER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
MADE = "/etc/harbor/samba-users.json"


def made_users(system):
    """The Linux users Harbor made for Samba alone (no shell, no home): they go with their Samba login"""
    path = system.path(MADE)
    try:
        found = json.loads(path.read_text()) if path.is_file() else []
    except ValueError:
        found = []
    return [name for name in found if isinstance(name, str)]


def remember_made(system, names):
    path = system.path(MADE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(set(names))) + "\n")


def users(system):
    """The Samba logins (pdbedit -L), each with whether Harbor made its Linux user for Samba alone"""
    found = system.run("pdbedit", "-L", check=False, capture=True) if system.which("pdbedit") else None
    made = made_users(system)
    names = [line.split(":", 1)[0] for line in (found.stdout if found and found.ok else "").splitlines() if line.strip()]
    return [{"name": name, "samba_only": name in made} for name in names]


def password_from(password):
    password = password or os.environ.get(PASSWORD_VARIABLE, "")
    if len(password) < 6 or "\n" in password:
        raise Stop("The Samba password needs at least 6 characters.")
    return password


def add_user(system, name, password=""):
    """A Samba login: its Linux user made when there is none (no shell, no home: it is for Samba alone), and its
    password. The first login besides root closes every share that lets any login in to root, so the new one reaches
    nothing until a share names it."""
    password = password_from(password)
    if not USER_NAME.fullmatch(name) or name == "root":
        raise Stop(f"Not a user name: {name} (lower case letters, digits, - and _)")
    if any(user["name"] == name for user in users(system)):
        raise Stop(f"{name} is a Samba user already.")
    linux = system.run("id", "-u", name, check=False, capture=True).ok
    open_shares = [share["name"] for share in shares(system) if not share["valid_users"]]
    system.plan([*([] if linux else [f"Make the Linux user {name} for Samba"]), f"Give {name} a Samba login",
                 *([f"Let only root into {', '.join(open_shares)}"] if open_shares else [])])
    if not linux:
        system.step(f"Make the Linux user {name} for Samba")
        system.run("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", name)
        remember_made(system, [*made_users(system), name])
    system.step(f"Give {name} a Samba login")
    system.run("smbpasswd", "-a", "-s", name, input=f"{password}\n{password}\n")
    if open_shares:
        system.step(f"Let only root into {', '.join(open_shares)}")
        conf = system.path(SMB_CONF)
        text = conf.read_text()
        for section, body in sections(text):
            if section in open_shares:
                lines = body.rstrip("\n").split("\n")
                text = text.replace(body, "\n".join([lines[0], *lines[1:], "   valid users = root"]) + "\n" + ("\n" if body.endswith("\n\n") else ""), 1)
        write_checked(system, text)
    system.say(f"{name} has a Samba login." + (f" It reaches no share yet: add it to one under Who may log in." if open_shares or shares(system) else ""))


def user_password(system, name, password=""):
    password = password_from(password)
    if not any(user["name"] == name for user in users(system)):
        raise Stop(f"{name} is not a Samba user.")
    system.plan([f"Give {name} a new Samba password"])
    system.step(f"Give {name} a new Samba password")
    system.run("smbpasswd", "-s", name, input=f"{password}\n{password}\n")
    system.say(f"{name} has a new Samba password.")


def delete_user(system, name):
    """A Samba login taken away, and its Linux user with it when Harbor made it for Samba alone. Root stays."""
    if name == "root":
        raise Stop("Root's Samba login stays: give it a new password instead.")
    if not any(user["name"] == name for user in users(system)):
        raise Stop(f"{name} is not a Samba user.")
    made = name in made_users(system)
    system.plan([f"Take away {name}'s Samba login", *([f"Delete the Linux user {name}"] if made else [])])
    system.step(f"Take away {name}'s Samba login")
    system.run("smbpasswd", "-x", name)
    if made:
        system.step(f"Delete the Linux user {name}")
        system.run("userdel", name, check=False)
        remember_made(system, [user for user in made_users(system) if user != name])
    system.say(f"{name} has no Samba login any more." + (" Its Linux user is deleted too." if made else " Its Linux user stays: it was there before."))


def status(system):
    """What Server tools shows: whether Samba is here, its shares (the whole server's among them), root given a
    password and smbd running, and the name the server is reached by"""
    from harbor.server.apache import own_name
    here = installed(system, "samba")
    return {"installed": here, "name": root_share(system) or share_name(system), "share": has_share(system), "host": own_name(system).lower(),
            "shares": shares(system) if here else [], "users": users(system) if here else [], "password": here and has_password(system),
            "running": here and system.output("systemctl", "is-active", "smbd", check=False) == "active"}


def write_checked(system, text):
    """smb.conf written, checked with testparm and Samba told; a config testparm refuses is taken back"""
    conf = system.path(SMB_CONF)
    before = conf.read_text() if conf.is_file() else ""
    conf.write_text(text)
    checked = system.run("testparm", "-s", check=False, capture=True)
    if not checked.ok:
        conf.write_text(before)
        raise Stop(f"Samba refused the change, so it is taken back: {(checked.stderr or checked.stdout).strip().splitlines()[-1:] or ''}")
    system.run("systemctl", "reload", "smbd", check=False)


def save_share(system, share, old_name=""):
    """A share made or changed: its name, folder and settings. Settings Server tools does not show stay as they were."""
    name = str(share.get("name", "")).strip()
    path = str(share.get("path", "")).strip()
    if not SHARE_NAME.fullmatch(name) or name.lower() in SYSTEM_SECTIONS or "]" in name:
        raise Stop(f"Not a share name: {name} (letters, digits, dots, hyphens, slashes and spaces)")
    if not path.startswith("/") or "\n" in path or ".." in path.split("/"):
        raise Stop(f"Not a folder: {path} (a full path, such as /var/www)")
    if not system.path(path).is_dir():
        raise Stop(f"There is no folder {path} on this server.")
    force_user = str(share.get("force_user", "")).strip()
    valid_users = str(share.get("valid_users", "")).strip()
    for value in (force_user, valid_users, str(share.get("comment", ""))):
        if "\n" in value or "\r" in value:
            raise Stop("A setting goes on one line.")
    conf = system.path(SMB_CONF)
    text = conf.read_text() if conf.is_file() else ""
    existing = dict(sections(text))
    old_name = str(old_name or "").strip()
    if old_name and old_name not in existing:
        raise Stop(f"There is no share {old_name}.")
    if name.lower() != old_name.lower() and any(section.lower() == name.lower() for section in existing):
        raise Stop(f"There is a share {name} already.")
    # Only what differs from Samba's own defaults is written, but read only always: its default (yes) surprises
    wanted = {"path": path, "read only": "yes" if share.get("read_only") else "no", "browseable": "" if share.get("browseable", True) else "no",
              "guest ok": "yes" if share.get("guest_ok") else "", "force user": force_user, "valid users": valid_users,
              "comment": str(share.get("comment", "")).strip()}
    # The other lines of a share that is there stay, its own settings changed in place
    kept, seen = [], set()
    for line in (existing[old_name].splitlines()[1:] if old_name else []):
        key = " ".join(line.strip().partition("=")[0].strip().lower().split())
        if key in ("writable", "writeable", "browsable", "read only", "browseable", "guest ok", "path", "force user", "valid users", "comment"):
            canonical = {"writable": "read only", "writeable": "read only", "browsable": "browseable"}.get(key, key)
            if canonical in seen or not wanted.get(canonical):
                continue
            seen.add(canonical)
            kept.append(f"   {canonical} = {wanted[canonical]}")
        elif line.strip():
            kept.append(line.rstrip())
    kept += [f"   {key} = {value}" for key, value in wanted.items() if value and key not in seen]
    section = f"[{name}]\n" + "\n".join(kept) + "\n"
    if old_name:
        body = existing[old_name]
        text = text.replace(body, section + ("\n" if body.endswith("\n\n") else ""), 1)
    else:
        text = text + ("" if not text or text.endswith("\n") else "\n") + "\n" + section
    system.plan([f"{'Change' if old_name else 'Add'} the share {name}"])
    system.step(f"{'Change' if old_name else 'Add'} the share {name}")
    write_checked(system, text)
    system.say(f"The share {name} is {'changed' if old_name else 'added'}: {path}" + (", read only" if share.get("read_only") else ""))


def delete_share(system, name):
    conf = system.path(SMB_CONF)
    text = conf.read_text() if conf.is_file() else ""
    existing = dict(sections(text))
    if name not in existing or name.lower() in SYSTEM_SECTIONS:
        raise Stop(f"There is no share {name}.")
    system.plan([f"Delete the share {name}"])
    system.step(f"Delete the share {name}")
    kept = text.replace(existing[name], "", 1)
    write_checked(system, kept.rstrip("\n") + "\n" if kept.strip() else "")
    system.say(f"The share {name} is deleted. Its folder and files stay.")


def turn(system, on):
    """Samba started (and when the server starts), or stopped: its shares stay as they are"""
    system.plan([f"Turn Samba {'on' if on else 'off'}"])
    system.step(f"Turn Samba {'on' if on else 'off'}")
    system.run("systemctl", "enable" if on else "disable", "--now", "smbd", "nmbd", check=not on)
    system.say("Samba is on: its shares can be reached." if on else "Samba is off: its shares stay, and Turn on shares them again.")


def set_password(system, password):
    if not password or "\n" in password:
        raise Stop("Give root a Samba password.")
    system.run("smbpasswd", "-a", "-s", "root", input=f"{password}\n{password}\n")


def share_over_samba(system, password=""):
    """Samba installed, the whole server shared as root and root given a Samba password (the one given, else asked
    in a terminal). A share there already stays as it is, and so does a password root has when none is given."""
    password = password or os.environ.get(PASSWORD_VARIABLE, "")
    name = share_name(system)
    steps = [*([] if installed(system, "samba") else ["Install Samba"]), f"Share the server as {name}"]
    system.plan(steps)
    if not installed(system, "samba"):
        system.step("Install Samba")
        system.run("apt-get", "install", "-y", "samba", env={"DEBIAN_FRONTEND": "noninteractive"})
    system.step(f"Share the server as {name}")
    conf = system.path(SMB_CONF)
    text = conf.read_text() if conf.is_file() else ""
    added = False
    if not has_share(system):
        system.say(f"Share / as {name}")
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"""
[{name}]
   path = /
   browseable = yes
   read only = no
   guest ok = no
   force user = root
   force group = root
""")
        added = True
    if password:
        set_password(system, password)
    elif not has_password(system):
        if system.interactive():
            password = system.ask("Samba password for root: ", secret=True)
            if password and password == system.ask("Again: ", secret=True):
                set_password(system, password)
            else:
                system.warn("The passwords were not the same, or none was given: set it with smbpasswd -a root")
        else:
            system.warn("Root has no Samba password yet: set it with smbpasswd -a root")
    system.run("systemctl", "enable", "--now", "smbd", "nmbd")
    if added:
        system.run("systemctl", "reload", "smbd")
    system.say(f"The server is shared over Samba as \\\\{name} (user root).")


def change_password(system, password=""):
    password = password or os.environ.get(PASSWORD_VARIABLE, "")
    system.plan(["Give root a new Samba password"])
    system.step("Give root a new Samba password")
    set_password(system, password)
    system.say("Root has a new Samba password.")


def stop_sharing(system):
    """The share taken out of smb.conf and Samba stopped: the server is not shared any more. Samba stays installed,
    and the other sections of smb.conf stay as they are."""
    name = root_share(system) or share_name(system)
    system.plan([f"Stop sharing the server as {name}"])
    system.step(f"Stop sharing the server as {name}")
    conf = system.path(SMB_CONF)
    if conf.is_file() and has_share(system):
        text = conf.read_text()
        # The section from its header to the next header (or the end)
        body = dict(sections(text))[name]
        kept = text.replace(body, "", 1)
        conf.write_text(kept.rstrip("\n") + "\n" if kept.strip() else "")
    system.run("systemctl", "disable", "--now", "smbd", "nmbd", check=False)
    system.say("The server is not shared any more. Samba stays installed: Share the server in Server tools shares it again.")
