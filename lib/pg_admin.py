"""PostgreSQL admin operations with psql, pg_dump and pg_restore (phase 10 of dev/HARBOR.md): what sql_admin.py does
for Microsoft SQL Server, for a connection whose engine is postgres. sql_admin.py hands such a connection here, so the
admin, its tasks and the usage call one module whatever the engine.

Backups are pg_dump files (custom format) in the connection's backup folder on this server: pg_dump runs here, so a
PostgreSQL elsewhere needs no SSH, and they are listed, restored and deleted here.

A script typed in the admin goes to the server as one request (psql -c), never through psql's own reading of a file: its
backslash commands (\\! runs a shell command, \\o writes a file, \\c logs in elsewhere) would run here, as root. A user who
is not an administrator runs it as a role made for it, a member of the database's owner only.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_PORT = 5432
SYSTEM_DATABASES = {"postgres", "template0", "template1"}
# Between the columns and the rows of a query's output: control characters text practically never holds
COLUMN_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"
# A script typed in the admin goes as one argument: Linux takes 128 KB at most in one
MAX_SCRIPT = 120 * 1024
ROLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.@-]{0,62}")


def tool(name):
    """psql, pg_dump or pg_restore: the newest PostgreSQL's here (a dump needs a pg_dump as new as the server), else the
    one on the PATH"""
    folders = sorted(Path("/usr/lib/postgresql").glob("*/bin"), key=lambda path: [int(part) for part in re.findall(r"\d+", path.parent.name)] or [0],
                     reverse=True) if Path("/usr/lib/postgresql").is_dir() else []
    for folder in folders:
        path = folder / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def tools_path():
    return tool("psql")


def literal(value):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Invalid text value")
    return "'" + value.replace("'", "''") + "'"


def identifier(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 63 or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid name: at most 63 characters, no control characters")
    return '"' + value.replace('"', '""') + '"'


def environment(settings, user=None, password=None):
    return os.environ | {
        "PGPASSWORD": settings["password"] if password is None else password,
        # A connection marked trusted takes TLS where the server offers it; one not marked must have a valid certificate
        "PGSSLMODE": "prefer" if settings.get("trust_certificate", True) else "verify-full",
        "PGCONNECT_TIMEOUT": "15", "PGAPPNAME": "VNS Harbor", "PGCLIENTENCODING": "UTF8",
        "PGUSER": user or settings["username"], "PGHOST": settings["host"], "PGPORT": str(settings.get("port") or DEFAULT_PORT),
    }


def run(argv, settings, *, user=None, password=None, timeout=3600, input=None):
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=environment(settings, user, password),
                            timeout=timeout, input=input)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f"{Path(argv[0]).name} failed").strip())
    return result


def psql():
    found = tool("psql")
    if not found:
        raise ValueError("psql is not installed on the admin server. Install the PostgreSQL tools in Server tools.")
    return found


def execute(settings, sql, database="postgres", timeout=3600):
    """Harbor's own SQL (made from checked names and literals), statement by statement, stopping at the first error"""
    descriptor, filename = tempfile.mkstemp(prefix="vns-pg-", suffix=".sql")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(sql + "\n")
        return run([psql(), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database, "-f", filename], settings, timeout=timeout).stdout
    finally:
        os.unlink(filename)


def query_json(settings, query, database="postgres"):
    output = run([psql(), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", database, "-c",
                  f"SELECT COALESCE(json_agg(q), '[]'::json) FROM ({query}) q"], settings, timeout=60).stdout.strip()
    data = json.loads(output or "[]")
    if not isinstance(data, list):
        raise ValueError("Invalid PostgreSQL response")
    return data


# ---- what the admin shows
def databases(settings):
    """As sql_admin's: name, id (1 to 3 for PostgreSQL's own, so the page keeps them apart as it does SQL Server's),
    state, recovery, size; and its owner"""
    rows = query_json(settings, """
SELECT d.datname AS name, d.datallowconn AS online, pg_get_userbyid(d.datdba) AS owner,
 CASE WHEN has_database_privilege(d.datname, 'CONNECT') THEN round(pg_database_size(d.datname) / 1048576.0, 1) END AS size_mb
FROM pg_database d WHERE d.datname <> 'template0' ORDER BY d.datname""")
    own = sorted(row["name"] for row in rows if row["name"] not in SYSTEM_DATABASES)
    result = []
    for row in rows:
        system_index = ["postgres", "template1"].index(row["name"]) + 1 if row["name"] in ("postgres", "template1") else None
        result.append({"name": row["name"], "id": system_index or 100 + own.index(row["name"]),
                       "state": "ONLINE" if row["online"] else "OFFLINE", "recovery": "", "is_in_standby": False,
                       "size_mb": float(row["size_mb"] or 0), "owner": row["owner"]})
    return result


def tables(settings, database):
    identifier(database)
    return query_json(settings, """
SELECT n.nspname AS schema, c.relname AS name, GREATEST(COALESCE(s.n_live_tup, c.reltuples::bigint), 0) AS rows,
 round(pg_total_relation_size(c.oid) / 1048576.0, 1) AS size_mb
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%'
ORDER BY n.nspname, c.relname""", database=database)


def backup_folder(settings):
    return Path(settings["backup_directory"])


def dump_header(path):
    """The database a dump holds, from pg_restore's list of it"""
    found = subprocess.run([pg_restore(), "-l", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if found.returncode:
        raise ValueError((found.stderr or "Not a pg_dump file").strip())
    match = re.search(r"^;\s+dbname: (.*)$", found.stdout, re.M | re.I)
    if not match:
        raise ValueError("The dump does not say which database it holds")
    return match.group(1).strip()


def backups(settings):
    """The dumps in the connection's backup folder on this server, newest first"""
    folder = backup_folder(settings)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.dump"), key=lambda item: item.stat().st_mtime, reverse=True)[:200]:
        info = path.stat()
        entry = {"id": "file-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest(), "finished": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info.st_mtime)),
                 "size_mb": round(info.st_size / 1048576, 1), "path": str(path), "position": 1, "media_count": 1, "source": "file"}
        try:
            entry["database_name"] = dump_header(path)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            entry.update(database_name=path.stem, media_count=0, error=str(error))
        found.append(entry)
    return found


def local_backup_access(settings):
    return True


def backup_file_access(settings):
    return True


def pg_dump():
    found = tool("pg_dump")
    if not found:
        raise ValueError("pg_dump is not installed on the admin server. Install the PostgreSQL tools in Server tools.")
    return found


def pg_restore():
    found = tool("pg_restore")
    if not found:
        raise ValueError("pg_restore is not installed on the admin server. Install the PostgreSQL tools in Server tools.")
    return found


def backup(settings, name):
    identifier(name)
    if "/" in name or name.startswith(".") or "\\" in name:
        raise ValueError("The database's name cannot be a backup file's name: " + name)
    folder = backup_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / f"{name}.dump"
    part = folder / f".{name}.dump.part"
    try:
        run([pg_dump(), "-Fc", "-d", name, "-f", str(part)], settings)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    print(f"Backup complete: {name}", flush=True)
    return str(path)


def terminate(settings, name):
    execute(settings, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = {literal(name)} AND pid <> pg_backend_pid();")


def restore(settings, name, path, owner=None):
    """A dump made into the database name, which must be there and empty: its objects owned by owner when given"""
    argv = [pg_restore(), "--no-owner", "--no-acl", "--exit-on-error", "-d", name]
    if owner and owner != settings["username"]:
        argv += ["--role", owner]
    run(argv + [str(path)], settings)
    print("Restore complete: " + name, flush=True)


def validate_login(data):
    username = data.get("login")
    if not isinstance(username, str) or not ROLE_NAME.fullmatch(username):
        raise ValueError("A login's name has letters, digits, _ . @ and -, at most 63, and starts with a letter or _")
    if username.lower() in ("postgres", "public") or username.lower().startswith("pg_"):
        raise ValueError("Choose a name other than one of PostgreSQL's own")
    password = data.get("login_password")
    if not isinstance(password, str) or not 6 <= len(password) <= 128 or any(char in password for char in ("\0", "\r", "\n")):
        raise ValueError("The new login's password must be 6-128 characters without line breaks")
    return username, password


def role_exists(settings, name):
    return bool(query_json(settings, f"SELECT 1 AS found FROM pg_roles WHERE rolname = {literal(name)}"))


def create_database(settings, name, owner=None):
    """A database of its own: owned by owner, and only its owner (and superusers) may connect"""
    sql = f"CREATE DATABASE {identifier(name)} ENCODING 'UTF8' TEMPLATE template0" + (f" OWNER {identifier(owner)}" if owner else "") + ";"
    execute(settings, sql)
    execute(settings, f"REVOKE CONNECT, TEMPORARY ON DATABASE {identifier(name)} FROM PUBLIC;"
                      + (f"\nGRANT CONNECT, TEMPORARY ON DATABASE {identifier(name)} TO {identifier(owner)};" if owner else ""))


def create_login(settings, username, password):
    execute(settings, f"CREATE ROLE {identifier(username)} WITH LOGIN PASSWORD {literal(password)};")
    print("Login created: " + username, flush=True)


def own_schema(settings, database, owner):
    """The public schema the owner's, as PostgreSQL 15 makes it (and an older one did not)"""
    execute(settings, f"ALTER SCHEMA public OWNER TO {identifier(owner)};", database=database)


def exclusive_logins(settings, database):
    """The database's owner, when it is a login of its own: not a superuser, not the connection's, owning no other
    database and given no other one"""
    rows = query_json(settings, f"""
SELECT r.rolname AS name FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
WHERE d.datname = {literal(database)} AND NOT r.rolsuper AND r.rolname <> {literal(settings['username'])}
AND NOT EXISTS (SELECT 1 FROM pg_database o WHERE o.datname <> d.datname AND (o.datdba = r.oid
 OR EXISTS (SELECT 1 FROM aclexplode(o.datacl) a WHERE a.grantee = r.oid)))""")
    return [row["name"] for row in rows]


def drop_database(settings, name, logins=()):
    terminate(settings, name)
    execute(settings, f"DROP DATABASE {identifier(name)} WITH (FORCE);")
    for login in logins:
        try:
            execute(settings, f"DROP ROLE {identifier(login)};")
            print("Deleted login: " + login, flush=True)
        except RuntimeError as error:
            print(f"Keeping login {login}: {error}", flush=True)


# ---- a script typed in the admin
def run_query(settings, database, query, sandbox=False):
    """A script typed in the admin, run against one database, as one request to the server. Returns its results as
    sql_admin.parse_results reads them (a header, a line of dashes, the rows), with the server's messages between."""
    if not isinstance(database, str) or not database or len(database) > 63 or any(char in database for char in "\0\r\n"):
        raise ValueError("Choose a database")
    if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > MAX_SCRIPT:
        raise ValueError("Enter a script of at most 120 KB")
    # Never a backslash command: psql runs a whole -c that begins with one as its own command
    script = "-- VNS Harbor\n" + query
    argv = [psql(), "-X", "-A", "-F", COLUMN_SEPARATOR, "-R", RECORD_SEPARATOR, "-P", "footer=on", "-P", "null=NULL",
            "-v", "ON_ERROR_STOP=1", "-d", database, "-c", script]
    if not sandbox:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=environment(settings), timeout=300)
    else:
        result = sandboxed(settings, database, argv)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "The script failed").strip())
    output = readable_output(result.stdout, result.stderr)
    if len(output) > 2 * 1024 * 1024:
        output = output[:2 * 1024 * 1024] + "\n… output cut at 2 MB"
    return output


def sandboxed(settings, database, argv):
    """A script of a user who is not an administrator: run as a role made for it, which logs in only for it, is a
    member of the database's owner and acts as it, and goes afterwards with what it made given to the owner"""
    if database in SYSTEM_DATABASES:
        raise ValueError(f"{database} is one of PostgreSQL's own databases: an administrator queries it.")
    found = query_json(settings, f"SELECT r.rolname AS owner, r.rolsuper AS super FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = {literal(database)}")
    if not found:
        raise ValueError("Unknown database")
    owner = found[0]["owner"]
    if found[0]["super"] or owner == settings["username"]:
        raise ValueError(f"{database} is owned by {owner}, who may do anything on the server: an administrator queries it.")
    role = "harbor_q_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    execute(settings, f"CREATE ROLE {identifier(role)} WITH LOGIN NOINHERIT PASSWORD {literal(password)} VALID UNTIL {literal(time.strftime('%Y-%m-%d %H:%M:%S%z', time.localtime(time.time() + 900)))} IN ROLE {identifier(owner)};\n"
                      f"ALTER ROLE {identifier(role)} SET role = {literal(owner)};\n"
                      f"GRANT CONNECT ON DATABASE {identifier(database)} TO {identifier(role)};")
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=environment(settings, role, password), timeout=300)
    finally:
        try:
            execute(settings, f"REASSIGN OWNED BY {identifier(role)} TO {identifier(owner)};\nDROP OWNED BY {identifier(role)};", database=database)
            execute(settings, f"DROP ROLE {identifier(role)};")
        except RuntimeError as error:
            print(f"The query's role {role} was not removed: {error}", file=sys.stderr, flush=True)


FOOTER = re.compile(r"\((\d+) rows?\)\n?")


def readable_output(stdout, stderr=""):
    """psql's unaligned output (records apart by RECORD_SEPARATOR, each ending in its (n rows)) as sqlcmd's table
    output: header, dashes, rows, an empty line after each; command tags and notices as lines of their own"""
    lines = []
    text = stdout
    position = 0
    while position < len(text):
        start = text.find(RECORD_SEPARATOR, position)
        if start < 0:
            lines.append(text[position:].rstrip("\n"))
            break
        # The header begins after the last line break before it, or where the text before it ended
        newline = text.rfind("\n", position, start)
        header_at = position if newline < 0 else newline + 1
        if header_at > position:
            lines.append(text[position:header_at].rstrip("\n"))
        records, at, done = [], header_at, False
        while at <= len(text):
            end = text.find(RECORD_SEPARATOR, at)
            if end < 0:
                break
            records.append(text[at:end])
            at = end + 1
            footer = FOOTER.match(text, at)
            if footer and int(footer.group(1)) == len(records) - 1:
                position, done = footer.end(), True
                break
        if not done:
            lines.append(text[header_at:].replace(RECORD_SEPARATOR, "\n").rstrip("\n"))
            break
        header = records[0].split(COLUMN_SEPARATOR)
        lines.append(COLUMN_SEPARATOR.join(header))
        lines.append(COLUMN_SEPARATOR.join("-" * max(len(column), 1) for column in header))
        lines.extend(records[1:])
        lines.append("")
    # The server's notices and warnings, without psql's own prefix
    for line in stderr.splitlines():
        line = re.sub(r"^psql:[^:]*:\d+: ", "", line)
        if line.strip():
            lines.append(line)
    return "\n".join(line for line in lines if line is not None) + "\n"


# ---- connecting
def check_login(settings, source="saved"):
    try:
        databases(settings)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(str(connect_error(settings, error, source))) from None


def tried_password(password, source="saved"):
    blanks = " It begins or ends with a blank." if password != password.strip() else ""
    count = len(password)
    return f"It tried the {source} password of {count} character{'' if count == 1 else 's'}.{blanks}"


def connect_error(settings, error, source="saved"):
    """What a failed first connection means, in place of what psql says of it"""
    text = str(error)
    where = f'{settings["host"]}:{settings.get("port") or DEFAULT_PORT}'
    if "password authentication failed" in text or "no password supplied" in text:
        return RuntimeError(f"PostgreSQL on {where} refused the login of {settings['username']}: the password does not match. "
                            f"{tried_password(settings.get('password') or '', source)} Edit the connection and give it the right one.")
    if "no pg_hba.conf entry" in text:
        return RuntimeError(f"PostgreSQL on {where} does not let {settings['username']} in from this server (pg_hba.conf). "
                            "Allow this server's address there, with a password.")
    if any(sign in text for sign in ("Connection refused", "could not connect", "timeout expired", "could not translate host name")):
        return RuntimeError(f"No PostgreSQL answers on {where}. Install PostgreSQL in Server tools, start it, or edit the connection "
                            "to point to a server that runs one.")
    return error


# ---- the admin's actions (a task)
def progress(data):
    if os.environ.get("HARBOR_PROGRESS") == "1":
        print(f"\x1b]7777;harbor;{json.dumps(data)}\x07", end="", flush=True)


def step(text):
    progress({"type": "step", "text": text})
    print(text, flush=True)


ACTION_STEPS = {"backup_all": "Back up every database", "create": "Create {name}", "backup": "Back up {name}", "drop": "Delete {name}",
                "copy": "Copy {name} to {target}", "restore": "Restore {target}", "delete_backup": "Delete the backup file of {name}"}


def selected_backup(settings, backup_id, name=None):
    if not isinstance(backup_id, str) or not re.fullmatch(r"file-[0-9a-f]{64}", backup_id):
        raise ValueError("Select a valid backup")
    found = next((item for item in backups(settings) if item["id"] == backup_id), None)
    if not found or found.get("error") or (name is not None and found["database_name"] != name):
        raise ValueError("Backup not found")
    path = Path(found["path"])
    if path.parent.resolve() != backup_folder(settings).resolve() or path.suffix != ".dump" or path.is_symlink():
        raise ValueError("Only a dump in the connection's backup folder")
    return found


def taken(data, said):
    """Why a name cannot be used, for an administrator; for a user, the same words whatever the reason: that a database
    or login of that name is there (one not theirs) is not theirs to learn"""
    return "That name cannot be used. Choose another." if data.get("private_names") else said


def run_action(settings, data):
    if not isinstance(data, dict):
        raise ValueError("Expected a database action")
    action = data.get("action")
    if action not in ACTION_STEPS:
        raise ValueError("Unknown database action")
    name = data.get("database", "")
    connect = f'Connect to {settings["name"]}'
    work = ACTION_STEPS[action].format(name=name, target=data.get("target", ""))
    progress({"type": "plan", "steps": [connect, work]})
    step(connect)
    try:
        rows = databases(settings)
    except RuntimeError as error:
        found = connect_error(settings, error)
        # A user who did not add the connection is not told where its server is
        if data.get("private_names"):
            found = type(found)(str(found).replace(f'{settings["host"]}:{settings.get("port")}', "the server").replace(str(settings["host"]), "the server"))
        raise found from None
    step(work)
    if action == "backup_all":
        count = 0
        for row in rows:
            if row["name"] not in SYSTEM_DATABASES and row["state"] == "ONLINE":
                backup(settings, row["name"])
                count += 1
        print(f"Backup complete: {count} database(s).", flush=True)
        return
    identifier(name)
    current = next((row for row in rows if row["name"] == name), None)
    if name in SYSTEM_DATABASES:
        raise ValueError("PostgreSQL's own databases cannot be changed here")
    if action == "delete_backup":
        if data.get("confirmation") != name:
            raise ValueError("Confirm the database name and select a backup")
        found = selected_backup(settings, data.get("backup_id"), name)
        Path(found["path"]).unlink()
        print("Deleted backup file: " + found["path"], flush=True)
        return
    if action in ("backup", "drop", "copy") and not current:
        raise ValueError("Database no longer exists")
    if action == "create":
        if current:
            raise ValueError(taken(data, "Database already exists"))
        login = validate_login(data) if data.get("login") is not None else None
        if login and role_exists(settings, login[0]):
            raise ValueError(taken(data, f"There is a login {login[0]} already. Choose another name for the new one."))
        if login:
            create_login(settings, *login)
        create_database(settings, name, login[0] if login else None)
        if login:
            own_schema(settings, name, login[0])
    elif action == "backup":
        backup(settings, name)
    elif action == "drop":
        if data.get("confirmation") != name:
            raise ValueError("Type the exact database name to confirm deletion")
        if not isinstance(data.get("delete_backups", False), bool) or not isinstance(data.get("delete_login", True), bool):
            raise ValueError("Invalid option")
        dumps = [Path(item["path"]) for item in backups(settings) if item["database_name"] == name and not item.get("error")] if data.get("delete_backups") else []
        logins = exclusive_logins(settings, name) if data.get("delete_login", True) else []
        drop_database(settings, name, logins)
        for path in dumps:
            path.unlink(missing_ok=True)
            print("Deleted backup file: " + str(path), flush=True)
    elif action in ("copy", "restore"):
        target = data.get("target", "")
        identifier(target)
        if target in SYSTEM_DATABASES:
            raise ValueError("Cannot overwrite one of PostgreSQL's own databases")
        existing = next((row for row in rows if row["name"] == target), None)
        overwrite = existing is not None
        if overwrite and (action == "copy" or data.get("confirmation") != target or data.get("overwrite") is not True):
            raise ValueError(taken(data, "Destination exists. Explicit overwrite confirmation is required for restore."))
        if action == "copy":
            # A dump of its own, outside the backups, deleted again: none of the database's backups is overwritten or added to
            handle, made = tempfile.mkstemp(prefix="harbor-copy-", suffix=".dump")
            os.close(handle)
            os.chmod(made, 0o600)
            path = Path(made)
            try:
                run([pg_dump(), "-Fc", "-d", name, "-f", str(path)], settings)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        else:
            path = Path(selected_backup(settings, data.get("backup_id"))["path"])
        try:
            if overwrite:
                # The same owner as before, in a database made again
                owner = existing.get("owner")
                drop_database(settings, target)
                create_database(settings, target, owner if owner and owner != settings["username"] else None)
                restore(settings, target, path, owner)
            else:
                login = validate_login(data)
                if role_exists(settings, login[0]):
                    raise ValueError(taken(data, "That login exists already. Choose another username."))
                create_login(settings, *login)
                create_database(settings, target, login[0])
                own_schema(settings, target, login[0])
                restore(settings, target, path, login[0])
        finally:
            if action == "copy":
                path.unlink(missing_ok=True)
    print("OK: " + action, flush=True)
