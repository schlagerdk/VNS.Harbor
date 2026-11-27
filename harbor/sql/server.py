"""SQL Server and its command-line tools on this server: install, update, remove, and the sa password the scripts in
sql/ use. From sql/install_server.sh, update_server.sh, uninstall_server.sh, install_tools.sh, uninstall_tools.sh and
set_sa_password.sh. They run as root (their wrappers ask sudo), so no step needs sudo of its own.

The sa password stays where the scripts kept it: on the "-U sa -P '...'" line of each script in sql/ that uses it,
where updates find it and keep it (sql/lib/sa_password.sh). Only that value changes.
"""
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from harbor.core.system import Stop
from harbor.sql.databases import BACKUPS, SQL_DIR, SQLCMD, prepare_backup_permissions

BASE_URL = "https://packages.microsoft.com"
# Newest first. The first version with a repository for this Ubuntu version is used.
SQL_VERSIONS = ("2025", "2022", "2019")
FALLBACK_UBUNTU = "24.04"
# The Ubuntu versions Microsoft publishes SQL Server for
SUPPORTED_UBUNTU = ("22.04", "24.04")
LIBLDAP = "https://deb.debian.org/debian/pool/main/o/openldap/libldap-2.5-0_2.5.13+dfsg-5_amd64.deb"
# Microsoft signs newer repositories (e.g. Ubuntu 26.04) with a different key than older ones, so both are added to
# the same keyring
KEYS = r'''set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
for key in microsoft.asc microsoft-2025.asc; do
	curl -fsSL "https://packages.microsoft.com/keys/${key}" | gpg --dearmor >"${tmp}/${key}.gpg"
done
cat "${tmp}"/*.gpg >/usr/share/keyrings/microsoft-prod.gpg
'''
SA_LINE = re.compile(r"(-U sa -P )'(?:[^']*'\\'')*[^']*'")


EDITIONS = ("Developer", "Express", "Evaluation", "Standard", "Enterprise")
# The admin gives the sa password in the task's environment, never on its command line
SA_VARIABLE = "HARBOR_SA_PASSWORD"


def yes(answer):
    return answer.strip().lower() in ("y", "yes")


def confirm(system, prompt, given=None):
    """The answer given as an option (the admin), else asked in the terminal"""
    return given if given is not None else yes(system.ask(prompt))


def os_release(system):
    values = {}
    for line in system.path("/etc/os-release").read_text().splitlines():
        key, _, value = line.partition("=")
        values[key] = value.strip().strip('"')
    return values


def ubuntu(system):
    release = os_release(system)
    if release.get("ID") != "ubuntu":
        raise Stop(f"This script supports Ubuntu only (found: {release.get('ID', '')}).")
    return release.get("VERSION_ID", "")


def installed(system, package):
    return system.run("dpkg", "-s", package, check=False, capture=True).ok


def microsoft_keys(system):
    if not system.which("gpg"):
        system.run("apt-get", "install", "-y", "gnupg")
    system.run("bash", "-c", KEYS)


def drop_duplicate_list(system):
    """Harbor's mssql-release.list taken away when Microsoft's own packages-microsoft-prod has the same repository in
    microsoft-prod.list (its 1.2 does): apt warns of every source named twice. True when it was."""
    ours, theirs = system.path("/etc/apt/sources.list.d/mssql-release.list"), system.path("/etc/apt/sources.list.d/microsoft-prod.list")
    lines = lambda path: {line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")}
    try:
        if ours.is_file() and theirs.is_file() and lines(ours) and lines(ours) <= lines(theirs):
            ours.unlink()
            return True
    except OSError:
        pass
    return False


def install_tools(system, version_id):
    """mssql-tools18 (sqlcmd) from Microsoft's repository for this Ubuntu"""
    system.plan(["Add Microsoft tools repository", "Install mssql-tools18"])
    system.step("Add Microsoft tools repository")
    listing = system.run("curl", "-fsSL", f"{BASE_URL}/config/ubuntu/{version_id}/prod.list", capture=True).stdout
    system.path("/etc/apt/sources.list.d/mssql-release.list").write_text(listing)
    drop_duplicate_list(system)
    system.step("Install mssql-tools18")
    system.run("apt-get", "update")
    system.run("env", "ACCEPT_EULA=Y", "apt-get", "install", "-y", "mssql-tools18", "unixodbc-dev")
    system.say("Add sqlcmd to PATH for all users")
    system.path("/etc/profile.d/mssql-tools.sh").write_text('export PATH="$PATH:/opt/mssql-tools18/bin"\n')
    if not system.path(SQLCMD).is_file():
        system.warn(f"Installation finished, but {SQLCMD} was not found.")
        system.run("apt-cache", "policy", "mssql-tools18", check=False)
        raise Stop("", 1)
    system.say("sqlcmd installed - open a new shell to get it on PATH")


def install_sql_tools(system):
    """Install SQL client tools on the admin server, without installing SQL Server"""
    version_id = ubuntu(system)
    microsoft_keys(system)
    install_tools(system, version_id)


def stop_backups(system, why):
    """The backup and log shrink jobs out of root's crontab: they need SQL Server and sqlcmd here (Server tools turns
    them on again once both are)"""
    from harbor.server.tools import JOBS, crontab, job_lines
    if any(job_lines(crontab(system), name) for name in JOBS):
        system.say(f"The backups stop: {why}.")
        from harbor.server.tools import cron
        cron(system, False)


def uninstall_sql_tools(system, assume_yes=None):
    """Remove SQL client tools without removing SQL Server or shared ODBC packages"""
    packages = [package for package in ("mssql-tools18", "mssql-tools") if installed(system, package)]
    if not packages:
        system.say("SQL command-line tools are not installed")
        return
    if not confirm(system, "Remove SQL command-line tools (sqlcmd)? [y/N]: ", assume_yes):
        system.say("SQL tools kept")
        return
    system.plan(["Remove SQL command-line tools"])
    system.step("Remove SQL command-line tools")
    system.run("apt-get", "remove", "--purge", "-y", *packages)
    system.path("/etc/profile.d/mssql-tools.sh").unlink(missing_ok=True)
    system.say("SQL tools removed. SQL Server, shared ODBC packages and Microsoft repositories are unchanged.")
    remove_admin_connections(system)
    stop_backups(system, "the SQL tools are gone")


# ---- the sa password in the scripts
def sa_scripts(system, folder=None):
    """The scripts in sql/ that hold the sa password"""
    folder = Path(folder or SQL_DIR)
    return sorted(path for path in Path(folder).rglob("*.sh") if path.is_file() and re.search(r"-U sa -P '.*' ", path.read_text(errors="replace")))


def sa_apply(password, folder=None):
    """The sa password in every script in sql/ that uses it: the placeholder after a fresh copy, or the one before"""
    folder = Path(folder or SQL_DIR)
    quoted = "'" + password.replace("'", "'\\''") + "'"
    for path in Path(folder).rglob("*.sh"):
        if path.is_file():
            text = path.read_text(errors="surrogateescape")
            changed = SA_LINE.sub(lambda match: match[1] + quoted, text)
            if changed != text:
                path.write_text(changed, errors="surrogateescape")


def save_sa_password(system, password):
    """The sa password in Harbor's secrets (root only): what the backups and database commands use once the scripts in
    sql/ that held it are gone"""
    from harbor.sql.databases import SA_SECRET
    from harbor.state.store import write_json
    path = system.path(SA_SECRET)
    write_json(path, {"sa_password": password}, private=True)
    for place in (system.path("/etc/harbor/secrets"), path.parent):
        place.chmod(0o700)
    update_admin_connections(system, password)


# The names SQL Server here is reached by, besides this server's own names and addresses
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "."}


def admin_connections(system):
    """(admin site, its file of SQL connections, the connections) for each admin site here that has any"""
    import json
    from harbor.install.admin import admin_sites, web_root
    for site in admin_sites(system):
        path = system.path(web_root()) / site / "app/sql-connections.json"
        try:
            items = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        except (OSError, ValueError):
            continue
        if isinstance(items, list) and items:
            yield site, path, items


def write_admin_connections(path, items):
    """As the admin writes them: whole or not at all, with the file's owner and mode"""
    import json
    import os
    info = path.stat()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".sql-connections-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(items, stream)
        os.chmod(temporary, info.st_mode & 0o777)
        try:
            os.chown(temporary, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_admin_connections(system, password):
    """The admin's SQL connections that log in as sa to the SQL Server here get its new sa password: each held a copy,
    which no longer matched once SQL Server was installed again or its password changed (1.0.69)"""
    names = set(LOCAL_HOSTS)
    for argv in (("hostname",), ("hostname", "-f"), ("hostname", "-I")):
        names.update(name.lower() for name in system.output(*argv, check=False).split())
    for site, path, items in admin_connections(system):
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host", "")).strip("[]").rstrip(".").lower()
            if (str(item.get("username", "")).lower() == "sa" and item.get("port", 1433) == 1433 and not item.get("ssh_enabled")
                    and host in names and item.get("password") != password):
                item["password"] = password
                changed = True
        if changed:
            write_admin_connections(path, items)
            system.say(f"The SQL connection of {site} to SQL Server here has the new sa password.")


def add_admin_connection(system, engine, name, port, username, password, backup_directory):
    """A connection to the server installed here in each admin site's connections, as its own login (sa, postgres): or
    the one there given the password, when there is one already. Microsoft SQL Server's and PostgreSQL's alike."""
    import json
    import uuid
    from harbor.install.admin import admin_sites, web_root
    names = set(LOCAL_HOSTS)
    for argv in (("hostname",), ("hostname", "-f"), ("hostname", "-I")):
        names.update(item.lower() for item in system.output(*argv, check=False).split())
    for site in admin_sites(system):
        path = system.path(web_root()) / site / "app/sql-connections.json"
        if not path.parent.is_dir():
            continue
        try:
            items = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        local = [item for item in items if isinstance(item, dict) and (item.get("engine") or "mssql") == engine
                 and str(item.get("host", "")).strip("[]").rstrip(".").lower() in names and item.get("port", port) == port
                 and str(item.get("username", "")).lower() == username]
        if local:
            for item in local:
                item["password"] = password
        else:
            items.append({"id": uuid.uuid4().hex, **({"engine": engine} if engine != "mssql" else {}), "name": name, "host": "localhost",
                          "port": port, "username": username, "password": password, "backup_directory": backup_directory,
                          "trust_certificate": True, "ssh_enabled": False})
        if not path.is_file():
            path.write_text("[]")
            path.chmod(0o600)
        write_admin_connections(path, items)
        system.say(f"The connection {name} of {site} to {'PostgreSQL' if engine == 'postgres' else 'Microsoft SQL Server'} here is {'updated' if local else 'added'}.")


def remove_admin_connections(system):
    """The admin's SQL connections go with the SQL tools: without sqlcmd none of them can be used (1.0.69)"""
    for site, path, items in admin_connections(system):
        write_admin_connections(path, [])
        system.say(f"The {len(items)} SQL connection{'' if len(items) == 1 else 's'} of {site} removed.")


def import_sa_password(system, folder=None):
    """The sa password from the scripts in sql/ (where set_sa_password.sh put it) into Harbor's secrets, once"""
    from harbor.sql.databases import SA_SECRET
    from harbor.state.store import read_json
    if (read_json(system.path(SA_SECRET)) or {}).get("sa_password"):
        system.say("Harbor has the sa password.")
        return
    for path in sa_scripts(system, folder):
        match = SA_LINE.search(path.read_text(errors="replace"))
        if not match:
            continue
        quoted = match[0][len(match[1]):]
        try:
            password = shlex.split(quoted)[0]
        except (ValueError, IndexError):
            continue
        if password and password != "@SA_PASSWORD@":
            save_sa_password(system, password)
            system.say("The sa password is in Harbor now.")
            return
    system.say("No sa password is set in the scripts: there is none to keep.")


def given_sa_password():
    import os
    password = os.environ.get(SA_VARIABLE, "")
    if password and len(password) < 8:
        raise Stop("SQL Server requires at least 8 characters")
    return password


def sa_prompt(system):
    if given_sa_password():
        return given_sa_password()
    while True:
        password = system.ask("Enter the sa password you set during SQL Server setup, so Harbor can use it: ", secret=True)
        confirm = system.ask("Repeat password: ", secret=True)
        if not password:
            system.warn("Password cannot be empty")
        elif password != confirm:
            system.warn("Passwords do not match")
        elif len(password) < 8:
            system.warn("SQL Server requires at least 8 characters")
        else:
            return password


def set_sa_password(system, folder=None, assume_yes=None):
    """The sa password in Harbor's secrets, after it was changed on the server. The scripts of VNS 1.x in /vns/sql that
    still hold it (the custom ones, such as clean_umbraco7_db.sh) get it too, while they are there."""
    folder = Path(folder or SQL_DIR)
    scripts = sa_scripts(system, folder) if folder.is_dir() else []
    if scripts:
        system.say("Scripts that use the sa password:\n" + "\n".join(f"  {path.relative_to(folder)}" for path in scripts) + "\n")
    password = sa_prompt(system)
    # The password goes in the environment, so it is not shown in the process list
    sqlcmd = SQLCMD if system.path(SQLCMD).is_file() else shutil.which("sqlcmd")
    if sqlcmd:
        if system.run(sqlcmd, "-C", "-S", "LOCALHOST", "-U", "sa", "-l", "10", "-b", "-Q", "SET NOCOUNT ON; SELECT 1",
                      env={"SQLCMDPASSWORD": password}, check=False, capture=True).ok:
            system.say("The password works on the local SQL Server.")
        elif not confirm(system, "The local SQL Server did not accept the password. Keep it anyway? [y/N]: ",
                         assume_yes if assume_yes is not None else (False if given_sa_password() else None)):
            system.say("Nothing was changed.")
            raise Stop("", 1)
    else:
        system.say("sqlcmd is not installed, so the password is not checked against the server.")
    save_sa_password(system, password)
    if scripts:
        sa_apply(password, folder)
        # The scripts now hold the password: only their owner and group may read them
        for path in scripts:
            path.chmod(path.stat().st_mode & ~0o007)
        system.say(f"The sa password is set in Harbor and in {len(scripts)} script(s). Others can no longer read them.")
    else:
        system.say("The sa password is set in Harbor.")


# ---- SQL Server
def find_sql_version(system, ubuntu_version):
    for version in SQL_VERSIONS:
        if system.run("curl", "-fsSI", f"{BASE_URL}/config/ubuntu/{ubuntu_version}/mssql-server-{version}.list", check=False, capture=True).ok:
            return version
    return ""


def install_server(system, edition="", with_tools=None, assume_yes=None, older_ubuntu=False):
    """The latest SQL Server available for this Ubuntu version, the sa password in the scripts, and sqlcmd if wanted.
    With an edition its setup asks nothing: the sa password comes from HARBOR_SA_PASSWORD (the admin)."""
    import os
    if edition and edition not in EDITIONS:
        raise Stop(f"Not an edition: {edition} ({', '.join(EDITIONS)})")
    if edition and not given_sa_password():
        raise Stop(f"Set the sa password in {SA_VARIABLE} to install without questions.")
    version_id = ubuntu(system)
    repository = os.environ.get("SQL_UBUNTU_VERSION") or version_id
    if not system.which("gpg"):
        system.run("apt-get", "install", "-y", "gnupg")
    system.plan(["Add Microsoft signing keys"])
    system.step("Add Microsoft signing keys")
    system.run("bash", "-c", KEYS)

    server_installed = False
    if installed(system, "mssql-server"):
        system.say("mssql-server is already installed (use update_server.sh to update it) - skipping server install")
    else:
        version = find_sql_version(system, repository)
        if not version:
            system.warn(f"Microsoft has no SQL Server repository for Ubuntu {repository}.")
            system.warn("Supported: Ubuntu 22.04 and 24.04 LTS.")
            # older_ubuntu: chosen in the admin, which says what it means
            if not confirm(system, f"Try the Ubuntu {FALLBACK_UBUNTU} repository instead (NOT supported by Microsoft, may fail)? [y/N]: ",
                           True if older_ubuntu else assume_yes if assume_yes is not None else (False if edition else None)):
                raise Stop("", 1)
            repository = FALLBACK_UBUNTU
            version = find_sql_version(system, repository)
            if not version:
                raise Stop(f"No repository found for Ubuntu {repository}")
        system.say(f"Installing SQL Server {version} using the Ubuntu {repository} repository (server runs Ubuntu {version_id})")
        # SQL Server 2025 requires libldap-2.5-0, which is not available in Ubuntu 24.04+
        libldap = version == "2025" and not system.run("apt-cache", "show", "libldap-2.5-0", check=False, capture=True).ok and not installed(system, "libldap-2.5-0")
        system.plan([*(["Install libldap-2.5-0"] if libldap else []), "Add SQL Server repository", "Install mssql-server",
                     *([f"Set up SQL Server ({edition})"] if edition else []), "Create backup folder"])
        if libldap:
            system.step("Install libldap-2.5-0")
            with tempfile.NamedTemporaryFile(suffix=".deb") as package:
                system.run("curl", "-fsSL", LIBLDAP, "-o", package.name)
                system.run("apt-get", "install", "-y", package.name)
        system.step("Add SQL Server repository")
        listing = system.run("curl", "-fsSL", f"{BASE_URL}/config/ubuntu/{repository}/mssql-server-{version}.list", capture=True).stdout
        system.path(f"/etc/apt/sources.list.d/mssql-server-{version}.list").write_text(listing)
        system.step("Install mssql-server")
        system.run("apt-get", "update")
        system.run("apt-get", "install", "-y", "mssql-server")
        if edition:
            system.step(f"Set up SQL Server ({edition})")
            system.run("/opt/mssql/bin/mssql-conf", "-n", "setup", "accept-eula",
                       env={"ACCEPT_EULA": "Y", "MSSQL_PID": edition, "MSSQL_SA_PASSWORD": given_sa_password()})
        else:
            # Runs interactively and asks for the sa password, among other things
            system.run("/opt/mssql/bin/mssql-conf", "setup")
        system.step("Create backup folder")
        prepare_backup_permissions(system)
        system.run("systemctl", "status", "mssql-server", "--no-pager", check=False)
        server_installed = True

    from harbor.sql.databases import SA_SECRET
    from harbor.state.store import read_json
    if server_installed or not (read_json(system.path(SA_SECRET)) or {}).get("sa_password"):
        password = sa_prompt(system)
        if SQL_DIR.is_dir():
            sa_apply(password)
        save_sa_password(system, password)

    if confirm(system, "Install SQL command-line tools (sqlcmd)? [y/N]: ", with_tools):
        if installed(system, "mssql-tools18"):
            system.say("mssql-tools18 is already installed (use update_server.sh to update it) - skipping tools install")
        else:
            install_tools(system, version_id)
    else:
        system.say("SQL tools installation skipped")
    # A connection to it in the admin, as sa, once its tools are here (none works without sqlcmd)
    from harbor.sql.databases import sa_password
    if server_installed and system.path(SQLCMD).is_file() and sa_password(system):
        add_admin_connection(system, "mssql", "Local", 1433, "sa", sa_password(system), BACKUPS)


def wait_for_logins(system, seconds=900):
    """Until SQL Server lets sa in: after an update it upgrades its databases first, and only it may log in meanwhile"""
    from harbor.sql.databases import sa_password
    if not system.path(SQLCMD).is_file():
        return
    password = sa_password(system)
    waited = 0
    while waited < seconds:
        if system.run(SQLCMD, "-C", "-S", "LOCALHOST", "-U", "sa", "-l", "5", "-Q", "SELECT 1", env={"SQLCMDPASSWORD": password},
                      check=False, capture=True).ok:
            system.say("SQL Server has upgraded its databases and lets logins in.")
            return
        if waited % 30 == 0:
            system.say("SQL Server is upgrading its databases (script upgrade mode): logins wait until it is done…")
        system.sleep(5)
        waited += 5
    system.warn(f"SQL Server did not let sa in within {seconds // 60} minutes. Look at /var/opt/mssql/log/errorlog.")


def update_server(system, assume_yes=None):
    """SQL Server (and sqlcmd) to the latest version in its repository. A new major version needs install_server."""
    if not installed(system, "mssql-server"):
        raise Stop("mssql-server is not installed - run install_server.sh first")
    system.say(f"Installed version: {system.output('dpkg-query', '-W', '-f=${Version}', 'mssql-server')}")
    system.plan(["Check for updates"])
    system.step("Check for updates")
    system.run("apt-get", "update")
    upgradable = system.run("apt", "list", "--upgradable", check=False, capture=True).stdout
    if not any(line.startswith("mssql-server/") for line in upgradable.splitlines()):
        system.say("SQL Server is already up to date")
    else:
        system.say("Update available. Take a backup first - e.g. harbor sql backup-everything")
        if not confirm(system, "Update now? The service is restarted. [y/N]: ", assume_yes):
            system.say("Aborted")
            raise Stop("", 1)
        system.run("apt-get", "install", "--only-upgrade", "-y", "mssql-server")
        # The package starts SQL Server itself, which then upgrades its databases ("script upgrade mode"). A restart
        # then hung: SQL Server does not stop while it upgrades, and systemd waits up to 30 minutes (kikayo). So it is
        # started only when it does not run, and waited for until it lets a login in.
        system.plan(["Start SQL Server and let it upgrade its databases"])
        system.step("Start SQL Server and let it upgrade its databases")
        if system.output("systemctl", "is-active", "mssql-server", check=False) != "active":
            system.run("systemctl", "start", "mssql-server")
        wait_for_logins(system)
    if installed(system, "mssql-tools18"):
        system.run("apt-get", "install", "--only-upgrade", "-y", "mssql-tools18")
    system.say(f"Version now: {system.output('dpkg-query', '-W', '-f=${Version}', 'mssql-server')}")
    system.run("systemctl", "status", "mssql-server", "--no-pager")


def uninstall_server(system, assume_yes=None, delete_data=None, with_tools=None):
    """SQL Server goes, then sqlcmd if wanted. Its data is kept unless DELETE is typed."""
    if installed(system, "mssql-server"):
        system.say("This will remove SQL Server (mssql-server). SQL tools are optional and can be kept.")
        if not confirm(system, "Continue? [y/N]: ", assume_yes):
            system.say("Aborted")
            raise Stop("", 1)
        system.plan(["Stop and disable service", "Remove mssql-server"])
        system.step("Stop and disable service")
        system.run("systemctl", "stop", "mssql-server", check=False)
        system.run("systemctl", "disable", "mssql-server", check=False)
        system.step("Remove mssql-server")
        system.run("apt-get", "remove", "--purge", "-y", "mssql-server")
        system.say("Remove SQL Server apt repositories and unused keys")
        for listing in system.path("/etc/apt/sources.list.d").glob("mssql-server-*.list"):
            listing.unlink()
        # The keys go only if no other repository uses them
        sources = [system.path("/etc/apt/sources.list"), *sorted(system.path("/etc/apt/sources.list.d").glob("*"))]
        if not any(path.is_file() and "microsoft-prod.gpg" in path.read_text(errors="replace") for path in sources):
            system.path("/usr/share/keyrings/microsoft-prod.gpg").unlink(missing_ok=True)
        system.run("apt-get", "update")
        system.run("apt-get", "autoremove", "-y")
        system.say("")
        system.say("Databases, logs and backups are stored in /var/opt/mssql")
        if delete_data is None:
            system.say("Type DELETE to permanently delete this folder, or press Enter to keep it.")
        if (system.ask("> ") == "DELETE") if delete_data is None else delete_data:
            shutil.rmtree(system.path("/var/opt/mssql"), ignore_errors=True)
            system.say("/var/opt/mssql deleted")
        else:
            system.say("/var/opt/mssql kept")
        system.say("SQL Server removed")
        stop_backups(system, "SQL Server is gone")
    else:
        system.say("mssql-server is not installed")
    # with_tools: sqlcmd removed too (True) or kept (False); asked when not given
    if with_tools is False:
        system.say("SQL tools kept")
    else:
        uninstall_sql_tools(system, with_tools)
