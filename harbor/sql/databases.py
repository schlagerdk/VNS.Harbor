"""The local SQL Server's databases, from the terminal and cron. From sql/*.sh and sql/cron/*.sh.
clean_umbraco7_db.sh and cron/delete_old_2dm_data.sh are custom scripts for some servers: they stay as they are, outside harbor.

sqlcmd gets the sa password in SQLCMDPASSWORD, not on its command line (the scripts used -P), so it is not in the
process list. The wrappers in sql/ hold the password, as set_sa_password.sh and updates set it there, and pass it on.
The SQL is templates/sql/*.sql, rendered to a file of its own for each run (the scripts edited some of them in place).
"""
from datetime import datetime
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from harbor.core.system import Stop
from harbor.core.paths import SA_SCRIPTS, SQL

SQL_DIR = SA_SCRIPTS
TEMPLATES = SQL
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
BACKUPS = "/var/opt/mssql/backup"
PLACEHOLDER = "@SA_PASSWORD@"
LINE = "-------------------------------------------------------------------------------"


SA_SECRET = "/etc/harbor/secrets/sql.json"


def sa_password(system=None):
    """The sa password: the one a wrapper in sql/ passes on (set_sa_password.sh set it there), else Harbor's own
    (/etc/harbor/secrets/sql.json, root only), which is all there is once the wrappers are gone"""
    password = os.environ.get("SQLCMDPASSWORD", "")
    if password and password != PLACEHOLDER:
        return password
    if system is not None:
        from harbor.state.store import read_json
        saved = read_json(system.path(SA_SECRET))
        if isinstance(saved, dict) and saved.get("sa_password"):
            return saved["sa_password"]
    raise Stop("The sa password is not set: set it under Server tools in the admin, or with harbor sql set-sa-password")


def sqlcmd(system, sql, *options, stop_on_error=True, capture=False):
    """sqlcmd against the local SQL Server as sa; -b stops at the first error, as the scripts that used it"""
    return system.run(SQLCMD, *(["-b"] if stop_on_error else []), "-C", "-S", "LOCALHOST", "-U", "sa", *options, "-i", str(sql),
                      env={"SQLCMDPASSWORD": sa_password(system)}, check=stop_on_error, capture=capture)


def database_name(system, given, prompt="Enter db name: ", missing="No database name given", wait=False):
    name = given or system.ask(prompt).strip()
    if not name:
        if wait:
            # The script waited for Enter before it stopped
            system.ask(missing)
            raise Stop("", 1)
        raise Stop(missing)
    return name


def new_login_password(system):
    """The password of the SQL login a new database gets, asked twice and not shown"""
    while True:
        password = system.ask("Enter password for the new SQL login: ", secret=True)
        confirm = system.ask("Repeat password: ", secret=True)
        if not password:
            system.warn("Password cannot be empty")
        elif password != confirm:
            system.warn("Passwords do not match")
        elif len(password) < 8:
            system.warn("Password must be at least 8 characters")
        elif "\r" in password:
            system.warn("Password cannot contain carriage returns")
        else:
            return password


def render(template, replacements, password=None):
    """A copy of a template with the names in it, and the login's password as a SQL string"""
    text = (TEMPLATES / template).read_text()
    for placeholder, value in replacements:
        text = text.replace(placeholder, value)
    if password is not None:
        text = text.replace("@NEW_LOGIN_PASSWORD@", "'" + password.replace("'", "''") + "'")
    file = tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False)
    with file:
        file.write(text)
    return file.name


def prepare_backup_permissions(system, quiet=False):
    """The backup folder, and every .bak in it, readable and writable for SQL Server: done before each use, as a .bak
    put there by hand may belong to someone else. quiet for the backup job, whose log tells only what it backed up."""
    folder = system.path(BACKUPS)
    if folder.is_symlink():
        raise Stop("Backup directory must not be a symbolic link.")
    folder.mkdir(parents=True, exist_ok=True)
    if not quiet:
        system.say("Ensure SQL Server can read and overwrite backup files")
    system.run("chown", "mssql:mssql", BACKUPS)
    system.run("chmod", "750", BACKUPS)
    for backup in sorted(folder.iterdir()):
        if not backup.name.lower().endswith(".bak"):
            continue
        if backup.is_symlink():
            raise Stop(f"Backup files must not be symbolic links: {BACKUPS}/{backup.name}")
        if not backup.is_file():
            continue
        system.run("chown", "mssql:mssql", f"{BACKUPS}/{backup.name}")
        system.run("chmod", "640", f"{BACKUPS}/{backup.name}")


def run_rendered(system, template, replacements, password=None, stop_on_error=True):
    sql = render(template, replacements, password)
    try:
        return sqlcmd(system, sql, stop_on_error=stop_on_error)
    finally:
        os.unlink(sql)


def create(system, name=""):
    name = database_name(system, name)
    password = new_login_password(system)
    run_rendered(system, "create_db.sql", [("NEW_NAME", name)], password)
    system.say(f"{name} is now created!")


def copy(system, source="", target=""):
    source = database_name(system, source, "Enter old db name: ", "No source database name given")
    target = database_name(system, target, "Enter new db name: ", "No destination database name given")
    password = new_login_password(system)
    prepare_backup_permissions(system)
    run_rendered(system, "copy_db.sql", [("OLD_NAME", source), ("NEW_NAME", target)], password)
    system.say(f"{source} was copied to {target}.")


def restore(system, name=""):
    name = database_name(system, name)
    password = new_login_password(system)
    prepare_backup_permissions(system)
    run_rendered(system, "restore_db.sql", [("NEW_NAME", name)], password)
    system.say(f"{name} is now restored!")


def drop(system, name="", remove_backup=""):
    name = database_name(system, name, missing="No name given...", wait=True)
    if not remove_backup:
        remove_backup = system.ask("Remove backup? (y/N): ")
    run_rendered(system, "drop_db.sql", [("NEW_NAME", name)], stop_on_error=False)
    if remove_backup[:1] in ("y", "Y"):
        backup = system.path(f"{BACKUPS}/{name}.bak")
        if backup.exists():
            backup.unlink()
        else:
            system.warn(f"rm: cannot remove '{BACKUPS}/{name}.bak': No such file or directory")
    system.say(f"{name} is now deleted!")



def columns(text):
    """Rows separated by |, in columns two spaces apart, as column -t -s '|'"""
    rows = [line.split("|") for line in text.splitlines() if line.strip()]
    widths = {}
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths.get(index, 0), len(cell))
    return "\n".join("  ".join(cell.ljust(widths[index]) if index < len(row) - 1 else cell for index, cell in enumerate(row)) for row in rows)


def list_databases(system):
    # -W trims padding, -s sets a column separator; the dashed header line goes, and the columns are aligned
    output = sqlcmd(system, TEMPLATES / "list_dbs.sql", "-W", "-s", "|", stop_on_error=False, capture=True).stdout
    lines = output.splitlines()
    system.say(columns("\n".join(lines[:1] + lines[2:])))


def list_backups(system):
    system.run("ls", "-la", "--color", BACKUPS, check=False)
    system.say(BACKUPS)


def stamp(zone=None):
    return datetime.now(ZoneInfo(zone) if zone else None).strftime("%d.%m.%Y %H:%M")


ROW = "ROW|"
# What sqlcmd says of work that went well: a backup's pages, and the rows DBCC SHRINKFILE returns (numbers only)
QUIET = ("Processed ", "BACKUP DATABASE successfully processed ", "DBCC execution completed.")


def table(headers, rows, right=()):
    """Rows as a Markdown table, so a log reads well as text and anywhere that shows Markdown; the columns in right
    (by index) are aligned to the right, as numbers are"""
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    def line(cells):
        return "| " + " | ".join(str(cell).rjust(width) if index in right else str(cell).ljust(width)
                                 for index, (cell, width) in enumerate(zip(cells, widths))) + " |"
    return "\n".join([line(headers), "|" + "|".join("-" * (width + 2) for width in widths) + "|", *map(line, rows)])


def size(kilobytes):
    value = float(kilobytes) / 1024
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.1f} MB"


def report(system, sql, *options, stop_on_error=True):
    """A script whose rows start with ROW|, run without the messages of what went well: its rows, and every other line
    sqlcmd writes (an error and what it says of it) as it is"""
    result = sqlcmd(system, sql, "-W", "-h", "-1", "-s", "|", *options, stop_on_error=False, capture=True)
    rows = []
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith(ROW):
            rows.append(line[len(ROW):].split("|"))
        elif line.strip() and not line.startswith(QUIET) and not set(line) <= set("0123456789|-. "):
            system.say(line)
    return result, rows


def backup_everything(system):
    zone = "Europe/Copenhagen"
    try:
        # The run's first line, which the admin starts its card of the run with
        system.say(f"----- {stamp(zone)} -----")
        prepare_backup_permissions(system, quiet=True)
        result, rows = report(system, TEMPLATES / "backup_everything.sql")
        if rows:
            system.say(table(["Nr", "Database", "Size", "Time", "Speed"],
                             [[number, name, size(kilobytes), f"{float(seconds):.1f} s",
                               f"{float(kilobytes) / 1024 / max(float(seconds), 0.001):.1f} MB/s"]
                              for number, name, kilobytes, seconds in rows], right={2, 3, 4}))
        if not result.ok:
            raise Stop("", result.returncode)
    except Exception:
        system.warn("SQL backup failed; inspect the preceding error. No successful completion was recorded.")
        raise
    system.say(f"Backup of everything done! ----- {stamp(zone)} -----")
    system.say("")


def shrink_logs(system):
    system.say(f"----- {stamp()} -----")
    result, rows = report(system, TEMPLATES / "shrink_logs.sql")
    if rows:
        system.say(table(["Nr", "Database", "Log before", "Log after", "Shrunk"],
                         [[number, name, size(before), size(after), "yes" if shrunk == "1" else "no, at its smallest"]
                          for number, name, before, after, shrunk in rows], right={2, 3}))
    if not result.ok:
        # Without -b sqlcmd goes on past an error in the SQL: a failure is that it did not run at all
        raise Stop("The logs were not shrunk; see the error above.", result.returncode)
    system.say(f"All logs now shrunk! ----- {stamp()} -----")
    system.say("")



def add_to_crontab(system):
    """The log shrink and the backup in root's crontab, as harbor commands (harbor server cron on)"""
    from harbor.server.tools import cron
    cron(system, True)
