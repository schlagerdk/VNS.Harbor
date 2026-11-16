"""Users of the admin and what each may do. Pure functions: the admin (app.py) loads, checks and saves.

Three kinds of user:
  root   the one in config.json's "user" and "password_hash", made when the admin was installed. It can do
         everything, cannot be changed or removed by anyone else, and other users never see that it exists.
  admin  can do everything as well, including managing users other than root.
  user   only what is given: Web Server and/or SQL Server, chosen sites and environments, and per site the actions.
         Never a terminal, server files, Apache, service files, or anything that runs as root by choice.

A user record, as kept in config.json's "users":
  {"id", "username", "password_hash", "role": "admin"|"user", "disabled", "version",
   "web": bool, "create_sites": bool,
   "sites": {"<site>": {"envs": ["main", "<environment id>", ...], "new_envs": bool, "actions": [...]}},
   "sql": bool, "manage_connections": bool,
   "connections": {"<connection id>": {"databases": ["*"] or [names], "actions": [...]}}}
"envs" are the environments of the site the user may see ("main" is the main site); "new_envs" adds environments
made later. What a user creates is added to what they may use (see grant_site, grant_environment, grant_database).
"""
import re
import secrets

ROOT = "root"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
ENV_RE = re.compile(r"^(main|[a-z0-9][a-z0-9-]{0,62})$")

# What a user may be given on a site, in the order the admin shows them
SITE_ACTIONS = [
    ("service", "Start, stop and restart"),
    ("logs", "Logs"),
    ("publish", "Publish"),
    ("sync_data", "Sync data"),
    ("sync_settings", "Sync settings"),
    ("clear_logs", "Clear logs"),
    ("appsettings", "Edit appsettings.json"),
    ("files", "Files"),
    ("terminal", "Terminal"),
    ("packages", "NuGet packages"),
    ("bindings", "Bindings and certificates"),
    ("create_env", "Add environments"),
    ("delete", "Delete"),
    ("scripts", "Run custom scripts"),
]
SQL_ACTIONS = [
    ("query", "Run queries"),
    ("backup", "Back up"),
    ("restore", "Restore"),
    ("create", "Create databases"),
    ("copy", "Copy databases"),
    ("delete", "Delete databases"),
]
SITE_ACTION_NAMES = {name for name, _ in SITE_ACTIONS}
SQL_ACTION_NAMES = {name for name, _ in SQL_ACTIONS}


def is_admin(user):
    return bool(user) and (user.get("id") == ROOT or user.get("role") == "admin")


def root_user(config):
    return {"id": ROOT, "username": config.get("user", ""), "name": config.get("name", ""), "role": "admin", "root": True}


def clean_name(value):
    """A user's name as others see it (the sync history, the menu): optional, one line, at most 80 characters"""
    name = " ".join(str(value or "").split())
    if len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError("The name may be at most 80 characters.")
    return name


def label(config, user_id):
    """A user by name, or by username when they have none; "" when there is no such user"""
    if user_id == ROOT:
        return config.get("name") or config.get("user", "")
    user = next((item for item in config.get("users", []) if item.get("id") == user_id), None)
    return (user.get("name") or user.get("username", "")) if user else ""


def find(config, user_id):
    """The user with this id, root included; None when there is none or it is disabled"""
    if user_id == ROOT:
        return root_user(config)
    for user in config.get("users", []):
        if user.get("id") == user_id and not user.get("disabled"):
            return user
    return None


def by_username(config, username):
    """For the login: (id, password_hash) of the user with this name, root included, or None"""
    if username and username == config.get("user"):
        return ROOT, config.get("password_hash", "")
    for user in config.get("users", []):
        if user.get("username") == username and not user.get("disabled"):
            return user["id"], user.get("password_hash", "")
    return None


def taken(config, username, except_id=None):
    return username == config.get("user") or any(
        user.get("username", "").lower() == username.lower() and user.get("id") != except_id for user in config.get("users", []))


# ---- what a user may do

def site_access(user, site):
    """The user's grant on a site: {"envs": set, "new_envs": bool, "actions": set}, or None"""
    if is_admin(user):
        return {"envs": None, "new_envs": True, "actions": set(SITE_ACTION_NAMES)}
    if not user or not user.get("web"):
        return None
    grant = (user.get("sites") or {}).get(site)
    if not isinstance(grant, dict):
        return None
    return {"envs": set(grant.get("envs") or []), "new_envs": bool(grant.get("new_envs")), "actions": set(grant.get("actions") or [])}


def sees_env(user, site, env):
    """Whether the user sees an environment of a site: one it was given, or one made after the grant when the grant
    takes new environments"""
    access = site_access(user, site)
    if access is None:
        return False
    if access["envs"] is None or env in access["envs"]:
        return True
    known = set(((user.get("sites") or {}).get(site) or {}).get("known") or [])
    return bool(access["new_envs"] and env != "main" and env not in known)


def can(user, site, env, action):
    """Whether the user may do action on one environment of a site"""
    if is_admin(user):
        return True
    access = site_access(user, site)
    return bool(access and action in access["actions"] and sees_env(user, site, env))


def can_sql(user, connection, database, action):
    if is_admin(user):
        return True
    if not user or not user.get("sql"):
        return False
    grant = (user.get("connections") or {}).get(connection)
    if not isinstance(grant, dict):
        return False
    if action == "view":
        return database is None or "*" in grant.get("databases", []) or database in grant.get("databases", [])
    databases = grant.get("databases") or []
    return action in (grant.get("actions") or []) and (database is None or "*" in databases or database in databases)


# ---- what a user creates becomes theirs

def grant_site(user, site, environments=("main",)):
    if is_admin(user):
        return False
    user.setdefault("sites", {})[site] = {"envs": list(environments), "new_envs": True, "actions": sorted(SITE_ACTION_NAMES)}
    return True


def grant_environment(user, site, env):
    if is_admin(user):
        return False
    grant = user.setdefault("sites", {}).setdefault(site, {"envs": [], "new_envs": False, "actions": []})
    if env not in grant.setdefault("envs", []):
        grant["envs"].append(env)
        return True
    return False


def grant_database(user, connection, database):
    if is_admin(user):
        return False
    grant = user.setdefault("connections", {}).setdefault(connection, {"databases": [], "actions": []})
    if "*" not in grant.setdefault("databases", []) and database not in grant["databases"]:
        grant["databases"].append(database)
        return True
    return False


# ---- managing users

def public(user):
    """A user as the admin shows it: never its password hash"""
    return {key: value for key, value in user.items() if key not in ("password_hash", "known")}


def clean(data, existing=None, sites=None, connections=None):
    """A user from what the admin sent, checked; existing keeps what was not sent (its password above all).
    sites: {site: [environment keys]} that exist, connections: ids that exist; grants on others are dropped."""
    existing = existing or {}
    user = {"id": existing.get("id") or "u_" + secrets.token_hex(6), "version": existing.get("version", 1)}
    username = str(data.get("username", existing.get("username", ""))).strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Use letters, numbers and . _ @ - for the username (max 64).")
    user["username"] = username
    user["name"] = clean_name(data.get("name", existing.get("name", "")))
    role = data.get("role", existing.get("role", "user"))
    if role not in ("admin", "user"):
        raise ValueError("The role must be admin or user.")
    user["role"] = role
    user["disabled"] = bool(data.get("disabled", existing.get("disabled", False)))
    user["password_hash"] = existing.get("password_hash", "")
    user["web"] = bool(data.get("web", existing.get("web", False)))
    user["create_sites"] = bool(data.get("create_sites", existing.get("create_sites", False)))
    # SFTP to the sites they have Files on, as their own Linux user (harbor/server/sftp.py)
    user["sftp"] = bool(data.get("sftp", existing.get("sftp", False)))
    user["sql"] = bool(data.get("sql", existing.get("sql", False)))
    user["manage_connections"] = bool(data.get("manage_connections", existing.get("manage_connections", False)))
    granted = data.get("sites", existing.get("sites", {})) or {}
    if not isinstance(granted, dict):
        raise ValueError("Invalid sites.")
    user["sites"] = {}
    for site, grant in granted.items():
        if sites is not None and site not in sites or not isinstance(grant, dict):
            continue
        envs = [env for env in grant.get("envs", []) if isinstance(env, str) and ENV_RE.match(env)
                and (sites is None or env in sites[site])]
        actions = sorted(action for action in grant.get("actions", []) if action in SITE_ACTION_NAMES)
        # A terminal reaches the site's folder as the user's Linux user: it is Files as well
        if "terminal" in actions and "files" not in actions:
            actions = sorted(actions + ["files"])
        if envs:
            entry = {"envs": envs, "new_envs": bool(grant.get("new_envs")), "actions": actions}
            # The environments there were when it was given: later ones count only with new_envs
            entry["known"] = sorted(sites[site]) if sites is not None else list((existing.get("sites", {}).get(site) or {}).get("known", envs))
            user["sites"][site] = entry
    granted = data.get("connections", existing.get("connections", {})) or {}
    if not isinstance(granted, dict):
        raise ValueError("Invalid SQL connections.")
    user["connections"] = {}
    for connection, grant in granted.items():
        if connections is not None and connection not in connections or not isinstance(grant, dict):
            continue
        databases = [name for name in grant.get("databases", []) if isinstance(name, str) and 0 < len(name) <= 128]
        if databases:
            user["connections"][connection] = {"databases": databases,
                                               "actions": sorted(action for action in grant.get("actions", []) if action in SQL_ACTION_NAMES)}
            # A connection the user made stays theirs to change
            if ((existing.get("connections") or {}).get(connection) or {}).get("owned"):
                user["connections"][connection]["owned"] = True
    if role == "admin":
        for key in ("sites", "connections"):
            user[key] = {}
        user["sftp"] = False
    return user


def visible_users(config, viewer):
    """The users a viewer may see and manage: everyone for root, and everyone but root for an admin"""
    if not is_admin(viewer):
        return []
    return [public(user) for user in config.get("users", [])]
