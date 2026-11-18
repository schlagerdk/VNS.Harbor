"""A Linux user for each user of the admin (phase 8a of dev/HARBOR.md), and a group per site for those with its Files
grant (8b).

Harbor only ever changes the Linux users in its group harbor-users: others on the server are never touched, also when
an admin user has the same name. Which admin user has which Linux user is kept in /var/lib/harbor/users.json, so a
user keeps their Linux name when their username changes. The users have no sudo and a locked password: they are reached
from the admin (the terminal, phase 8c) and, later, over SFTP (8d).
"""
import hashlib
import re

from harbor.install.admin import is_admin_site
from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

GROUP = "harbor-users"
MAPPING = "/var/lib/harbor/users.json"
SITE_GROUPS = "/var/lib/harbor/site-groups.json"
CLOSED = "/var/lib/harbor/closed-sites.json"
# Sites whose folder had the sticky bit, taken off while they have a group (or the group could not delete root's files)
STICKY = "/var/lib/harbor/site-sticky.json"
WEB_ROOT = "/var/www"
# Hold passwords until phase 6: a site's group never reaches them
EXCLUDED = ("scripts", ".vns-publish")
SITE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
SHELL = "/bin/bash"
NO_SHELL = "/usr/sbin/nologin"
# Names a user never gets, though no such user exists here yet
RESERVED = {"root", "admin", "www-data", "harbor", "nobody", "sshd", "daemon", "bin", "sys", "sync", "mail", "mssql", "ubuntu", "postgres", "mysql"}


def passwd(system):
    """{name: (uid, home, shell)} of the Linux users here"""
    path = system.path("/etc/passwd")
    found = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
        parts = line.split(":")
        if len(parts) >= 7:
            found[parts[0]] = (parts[2], parts[5], parts[6])
    return found


def members(system, group):
    path = system.path("/etc/group")
    for line in path.read_text().splitlines() if path.is_file() else []:
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == group:
            return {name for name in parts[3].split(",") if name}
    return set()


def group_exists(system, group):
    path = system.path("/etc/group")
    return path.is_file() and any(line.split(":")[0] == group for line in path.read_text().splitlines())


def base_name(username):
    """A valid Linux name from an admin username: the part before @, lower case, letters, digits, _ and -"""
    name = re.sub(r"[^a-z0-9_-]+", "-", username.split("@")[0].lower()).strip("-_")
    if not name or not name[0].isalpha():
        name = "u" + name
    return name[:28].rstrip("-_")


def free_name(username, taken):
    name = base_name(username)
    candidate, number = name, 2
    while candidate in taken or candidate in RESERVED:
        candidate = f"{name}-{number}"
        number += 1
    return candidate


def comment(user):
    """The account's comment (GECOS): no colon, comma or line break"""
    return "Harbor " + re.sub(r"[:,\n]", " ", user["username"])


def mapping(system):
    found = read_json(system.path(MAPPING))
    return found if isinstance(found, dict) else {}


def linux_name(system, user_id):
    return (mapping(system).get(user_id) or {}).get("linux", "")


def sync(system, users):
    """The Linux users equal to the admin's users: [{id, username, role, disabled}], root not among them"""
    users = [user for user in users if isinstance(user, dict) and user.get("id") and user.get("id") != "root"]
    if not system.which("useradd"):
        raise Stop("useradd is missing: Linux users can only be made on the server.")
    if not group_exists(system, GROUP):
        system.run("groupadd", "--system", GROUP)
    known = mapping(system)
    accounts = passwd(system)
    ours = members(system, GROUP)
    try:
        names = apply(system, users, known, accounts, ours)
    finally:
        # Also after a failed step: a Linux user made is never forgotten
        write_json(system.path(MAPPING), known)
    sync_sites(system, users, names)
    keep_sites_closed(system, bool(members(system, GROUP)))
    from harbor.server import sftp
    sftp.sync(system, users, names, wanted_sites(system, users, names))
    return names


def apply(system, users, known, accounts, ours):
    wanted = {user["id"] for user in users}
    # Removed from the admin: their Linux user goes, with its home (never one Harbor did not make)
    for user_id in [key for key in known if key not in wanted]:
        name = known[user_id].get("linux", "")
        if name in accounts and name in ours:
            system.plan([f"Remove the Linux user {name}"])
            system.step(f"Remove the Linux user {name}")
            system.run("pkill", "-KILL", "-u", name, check=False, capture=True)
            system.run("userdel", "--remove", name, check=False, capture=True)
        del known[user_id]
    for user in users:
        entry = known.get(user["id"]) or {}
        name = entry.get("linux", "")
        if not name or name not in accounts or name not in ours:
            if name and name in accounts and name not in ours:
                # The name went to a user Harbor does not manage: this user gets another one
                name = ""
            name = name or free_name(user["username"], set(accounts) | {item.get("linux") for item in known.values()})
            system.plan([f"Make the Linux user {name} for {user['username']}"])
            system.step(f"Make the Linux user {name} for {user['username']}")
            system.run("useradd", "--create-home", "--user-group", "--groups", GROUP, "--shell", SHELL,
                       "--comment", comment(user), name)
            accounts[name] = ("", f"/home/{name}", SHELL)
            ours.add(name)
            entry = {"linux": name, "username": user["username"], "disabled": False}
            known[user["id"]] = entry
        disabled = bool(user.get("disabled"))
        if entry.get("disabled", False) != disabled or entry.get("username") != user["username"]:
            # A disabled user's account is expired and has no shell; enabled again, it gets both back
            if disabled:
                system.run("usermod", "--expiredate", "1", "--shell", NO_SHELL, name)
                system.run("pkill", "-KILL", "-u", name, check=False, capture=True)
            else:
                system.run("usermod", "--expiredate", "", "--shell", SHELL, "--comment", comment(user), name)
        entry.update(linux=name, username=user["username"], disabled=disabled)
        known[user["id"]] = entry
    system.say(f"{len(users)} admin user(s), each with a Linux user.")
    return {user_id: entry["linux"] for user_id, entry in known.items()}


# ---- a group per site (8b)
def site_group(site):
    """site-<name> as a valid group name (at most 32 characters): dots become -, a long name ends in a hash of it"""
    name = "site-" + re.sub(r"[^a-z0-9-]+", "-", site.lower()).strip("-")
    if len(name) > 32:
        name = name[:23].rstrip("-") + "-" + hashlib.sha1(site.encode()).hexdigest()[:8]
    return name


def wanted_sites(system, users, names):
    """{site: Linux users} of the users who are not administrators, not disabled, with the Files grant on the site"""
    found = {}
    for user in users:
        if user.get("role") == "admin" or user.get("disabled") or not names.get(user["id"]):
            continue
        for site, grant in (user.get("sites") or {}).items():
            if (isinstance(grant, dict) and {"files", "terminal"} & set(grant.get("actions") or []) and SITE_NAME.fullmatch(site)
                    and system.path(f"{WEB_ROOT}/{site}").is_dir() and not system.path(f"{WEB_ROOT}/{site}").is_symlink()):
                found.setdefault(site, set()).add(names[user["id"]])
    return found


def acl_tools(system):
    if not system.which("setfacl"):
        system.plan(["Install the acl tools"])
        system.step("Install the acl tools")
        system.run("apt-get", "install", "-y", "-q", "acl", capture=True, env={"DEBIAN_FRONTEND": "noninteractive"})


def grant_folder(system, site, group):
    """rwX for the site's group on the site's folder, and on what is made in it later; never on EXCLUDED"""
    root = f"{WEB_ROOT}/{site}"
    entry = f"g:{group}:rwX,m::rwX"
    # In a folder with the sticky bit only a file's owner may delete or rename it: the site's group could only add
    if system.path(root).stat().st_mode & 0o1000:
        sticky = read_json(system.path(STICKY)) or []
        system.run("chmod", "-t", root)
        write_json(system.path(STICKY), sorted(set(sticky) | {site}))
    system.run("setfacl", "-R", "-P", "-m", entry, root)
    system.run("find", "-P", root, "-type", "d", "-exec", "setfacl", "-d", "-m", entry, "{}", "+")
    for name in EXCLUDED:
        if system.path(f"{root}/{name}").exists():
            revoke_folder(system, f"{root}/{name}", group)


def revoke_folder(system, folder, group):
    sticky = read_json(system.path(STICKY)) or []
    site = folder.rstrip("/").rsplit("/", 1)[-1]
    if folder == f"{WEB_ROOT}/{site}" and site in sticky:
        system.run("chmod", "+t", folder, check=False, capture=True)
        write_json(system.path(STICKY), [name for name in sticky if name != site])
    system.run("setfacl", "-R", "-P", "-x", f"g:{group}", folder, check=False, capture=True)
    system.run("find", "-P", folder, "-type", "d", "-exec", "setfacl", "-d", "-x", f"g:{group}", "{}", "+", check=False, capture=True)


def site_groups(system):
    found = read_json(system.path(SITE_GROUPS))
    return found if isinstance(found, dict) else {}


def sync_sites(system, users, names):
    """The site groups equal to the Files grants: made with their ACLs on the first user, members set, and gone with
    their ACLs when nobody has the grant any more"""
    groups = site_groups(system)
    wanted = wanted_sites(system, users, names)
    try:
        for site in [site for site in groups if site not in wanted]:
            group = groups.pop(site)
            system.plan([f"Take the group {group} off {site}"])
            system.step(f"Take the group {group} off {site}")
            if system.path(f"{WEB_ROOT}/{site}").is_dir() and system.which("setfacl"):
                revoke_folder(system, f"{WEB_ROOT}/{site}", group)
            system.run("groupdel", group, check=False, capture=True)
        for site, names_here in sorted(wanted.items()):
            group = groups.get(site) or site_group(site)
            if site not in groups:
                acl_tools(system)
                if not group_exists(system, group):
                    system.run("groupadd", group)
                system.plan([f"Give the group {group} the folder of {site}"])
                system.step(f"Give the group {group} the folder of {site}")
                grant_folder(system, site, group)
                groups[site] = group
            elif system.path(f"{WEB_ROOT}/{site}").stat().st_mode & 0o1000:
                # Given before the sticky bit was taken off, or set again by hand
                sticky = read_json(system.path(STICKY)) or []
                system.run("chmod", "-t", f"{WEB_ROOT}/{site}")
                write_json(system.path(STICKY), sorted(set(sticky) | {site}))
            if names_here != members(system, group):
                system.run("gpasswd", "-M", ",".join(sorted(names_here)), group, capture=True)
    finally:
        write_json(system.path(SITE_GROUPS), groups)


def keep_site(system, site):
    """The site's group has its rights again after something set the folder's modes (fix permissions, a publish): a
    chmod narrows the ACL mask"""
    group = site_groups(system).get(site)
    if group and system.path(f"{WEB_ROOT}/{site}").is_dir() and system.which("setfacl"):
        grant_folder(system, site, group)


# ---- the site folders closed to others while the server has users (8b2)
def site_folders(system):
    """The sites' folders in /var/www: never the admin's own (root's, with its config) or a link"""
    web = system.path(WEB_ROOT)
    for path in sorted(web.iterdir()) if web.is_dir() else []:
        if (path.is_dir() and not path.is_symlink() and SITE_NAME.fullmatch(path.name)
                and not is_admin_site(path)):
            yield path


def keep_sites_closed(system, closed):
    """With users on the server, a site's folder is closed to others: only its owner, its group (vns-build), www-data
    (by an ACL) and the site's own group get in, so a user reaches no other site's settings. Only the site's own
    folder changes; what is in it stays as it is. Without users, each folder gets its mode back."""
    record = read_json(system.path(CLOSED))
    record = record if isinstance(record, dict) else {}
    try:
        if closed:
            for path in site_folders(system):
                mode = path.stat().st_mode & 0o7777
                if mode & 0o007:
                    acl_tools(system)
                    folder = f"{WEB_ROOT}/{path.name}"
                    system.plan([f"Close the folder of {path.name} to others"])
                    system.step(f"Close the folder of {path.name} to others")
                    # www-data first, so the site is never without it
                    system.run("setfacl", "-m", "u:www-data:rX", folder)
                    system.run("chmod", "o-rwx", folder)
                    record.setdefault(path.name, format(mode, "o"))
        else:
            for site, mode in sorted(record.items()):
                folder = f"{WEB_ROOT}/{site}"
                if system.path(folder).is_dir():
                    system.plan([f"Open the folder of {site} again"])
                    system.step(f"Open the folder of {site} again")
                    system.run("chmod", mode, folder)
                    system.run("setfacl", "-x", "u:www-data", folder, check=False, capture=True)
            record = {}
    finally:
        write_json(system.path(CLOSED), record)
