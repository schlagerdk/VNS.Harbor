"""SQL Server admin operations using sqlcmd and server-side backup paths. A connection whose engine is postgres is
handed to pg_admin.py, which does the same for PostgreSQL: the admin, its tasks and the usage call this module alone."""
import argparse
import hashlib
import importlib.util
import shlex
import socket
import stat
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

_file_spec = importlib.util.spec_from_file_location("vns_backup_files", Path(__file__).with_name("backup_files.py"))
_file_module = importlib.util.module_from_spec(_file_spec)
_file_spec.loader.exec_module(_file_module)


ENGINES = ("mssql", "postgres")
DEFAULT_PORTS = {"mssql": 1433, "postgres": 5432}
_pg = None


def pg():
    """pg_admin.py, beside this file"""
    global _pg
    if _pg is None:
        spec = importlib.util.spec_from_file_location("vns_pg_admin", Path(__file__).with_name("pg_admin.py"))
        _pg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pg)
    return _pg


def engine(settings):
    """A connection's engine: postgres, or mssql (every connection made before PostgreSQL came)"""
    return "postgres" if isinstance(settings, dict) and settings.get("engine") == "postgres" else "mssql"


def postgres(settings):
    return engine(settings) == "postgres"


def tools_path(kind="mssql"):
    """The engine's command-line tool on the admin server, or None"""
    return pg().tools_path() if kind == "postgres" else sqlcmd_path()


def ssh_settings(data, existing=None):
    enabled = data.get("ssh_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Invalid SSH setting")
    if not enabled:
        return {"ssh_enabled": False}
    host = data.get("ssh_host", "")
    user = data.get("ssh_username", "")
    if not isinstance(host, str) or not isinstance(user, str):
        raise ValueError("Invalid SSH host or username")
    host, user = host.strip(), user.strip()
    port = data.get("ssh_port", 22)
    password = data.get("ssh_password", "")
    if existing and password == "":
        password = existing.get("ssh_password", "")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("Invalid SSH host")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        raise ValueError("Invalid SSH username")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    if not isinstance(password, str) or len(password) > 4096 or "\0" in password:
        raise ValueError("Invalid SSH password")
    return dict(ssh_enabled=True, ssh_host=host, ssh_username=user, ssh_port=port, ssh_password=password)


def connections(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key]
                                         for key in ("id", "name", "host", "username", "password", "backup_directory"))
        or isinstance(item.get("port"), bool) or not isinstance(item.get("port"), int)
        or not 1 <= item["port"] <= 65535 or not isinstance(item.get("trust_certificate"), bool)
        or item.get("engine", "mssql") not in ENGINES
        for item in data
    ):
        raise ValueError("Invalid SQL connection store")
    for item in data:
        ssh_settings(item)
    return data


def public_connections(path):
    return [{key: item[key] for key in (
        "id", "name", "host", "port", "username", "backup_directory", "trust_certificate",
        "ssh_enabled", "ssh_host", "ssh_username", "ssh_port"
    ) if key in item} | {"engine": engine(item)} for item in connections(path)]


def save_connections(path, items):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".sql-connections-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(items, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_connection(path, data, check=None):
    return edit_connection(path, data, check=check)


def edit_connection(path, data, connection_id=None, check=None):
    """A connection added, or changed. check(fields), when given, tries it before it is saved: a ValueError keeps it
    from being saved."""
    items = connections(path)
    existing = next((item for item in items if item["id"] == connection_id), None)
    if connection_id is not None and existing is None:
        raise ValueError("Unknown SQL connection")
    if existing and data.get("password", "") == "":
        data = data | {"password": existing["password"]}
    # The engine is chosen when the connection is made; it stays what it is
    kind = engine(existing) if existing else data.get("engine", "mssql")
    if kind not in ENGINES:
        raise ValueError("Choose Microsoft SQL Server or PostgreSQL")
    fields = {}
    for key in ("name", "host", "username", "password", "backup_directory"):
        value = data.get(key)
        if key == "password" and value == "":
            raise ValueError("Type the password of the SQL login.")
        if not isinstance(value, str) or not value or "\0" in value or len(value) > 1024:
            raise ValueError("Invalid connection field: " + key)
        fields[key] = value if key == "password" else value.strip()
        if not fields[key]:
            raise ValueError("Missing connection field: " + key)
    if not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", fields["host"]):
        raise ValueError("Enter a hostname or IP address without scheme or port")
    port = data.get("port", DEFAULT_PORTS[kind])
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid SQL port")
    trust = data.get("trust_certificate", False)
    if not isinstance(trust, bool):
        raise ValueError("Invalid certificate setting")
    fields.update(id=connection_id or uuid.uuid4().hex, port=port, trust_certificate=trust)
    if kind == "postgres":
        # pg_dump runs here: its files are in a folder on this server, and SSH is never needed
        fields.update(engine="postgres", ssh_enabled=False)
        if not fields["backup_directory"].startswith("/") or ".." in fields["backup_directory"].split("/"):
            raise ValueError("The backup folder is a full path on this server, such as /var/opt/postgresql/backup")
    else:
        fields.update(ssh_settings(data | {"ssh_host": fields["host"] if fields["host"] != "." else "localhost"}, existing))
    if fields["ssh_enabled"] and (not fields["backup_directory"].startswith("/") or "\\" in fields["backup_directory"]):
        raise ValueError("Linux SSH backup access requires an absolute Linux backup directory")
    host = fields["host"].strip("[]").rstrip(".").casefold()
    if any(item["id"] != connection_id and item["host"].strip("[]").rstrip(".").casefold() == host and item["port"] == port
           and engine(item) == kind for item in items):
        raise ValueError("A connection to this SQL host and port already exists")
    if check:
        check(fields)
    if existing:
        items = [fields if item["id"] == connection_id else item for item in items]
    else:
        items.append(fields)
    save_connections(path, items)
    return fields["id"]


def check_login(settings, source="saved"):
    """Whether the connection logs in: a ValueError that says why, in plain words, when it does not"""
    if postgres(settings):
        return pg().check_login(settings, source)
    try:
        databases(settings)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(str(connect_error(settings, error, source))) from None


def remove_connection(path, identifier):
    items = connections(path)
    if not any(item["id"] == identifier for item in items):
        raise ValueError("Unknown SQL connection")
    save_connections(path, [item for item in items if item["id"] != identifier])


def connection(path, identifier):
    for item in connections(path):
        if item["id"] == identifier:
            return item
    raise ValueError("Unknown SQL connection")


def literal(value):
    return "N'" + value.replace("'", "''") + "'"


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid database name")
    return "[" + value.replace("]", "]]") + "]"


def sqlcmd_path():
    return next((str(path) for path in (Path("/opt/mssql-tools18/bin/sqlcmd"), Path("/opt/mssql-tools/bin/sqlcmd"))
                 if path.is_file() and os.access(path, os.X_OK)), None) or shutil.which("sqlcmd")


# Between the columns of a query result: a control character that SQL text practically never holds, unlike |
COLUMN_SEPARATOR = "\x1f"


def execute(settings, query, table=False, timeout=3600, database="master", separator="|"):
    tool = sqlcmd_path()
    if not tool:
        raise ValueError("sqlcmd is not installed on the admin server. Install the Microsoft SQL command-line tools.")
    descriptor, filename = tempfile.mkstemp(prefix="vns-sql-", suffix=".sql")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("SET NOCOUNT ON;\n" + query + "\n")
        args = [tool, "-S", f'tcp:{settings["host"]},{settings["port"]}', "-U", settings["username"],
                "-d", database, "-b", "-x", "-r", "0", "-l", "15", "-t", "0", "-w", "65535", "-f", "65001", "-i", filename]
        args += ["-W", "-s", separator] if table else ["-y", "0"]
        if table and separator == COLUMN_SEPARATOR:
            args.append("-k1")  # line breaks in values become spaces, so each row is one line
        if settings["trust_certificate"]:
            args.append("-C")
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                env=os.environ | {"SQLCMDPASSWORD": settings["password"]}, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or "SQL command failed").strip())
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr, end="", flush=True)
        return result.stdout
    finally:
        os.unlink(filename)


# A line that ends a batch, as sqlcmd reads it: GO, maybe a count, maybe a comment after it
GO_LINE = re.compile(r"[ \t]*go(?:[ \t]+(\d+))?[ \t]*(?:--.*)?", re.IGNORECASE)
SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}


def script_batches(script):
    """The batches of a script split at its GO lines, each with how many times it runs"""
    found, lines = [], []
    for line in script.replace("\r\n", "\n").split("\n"):
        match = GO_LINE.fullmatch(line)
        if match:
            found.append(("\n".join(lines), min(int(match.group(1) or 1), 1000)))
            lines = []
        else:
            lines.append(line)
    found.append(("\n".join(lines), 1))
    return [(batch, count) for batch, count in found if batch.strip()]


def sealed_script(script, sandbox=False):
    """A typed script as sqlcmd gets it: each batch as hex, run with sp_executesql, so sqlcmd never reads a line of it.
    sqlcmd's own commands in it (!! ran a shell command as the admin's user, :r read a file, :out wrote one) are then
    only text SQL Server refuses. In a sandbox the batches run as the database's dbo, which cannot revert: SQL Server
    keeps it to that database, with nothing on the server (1.0.70)"""
    parts = ["EXECUTE AS USER = N'dbo' WITH NO REVERT;", "GO"] if sandbox else []
    for batch, count in script_batches(script):
        parts += [f"DECLARE @vns_batch nvarchar(max) = CAST(0x{batch.encode('utf-16-le').hex()} AS nvarchar(max));",
                  "EXEC sys.sp_executesql @vns_batch;", "GO" + (f" {count}" if count > 1 else "")]
    return "\n".join(parts)


def run_query(settings, database, query, sandbox=False):
    """A script typed in the admin, run against one database. Returns what sqlcmd prints. A sandbox (a user who is not
    an administrator) keeps it to that database."""
    if postgres(settings):
        return pg().run_query(settings, database, query, sandbox=sandbox)
    if not isinstance(database, str) or not database or len(database) > 128 or any(char in database for char in "\0\r\n"):
        raise ValueError("Choose a database")
    if not isinstance(query, str) or not query.strip() or len(query) > 1024 * 1024:
        raise ValueError("Enter a script of at most 1 MB")
    if sandbox:
        if database.lower() in SYSTEM_DATABASES:
            raise ValueError(f"{database} is one of SQL Server's own databases: an administrator queries it.")
        # TRUSTWORTHY lets a database's dbo act on the server too: then the sandbox holds nothing
        found = query_json(settings, "SELECT CAST(is_trustworthy_on AS int) AS trustworthy FROM sys.databases WHERE name=" + literal(database))
        if not found:
            raise ValueError("Unknown database")
        if found[0]["trustworthy"]:
            raise ValueError(f"{database} is TRUSTWORTHY, which would let a script act on the whole server: an administrator queries it.")
    output = execute(settings, sealed_script(query, sandbox), table=True, timeout=300, database=database, separator=COLUMN_SEPARATOR)
    if len(output) > 2 * 1024 * 1024:
        output = output[:2 * 1024 * 1024] + "\n… output cut at 2 MB"
    return output


def parse_results(output, separator=COLUMN_SEPARATOR):
    """Split sqlcmd output into result sets with columns and rows. Anything else, like PRINT, is a message.
    A value with line breaks spans several lines, so a row is collected until it has as many columns as the
    header. A table that does not add up is shown as text, so nothing is shown in the wrong column."""
    results, messages, text = [], [], []
    lines = output.strip("\n").split("\n")
    dashes = re.compile(r"-+(" + re.escape(separator) + r"-+)*")

    def flush_text():
        block = "\n".join(text).strip("\n")
        if block.strip():
            messages.append(block.replace(separator, " | "))
        text.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        columns = line.split(separator)
        if not (dashes.fullmatch(following.strip()) and following.count(separator) == len(columns) - 1):
            if line.strip():
                text.append(line)
            else:
                flush_text()
            index += 1
            continue
        flush_text()
        rows, pending, end = [], None, index + 2
        while end < len(lines):
            current = lines[end]
            if pending is None and not current.strip():
                break
            pending = current if pending is None else pending + "\n" + current
            end += 1
            if pending.count(separator) >= len(columns) - 1:
                rows.append(pending.split(separator))
                pending = None
        if pending is None and all(len(row) == len(columns) for row in rows):
            results.append({"columns": columns, "rows": rows})
        else:
            messages.append("\n".join(lines[index:end]).replace(separator, " | "))
        index = end
    flush_text()
    return results, messages


def query_json(settings, query, *, preamble="", database="master"):
    output = execute(settings, preamble + "\nSELECT COALESCE((" + query + " FOR JSON PATH), N'[]') AS vns_json;", timeout=30,
                     database=database)
    lines = output.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError("sqlcmd returned no JSON output. Check the SQL command-line tools and server logs.")
    if lines[0].strip() == "vns_json":
        if len(lines) < 3 or not re.fullmatch(r"-+", lines[1].strip()):
            raise ValueError("Invalid sqlcmd JSON result header")
        lines = lines[2:]
    payload = "".join(lines)
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("Invalid SQL Server response")
    return data


def databases(settings):
    if postgres(settings):
        return pg().databases(settings)
    # umbraco: an Umbraco site's database, which has Umbraco's own table umbracoNode (looked up by its three-part name,
    # so no database is opened for it; one offline or not readable is not one)
    return query_json(settings, """
SELECT x.*, CAST(CASE WHEN x.state = N'ONLINE' AND x.id > 4 AND HAS_DBACCESS(x.name) = 1
 AND OBJECT_ID(QUOTENAME(x.name) + N'.dbo.umbracoNode', N'U') IS NOT NULL THEN 1 ELSE 0 END AS bit) AS umbraco FROM (
SELECT d.name, d.database_id AS id, d.state_desc AS state, d.recovery_model_desc AS recovery, d.is_in_standby,
 CAST(COALESCE(SUM(CAST(f.size AS bigint)),0)*8.0/1024 AS decimal(18,1)) AS size_mb
FROM sys.databases d LEFT JOIN sys.master_files f ON f.database_id=d.database_id
GROUP BY d.name,d.database_id,d.state_desc,d.recovery_model_desc,d.is_in_standby) x ORDER BY x.name""")


def tables(settings, database):
    """The tables of one database with their row count and size, for unfolding a database in the admin."""
    if postgres(settings):
        return pg().tables(settings, database)
    identifier(database)
    # The rows from the partitions alone: joined with the allocation units (in-row, LOB, row-overflow), each partition's
    # rows were counted once per unit, three times for a table with text (1.0.82)
    return query_json(settings, """
SELECT s.name AS [schema], t.name AS name,
 COALESCE((SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id=t.object_id AND p.index_id IN (0,1)),0) AS [rows],
 CAST(COALESCE((SELECT SUM(a.total_pages) FROM sys.partitions p JOIN sys.allocation_units a ON a.container_id=p.partition_id
  WHERE p.object_id=t.object_id),0)*8.0/1024 AS decimal(18,1)) AS size_mb
FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
ORDER BY s.name,t.name""", database=database)


def backups(settings):
    if postgres(settings):
        return pg().backups(settings)
    history = query_json(settings, """
SELECT TOP (200) b.backup_set_id AS id, b.database_name AS database_name,
 CONVERT(varchar(30),b.backup_finish_date,126) AS finished,
 CAST(b.backup_size/1048576.0 AS decimal(18,1)) AS size_mb,
 m.physical_device_name AS path, b.position,
 (SELECT COUNT(*) FROM msdb.dbo.backupmediafamily x WHERE x.media_set_id=b.media_set_id) AS media_count
FROM msdb.dbo.backupset b JOIN msdb.dbo.backupmediafamily m ON m.media_set_id=b.media_set_id
WHERE b.type='D' AND m.family_sequence_number=1 AND m.device_type=2
ORDER BY b.backup_finish_date DESC""")
    if local_backup_access(settings):
        directory = Path(settings["backup_directory"]).resolve()
        history = [item for item in history if not (
            Path(item["path"]).parent.resolve() == directory and not Path(item["path"]).exists()
        )]
    elif settings.get("ssh_enabled"):
        paths = {item["path"]: None for item in history
                 if str(Path(item["path"]).parent) == settings["backup_directory"].rstrip("/")
                 and Path(item["path"]).suffix.casefold() == ".bak"}
        if paths:
            present = file_operation(settings, "inspect", paths, skip_missing=True)
            history = [item for item in history if item["path"] not in paths or item["path"] in present]
    files = query_json(settings,
        "SELECT subdirectory AS name FROM @files WHERE is_file=1 AND LOWER(RIGHT(subdirectory,4))='.bak'",
        preamble=f"""
DECLARE @files TABLE (subdirectory nvarchar(512), depth int, is_file bit);
INSERT INTO @files EXEC master.sys.xp_dirtree {literal(settings["backup_directory"])}, 1, 1;""")
    known_paths = {item["path"] for item in history}
    separator = "\\" if "\\" in settings["backup_directory"] else "/"
    for entry in files:
        filename = entry["name"]
        if not filename or any(char in filename for char in ("/", "\\", "\0")):
            raise ValueError("SQL Server returned an invalid backup filename")
        path = settings["backup_directory"].rstrip("/\\") + separator + filename
        if path in known_paths:
            continue
        try:
            media = backup_table(settings, f"RESTORE LABELONLY FROM DISK={literal(path)};")
            if len(media) != 1:
                raise ValueError("Cannot read backup media information")
            family_count = int(media[0]["FamilyCount"])
            headers = backup_table(settings, f"RESTORE HEADERONLY FROM DISK={literal(path)};")
            full = [header for header in headers if header["BackupType"] == "1"]
            if not full:
                raise ValueError("File contains no full database backup")
            for header in full:
                position = int(header["Position"])
                if position < 1:
                    raise ValueError("Invalid backup position")
                key = path + "\0" + str(position) + "\0" + header["BackupSetGUID"]
                history.append({
                    "id": "file-" + hashlib.sha256(key.encode("utf-8")).hexdigest(),
                    "database_name": header["DatabaseName"], "finished": header["BackupFinishDate"].replace(" ", "T", 1),
                    "size_mb": round(int(header["BackupSize"]) / 1048576, 1),
                    "path": path, "position": position,
                    "media_count": family_count if header.get("IsDamaged") == "0" else 0,
                    "source": "file",
                })
        except (ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"Cannot read backup file {path}: {exc}", file=sys.stderr, flush=True)
            history.append({"id": "file-" + hashlib.sha256(path.encode("utf-8")).hexdigest(),
                            "database_name": filename, "finished": "", "size_mb": None, "path": path,
                            "position": 0, "media_count": 0, "source": "file", "error": str(exc)})
    return history


def backup_table(settings, query):
    output = execute(settings, query, table=True, timeout=30)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 3:
        raise ValueError("SQL Server returned no backup metadata")
    columns = [column.strip() for column in lines[0].split("|")]
    rows = []
    for line in lines[2:]:
        values = [value.strip() for value in line.split("|")]
        if len(values) != len(columns):
            raise ValueError("Invalid backup metadata columns")
        rows.append(dict(zip(columns, values)))
    return rows


def backup(settings, name):
    identifier(name)
    if any(char in name for char in '/\\:*?"<>|'):
        raise ValueError("Database name cannot be used as a backup filename: " + name)
    separator = "\\" if "\\" in settings["backup_directory"] else "/"
    if separator == "\\" and re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        raise ValueError("Database name is a reserved Windows backup filename: " + name)
    path = settings["backup_directory"].rstrip("/\\") + separator + name + ".bak"
    prepare_backup_access(settings, path, write=True)
    execute(settings, f"BACKUP DATABASE {identifier(name)} TO DISK={literal(path)} WITH INIT;")
    # Where it lies is on the Backups tab for an administrator; a user running this is not told
    print(f"Backup complete: {name}", flush=True)
    return path


def copy_backup(settings, name):
    """A copy-only backup of a database to a file of its own in the backup directory, for a copy: <name>.copy-<id>.bak"""
    identifier(name)
    if any(char in name for char in '/\\:*?"<>|'):
        raise ValueError("Database name cannot be used as a backup filename: " + name)
    separator = "\\" if "\\" in settings["backup_directory"] else "/"
    path = settings["backup_directory"].rstrip("/\\") + separator + f"{name}.copy-{uuid.uuid4().hex[:8]}.bak"
    prepare_backup_access(settings, path, write=True)
    execute(settings, f"BACKUP DATABASE {identifier(name)} TO DISK={literal(path)} WITH INIT, COPY_ONLY;")
    return path


def forget_copy_backup(settings, path):
    """The copy's own backup file deleted again, where Harbor reaches the backup directory; else said where it is"""
    try:
        if backup_file_access(settings) and "\\" not in settings["backup_directory"]:
            found = file_operation(settings, "inspect", {path: None}, skip_missing=True)
            if found:
                file_operation(settings, "delete", found)
            return
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    print(f"The copy's backup file is left on the SQL server: {path}", file=sys.stderr, flush=True)


def restore(settings, name, path, position, overwrite):
    prepare_backup_access(settings, path, write=False)
    files = execute(settings, f"RESTORE FILELISTONLY FROM DISK={literal(path)} WITH FILE={position};", table=True)
    lines = [line.strip() for line in files.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("LogicalName|PhysicalName|Type|"):
        raise ValueError("Cannot read backup logical files")
    entries = []
    for line in lines[2:]:
        fields = line.split("|")
        if len(fields) < 3 or fields[2] not in ("D", "L"):
            raise ValueError("Unsupported backup file type or invalid backup metadata")
        entries.append((fields[0], fields[2]))
    if not entries:
        raise ValueError("Backup contains no database files")
    paths = query_json(settings, """
SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(4000)) AS data_path,
 CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(4000)) AS log_path""")
    if not paths or not paths[0].get("data_path") or not paths[0].get("log_path"):
        raise ValueError("SQL Server default data/log paths are unavailable")
    # Unique physical filenames avoid collisions with other databases.
    prefix = "vns-" + uuid.uuid4().hex
    moves = []
    for index, (logical, kind) in enumerate(entries):
        directory = paths[0]["log_path" if kind == "L" else "data_path"]
        separator = "\\" if "\\" in directory else "/"
        physical = directory.rstrip("/\\") + separator + prefix + "-" + str(index) + (".ldf" if kind == "L" else ".mdf")
        moves.append(f"MOVE {literal(logical)} TO {literal(physical)}")
    sql = f"RESTORE DATABASE {identifier(name)} FROM DISK={literal(path)} WITH FILE={position}, " + ", ".join(moves)
    if overwrite:
        sql = f"""
ALTER DATABASE {identifier(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
BEGIN TRY
 {sql}, REPLACE;
 ALTER DATABASE {identifier(name)} SET MULTI_USER;
END TRY
BEGIN CATCH
 ALTER DATABASE {identifier(name)} SET MULTI_USER;
 THROW;
END CATCH;"""
    execute(settings, sql)
    print("Restore complete: " + name, flush=True)


def validate_restore_login(data):
    username = data.get("login")
    identifier(username)
    if username.casefold() in {"dbo", "guest", "sys", "information_schema"}:
        raise ValueError("Choose a username other than a built-in database user")
    password = data.get("login_password")
    if not isinstance(password, str) or not 6 <= len(password) <= 128 or any(char in password for char in ("\0", "\r", "\n")):
        raise ValueError("The new SQL login password must be 6-128 characters without line breaks")
    return username, password


def local_backup_access(settings):
    if postgres(settings):
        return pg().local_backup_access(settings)
    if settings["host"].strip() == ".":
        return True
    host = settings["host"].strip("[]").rstrip(".").casefold()
    # Also by one of its own addresses: a site on another server is given this one's SQL Server by its address
    if host in {"localhost", "127.0.0.1", "::1", socket.gethostname().casefold(), socket.getfqdn().casefold()}:
        return True
    return bool(re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host)) and host in own_addresses()


def own_addresses():
    """This server's own addresses (hostname -I)"""
    try:
        found = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        return set(found.split()) if isinstance(found, str) else set()
    except Exception:  # noqa: BLE001 - none known: only its names count
        return set()


def backup_file_access(settings):
    if postgres(settings):
        return pg().backup_file_access(settings)
    return local_backup_access(settings) or settings.get("ssh_enabled", False)


def prepare_backup_access(settings, path, write):
    if "\\" in settings["backup_directory"]:
        print("Windows backup permissions must be configured on the SQL Server host.",
              file=sys.stderr, flush=True)
    elif local_backup_access(settings) or settings.get("ssh_enabled", False):
        file_operation(settings, "prepare_write" if write else "prepare_read", {path: []})
    else:
        print("SQL backup file permissions cannot be prepared without local or SSH access; "
              "the SQL Server service account must have access on its server.", file=sys.stderr, flush=True)


def backup_file_pipe(settings, path, write):
    """How a backup file on the SQL server is read (cat) or written (cat >) as root, as argv and env: None when it is
    here. For a database moved to another server: its backup goes from one server's backup folder to the other's"""
    if local_backup_access(settings):
        return None
    config = ssh_settings(settings)
    if not config["ssh_enabled"]:
        raise ValueError(f"No access to the backup folder of {settings.get('host')}: turn on SSH in its connection")
    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("ssh is not installed on the admin server")
    args = [ssh, "-T", "-p", str(config["ssh_port"]), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    env = os.environ.copy()
    if config["ssh_password"]:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise ValueError("sshpass is required for password login: sudo apt install sshpass")
        args = [sshpass, "-e", *args]
        env["SSHPASS"] = config["ssh_password"]
    else:
        args += ["-o", "BatchMode=yes"]
    sudo = "" if config["ssh_username"] == "root" else "sudo -n "
    command = f"{sudo}sh -c " + shlex.quote(("cat > " if write else "cat ") + shlex.quote(path))
    return [*args, config["ssh_username"] + "@" + config["ssh_host"], command], env


def file_operation(settings, operation, files, skip_missing=False):
    data = dict(directory=settings["backup_directory"], operation=operation, files=files, skip_missing=skip_missing)
    if local_backup_access(settings):
        return _file_module.operate(data)
    config = ssh_settings(settings)
    if not config["ssh_enabled"]:
        raise ValueError("Configure Linux SSH access to delete remote backup files")
    ssh = shutil.which("ssh")
    if not ssh:
        raise ValueError("ssh is not installed on the admin server")
    args = [ssh, "-T", "-p", str(config["ssh_port"]), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    env = os.environ.copy()
    if config["ssh_password"]:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise ValueError("sshpass is required for password login: sudo apt install sshpass")
        args = [sshpass, "-e", *args]
        env["SSHPASS"] = config["ssh_password"]
    else:
        args += ["-o", "BatchMode=yes"]
    source = Path(__file__).with_name("backup_files.py").read_text()
    command = "python3 -c " + shlex.quote(source)
    if operation.startswith("prepare_") and config["ssh_username"] != "root":
        command = "sudo -n " + command
    args += [config["ssh_username"] + "@" + config["ssh_host"], command]
    result = subprocess.run(args, input=json.dumps(data), capture_output=True, text=True,
                            encoding="utf-8", env=env, timeout=60)
    if result.returncode:
        raise RuntimeError((result.stderr or "Remote backup file operation failed").strip())
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr, end="", flush=True)
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or any(
        not isinstance(path, str) or path not in files or not isinstance(identity, list)
        or len(identity) != 2 or any(type(value) is not int for value in identity)
        for path, identity in response.items()
    ):
        raise ValueError("Invalid remote backup file response")
    if operation != "prepare_write" and (operation == "delete" or not skip_missing) and set(response) != set(files):
        raise ValueError("Remote backup file operation did not confirm every file")
    for filename in (set(files) - set(response) if operation != "prepare_write" else set()):
        print("Backup file no longer exists; skipping: " + filename, flush=True)
    return response


def backup_files_for_deletion(settings, name, backup_id=None):
    if not backup_file_access(settings):
        raise ValueError("Backup file deletion requires a local connection or Linux SSH access")
    records = backups(settings)
    if backup_id is not None:
        records = [item for item in records if item["id"] == backup_id and item["database_name"] == name]
        if not records:
            raise ValueError("Backup no longer exists")
    else:
        records = [item for item in records if item["database_name"].casefold() == name.casefold()]
    paths = {}
    for item in records:
        path = Path(item["path"])
        if path.suffix.casefold() != ".bak" or str(path.parent) != settings["backup_directory"].rstrip("/"):
            raise ValueError("Backup deletion is restricted to regular .bak files in the backup directory")
        paths[str(path)] = None
    checked = file_operation(settings, "inspect", paths, skip_missing=backup_id is None)
    for filename in checked:
        path = Path(filename)
        headers = backup_table(settings, f"RESTORE HEADERONLY FROM DISK={literal(str(path))};")
        if any(header["DatabaseName"].casefold() != name.casefold() for header in headers):
            raise ValueError("Backup file contains backups of another database; deletion refused")
    return checked


def delete_backup_files(paths, settings=None):
    if not paths:
        return
    if settings is not None:
        file_operation(settings, "delete", paths)
        for filename in paths:
            print("Deleted backup file: " + filename, flush=True)
        return
    for filename, identity in paths.items():
        path = Path(filename)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != tuple(identity):
            raise ValueError("Backup file changed before deletion")
        path.unlink()
        print("Deleted backup file: " + filename, flush=True)


def create_restore_login(settings, database, username, password):
    execute(settings, f"""
CREATE LOGIN {identifier(username)} WITH PASSWORD={literal(password)}, CHECK_POLICY=OFF,
 CHECK_EXPIRATION=OFF, DEFAULT_DATABASE={identifier(database)};
USE {identifier(database)};
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name={literal(username)})
 ALTER USER {identifier(username)} WITH LOGIN={identifier(username)};
ELSE
 CREATE USER {identifier(username)} FOR LOGIN {identifier(username)};
ALTER ROLE [db_owner] ADD MEMBER {identifier(username)};""")
    print("SQL login and db_owner mapping created: " + username, flush=True)


def exclusive_database_logins(settings, database):
    candidates = query_json(settings, f"""
SELECT DISTINCT sp.name
FROM {identifier(database)}.sys.database_principals dp
JOIN sys.server_principals sp ON sp.sid=dp.sid
WHERE dp.type='S' AND dp.principal_id>4 AND sp.type='S' AND sp.principal_id>1
AND sp.name<>{literal(settings["username"])}
AND NOT EXISTS (SELECT 1 FROM sys.server_role_members r WHERE r.member_principal_id=sp.principal_id)
AND NOT EXISTS (SELECT 1 FROM sys.server_permissions p WHERE p.grantee_principal_id=sp.principal_id
 AND p.permission_name<>'CONNECT SQL')""")
    if not candidates:
        return []
    # Inaccessible databases prevent proving that a login is exclusive.
    others = query_json(settings, "SELECT name, state_desc AS state FROM sys.databases WHERE name<>" + literal(database))
    if any(row["state"] != "ONLINE" for row in others):
        print("Keeping SQL logins: another database is offline; shared usage cannot be verified.", flush=True)
        return []
    result = []
    for login in candidates:
        usage = query_json(settings, f"SELECT name FROM sys.databases WHERE name<>{literal(database)} AND owner_sid=SUSER_SID({literal(login['name'])})")
        for row in others:
            usage.extend(query_json(settings, f"""
SELECT name FROM {identifier(row["name"])}.sys.database_principals
WHERE sid=SUSER_SID({literal(login["name"])})"""))
        if usage:
            print("Keeping shared SQL login: " + login["name"], flush=True)
        else:
            result.append(login["name"])
    return result


def progress(data):
    """A step for the admin's window (as harbor's System sends it), which a terminal does not show"""
    if os.environ.get("HARBOR_PROGRESS") == "1":
        print(f"\x1b]7777;harbor;{json.dumps(data)}\x07", end="", flush=True)


def step(text):
    progress({"type": "step", "text": text})
    print(text, flush=True)


# What each action does, as its step in the admin's window
ACTION_STEPS = {"backup_all": "Back up every database", "create": "Create {name}", "backup": "Back up {name}", "drop": "Delete {name}",
                "copy": "Copy {name} to {target}", "restore": "Restore {target}", "delete_backup": "Delete the backup file of {name}"}


def tried_password(password, source="saved"):
    """Which password a refused login tried, without showing it: where it came from, how long it is, and blanks at its
    ends, which a password typed by hand never has"""
    blanks = " It begins or ends with a blank." if password != password.strip() else ""
    count = len(password)
    return f"It tried the {source} password of {count} character{'' if count == 1 else 's'}.{blanks}"


def connect_error(settings, error, source="saved"):
    """What a failed first connection means, in place of what sqlcmd says of it"""
    if postgres(settings):
        return pg().connect_error(settings, error, source)
    text = str(error)
    where = f'{settings["host"]}:{settings["port"]}'
    if "Login failed" in text:
        return RuntimeError(f"SQL Server on {where} refused the login of {settings['username']}: the password does not match the "
                            f"one SQL Server has. {tried_password(settings.get('password') or '', source)} Edit the connection and "
                            "give it the right one.")
    if any(sign in text for sign in ("Login timeout expired", "Server is not found", "TCP Provider", "network-related")):
        return RuntimeError(f"No SQL Server answers on {where}. Install SQL Server in Server tools, start it, or edit the connection "
                            "to point to a server that runs one.")
    return error


def taken(data, said):
    """Why a name cannot be used, for an administrator; for a user, the same words whatever the reason: that a database
    or login of that name is there (one not theirs) is not theirs to learn"""
    return "That name cannot be used. Choose another." if data.get("private_names") else said


def run_action(settings, data):
    if postgres(settings):
        return pg().run_action(settings, data)
    if not isinstance(data, dict):
        raise ValueError("Expected a SQL action object")
    action = data.get("action")
    if action not in ("backup_all", "create", "backup", "drop", "copy", "restore", "delete_backup"):
        raise ValueError("Unknown SQL action")
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
            if row["id"] > 4 and row["state"] == "ONLINE" and not row.get("is_in_standby", False):
                backup(settings, row["name"])
                count += 1
        print(f"Backup complete: {count} online user database(s).", flush=True)
        return
    identifier(name)
    current = next((row for row in rows if row["name"].casefold() == name.casefold()), None)
    if current and current["id"] <= 4:
        raise ValueError("System databases cannot be changed here")
    if action == "delete_backup":
        if data.get("confirmation") != name or not isinstance(data.get("backup_id"), (int, str)) or isinstance(data.get("backup_id"), bool):
            raise ValueError("Confirm the database name and select a backup")
        delete_backup_files(backup_files_for_deletion(settings, name, data["backup_id"]), settings)
        return
    if action in ("backup", "drop", "copy") and not current:
        raise ValueError("Database no longer exists")
    if action == "create":
        if current:
            raise ValueError(taken(data, "Database already exists"))
        # With a login of its own, db_owner in it, as harbor sql create makes one (create_db.sh did)
        login = validate_restore_login(data) if data.get("login") is not None else None
        if login and query_json(settings, "SELECT name FROM sys.server_principals WHERE name=" + literal(login[0])):
            raise ValueError(taken(data, f"There is a SQL login {login[0]} already. Choose another name for the new one."))
        execute(settings, f"CREATE DATABASE {identifier(name)};")
        if login:
            create_restore_login(settings, name, *login)
    elif action == "backup":
        if current["state"] != "ONLINE" or current.get("is_in_standby", False):
            raise ValueError("Backups require an online database that is not in standby")
        backup(settings, current["name"])
    elif action == "drop":
        if data.get("confirmation") != name:
            raise ValueError("Type the exact database name to confirm deletion")
        if not isinstance(data.get("delete_backups", False), bool):
            raise ValueError("Invalid backup deletion option")
        files_to_delete = backup_files_for_deletion(settings, name) if data.get("delete_backups") else {}
        if not isinstance(data.get("delete_login", True), bool):
            raise ValueError("Invalid login deletion option")
        logins_to_delete = exclusive_database_logins(settings, name) if data.get("delete_login", True) else []
        execute(settings, f"""
ALTER DATABASE {identifier(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
BEGIN TRY
 DROP DATABASE {identifier(name)};
END TRY
BEGIN CATCH
 ALTER DATABASE {identifier(name)} SET MULTI_USER;
 THROW;
END CATCH;""")
        for login in logins_to_delete:
            execute(settings, "DROP LOGIN " + identifier(login) + ";")
            print("Deleted SQL login: " + login, flush=True)
        delete_backup_files(files_to_delete, settings)
    elif action in ("copy", "restore"):
        target = data.get("target", "")
        identifier(target)
        existing = next((row for row in rows if row["name"].casefold() == target.casefold()), None)
        if existing and existing["id"] <= 4:
            raise ValueError("Cannot overwrite a system database")
        overwrite = existing is not None
        if overwrite and (action == "copy" or data.get("confirmation") != target or data.get("overwrite") is not True):
            raise ValueError(taken(data, "Destination exists. Explicit overwrite confirmation is required for restore."))
        login = None
        if action == "copy":
            login = validate_restore_login(data)
            if query_json(settings, "SELECT name FROM sys.server_principals WHERE name=" + literal(login[0])):
                raise ValueError(taken(data, "SQL login already exists. Choose another username before copying/restoring."))
        if action == "copy":
            # A backup of its own, copy-only, deleted again: none of the database's backups is overwritten or added to
            path, position = copy_backup(settings, name), 1
        else:
            backup_id = data.get("backup_id")
            if isinstance(backup_id, bool) or not (
                isinstance(backup_id, int) or isinstance(backup_id, str) and re.fullmatch(r"file-[0-9a-f]{64}", backup_id)
            ):
                raise ValueError("Select a valid backup")
            selected = next((item for item in backups(settings) if item["id"] == data.get("backup_id")), None)
            if not selected or selected["media_count"] != 1:
                raise ValueError("Backup not found or striped backups are unsupported")
            path, position = selected["path"], selected["position"]
        if action == "restore" and not overwrite:
            login = validate_restore_login(data)
            if query_json(settings, "SELECT name FROM sys.server_principals WHERE name=" + literal(login[0])):
                raise ValueError(taken(data, "SQL login already exists. Choose another username before restoring."))
        try:
            restore(settings, target, path, position, overwrite)
        finally:
            if action == "copy":
                forget_copy_backup(settings, path)
        if login:
            create_restore_login(settings, target, *login)
    print("OK: " + action, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("store")
    parser.add_argument("connection")
    parser.add_argument("payload")
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload)
        password = os.environ.pop("VNS_SQL_LOGIN_PASSWORD", None)
        if password is not None:
            payload["login_password"] = password
        run_action(connection(args.store, args.connection), payload)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # The window shows it at the step that failed, as for harbor's own commands
        progress({"type": "warning", "text": str(exc)})
        print("ERROR: " + str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
