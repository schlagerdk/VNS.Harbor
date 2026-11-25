"""Tables and logins of a SQL Server, as SQL Server Management Studio has them: a table's columns designed (its types,
lengths, NULL, defaults, identity and primary key) and the SQL that makes them, shown before it is run; and the SQL
logins with the databases they reach and their roles there.

Each change is made as SQL first (plan_*), which the admin shows and then runs (apply_*). The SQL is made from checked
names and types only: a name is always a bracketed identifier, a value a literal, a default one of a few kinds.
"""
import importlib.util
from pathlib import Path
import re

# sql_admin as the admin loaded it (use()), or loaded from beside this file
sql = None


def use(module):
    global sql
    sql = module


DEFAULT_SCHEMA = "dbo"
_pg_design = None


def for_connection(settings):
    """pg_design.py for a PostgreSQL connection, which designs its tables and logins; None for Microsoft SQL Server, whose
    are this module's (the admin loads it under a name of its own, so it is not looked up here)"""
    global _pg_design
    if not (isinstance(settings, dict) and settings.get("engine") == "postgres"):
        return None
    if _pg_design is None:
        spec = importlib.util.spec_from_file_location("vns_pg_design", Path(__file__).with_name("pg_design.py"))
        _pg_design = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pg_design)
    return _pg_design


def helper():
    global sql
    if sql is None:
        spec = importlib.util.spec_from_file_location("vns_sql_admin_for_design", Path(__file__).with_name("sql_admin.py"))
        sql = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sql)
    return sql


def identifier(value):
    return helper().identifier(value)


def literal(value):
    return helper().literal(value)

# ---- types: what each takes (nothing, a length, or a precision and scale)
LENGTH_TYPES = {"char": 8000, "varchar": 8000, "nchar": 4000, "nvarchar": 4000, "binary": 8000, "varbinary": 8000}
MAX_TYPES = {"varchar", "nvarchar", "varbinary"}
DECIMAL_TYPES = {"decimal", "numeric"}
SCALE_TYPES = {"datetime2", "time", "datetimeoffset"}
PLAIN_TYPES = {"bit", "tinyint", "smallint", "int", "bigint", "float", "real", "money", "smallmoney", "date", "datetime",
               "smalldatetime", "uniqueidentifier", "xml", "sql_variant", "image", "text", "ntext", "timestamp", "rowversion",
               "hierarchyid", "geography", "geometry"}
# The types offered when a column is made, the most used first
OFFERED = ["int", "bigint", "nvarchar", "varchar", "bit", "datetime2", "date", "decimal", "uniqueidentifier", "nchar", "char",
           "smallint", "tinyint", "float", "real", "money", "datetime", "datetimeoffset", "time", "varbinary", "binary", "xml",
           "numeric", "smallmoney", "smalldatetime"]
# Defaults that are SQL Server's own functions, not a value
FUNCTIONS = ["GETDATE()", "GETUTCDATE()", "SYSDATETIME()", "SYSUTCDATETIME()", "SYSDATETIMEOFFSET()", "NEWID()", "NEWSEQUENTIALID()",
             "CURRENT_TIMESTAMP", "SUSER_SNAME()"]
ROLES = ["db_owner", "db_datareader", "db_datawriter", "db_ddladmin", "db_backupoperator"]
NAME = re.compile(r"[^\[\]\x00-\x1f]{1,128}")


def name(value, what="name"):
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value.strip()


def type_sql(column):
    """A column's type as SQL, checked: nvarchar(100), decimal(18,2), datetime2(7), int"""
    kind = str(column.get("type", "")).lower()
    if kind in LENGTH_TYPES:
        length = column.get("length")
        if length in ("max", "MAX", -1):
            if kind not in MAX_TYPES:
                raise ValueError(f"{kind} has no max length")
            return f"{kind}(max)"
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= LENGTH_TYPES[kind]:
            raise ValueError(f"{column.get('name')}: the length of {kind} is 1 to {LENGTH_TYPES[kind]}" + (" or max" if kind in MAX_TYPES else ""))
        return f"{kind}({length})"
    if kind in DECIMAL_TYPES:
        precision, scale = column.get("precision", 18), column.get("scale", 0)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in (precision, scale)) or not 1 <= precision <= 38 or not 0 <= scale <= precision:
            raise ValueError(f"{column.get('name')}: {kind} takes a precision of 1 to 38 and a scale from 0 to the precision")
        return f"{kind}({precision},{scale})"
    if kind in SCALE_TYPES:
        scale = column.get("scale", 7)
        if isinstance(scale, bool) or not isinstance(scale, int) or not 0 <= scale <= 7:
            raise ValueError(f"{column.get('name')}: {kind} takes a scale of 0 to 7")
        return f"{kind}({scale})"
    if kind in PLAIN_TYPES:
        return kind
    raise ValueError(f"{column.get('name')}: not a type SQL Server has: {column.get('type')!r}")


def default_sql(value):
    """A default as SQL: one of SQL Server's functions, a number, NULL, or else a text value (quoted)"""
    value = "" if value is None else str(value).strip()
    if not value:
        return ""
    if value.upper() in {item.upper() for item in FUNCTIONS} or re.fullmatch(r"-?\d+(\.\d+)?", value) or value.upper() == "NULL":
        return value.upper() if not re.fullmatch(r"-?\d+(\.\d+)?", value) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1].replace("''", "'")
    return literal(value)


def default_value(definition):
    """A default as SQL Server keeps it ((getdate()), ((0)), (N'abc')) as it is shown and typed: GETDATE(), 0, abc"""
    text = (definition or "").strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    match = re.fullmatch(r"N?'(.*)'", text, re.S)
    if match:
        return match.group(1).replace("''", "'")
    return text.upper() if text.upper() in {item.upper() for item in FUNCTIONS} else text


def column_sql(column, table):
    parts = [identifier(name(column["name"], "column name")), type_sql(column)]
    if column.get("identity"):
        if str(column.get("type", "")).lower() not in {"int", "bigint", "smallint", "tinyint", "decimal", "numeric"}:
            raise ValueError(f"{column['name']}: only a whole number can be an identity")
        parts.append("IDENTITY(1,1)")
    parts.append("NULL" if column.get("nullable") and not column.get("identity") and not column.get("primary") else "NOT NULL")
    default = default_sql(column.get("default"))
    if default and not column.get("identity"):
        parts.append(f"CONSTRAINT {identifier('DF_' + table + '_' + column['name'])} DEFAULT {default}")
    return " ".join(parts)


def full_name(schema, table):
    return f"{identifier(name(schema or 'dbo', 'schema'))}.{identifier(name(table, 'table name'))}"


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
    lines = [column_sql(column, table) for column in columns]
    primary = [column["name"] for column in columns if column.get("primary")]
    if primary:
        lines.append(f"CONSTRAINT {identifier('PK_' + table)} PRIMARY KEY ({', '.join(identifier(item) for item in primary)})")
    return f"CREATE TABLE {full_name(schema, table)} (\n    " + ",\n    ".join(lines) + "\n);"


def schemas(settings, database):
    """The schemas a table can be made in: dbo first, then the database's own (not sys, guest or INFORMATION_SCHEMA, nor
    the roles' schemas)"""
    identifier(database)
    rows = helper().query_json(settings, """
SELECT name FROM sys.schemas
WHERE schema_id < 16384 AND name NOT IN (N'sys', N'guest', N'INFORMATION_SCHEMA')
ORDER BY CASE name WHEN N'dbo' THEN 0 ELSE 1 END, name""", database=database)
    return [row["name"] for row in rows] or ["dbo"]


def read_table(settings, database, schema, table):
    """The columns of a table as they are, with what the designer needs of each, and its primary key's name"""
    identifier(database)
    target = literal(f"{identifier(name(schema or 'dbo', 'schema'))}.{identifier(name(table, 'table name'))}")
    columns = helper().query_json(settings, f"""
SELECT c.column_id AS id, c.name, ty.name AS [type], c.max_length, c.precision, c.scale, c.is_nullable AS nullable,
 c.is_identity AS [identity], dc.definition AS [default], dc.name AS default_name,
 CAST(CASE WHEN ic.column_id IS NULL THEN 0 ELSE 1 END AS bit) AS [primary]
FROM sys.columns c JOIN sys.types ty ON ty.user_type_id=c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id=c.default_object_id
LEFT JOIN sys.indexes pk ON pk.object_id=c.object_id AND pk.is_primary_key=1
LEFT JOIN sys.index_columns ic ON ic.object_id=pk.object_id AND ic.index_id=pk.index_id AND ic.column_id=c.column_id
WHERE c.object_id=OBJECT_ID({target}) ORDER BY c.column_id""", database=database)
    if not columns:
        raise ValueError(f"No table {schema}.{table} in {database}")
    keys = helper().query_json(settings, f"SELECT name FROM sys.key_constraints WHERE type='PK' AND parent_object_id=OBJECT_ID({target})", database=database)
    for column in columns:
        kind = column["type"]
        if kind in LENGTH_TYPES:
            size = column["max_length"]
            column["length"] = "max" if size == -1 else size // 2 if kind in ("nchar", "nvarchar") else size
        if kind in SCALE_TYPES:
            column["precision"] = None
        column["default"] = default_value(column.get("default"))
        for key in ("nullable", "identity", "primary"):
            column[key] = bool(column.get(key))
    return {"schema": schema or "dbo", "name": table, "columns": columns, "primary_key": keys[0]["name"] if keys else ""}


def plan_alter(current, columns):
    """The SQL that makes a table's columns as they are given: each one with the id it had keeps its place (renamed,
    retyped, its NULL or default changed), one without is added, one left out is dropped, and the primary key follows"""
    check_columns(columns)
    schema, table = current["schema"], current["name"]
    target = full_name(schema, table)
    before = {column["id"]: column for column in current["columns"]}
    after = {column["id"]: column for column in columns if column.get("id") in before}
    added = [column for column in columns if column.get("id") not in before]
    dropped = [column for key, column in before.items() if key not in after]
    old_key = [column["name"] for column in current["columns"] if column["primary"]]
    new_key = [column["name"] for column in columns if column.get("primary")]
    renamed_key = [after[column["id"]]["name"] if column["id"] in after else None for column in current["columns"] if column["primary"]]
    key_changes = renamed_key != new_key or any(key is None for key in renamed_key)
    first, middle, last = [], [], []
    if key_changes and current.get("primary_key"):
        first.append(f"ALTER TABLE {target} DROP CONSTRAINT {identifier(current['primary_key'])};")
    for column in dropped:
        if column.get("default_name"):
            first.append(f"ALTER TABLE {target} DROP CONSTRAINT {identifier(column['default_name'])};")
        middle.append(f"ALTER TABLE {target} DROP COLUMN {identifier(column['name'])};")
    for key, column in after.items():
        old = before[key]
        new_name = name(column["name"], "column name")
        if bool(column.get("identity")) != old["identity"]:
            raise ValueError(f"{old['name']}: identity can only be chosen when a column is added")
        if new_name != old["name"]:
            middle.append(f"EXEC sp_rename {literal(schema + '.' + table + '.' + old['name'])}, {literal(new_name)}, 'COLUMN';")
        old_type = type_sql(old)
        new_type = type_sql(column)
        nullable = bool(column.get("nullable")) and not column.get("primary")
        if new_type != old_type or nullable != old["nullable"]:
            middle.append(f"ALTER TABLE {target} ALTER COLUMN {identifier(new_name)} {new_type} {'NULL' if nullable else 'NOT NULL'};")
        new_default = default_sql(column.get("default"))
        if new_default != default_sql(old.get("default")):
            if old.get("default_name"):
                first.append(f"ALTER TABLE {target} DROP CONSTRAINT {identifier(old['default_name'])};")
            if new_default:
                last.append(f"ALTER TABLE {target} ADD CONSTRAINT {identifier('DF_' + table + '_' + new_name)} DEFAULT {new_default} FOR {identifier(new_name)};")
    for column in added:
        middle.append(f"ALTER TABLE {target} ADD {column_sql(column, table)};")
    if key_changes and new_key:
        last.append(f"ALTER TABLE {target} ADD CONSTRAINT {identifier('PK_' + table)} PRIMARY KEY ({', '.join(identifier(item) for item in new_key)});")
    steps = first + middle + last
    if not steps:
        return ""
    # All of it, or none of it
    return "SET XACT_ABORT ON;\nBEGIN TRANSACTION;\n" + "\n".join(steps) + "\nCOMMIT;"


def plan_rename(schema, table, new_name):
    old = name(schema or "dbo", "schema") + "." + name(table, "table name")
    return f"EXEC sp_rename {literal(old)}, {literal(name(new_name, 'table name'))};"


def plan_drop(schema, table):
    return f"DROP TABLE {full_name(schema, table)};"


# ---- logins
PROTECTED = {"sa"}


def logins(settings):
    """The SQL logins (not SQL Server's own), and the databases each reaches with its roles there"""
    found = helper().query_json(settings, """
SELECT name, is_disabled AS disabled, default_database_name AS default_database, create_date AS created
FROM sys.sql_logins WHERE name NOT LIKE '##%' ORDER BY name""")
    online = [row["name"] for row in helper().databases(settings) if row["id"] > 4 and row["state"] == "ONLINE"]
    sysadmins = {row["name"] for row in helper().query_json(settings, """
SELECT l.name FROM sys.server_role_members m JOIN sys.server_principals r ON r.principal_id=m.role_principal_id
JOIN sys.server_principals l ON l.principal_id=m.member_principal_id WHERE r.name=N'sysadmin'""")}
    access, unlinked = {}, {}
    for database in online:
        db = identifier(database)
        # A database's SQL users with their roles: the login each belongs to, or none (a user left without its login, as
        # after a database came from another server: a login of its name made since is another login)
        for row in helper().query_json(settings, f"""
SELECT dp.name AS [user], sp.name AS login, (SELECT STRING_AGG(r.name, ',') FROM {db}.sys.database_role_members m
 JOIN {db}.sys.database_principals r ON r.principal_id=m.role_principal_id WHERE m.member_principal_id=dp.principal_id) AS roles
FROM {db}.sys.database_principals dp LEFT JOIN sys.server_principals sp ON sp.sid=dp.sid
WHERE dp.type='S' AND (sp.name IS NOT NULL OR (dp.principal_id > 4 AND dp.authentication_type=1))"""):
            item = {"database": database, "roles": [role for role in (row.get("roles") or "").split(",") if role]}
            if row.get("login"):
                access.setdefault(row["login"], []).append(item)
            else:
                unlinked.setdefault(row["user"], []).append(item)
    for login in found:
        login["disabled"] = bool(login["disabled"])
        login["databases"] = access.get(login["name"], [])
        login["sysadmin"] = login["name"] in sysadmins
        # Users of its name in databases that are not its own: they can be given to it
        login["unlinked"] = unlinked.get(login["name"], [])
        login["protected"] = login["name"] in PROTECTED or login["name"].lower() == settings["username"].lower()
    return {"logins": found, "databases": online, "roles": ROLES}


def check_login_name(value, settings):
    value = name(value, "login name")
    if value.lower() in PROTECTED or value.lower() == settings["username"].lower():
        raise ValueError(f"{value} is the connection's own login, or SQL Server's: change it elsewhere")
    return value


PASSWORD_RULE = "A password has at least 6 characters"


def check_password(value):
    """At least 6 characters: the logins made here are not held to SQL Server's policy (CHECK_POLICY=OFF), which asks
    for 8 and three kinds of characters"""
    if not isinstance(value, str) or len(value) < 6 or len(value) > 128 or "\n" in value:
        raise ValueError(PASSWORD_RULE)
    return value


def grants_sql(login, grants, current=None):
    """Each database's user and roles as given: added where it has none, roles added and taken away, and its user
    dropped where it is given no database any more"""
    current = {item["database"]: set(item["roles"]) for item in (current or [])}
    wanted = {}
    for grant in grants or []:
        database = name(grant.get("database", ""), "database")
        roles = set(grant.get("roles") or [])
        if roles - set(ROLES):
            raise ValueError(f"Not a role offered here: {', '.join(sorted(roles - set(ROLES)))}")
        wanted[database] = roles
    user = identifier(login)
    steps = []
    for database, roles in wanted.items():
        db = identifier(database)
        steps.append(f"USE {db};")
        if database not in current:
            steps.append(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name={literal(login)}) CREATE USER {user} FOR LOGIN {user};")
        for role in sorted(roles - current.get(database, set())):
            steps.append(f"ALTER ROLE {identifier(role)} ADD MEMBER {user};")
        for role in sorted(current.get(database, set()) - roles):
            if role in ROLES:
                steps.append(f"ALTER ROLE {identifier(role)} DROP MEMBER {user};")
    for database in current:
        if database not in wanted:
            steps += [f"USE {identifier(database)};", f"DROP USER IF EXISTS {user};"]
    return steps


def plan_login(action, data, settings, current=None):
    """The SQL of a change to a login. Its password is in it as a literal: it is shown masked, and run, never kept."""
    login = check_login_name(data.get("name", ""), settings)
    if action == "create":
        default = name(data.get("default_database") or "master", "database")
        steps = [f"CREATE LOGIN {identifier(login)} WITH PASSWORD={literal(check_password(data.get('password')))}, "
                 f"DEFAULT_DATABASE={identifier(default)}, CHECK_POLICY=OFF, CHECK_EXPIRATION=OFF;"]
        steps += grants_sql(login, data.get("databases"))
    elif action == "password":
        steps = [f"ALTER LOGIN {identifier(login)} WITH PASSWORD={literal(check_password(data.get('password')))}, CHECK_POLICY=OFF;"]
    elif action in ("enable", "disable"):
        steps = [f"ALTER LOGIN {identifier(login)} {action.upper()};"]
    elif action == "access":
        steps = grants_sql(login, data.get("databases"), (current or {}).get("databases"))
        default = data.get("default_database")
        if default:
            steps = [f"ALTER LOGIN {identifier(login)} WITH DEFAULT_DATABASE={identifier(name(default, 'database'))};"] + steps
    elif action == "link":
        # Each user of its name that has lost its login is given to it, with the roles it has
        steps = []
        for item in (current or {}).get("unlinked", []):
            steps += [f"USE {identifier(item['database'])};", f"ALTER USER {identifier(login)} WITH LOGIN = {identifier(login)};"]
        if not steps:
            raise ValueError("No user of its name waits for it")
        steps.append("USE [master];")
    elif action == "unsysadmin":
        if not (current or {}).get("sysadmin"):
            raise ValueError("It is not a sysadmin")
        steps = [f"ALTER SERVER ROLE [sysadmin] DROP MEMBER {identifier(login)};"]
    elif action == "delete":
        steps = []
        for item in (current or {}).get("databases", []):
            steps += [f"USE {identifier(item['database'])};", f"DROP USER IF EXISTS {identifier(login)};"]
        steps += ["USE [master];", f"DROP LOGIN {identifier(login)};"]
    else:
        raise ValueError("Unknown login action")
    return "\n".join(steps)


def masked(sql, password):
    return sql.replace(literal(password), "N'********'") if password else sql


def apply(settings, sql_text, database="master"):
    """Run it. A login's CREATE LOGIN runs on its own first: when SQL Server refuses it, nothing after it runs."""
    if not sql_text.strip():
        return
    first, _, rest = sql_text.partition("\n")
    if first.startswith("CREATE LOGIN ") and rest.strip():
        helper().execute(settings, first, database=database, timeout=600)
        sql_text = rest
    helper().execute(settings, sql_text, database=database, timeout=600)


def readable(error):
    """What SQL Server said, without its Msg 33064, Level 16, State 2, Server … lines"""
    lines = [re.sub(r"^psql:[^:]*:\d+: ", "", line) for line in str(error).splitlines()
             if line.strip() and not re.match(r"Msg \d+, Level \d+", line.strip())]
    return "\n".join(lines) or str(error)
