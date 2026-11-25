"""Tables and logins of a PostgreSQL server (phase 10 of dev/HARBOR.md), as sql_design.py has them for Microsoft SQL
Server: a table's columns designed (PostgreSQL's types, lengths, NULL, defaults, identity and primary key) and the SQL
that makes them, shown before it is run; and the logins (roles that may log in) with the databases they reach and what
they may do there: owner, read, write.

Each change is made as SQL first (plan_*), which the admin shows and then runs (apply). The SQL is made from checked
names and types only. PostgreSQL cannot change database within a script: SQL for several databases is in parts, each
headed by a line "-- In database <name>", which apply() runs in that database.
"""
import importlib.util
from pathlib import Path
import re

pg = None


def helper():
    global pg
    if pg is None:
        spec = importlib.util.spec_from_file_location("vns_pg_admin_for_design", Path(__file__).with_name("pg_admin.py"))
        pg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pg)
    return pg


def identifier(value):
    return helper().identifier(value)


def literal(value):
    return helper().literal(value)


DEFAULT_SCHEMA = "public"
SYSTEM_DATABASES = {"postgres", "template0", "template1"}
# ---- types: what each takes (nothing, a length, or a precision and scale)
LENGTH_TYPES = {"varchar": 10485760, "char": 10485760, "bit": 83886080, "varbit": 83886080}
MAX_TYPES = set()
DECIMAL_TYPES = {"numeric", "decimal"}
SCALE_TYPES = {"timestamp", "timestamptz", "time", "timetz", "interval"}
PLAIN_TYPES = {"integer", "bigint", "smallint", "boolean", "text", "real", "double precision", "date", "uuid", "json", "jsonb",
               "bytea", "money", "inet", "cidr", "macaddr", "xml", "tsvector", "point"}
# The types offered when a column is made, the most used first
OFFERED = ["integer", "bigint", "text", "varchar", "boolean", "timestamptz", "date", "numeric", "uuid", "jsonb", "smallint",
           "char", "real", "double precision", "timestamp", "time", "interval", "json", "bytea", "money", "inet", "xml"]
# PostgreSQL's names for the types as the catalog gives them (format_type), to the names offered
ALIASES = {"character varying": "varchar", "character": "char", "timestamp without time zone": "timestamp",
           "timestamp with time zone": "timestamptz", "time without time zone": "time", "time with time zone": "timetz",
           "bit varying": "varbit", "int": "integer", "int4": "integer", "int8": "bigint", "int2": "smallint", "bool": "boolean",
           "float8": "double precision", "float4": "real", "decimal": "numeric"}
# Defaults that are functions, not a value
FUNCTIONS = ["now()", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "LOCALTIMESTAMP", "gen_random_uuid()", "CURRENT_USER"]
# What a login may do in a database: own it, read its tables, write in them
ROLES = ["owner", "read", "write"]
NAME = re.compile(r'[^\x00-\x1f]{1,63}')
INTEGERS = {"integer", "bigint", "smallint"}


def name(value, what="name"):
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()) or len(value.strip().encode("utf-8")) > 63:
        raise ValueError(f"Invalid {what}: {value!r}")
    return value.strip()


def type_sql(column):
    """A column's type as SQL, checked: varchar(100), numeric(18,2), timestamptz(6), integer"""
    kind = ALIASES.get(str(column.get("type", "")).lower(), str(column.get("type", "")).lower())
    if kind in LENGTH_TYPES:
        length = column.get("length")
        if length in (None, "", "max", "MAX", -1) and kind in ("varchar", "varbit"):
            return kind
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= LENGTH_TYPES[kind]:
            raise ValueError(f"{column.get('name')}: the length of {kind} is 1 to {LENGTH_TYPES[kind]}")
        return f"{kind}({length})"
    if kind in DECIMAL_TYPES:
        precision, scale = column.get("precision"), column.get("scale", 0)
        if precision in (None, ""):
            return "numeric"
        if any(isinstance(item, bool) or not isinstance(item, int) for item in (precision, scale)) or not 1 <= precision <= 1000 or not 0 <= scale <= precision:
            raise ValueError(f"{column.get('name')}: numeric takes a precision of 1 to 1000 and a scale from 0 to the precision")
        return f"numeric({precision},{scale})"
    if kind in SCALE_TYPES:
        scale = column.get("scale", 6)
        if scale in (None, ""):
            return kind
        if isinstance(scale, bool) or not isinstance(scale, int) or not 0 <= scale <= 6:
            raise ValueError(f"{column.get('name')}: {kind} takes a precision of 0 to 6")
        return f"{kind}({scale})"
    if kind in PLAIN_TYPES:
        return kind
    raise ValueError(f"{column.get('name')}: not a type offered here: {column.get('type')!r}")


def default_sql(value):
    """A default as SQL: one of the functions, a number, true or false, NULL, or else a text value (quoted)"""
    value = "" if value is None else str(value).strip()
    if not value:
        return ""
    functions = {item.lower(): item for item in FUNCTIONS}
    if value.lower() in functions:
        return functions[value.lower()]
    if re.fullmatch(r"-?\d+(\.\d+)?", value) or value.upper() in ("NULL", "TRUE", "FALSE"):
        return value if re.fullmatch(r"-?\d+(\.\d+)?", value) else value.upper()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1].replace("''", "'")
    return literal(value)


def default_value(definition):
    """A default as PostgreSQL keeps it ('abc'::character varying, now(), 0) as it is shown and typed: abc, now(), 0"""
    text = (definition or "").strip()
    match = re.fullmatch(r"'(.*)'(::[\w\s\".\[\]()]+)?", text, re.S)
    if match:
        return match.group(1).replace("''", "'")
    functions = {item.lower(): item for item in FUNCTIONS}
    return functions.get(text.lower(), text)


def column_sql(column):
    parts = [identifier(name(column["name"], "column name")), type_sql(column)]
    kind = ALIASES.get(str(column.get("type", "")).lower(), str(column.get("type", "")).lower())
    if column.get("identity"):
        if kind not in INTEGERS:
            raise ValueError(f"{column['name']}: only a whole number can be an identity")
        parts.append("GENERATED BY DEFAULT AS IDENTITY")
    parts.append("NULL" if column.get("nullable") and not column.get("identity") and not column.get("primary") else "NOT NULL")
    default = default_sql(column.get("default"))
    if default and not column.get("identity"):
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


def full_name(schema, table):
    return f"{identifier(name(schema or DEFAULT_SCHEMA, 'schema'))}.{identifier(name(table, 'table name'))}"


def check_columns(columns):
    if not isinstance(columns, list) or not columns:
        raise ValueError("A table needs a column")
    seen = set()
    for column in columns:
        key = name(column.get("name", ""), "column name").lower()
        if key in seen:
            raise ValueError(f"Two columns are called {column['name']}")
        seen.add(key)
    if sum(1 for column in columns if column.get("identity")) > 1:
        raise ValueError("A table has one identity column at most")


# ---- tables
def plan_create(schema, table, columns):
    check_columns(columns)
    lines = [column_sql(column) for column in columns]
    primary = [column["name"] for column in columns if column.get("primary")]
    if primary:
        lines.append(f"CONSTRAINT {identifier('pk_' + table)} PRIMARY KEY ({', '.join(identifier(item) for item in primary)})")
    return f"CREATE TABLE {full_name(schema, table)} (\n    " + ",\n    ".join(lines) + "\n);"


def schemas(settings, database):
    """The schemas a table can be made in: public first, then the database's own"""
    identifier(database)
    rows = helper().query_json(settings, """
SELECT nspname AS name FROM pg_namespace
WHERE nspname NOT IN ('information_schema') AND nspname NOT LIKE 'pg\\_%'
ORDER BY CASE nspname WHEN 'public' THEN 0 ELSE 1 END, nspname""", database=database)
    return [row["name"] for row in rows] or [DEFAULT_SCHEMA]


def read_table(settings, database, schema, table):
    """The columns of a table as they are, with what the designer needs of each, and its primary key's name"""
    identifier(database)
    target = literal(full_name(schema, table))
    columns = helper().query_json(settings, f"""
SELECT a.attnum AS id, a.attname AS name, format_type(a.atttypid, NULL) AS base, format_type(a.atttypid, a.atttypmod) AS full_type,
 NOT a.attnotnull AS nullable, a.attidentity <> '' AS identity, pg_get_expr(d.adbin, d.adrelid) AS "default",
 COALESCE(a.attnum = ANY(i.indkey), false) AS "primary"
FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND i.indisprimary
WHERE a.attrelid = {target}::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", database=database)
    if not columns:
        raise ValueError(f"No table {schema}.{table} in {database}")
    keys = helper().query_json(settings, f"SELECT conname AS name FROM pg_constraint WHERE contype = 'p' AND conrelid = {target}::regclass", database=database)
    for column in columns:
        kind = ALIASES.get(column.pop("base"), None) or column["full_type"].split("(")[0]
        kind = ALIASES.get(kind, kind)
        numbers = [int(part) for part in re.findall(r"\d+", column["full_type"].split("(", 1)[1])] if "(" in column["full_type"] else []
        column["type"] = kind
        if kind in LENGTH_TYPES:
            column["length"] = numbers[0] if numbers else None
        if kind in DECIMAL_TYPES:
            column["precision"], column["scale"] = (numbers + [None, 0])[:2] if numbers else (None, 0)
        if kind in SCALE_TYPES:
            column["scale"] = numbers[0] if numbers else None
        column.pop("full_type")
        column["default"] = "" if column.get("identity") else default_value(column.get("default"))
        column["default_name"] = ""
        for key in ("nullable", "identity", "primary"):
            column[key] = bool(column.get(key))
    return {"schema": schema or DEFAULT_SCHEMA, "name": table, "columns": columns, "primary_key": keys[0]["name"] if keys else ""}


def plan_alter(current, columns):
    """The SQL that makes a table's columns as they are given: each one with the id it had keeps its place (renamed,
    retyped, its NULL or default changed), one without is added, one left out is dropped, and the primary key follows.
    In one transaction: PostgreSQL changes a table's columns all or not at all."""
    check_columns(columns)
    target = full_name(current["schema"], current["name"])
    before = {column["id"]: column for column in current["columns"]}
    after = {column["id"]: column for column in columns if column.get("id") in before}
    added = [column for column in columns if column.get("id") not in before]
    dropped = [column for key, column in before.items() if key not in after]
    new_key = [column["name"] for column in columns if column.get("primary")]
    renamed_key = [after[column["id"]]["name"] if column["id"] in after else None for column in current["columns"] if column["primary"]]
    key_changes = renamed_key != new_key or any(key is None for key in renamed_key)
    first, middle, last = [], [], []
    if key_changes and current.get("primary_key"):
        first.append(f"ALTER TABLE {target} DROP CONSTRAINT {identifier(current['primary_key'])};")
    for column in dropped:
        middle.append(f"ALTER TABLE {target} DROP COLUMN {identifier(column['name'])};")
    for key, column in after.items():
        old = before[key]
        new_name = name(column["name"], "column name")
        if bool(column.get("identity")) != old["identity"]:
            raise ValueError(f"{old['name']}: identity can only be chosen when a column is added")
        if new_name != old["name"]:
            middle.append(f"ALTER TABLE {target} RENAME COLUMN {identifier(old['name'])} TO {identifier(new_name)};")
        old_type, new_type = type_sql(old), type_sql(column)
        if new_type != old_type:
            middle.append(f"ALTER TABLE {target} ALTER COLUMN {identifier(new_name)} TYPE {new_type} USING {identifier(new_name)}::{new_type};")
        nullable = bool(column.get("nullable")) and not column.get("primary")
        if nullable != old["nullable"]:
            middle.append(f"ALTER TABLE {target} ALTER COLUMN {identifier(new_name)} {'DROP' if nullable else 'SET'} NOT NULL;")
        new_default = default_sql(column.get("default"))
        if new_default != default_sql(old.get("default")) and not old["identity"]:
            last.append(f"ALTER TABLE {target} ALTER COLUMN {identifier(new_name)} " + (f"SET DEFAULT {new_default};" if new_default else "DROP DEFAULT;"))
    for column in added:
        middle.append(f"ALTER TABLE {target} ADD COLUMN {column_sql(column)};")
    if key_changes and new_key:
        last.append(f"ALTER TABLE {target} ADD CONSTRAINT {identifier('pk_' + current['name'])} PRIMARY KEY ({', '.join(identifier(item) for item in new_key)});")
    steps = first + middle + last
    if not steps:
        return ""
    return "BEGIN;\n" + "\n".join(steps) + "\nCOMMIT;"


def plan_rename(schema, table, new_name):
    return f"ALTER TABLE {full_name(schema, table)} RENAME TO {identifier(name(new_name, 'table name'))};"


def plan_drop(schema, table):
    return f"DROP TABLE {full_name(schema, table)};"


# ---- logins
PROTECTED = {"postgres"}


def logins(settings):
    """The roles that may log in (or did: one locked has a date in the past), not PostgreSQL's own, and the databases
    each reaches with what it may do there"""
    found = helper().query_json(settings, """
SELECT r.rolname AS name, COALESCE(r.rolvaliduntil < now(), false) AS disabled, r.rolsuper AS sysadmin
FROM pg_roles r WHERE (r.rolcanlogin OR r.rolvaliduntil < now()) AND r.rolname NOT LIKE 'pg\\_%' AND r.rolname NOT LIKE 'harbor\\_q\\_%'
ORDER BY r.rolname""")
    online = [row["name"] for row in helper().databases(settings) if row["name"] not in SYSTEM_DATABASES and row["state"] == "ONLINE"]
    access = {}
    for database in online:
        # Owner, or given CONNECT; and whether it reads and writes every table there (or is given that for those made later)
        for row in helper().query_json(settings, f"""
SELECT r.rolname AS login, d.datdba = r.oid AS owner,
 (SELECT bool_and(has_table_privilege(r.oid, c.oid, 'SELECT')) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p','v') AND n.nspname NOT IN ('pg_catalog','information_schema')) AS reads,
 (SELECT bool_and(has_table_privilege(r.oid, c.oid, 'INSERT,UPDATE,DELETE')) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')) AS writes,
 EXISTS (SELECT 1 FROM pg_default_acl a, aclexplode(a.defaclacl) e WHERE e.grantee = r.oid AND e.privilege_type = 'SELECT') AS reads_later,
 EXISTS (SELECT 1 FROM pg_default_acl a, aclexplode(a.defaclacl) e WHERE e.grantee = r.oid AND e.privilege_type = 'INSERT') AS writes_later
FROM pg_roles r, pg_database d
WHERE d.datname = current_database() AND (r.rolcanlogin OR r.rolvaliduntil < now()) AND NOT r.rolsuper
 AND (d.datdba = r.oid OR EXISTS (SELECT 1 FROM aclexplode(d.datacl) e WHERE e.grantee = r.oid AND e.privilege_type = 'CONNECT'))""", database=database):
            roles = [role for role, given in (("owner", row["owner"]), ("read", row["reads_later"] or row["reads"] is True and not row["owner"]),
                                              ("write", row["writes_later"] or row["writes"] is True and not row["owner"])) if given]
            access.setdefault(row["login"], []).append({"database": database, "roles": roles})
    for login in found:
        login["disabled"] = bool(login["disabled"])
        login["sysadmin"] = bool(login["sysadmin"])
        login["databases"] = access.get(login["name"], [])
        login["unlinked"] = []
        login["protected"] = login["name"] in PROTECTED or login["name"].lower() == settings["username"].lower()
    return {"logins": found, "databases": online, "roles": ROLES}


def check_login_name(value, settings):
    value = name(value, "login name")
    if not helper().ROLE_NAME.fullmatch(value):
        raise ValueError("A login's name has letters, digits, _ . @ and -, at most 63, and starts with a letter or _")
    if value.lower() in PROTECTED or value.lower() == settings["username"].lower() or value.lower().startswith("pg_"):
        raise ValueError(f"{value} is the connection's own login, or PostgreSQL's: change it elsewhere")
    return value


PASSWORD_RULE = "A password has at least 6 characters"


def check_password(value):
    if not isinstance(value, str) or len(value) < 6 or len(value) > 128 or "\n" in value:
        raise ValueError(PASSWORD_RULE)
    return value


def in_database(database):
    return f"-- In database {name(database, 'database')}"


def grant_steps(login, database, roles, granting, settings):
    """What a login may do in one database: owner (the database is its), read or write every table there and those
    made later by its owner"""
    user = identifier(login)
    steps = []
    db = identifier(database)
    owner_after = settings["username"]
    for role in sorted(roles):
        if role == "owner":
            steps.append(f"ALTER DATABASE {db} OWNER TO {user if granting else identifier(owner_after)};")
    inside = []
    if "read" in roles:
        if granting:
            inside += [f"GRANT USAGE ON SCHEMA public TO {user};", f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {user};",
                       f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {user};"]
        else:
            inside += [f"REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM {user};",
                       f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {user};"]
    if "write" in roles:
        if granting:
            inside += [f"GRANT USAGE ON SCHEMA public TO {user};", f"GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user};",
                       f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {user};",
                       f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT INSERT, UPDATE, DELETE ON TABLES TO {user};",
                       f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {user};"]
        else:
            inside += [f"REVOKE INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {user};",
                       f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE INSERT, UPDATE, DELETE ON TABLES FROM {user};"]
    return steps, inside


def grants_sql(login, grants, settings, current=None):
    """Each database's grants as given: CONNECT and what it may do added, taken away, and all of it taken where it is
    given no database any more"""
    current = {item["database"]: set(item["roles"]) for item in (current or [])}
    wanted = {}
    for grant in grants or []:
        database = name(grant.get("database", ""), "database")
        roles = set(grant.get("roles") or [])
        if roles - set(ROLES):
            raise ValueError(f"Not a role offered here: {', '.join(sorted(roles - set(ROLES)))}")
        wanted[database] = roles
    user = identifier(login)
    server, parts = [], {}
    for database, roles in wanted.items():
        db = identifier(database)
        if database not in current:
            server.append(f"GRANT CONNECT ON DATABASE {db} TO {user};")
        added, inside_added = grant_steps(login, database, roles - current.get(database, set()), True, settings)
        removed, inside_removed = grant_steps(login, database, current.get(database, set()) - roles, False, settings)
        server += added + removed
        if inside_added or inside_removed:
            parts[database] = inside_added + inside_removed
    for database, roles in current.items():
        if database not in wanted:
            removed, inside = grant_steps(login, database, roles, False, settings)
            server += removed + [f"REVOKE CONNECT ON DATABASE {identifier(database)} FROM {user};"]
            parts[database] = inside + [f"REVOKE USAGE ON SCHEMA public FROM {user};"]
    steps = server
    for database, inside in parts.items():
        steps += [in_database(database), *inside]
    return steps


def plan_login(action, data, settings, current=None):
    """The SQL of a change to a login. Its password is in it as a literal: it is shown masked, and run, never kept."""
    login = check_login_name(data.get("name", ""), settings)
    user = identifier(login)
    if action == "create":
        steps = [f"CREATE ROLE {user} WITH LOGIN PASSWORD {literal(check_password(data.get('password')))};"]
        steps += grants_sql(login, data.get("databases"), settings)
    elif action == "password":
        steps = [f"ALTER ROLE {user} WITH PASSWORD {literal(check_password(data.get('password')))};"]
    elif action == "disable":
        # Locked: its password no longer lets it in, and it keeps what it was given
        steps = [f"ALTER ROLE {user} VALID UNTIL '-infinity';"]
    elif action == "enable":
        steps = [f"ALTER ROLE {user} WITH LOGIN VALID UNTIL 'infinity';"]
    elif action == "access":
        steps = grants_sql(login, data.get("databases"), settings, (current or {}).get("databases"))
    elif action == "unsysadmin":
        if not (current or {}).get("sysadmin"):
            raise ValueError("It is not a superuser")
        steps = [f"ALTER ROLE {user} WITH NOSUPERUSER;"]
    elif action == "delete":
        steps = []
        for item in (current or {}).get("databases", []):
            steps += [in_database(item["database"]), f"REASSIGN OWNED BY {user} TO {identifier(settings['username'])};", f"DROP OWNED BY {user};"]
        steps += [in_database("postgres"), f"DROP ROLE {user};"]
    elif action == "link":
        raise ValueError("A PostgreSQL login belongs to the whole server: there is nothing to link")
    else:
        raise ValueError("Unknown login action")
    return "\n".join(steps)


def masked(sql, password):
    return sql.replace(literal(password), "'********'") if password else sql


def apply(settings, sql_text, database="postgres"):
    """Run it: each part in its database (the part before the first heading in the one given). A CREATE ROLE runs on its
    own first: when PostgreSQL refuses it, nothing after it runs."""
    if not sql_text.strip():
        return
    parts, current, lines = [], database, []
    for line in sql_text.split("\n"):
        match = re.fullmatch(r"-- In database (.+)", line)
        if match:
            if any(item.strip() for item in lines):
                parts.append((current, "\n".join(lines)))
            current, lines = match.group(1), []
        else:
            lines.append(line)
    if any(item.strip() for item in lines):
        parts.append((current, "\n".join(lines)))
    for target, text in parts:
        first, _, rest = text.strip().partition("\n")
        if first.startswith("CREATE ROLE ") and rest.strip():
            helper().execute(settings, first, database=target, timeout=600)
            text = rest
        helper().execute(settings, text, database=target, timeout=600)


def readable(error):
    """What PostgreSQL said, without psql's own prefix"""
    lines = [re.sub(r"^psql:[^:]*:\d+: ", "", line) for line in str(error).splitlines() if line.strip()]
    return "\n".join(lines) or str(error)
