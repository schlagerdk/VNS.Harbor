"""The local SQL Server's databases, from the terminal and cron. From sql/*.sh and sql/cron/*.sh.
clean_umbraco7_db.sh and cron/delete_old_2dm_data.sh are custom scripts for some servers: they stay as they are, outside harbor.

sqlcmd gets the sa password in SQLCMDPASSWORD, not on its command line (the scripts used -P), so it is not in the
process list. The wrappers in sql/ hold the password, as set_sa_password.sh and updates set it there, and pass it on.
The SQL is sql/scripts/*.sql, rendered to a file of its own for each run (the scripts edited some of them in place).
"""
from datetime import datetime
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from harbor.core.system import Stop

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
TEMPLATES = SQL_DIR / "scripts"
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
BACKUPS = "/var/opt/mssql/backup"
PLACEHOLDER = "@SA_PASSWORD@"
LINE = "-------------------------------------------------------------------------------"


def sa_password():
    """The sa password the wrapper passes on, as set by set_sa_password.sh"""
    password = os.environ.get("SQLCMDPASSWORD", "")
    if not password or password == PLACEHOLDER:
        raise Stop(f"The sa password is not set in {SQL_DIR}: run {SQL_DIR}/set_sa_password.sh")
    return password


def sqlcmd(system, sql, *options, stop_on_error=True, capture=False):
    """sqlcmd against the local SQL Server as sa; -b stops at the first error, as the scripts that used it"""
    return system.run(SQLCMD, *(["-b"] if stop_on_error else []), "-C", "-S", "LOCALHOST", "-U", "sa", *options, "-i", str(sql),
                      env={"SQLCMDPASSWORD": sa_password()}, check=stop_on_error, capture=capture)


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


def prepare_backup_permissions(system):
    """The backup folder, and every .bak in it, readable and writable for SQL Server"""
    folder = system.path(BACKUPS)
    if folder.is_symlink():
        raise Stop("Backup directory must not be a symbolic link.")
    folder.mkdir(parents=True, exist_ok=True)
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


def backup_everything(system):
    zone = "Europe/Copenhagen"
    try:
        prepare_backup_permissions(system)
        system.say(f"----- {stamp(zone)} {LINE}")
        sqlcmd(system, TEMPLATES / "backup_everything.sql")
    except Exception:
        system.warn("SQL backup failed; inspect the preceding error. No successful completion was recorded.")
        raise
    system.say(f"Backup of everything done! ----------------------------------------------------- {stamp(zone)} -----")


def shrink_logs(system):
    system.say(f"----- {stamp()} {LINE}")
    sqlcmd(system, TEMPLATES / "shrink_logs.sql", stop_on_error=False)
    system.say(f"All logs now shrinked! -------------------------------------------------------- {stamp()} -----")
    system.say("")



CRON = ("0 */4 * * * /vns/sql/cron/shrink_logs.sh >> /var/log/sql_shrink_logs.log 2>&1",
        "15 */4 * * * /vns/sql/cron/backup_everything.sh >> /var/log/sql_backup_everything.log 2>&1")


def add_to_crontab(system):
    """The log shrink and the backup in root's crontab, each added to what is there"""
    for line in CRON:
        current = system.run("crontab", "-l", check=False, capture=True)
        existing = current.stdout if current.ok else ""
        system.run("crontab", "-", input=existing + line + "\n")
