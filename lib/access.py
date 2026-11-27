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
   "sites": {"<site>": {"envs": ["main", "<environment id>", ...], "new_envs": bool, "actions": [...], "size_mb": int}},
   "sql": bool, "manage_connections": bool, "connection_engines": ["mssql", "postgres"] (which they may add),
   "connections": {"<connection id>": {"databases": ["*"] or [names], "actions": [...], "own": bool,
                                       "limits": {"databases", "database_mb"}, "sizes": {"<database>": mb}}},
   "limits": {"sites", "site_mb"},
   "made": {"sites": [names], "databases": {"<connection id>": [names]}, "logins": {"<connection id>": [names]}}}
"envs" are the environments of the site the user may see ("main" is the main site); "new_envs" adds environments
made later. What a user creates is added to what they may use (see grant_site, grant_environment, grant_database).
"limits" are how many sites a user may make and how big each may be (MB), a connection's the same of its databases,
each left out for none;
"made" is what they made, which the limits count (record_made) and lib/usage.py measures.
"""
import re
import secrets

ROOT = "root"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def clean_email(value):
    """A user's email, for a forgotten password: optional"""
    value = str(value or "").strip()
    if value and (len(value) > 254 or not EMAIL_RE.fullmatch(value)):
        raise ValueError("Enter a valid email address, or none.")
    return value


def mail_of(user):
    """Where a user's mail goes: their email, or their username when it is one"""
    if (user or {}).get("email"):
        return user["email"]
    username = (user or {}).get("username", "")
    return username if EMAIL_RE.fullmatch(username) else ""


def for_reset(config, said):
    """The user a forgotten password is asked for, by username or email (either case): never root, nor a locked user.
    Root's password is set on the server (harbor), never by a link in a mail"""
    said = str(said or "").strip().lower()
    if not said or len(said) > 254:
        return None
    for user in config.get("users", []):
        if user.get("disabled"):
            continue
        if user.get("username", "").lower() == said or (user.get("email") or "").lower() == said:
            return user
    return None
ENV_RE = re.compile(r"^(main|[a-z0-9][a-z0-9-]{0,62})$")

# What a user may be given on a site, in the order the admin shows them
SITE_ACTIONS = [
    # Running it, and its logs
    ("service", "Start, stop and restart"),
    ("logs", "Logs"),
    ("clear_logs", "Clear logs"),
    # Publishing and syncing
    ("publish", "Publish"),
    ("sync_data", "Sync data"),
    ("sync_settings", "Sync settings"),
    ("sync_setup", "Change Sync setup"),
    # Its files and what runs in them
    ("scripts", "Run custom scripts"),
    # Each gives the ones before it: Terminal gives Files, Files Edit appsettings.json and the packages (it can edit
    # the .csproj), and changing them is seeing them. Files gives Delete too
    ("appsettings", "Edit appsettings.json"),
    ("packages_view", "See NuGet packages"),
    ("packages", "Change NuGet packages"),
    ("files", "Files"),
    ("terminal", "Terminal"),
    # Its setup
    ("bindings", "Change bindings"),
    ("certificate", "Issue certificate"),
    ("environment", "Change environment"),
    ("create_env", "Add environments"),
    ("delete", "Delete"),
]
SQL_ACTIONS = [
    # Working in a database
    ("query", "Run queries"),
    ("tables", "Tables"),
    ("backup", "Back up"),
    ("restore", "Restore"),
    # Making and removing databases, and their logins
    ("create", "Create databases"),
    ("copy", "Copy databases"),
    ("delete", "Delete databases"),
    ("logins", "Logins"),
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
    return {"envs": set(grant.get("envs") or []), "new_envs": bool(grant.get("new_envs")), "actions": set(site_actions(grant.get("actions") or []))}


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


def clean_limits(data, keys=("sites", "site_mb")):
    """Limits: each a whole number, or left out for no limit. A user's are how many sites they may make and how big
    each may be (MB); a SQL connection's, how many databases there and how big each."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid limits.")
    limits = {}
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError("A limit is a whole number, or empty for none.")
        top = 10000 if key in ("sites", "databases") else 100 * 1024 * 1024
        if not 0 <= int(value) <= top:
            raise ValueError(f"A limit is between 0 and {top}.")
        limits[key] = int(value)
    return limits


def made_count(user, kind, exists=None, connection=None):
    """How many sites a user made, or databases on a connection, that are still there (exists(name) says)"""
    made = (user or {}).get("made") or {}
    names = made.get("sites", []) if kind == "sites" else (made.get("databases") or {}).get(connection, [])
    return sum(1 for name in names if exists is None or exists(name))


def limits_of(user, connection=None):
    """A user's limits: their own for sites, or a SQL connection's for its databases"""
    if connection is None:
        return (user or {}).get("limits") or {}
    return (((user or {}).get("connections") or {}).get(connection) or {}).get("limits") or {}


def may_make(user, kind, exists=None, connection=None):
    """None when the user may make another site, or database on the connection, else why not"""
    if is_admin(user):
        return None
    limit = limits_of(user, None if kind == "sites" else connection).get(kind)
    if limit is None:
        return None
    count = made_count(user, kind, exists, connection)
    if count < limit:
        return None
    noun = "site" if kind == "sites" else "database"
    return f"You have made {count} {noun}{'' if count == 1 else 's'} of the {limit} you may make. Delete one, or ask an administrator for more."


def record_made(user, kind, name, connection=None):
    """A site or database the user just made, for their limits"""
    if is_admin(user):
        return False
    made = user.setdefault("made", {})
    if kind == "sites":
        names = made.setdefault("sites", [])
    else:
        # databases and logins: by connection
        names = made.setdefault(kind, {}).setdefault(connection, [])
    if name in names:
        return False
    names.append(name)
    return True


def forget_made(user, kind, name, connection=None):
    """A site or database of theirs that is gone"""
    made = (user or {}).get("made") or {}
    names = made.get("sites", []) if kind == "sites" else (made.get(kind) or {}).get(connection, [])
    if name in names:
        names.remove(name)
        return True
    return False


# A user template: rules under a name to start new users from. Which sites and SQL connections a user gets is chosen
# for each user; the template says what they may do on each one they are given (1.0.77). A name, password, lock or
# what they made is never in it.
TEMPLATE_KEYS = ("web", "create_sites", "sftp", "sql", "manage_connections", "connection_engines", "limits", "site_rules", "sql_rules", "dns", "cloudflare_parts", "servers")
# The SQL Servers a connection can be to: Microsoft SQL Server and PostgreSQL
ENGINES = ("mssql", "postgres")


def clean_engines(given):
    """The engines given, checked; every one when none were ever given (a user or template saved before 1.0.82)"""
    if not isinstance(given, list):
        return list(ENGINES)
    return [engine for engine in ENGINES if engine in given]


def has_web(user):
    """Whether the Web Server is a user's: on, and a site given (or the right to make one). On with nothing in it is
    no access: it is not shown."""
    if is_admin(user):
        return True
    return bool(user and user.get("web") and ((user.get("sites") or {}) or user.get("create_sites")))


DOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")


CLOUDFLARE_PARTS = ("dns", "analytics", "cache", "speed", "rules", "ssl", "security")


def cloudflare_part(user, part):
    """Whether the user may use a part of Cloudflare (on the domains given them): every part for an administrator"""
    if is_admin(user):
        return True
    return has_dns(user) and part in (user.get("cloudflare_parts") or ["dns"])


def domain_name(name):
    """Whether a name is a domain, also one with æ, ø or å (checked as its punycode, ledernøn.dk as xn--…)"""
    try:
        ascii_name = name.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return False
    return bool(DOMAIN_RE.fullmatch(ascii_name.lower()))


def has_dns(user):
    """Whether DNS is a user's: on, with domains given. An administrator keeps every zone of the account"""
    if is_admin(user):
        return True
    return bool(user and user.get("dns") and user.get("dns_domains"))


def dns_zone(user, name):
    """Whether the user may keep a zone (a domain) of the Cloudflare account"""
    if is_admin(user):
        return True
    return has_dns(user) and str(name or "").lower() in (user.get("dns_domains") or [])


def has_sql(user):
    """Whether SQL Server is a user's: on, and a connection given (or the right to add one to a SQL Server). On with
    nothing in it is no access: it is not shown."""
    if is_admin(user):
        return True
    return bool(user and user.get("sql") and ((user.get("connections") or {}) or connection_engines(user)))


def connection_engines(user):
    """The engines a user may add connections to: every one for an administrator, none without May add SQL connections"""
    if is_admin(user):
        return list(ENGINES)
    if not user or not user.get("sql") or not user.get("manage_connections"):
        return []
    return clean_engines(user.get("connection_engines"))
# What a site or SQL connection gives when nothing else says (as the user window ticks them)
DEFAULT_SITE_ACTIONS = ["clear_logs", "logs", "publish", "service", "sync_data", "sync_settings"]
DEFAULT_SQL_ACTIONS = ["backup", "query"]


def site_actions(given):
    actions = {action for action in given if action in SITE_ACTION_NAMES}
    # A terminal reaches the site's folder as the user's Linux user: it is Files as well
    if "terminal" in actions:
        actions.add("files")
    # Files can change and delete anything in the folder: the .csproj (and with it the NuGet packages), appsettings.json,
    # the site itself. It is Change NuGet packages, Edit appsettings.json and Delete as well
    if "files" in actions:
        actions.update(("packages", "appsettings", "delete"))
    # Changing the NuGet packages is seeing them as well
    if "packages" in actions:
        actions.add("packages_view")
    return sorted(actions)


def split_bindings(config):
    """Before 1.0.82 one action, bindings, gave Change bindings, Issue certificate and Change environment: those who
    had it keep all three, once (the config then says so). True when the config changed."""
    return split_actions(config, "bindings_split", lambda actions: "bindings" in actions, ("certificate", "environment"))


def split_packages(config):
    """Before 1.0.82 everyone who saw a main site saw its NuGet packages: they keep See NuGet packages on each site
    given with its main site, once. True when the config changed."""
    return split_actions(config, "packages_split", lambda actions: True, ("packages_view",), main_only=True)


def split_actions(config, flag, when, added, main_only=False):
    """Adds actions to the site grants (and templates' rules) that had what one action gave before it was split, once"""
    if config.get(flag):
        return False
    def widen(actions, envs=None):
        if isinstance(actions, list) and when(actions) and (not main_only or envs is None or "main" in envs):
            actions.extend(action for action in added if action not in actions)
            actions.sort()
    for user in config.get("users") or []:
        for grant in ((user.get("sites") or {}) if isinstance(user, dict) else {}).values():
            if isinstance(grant, dict):
                widen(grant.get("actions"), grant.get("envs") or [])
    for template in config.get("user_templates") or []:
        rules = (template.get("access") or {}) if isinstance(template, dict) else {}
        widen((rules.get("site_rules") or {}).get("actions"))
        for grant in (rules.get("sites") or {}).values() if isinstance(rules.get("sites"), dict) else []:
            if isinstance(grant, dict):
                widen(grant.get("actions"))
    config[flag] = True
    return True


def template_rules(given):
    """A template's rules, checked. One saved before 1.0.77 kept the sites and connections of a user: what they gave on
    them (every action on any of them) becomes the rules."""
    given = given if isinstance(given, dict) else {}
    site = given.get("site_rules")
    sql = given.get("sql_rules")
    if not isinstance(site, dict):
        grants = [grant for grant in (given.get("sites") or {}).values() if isinstance(grant, dict)] if isinstance(given.get("sites"), dict) else []
        site = {"actions": [action for grant in grants for action in grant.get("actions") or []] if grants else DEFAULT_SITE_ACTIONS,
                "new_envs": all(grant.get("new_envs") for grant in grants) if grants else True}
    if not isinstance(sql, dict):
        grants = [grant for grant in (given.get("connections") or {}).values() if isinstance(grant, dict)] if isinstance(given.get("connections"), dict) else []
        sql = {"actions": [action for grant in grants for action in grant.get("actions") or []] if grants else DEFAULT_SQL_ACTIONS,
               "own": bool(grants) and all(grant.get("own") for grant in grants),
               "limits": next((grant["limits"] for grant in grants if isinstance(grant.get("limits"), dict)), None)}
    rules = {key: bool(given.get(key, key == "web")) for key in ("web", "create_sites", "sftp", "sql", "manage_connections", "dns")}
    parts = given.get("cloudflare_parts")
    rules["cloudflare_parts"] = [part for part in CLOUDFLARE_PARTS if isinstance(parts, list) and part in parts] or ["dns"]
    rules["connection_engines"] = clean_engines(given.get("connection_engines"))
    rules["limits"] = clean_limits(given.get("limits"))
    rules["site_rules"] = {"actions": site_actions(site.get("actions") or []), "new_envs": bool(site.get("new_envs", True))}
    sql_rules = {"own": sql.get("own") is True, "actions": sorted({action for action in sql.get("actions") or [] if action in SQL_ACTION_NAMES})}
    # How many databases they may make on a connection, and how big each: with Create or Copy databases
    limits = clean_limits(sql.get("limits"), ("databases", "database_mb"))
    if limits and {"create", "copy"} & set(sql_rules["actions"]):
        sql_rules["limits"] = limits
    rules["sql_rules"] = sql_rules
    # On a master: what a user made from it has on each member (the parts and limits; their sites there are given in
    # their own window, each by the rules above)
    servers = {}
    for name, part in given["servers"].items() if isinstance(given.get("servers"), dict) else []:
        if not SERVER_NAME.fullmatch(str(name)) or not isinstance(part, dict):
            continue
        kept = {key: bool(part.get(key)) for key in ("web", "create_sites", "create_envs", "sftp", "sql", "manage_connections")}
        if not (kept["web"] or kept["sql"] or kept["create_envs"]):
            continue
        kept["connection_engines"] = clean_engines(part.get("connection_engines"))
        kept["limits"] = clean_limits(part.get("limits"))
        servers[name] = kept
    if servers:
        rules["servers"] = servers
    return rules


def clean_template(data, existing=None):
    """A template from what the template window sent, checked: {id, name, access}"""
    if not isinstance(data, dict):
        raise ValueError("Expected a template.")
    name = " ".join(str(data.get("name", "")).split())
    if not name or len(name) > 60:
        raise ValueError("Give the template a name of at most 60 characters.")
    return {"id": (existing or {}).get("id") or "t_" + secrets.token_hex(6), "name": name, "access": template_rules(data.get("access"))}


def templates(config):
    """The templates as the admin shows them, those saved before 1.0.77 as rules"""
    return [clean_template(template, existing=template) for template in config.get("user_templates", []) if isinstance(template, dict) and template.get("name")]


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
    user["email"] = clean_email(data.get("email", existing.get("email", "")))
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
    # Which SQL Servers they may add connections to (with May add SQL connections)
    user["connection_engines"] = clean_engines(data.get("connection_engines", existing.get("connection_engines")))
    granted = data.get("sites", existing.get("sites", {})) or {}
    if not isinstance(granted, dict):
        raise ValueError("Invalid sites.")
    user["sites"] = {}
    for site, grant in granted.items():
        if sites is not None and site not in sites or not isinstance(grant, dict):
            continue
        envs = [env for env in grant.get("envs", []) if isinstance(env, str) and ENV_RE.match(env)
                and (sites is None or env in sites[site])]
        actions = site_actions(grant.get("actions", []))
        if envs:
            entry = {"envs": envs, "new_envs": bool(grant.get("new_envs")), "actions": actions}
            # A site can have its own size limit: one they made in place of the one their limits give every site, one
            # they were given (an administrator's) as theirs in Usage while they have it
            size = clean_limits({"site_mb": grant.get("size_mb")}, ("site_mb",)).get("site_mb")
            if size is not None:
                entry["size_mb"] = size
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
        # Only their own: the databases they make become theirs, and none else; given before they have made any (1.0.70)
        own = grant.get("own") is True and "*" not in databases
        if databases or own:
            user["connections"][connection] = {"databases": databases,
                                               "actions": sorted(action for action in grant.get("actions", []) if action in SQL_ACTION_NAMES)}
            if own:
                user["connections"][connection]["own"] = True
            # How many databases they may make there, and how big each: with Create or Copy databases
            limits = clean_limits(grant.get("limits"), ("databases", "database_mb"))
            if limits and {"create", "copy"} & set(user["connections"][connection]["actions"]):
                user["connections"][connection]["limits"] = limits
            # A database they made there can have its own size, in place of the one for every database there
            sizes = grant.get("sizes") or {}
            if not isinstance(sizes, dict):
                raise ValueError("Invalid database sizes.")
            sizes = {name: clean_limits({"database_mb": size}, ("database_mb",)).get("database_mb") for name, size in sizes.items()
                     if isinstance(name, str) and 0 < len(name) <= 128}
            sizes = {name: size for name, size in sizes.items() if size is not None}
            if sizes:
                user["connections"][connection]["sizes"] = sizes
            # A connection the user made stays theirs to change
            if ((existing.get("connections") or {}).get(connection) or {}).get("owned"):
                user["connections"][connection]["owned"] = True
    # DNS (phase 14): the domains in the Cloudflare account they may keep, by name
    user["dns"] = bool(data.get("dns", existing.get("dns", False)))
    domains = data.get("dns_domains", existing.get("dns_domains", [])) or []
    if not isinstance(domains, list):
        raise ValueError("Invalid DNS domains.")
    user["dns_domains"] = sorted({str(name).strip().lower().rstrip(".") for name in domains
                                  if isinstance(name, str) and domain_name(str(name).strip().lower().rstrip("."))})
    if not user["dns_domains"]:
        user["dns"] = False
    # Which parts of Cloudflare they may use on those domains (DNS only for one given before there was a choice)
    parts = data.get("cloudflare_parts", existing.get("cloudflare_parts", ["dns"]))
    user["cloudflare_parts"] = [part for part in CLOUDFLARE_PARTS if isinstance(parts, list) and part in parts] or ["dns"]
    # A part with nothing in it is turned off as it is saved: SQL Server without a connection or a SQL Server they may add
    # one to, the Web Server without a site or the right to make one. On with nothing in it would be no access.
    if user["sql"] and not user["connections"] and not (user["manage_connections"] and user["connection_engines"]):
        user["sql"] = False
    # Web Server with nothing in it is no access, unless their environments are hosted here (a master's user)
    if user["web"] and not user["sites"] and not user["create_sites"] and not data.get("create_envs", existing.get("create_envs")):
        user["web"] = False
    user["limits"] = clean_limits(data.get("limits", existing.get("limits")))
    # What they made is kept by the admin as they make it, never sent by a form
    user["made"] = existing.get("made") or {}
    # On a master: what they may on each member, as it would be there; that member checks it against its own sites
    user["servers"] = clean_servers(data.get("servers", existing.get("servers")))
    # Environments made on a member are for the sites given them that let them add environments (Add environments): May
    # create sites is another thing. None on any server: no server makes environments for them
    if not adds_environments(user):
        for access in user["servers"].values():
            access.pop("create_envs", None)
        user["servers"] = {name: access for name, access in user["servers"].items() if access.get("web") or access.get("sql")}
    # On a member: the other members (by name in Servers) the master lets them make sites on, for an environment there
    given_on = data.get("create_on", existing.get("create_on"))
    user["create_on"] = [str(name) for name in (given_on if isinstance(given_on, list) else []) if SERVER_NAME.fullmatch(str(name))][:50]
    # A master's user on a member: their environments may be made here (create_envs), and on these other members
    user["create_envs"] = bool(data.get("create_envs", existing.get("create_envs", False)))
    given_envs = data.get("envs_on", existing.get("envs_on"))
    user["envs_on"] = [str(name) for name in (given_envs if isinstance(given_envs, list) else []) if SERVER_NAME.fullmatch(str(name))][:50]
    if role == "admin":
        for key in ("sites", "connections"):
            user[key] = {}
        user["sftp"] = False
        user["limits"] = {}
        user["servers"] = {}
    return user


# What of a user's access a master keeps for each member: the parts that are a server's own (not Cloudflare's)
SERVER_PARTS = ("web", "create_sites", "create_envs", "sftp", "sql", "manage_connections", "connection_engines", "sites", "connections", "limits")
SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def adds_environments(user):
    """Whether a user may add environments anywhere: a site given them that lets them (Add environments), here or on a
    member. May create sites does not count: making sites and making environments are each their own"""
    grants = [user.get("sites") or {}] + [(access or {}).get("sites") or {} for access in (user.get("servers") or {}).values()]
    return any("create_env" in (grant.get("actions") or []) for sites in grants for grant in sites.values() if isinstance(grant, dict))


def clean_servers(value):
    """A user's access on each member of a master: {member: {web, sites, sql, connections, …}}, only what has some"""
    import json
    found = {}
    for name, access in (value or {}).items() if isinstance(value, dict) else []:
        if not SERVER_NAME.fullmatch(str(name)) or not isinstance(access, dict):
            continue
        part = {key: access[key] for key in SERVER_PARTS if key in access}
        # Web Server with nothing on it there (no site, nor sites or environments to make) is no access there, nor
        # SQL Server with no connection nor connections to add: turned off, so the server is not said to be theirs
        if not (part.get("sites") or part.get("create_sites") or part.get("create_envs")):
            part["web"] = False
        if not (part.get("connections") or part.get("manage_connections")):
            part["sql"] = False
        if not (part.get("web") or part.get("sql") or part.get("create_envs")):
            continue
        if len(json.dumps(part)) > 200000:
            raise ValueError(f"The access on {name} is too large.")
        found[name] = part
    return found


def visible_users(config, viewer):
    """The users a viewer may see and manage: everyone for root, and everyone but root for an admin"""
    if not is_admin(viewer):
        return []
    return [public(user) for user in config.get("users", [])]
