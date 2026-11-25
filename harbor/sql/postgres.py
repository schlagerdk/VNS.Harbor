"""PostgreSQL on this server, beside or instead of Microsoft SQL Server (phase 10 of dev/HARBOR.md): installed from
Ubuntu's own packages, updated and removed; its tools (psql, pg_dump, pg_restore) alone to reach one elsewhere; and the
password of its postgres login, which Harbor keeps (/etc/harbor/secrets/postgres.json) and gives the admin's
connections to it.

It listens on every address with passwords (scram-sha-256), as Microsoft SQL Server does on its port: a firewall or the
network decides who reaches it. Harbor's own settings are files of their own (conf.d/harbor.conf, and a marked block in
pg_hba.conf), so an update of the package keeps them and a removal takes them.
"""
import json
import os
import re
import shutil
import uuid

from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

SECRET = "/etc/harbor/secrets/postgres.json"
# The admin gives the password in the task's environment, never on its command line
PASSWORD_VARIABLE = "HARBOR_POSTGRES_PASSWORD"
# pg_dump's files for the connection to PostgreSQL here: on this server, root's, beside Microsoft SQL Server's
# (/var/opt/mssql/backup)
BACKUPS = "/var/opt/postgresql/backup"
# Where they were first (1.0.82 before it was released): a connection with it is moved
OLD_BACKUPS = "/var/lib/harbor/postgres-backups"
# Its databases (and backups), as Microsoft SQL Server's in /var/opt/mssql: each version's cluster in <version>/main,
# set in Ubuntu's createcluster.conf before PostgreSQL is installed, so its own tools (and an upgrade) make them here
DATA = "/var/opt/postgresql"
CREATECLUSTER = "/etc/postgresql-common/createcluster.conf"
DATA_LINE = f"data_directory = '{DATA}/%v/%c'"
# Where Ubuntu keeps them otherwise: a cluster there before 1.0.82 is left there while it holds databases
UBUNTU_DATA = "/var/lib/postgresql"
PORT = 5432
HBA_BEGIN = "# Harbor: logins with a password from anywhere (harbor postgres install)"
HBA_END = "# Harbor: end"
HBA = f"""{HBA_BEGIN}
host    all             all             0.0.0.0/0               scram-sha-256
host    all             all             ::/0                    scram-sha-256
{HBA_END}
"""
CONF = """# Harbor: PostgreSQL listens on every address; pg_hba.conf asks a password of each login (harbor postgres install)
listen_addresses = '*'
password_encryption = scram-sha-256
"""


def installed(system, package):
    return system.run("dpkg", "-s", package, check=False, capture=True).ok


def package_version(system, package):
    return system.output("dpkg-query", "-W", "-f=${Version}", package, check=False) if installed(system, package) else ""


def clusters(system):
    """The version folders of PostgreSQL's settings here: /etc/postgresql/<version>/main"""
    root = system.path("/etc/postgresql")
    return sorted((path.name for path in root.iterdir() if (path / "main").is_dir()), key=lambda name: [int(part) for part in re.findall(r"\d+", name)] or [0]) if root.is_dir() else []


def tools_installed(system):
    return bool(system.which("psql")) or system.path("/usr/bin/psql").is_file()


def password(system):
    return (read_json(system.path(SECRET)) or {}).get("postgres_password", "")


def given_password():
    value = os.environ.get(PASSWORD_VARIABLE, "")
    if value and len(value) < 8:
        raise Stop("The postgres password needs at least 8 characters")
    return value


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def as_postgres(system, sql):
    """SQL run as the postgres user of the server, through its socket: the SQL on stdin, so a password in it is never on
    a command line"""
    system.run("runuser", "-u", "postgres", "--", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", "postgres", input=sql, capture=True)


def save_password(system, value):
    """The postgres password in Harbor's secrets (root only), and in the admin's connections to PostgreSQL here"""
    path = system.path(SECRET)
    write_json(path, {"postgres_password": value}, private=True)
    for place in (system.path("/etc/harbor/secrets"), path.parent):
        place.chmod(0o700)
    update_admin_connections(system, value)


def set_password(system):
    """The postgres login's password: from HARBOR_POSTGRES_PASSWORD (the admin), set in PostgreSQL and kept by Harbor"""
    value = given_password()
    if not value:
        raise Stop(f"Give the password in {PASSWORD_VARIABLE}.")
    if not installed(system, "postgresql"):
        raise Stop("PostgreSQL is not installed here.")
    system.plan(["Set the postgres password"])
    system.step("Set the postgres password")
    as_postgres(system, f"ALTER ROLE postgres WITH PASSWORD {literal(value)};\n")
    save_password(system, value)
    system.say("The postgres password is set, and Harbor keeps it.")


def configure(system):
    """Listen on every address, and a password for every login from the network: Harbor's own files, the package's own
    settings as they are"""
    for version in clusters(system):
        folder = system.path(f"/etc/postgresql/{version}/main")
        (folder / "conf.d").mkdir(exist_ok=True)
        (folder / "conf.d/harbor.conf").write_text(CONF)
        hba = folder / "pg_hba.conf"
        text = hba.read_text() if hba.is_file() else ""
        if HBA_BEGIN not in text:
            hba.write_text(text + ("" if text.endswith("\n") or not text else "\n") + HBA)


UTF8 = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def data_here(system):
    """Ubuntu's createcluster.conf with Harbor's data_directory: the clusters made after it are in DATA"""
    path = system.path(CREATECLUSTER)
    text = path.read_text() if path.is_file() else ""
    if DATA_LINE in text.splitlines():
        return
    lines = [line for line in text.splitlines() if not line.strip().startswith("data_directory")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines + ["# Harbor: the databases in /var/opt/postgresql, as Microsoft SQL Server's in /var/opt/mssql", DATA_LINE]) + "\n")
    folder = system.path(DATA)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)


def made_again(system):
    """A cluster in another encoding (SQL_ASCII, when the locale was C) or outside DATA is made again, in UTF-8 in
    DATA, while it holds no database of its own; one that does is left as it is, and said"""
    for version in clusters(system):
        found = system.run("runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-d", "postgres", "-c",
                           "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='template1';"
                           "SELECT count(*) FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres';"
                           "SHOW data_directory;",
                           check=False, capture=True)
        lines = found.stdout.split()
        if not found.ok or len(lines) < 3:
            continue
        encoding, own, folder = lines[0], lines[1], lines[2]
        why = [*([f"in {encoding}, not UTF-8"] if encoding != "UTF8" else []), *([f"in {folder}"] if not folder.startswith(DATA + "/") else [])]
        if not why:
            continue
        if own != "0":
            system.warn(f"PostgreSQL {version} keeps its databases {' and '.join(why)}: it holds databases, so it is left as it is.")
            continue
        system.say(f"PostgreSQL {version} was made {' and '.join(why)}: it is made again in UTF-8 in {DATA}.")
        system.run("pg_dropcluster", "--stop", version, "main", env=UTF8)
        system.run("pg_createcluster", "--encoding=UTF8", "--locale=C.UTF-8", "--datadir", f"{DATA}/{version}/main", "--start", version, "main", env=UTF8)


def install(system):
    """PostgreSQL from Ubuntu's packages, listening with passwords, its postgres password set from
    HARBOR_POSTGRES_PASSWORD, a folder for its backups, and a connection to it in the admin, as Microsoft SQL Server's
    install adds one"""
    value = given_password()
    if not value and not password(system):
        raise Stop(f"Give the postgres password in {PASSWORD_VARIABLE}.")
    steps = ["Install PostgreSQL", "Let it be reached with a password", "Set the postgres password", "Create the backup folder",
             "Add the connection in the admin"]
    system.plan(steps)
    system.step(steps[0])
    if installed(system, "postgresql"):
        system.say("PostgreSQL is installed already.")
    else:
        system.run("apt-get", "update")
        # Ubuntu's cluster tools first, told where the databases go; then PostgreSQL, whose cluster is made there, and in
        # UTF-8 (it takes its encoding from the locale, also when the task has none)
        system.run("apt-get", "install", "-y", "postgresql-common", env=UTF8 | {"DEBIAN_FRONTEND": "noninteractive"})
        data_here(system)
        system.run("apt-get", "install", "-y", "postgresql", "postgresql-client", env=UTF8 | {"DEBIAN_FRONTEND": "noninteractive"})
    data_here(system)
    made_again(system)
    system.step(steps[1])
    configure(system)
    system.run("systemctl", "enable", "postgresql", check=False, capture=True)
    system.run("systemctl", "restart", "postgresql")
    system.step(steps[2])
    value = value or password(system)
    as_postgres(system, f"ALTER ROLE postgres WITH PASSWORD {literal(value)};\n")
    save_password(system, value)
    system.step(steps[3])
    folder = system.path(BACKUPS)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    # Dumps made where they were first go with them
    old = system.path(OLD_BACKUPS)
    if old.is_dir():
        for dump in old.glob("*.dump"):
            dump.replace(folder / dump.name)
        try:
            old.rmdir()
        except OSError:
            pass
    system.step(steps[4])
    from harbor.sql.server import add_admin_connection
    add_admin_connection(system, "postgres", "Local", PORT, "postgres", value, BACKUPS)
    versions = [package_version(system, f"postgresql-{major}").split("-")[0] for major in clusters(system)]
    system.say(f"PostgreSQL {next((item for item in reversed(versions) if item), package_version(system, 'postgresql'))} runs here.")


def update(system):
    """PostgreSQL and its tools to the newest packages of this Ubuntu: a new major version comes with a new Ubuntu"""
    if not installed(system, "postgresql") and not tools_installed(system):
        raise Stop("PostgreSQL is not installed here.")
    system.plan(["Update PostgreSQL"])
    system.step("Update PostgreSQL")
    system.run("apt-get", "update")
    packages = [line.split("\t")[0] for line in system.output("dpkg-query", "-W", "-f=${Package}\t${Status}\n", "postgresql*", check=False).splitlines()
                if line.endswith("install ok installed")]
    if packages:
        system.run("apt-get", "install", "--only-upgrade", "-y", *packages, env={"DEBIAN_FRONTEND": "noninteractive"})
    system.say(f"PostgreSQL {package_version(system, 'postgresql') or package_version(system, 'postgresql-client')}.")


def uninstall(system, delete_data=False, with_tools=False):
    """PostgreSQL goes, its tools too when asked; its databases and backups stay in /var/opt/postgresql unless
    delete_data"""
    if not installed(system, "postgresql"):
        raise Stop("PostgreSQL is not installed here.")
    versions = clusters(system)
    steps = ["Stop PostgreSQL", "Remove PostgreSQL", *(["Delete its databases and backups"] if delete_data else [])]
    system.plan(steps)
    system.step(steps[0])
    system.run("systemctl", "stop", "postgresql", check=False)
    system.run("systemctl", "disable", "postgresql", check=False, capture=True)
    system.step(steps[1])
    if delete_data:
        # Harbor's own settings are no package's file: they go first, so the package takes its folders with it (else
        # dpkg says /etc/postgresql is not empty and leaves it). Kept with the databases, they are there for them.
        for version in versions:
            system.path(f"/etc/postgresql/{version}/main/conf.d/harbor.conf").unlink(missing_ok=True)
    packages = ["postgresql", *(f"postgresql-{version}" for version in versions)]
    if with_tools:
        packages += ["postgresql-client", *(f"postgresql-client-{version}" for version in versions), "postgresql-common", "postgresql-client-common"]
    system.run("apt-get", "remove", *(["--purge"] if delete_data else []), "-y", *packages, env={"DEBIAN_FRONTEND": "noninteractive"})
    system.run("apt-get", "autoremove", "-y", check=False, capture=True)
    if delete_data:
        system.step(steps[2])
        # What is left of the settings' folders, empty, goes too
        for folder in sorted(system.path("/etc/postgresql").rglob("*"), key=lambda path: len(path.parts), reverse=True) if system.path("/etc/postgresql").is_dir() else []:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        if system.path("/etc/postgresql").is_dir() and not any(system.path("/etc/postgresql").iterdir()):
            system.path("/etc/postgresql").rmdir()
        for folder in (DATA, UBUNTU_DATA):
            if system.path(folder).exists():
                shutil.rmtree(system.path(folder), ignore_errors=True)
                system.say(f"{folder} deleted.")
    else:
        system.say(f"The databases are kept in {DATA}.")
    remove_admin_connections(system, tools_too=with_tools)
    system.path(SECRET).unlink(missing_ok=True)
    system.say("PostgreSQL removed.")


def install_tools(system):
    """psql, pg_dump and pg_restore, to reach a PostgreSQL on another server"""
    system.plan(["Install the PostgreSQL tools"])
    system.step("Install the PostgreSQL tools")
    if tools_installed(system):
        system.say("The PostgreSQL tools are installed already.")
        return
    system.run("apt-get", "update")
    system.run("apt-get", "install", "-y", "postgresql-client", env={"DEBIAN_FRONTEND": "noninteractive"})


def uninstall_tools(system):
    if installed(system, "postgresql"):
        raise Stop("PostgreSQL itself is installed here: remove it with its tools.")
    system.plan(["Remove the PostgreSQL tools"])
    system.step("Remove the PostgreSQL tools")
    system.run("apt-get", "remove", "-y", "postgresql-client", "postgresql-client-common", env={"DEBIAN_FRONTEND": "noninteractive"})
    system.run("apt-get", "autoremove", "-y", check=False, capture=True)
    remove_admin_connections(system, tools_too=True)


# ---- the admin's connections to it (the same file as Microsoft SQL Server's, each with its engine)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def local_names(system):
    names = set(LOCAL_HOSTS)
    for argv in (("hostname",), ("hostname", "-f"), ("hostname", "-I")):
        names.update(name.lower() for name in system.output(*argv, check=False).split())
    return names


def is_local(item, names):
    return (item.get("engine") == "postgres" and str(item.get("host", "")).strip("[]").rstrip(".").lower() in names
            and item.get("port", PORT) == PORT)


def update_admin_connections(system, value):
    """The admin's connections that log in as postgres to PostgreSQL here get its new password"""
    from harbor.sql.server import admin_connections, write_admin_connections
    names = local_names(system)
    for site, path, items in admin_connections(system):
        changed = False
        for item in items:
            if isinstance(item, dict) and is_local(item, names) and str(item.get("username", "")).lower() == "postgres" and item.get("password") != value:
                item["password"] = value
                changed = True
        if changed:
            write_admin_connections(path, items)
            system.say(f"The connection of {site} to PostgreSQL here has the new password.")


def remove_admin_connections(system, tools_too=False):
    """With PostgreSQL gone, the connections to it here go; with its tools gone, every PostgreSQL connection (none of
    them can be used without psql)"""
    from harbor.sql.server import admin_connections, write_admin_connections
    names = local_names(system)
    for site, path, items in admin_connections(system):
        kept = [item for item in items if not (isinstance(item, dict) and item.get("engine") == "postgres" and (tools_too or is_local(item, names)))]
        if len(kept) != len(items):
            write_admin_connections(path, kept)
            gone = len(items) - len(kept)
            system.say(f"The {gone} PostgreSQL connection{'' if gone == 1 else 's'} of {site} removed.")


# ---- backups of every database here, as Harbor's scheduled job
def backup_everything(system):
    """Every database of PostgreSQL here dumped (pg_dump, custom format) into the backup folder, one file each, the one
    before replaced; its log as Microsoft SQL Server's job writes it: the run's start, a table of the databases with
    their size, time and speed, and its end"""
    import time
    from harbor.sql.databases import stamp, table
    zone = "Europe/Copenhagen"
    system.say(f"----- {stamp(zone)} -----")
    try:
        if not installed(system, "postgresql"):
            raise Stop("PostgreSQL is not installed here.")
        value = password(system)
        if not value:
            raise Stop("Harbor has no postgres password: set it in Server tools.")
        folder = system.path(BACKUPS)
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        env = {"PGPASSWORD": value, "PGHOST": "localhost", "PGUSER": "postgres", "PGPORT": str(PORT)}
        names = system.output("psql", "-X", "-At", "-d", "postgres", "-c",
                              "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname <> 'postgres' ORDER BY datname",
                              env=env).splitlines()
        rows = []
        for name in names:
            if not name.strip() or "/" in name or name.startswith("."):
                if name.strip():
                    system.warn(f"Not backed up, as its name cannot be a file's: {name!r}")
                continue
            target = f"{BACKUPS}/{name}.dump"
            started = time.monotonic()
            system.run("pg_dump", "-Fc", "-d", name, "-f", target + ".part", env=env)
            seconds = max(time.monotonic() - started, 0.001)
            system.path(target + ".part").replace(system.path(target))
            megabytes = system.path(target).stat().st_size / 1048576
            # A small one in KB, so it does not read 0.0 MB
            shown = f"{megabytes / 1024:.1f} GB" if megabytes >= 1024 else f"{megabytes:.1f} MB" if megabytes >= 0.1 else f"{megabytes * 1024:.0f} KB"
            rows.append([str(len(rows) + 1), name, shown,
                         f"{seconds:.1f} s", f"{megabytes / seconds:.1f} MB/s"])
        if rows:
            system.say(table(["Nr", "Database", "Size", "Time", "Speed"], rows, right={2, 3, 4}))
        else:
            system.say("No databases of their own to back up.")
    except Exception:
        system.warn("PostgreSQL backup failed; inspect the preceding error. No successful completion was recorded.")
        raise
    system.say(f"Backup of everything done! ----- {stamp(zone)} -----")
    system.say("")
