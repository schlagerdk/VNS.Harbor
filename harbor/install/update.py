"""Harbor updates itself from its releases on GitHub (cutover step 2c of dev/HARBOR.md).

  REPOSITORY           where updates come from: VNS Harbor's repository
  <install>/REPOSITORY where the installed code came from

An update is finished by the code it brings, never by the code that runs it: `self update` fetches the release and runs
`harbor self finish-update` from the fetched copy. An updater replaced by the update it runs is what kept 1.2.0 from
moving a server on 1.1.x over; this way a release always installs itself as it means to.

When the installed code came from another repository (the scripts in /vns before Harbor 1.0), any release in
REPOSITORY is newer, as VNS Harbor starts its own versions. Without a release there, nothing is updated: never a branch.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from harbor.core.paths import INSTALL
from harbor.core.system import Stop

# HARBOR_UPDATE_REPOSITORY points a test at another repository
REPOSITORY = os.environ.get("HARBOR_UPDATE_REPOSITORY") or "schlagerdk/VNS.Harbor"
# The installed code came from somewhere else when it does not say where
OTHER = "other"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def installed_repository(root=INSTALL):
    path = Path(root) / "REPOSITORY"
    found = path.read_text().strip() if path.is_file() else ""
    return found or OTHER


def installed_version(root=INSTALL):
    path = Path(root) / "VERSION"
    found = path.read_text().strip() if path.is_file() else ""
    return found if VERSION.fullmatch(found) else ""


def key(version):
    return tuple(int(part) for part in version.split("."))


def release_versions(system, repository=REPOSITORY):
    """The released versions, newest first, from the repository's tags (git, not GitHub's API and its limits)"""
    result = system.run("git", "ls-remote", "--tags", "--refs", f"https://github.com/{repository}.git", check=False, capture=True)
    if not result.ok:
        raise Stop(f"Could not read the releases of {repository} on GitHub: {(result.stderr or result.stdout).strip()}")
    versions = [line.split("refs/tags/v", 1)[1] for line in result.stdout.splitlines() if "refs/tags/v" in line]
    return sorted({version for version in versions if VERSION.fullmatch(version)}, key=key, reverse=True)


def newer(versions, installed=None, from_repository=None):
    """The versions that are an update: all of them when the installed code came from another repository"""
    installed = installed_version() if installed is None else installed
    from_repository = installed_repository() if from_repository is None else from_repository
    if from_repository != REPOSITORY or not installed:
        return list(versions)
    return [version for version in versions if key(version) > key(installed)]


def check(system):
    """{installed, repository, latest, newer} for the admin's update check"""
    versions = release_versions(system)
    return {"installed": installed_version(), "from": installed_repository(), "repository": REPOSITORY,
            "latest": versions[0] if versions else "", "newer": newer(versions)}


def update(system, ref=""):
    """The newest release (or ref, such as v1.0.0) fetched and installed by its own code (its self finish-update)"""
    if ref and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", ref):
        raise Stop(f"Not a release: {ref}")
    if not ref:
        found = newer(release_versions(system))
        if not found:
            versions = release_versions(system)
            raise Stop("There is no release to update to yet." if not versions else f"Harbor is up to date ({installed_version() or versions[0]}).")
        ref = "v" + found[0]
    folder = Path(tempfile.mkdtemp(prefix="harbor-update-"))
    # The fetched code's own steps come under Install
    system.plan([f"Fetch {ref} from github.com/{REPOSITORY}", f"Install {ref}"])
    try:
        system.step(f"Fetch {ref} from github.com/{REPOSITORY}")
        system.run("git", "clone", "--quiet", "--depth", "1", "--branch", ref, f"https://github.com/{REPOSITORY}.git", str(folder / "source"))
        source = folder / "source"
        revision = system.output("git", "-C", str(source), "rev-parse", "HEAD", check=False)
        (source / ".vns-version").write_text(revision + "\n")
        if not (source / "bin/harbor").is_file():
            raise Stop(f"{ref} has no bin/harbor: it is not a release of Harbor.")
        system.step(f"Install {ref}")
        # The fetched code finishes the update, as it means to
        result = system.run("python3", str(source / "bin/harbor"), "self", "finish-update", str(source), check=False)
        if not result.ok:
            raise Stop("", result.returncode or 1)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def finish_update(system, source):
    """This release, installed from a copy of it (run from that copy by `self update`): the release made the one in use,
    then the rest done by the installed release itself (`self after-update`), so what it writes down names
    /opt/harbor/current and never the copy, which is removed"""
    from harbor.install import release
    system.plan(["Install the release in /opt/harbor"])
    system.step("Install the release in /opt/harbor")
    release.install(system, source)
    result = system.run("python3", f"{release.CURRENT}/bin/harbor", "self", "after-update", check=False)
    if not result.ok:
        raise Stop("", result.returncode or 1)


def attempt(system, text, *argv):
    """A step that is reported when it fails, and the update goes on. Whether it worked."""
    if system.run(*argv, check=False).ok:
        return True
    system.warn(text)
    return False


def after_update(system):
    """The server brought up to the release in use: what update.sh and update_admin_site.sh did for VNS 1.x, and the
    move from /vns (cutover step 4). Each step is harmless to repeat."""
    from harbor.core.paths import BIN, LIB
    from harbor.install import admin
    from harbor.install.release import COMMAND
    from harbor.server.init import INITIALIZED
    harbor = str(BIN)
    worked = []
    sites = admin.admin_sites(system)
    system.plan(["Update the server-wide Apache configs", "Bring the sites up to date", *(f"Update the admin site {name}" for name in sites)])
    system.step("Update the server-wide Apache configs")
    # Apache's own name, so it does not warn that it cannot tell it (harbor server init sets it on a new server)
    worked.append(attempt(system, "Apache's ServerName was not set.", "python3", harbor, "server", "set-servername"))
    worked.append(attempt(system, "The shared TLS policy was not installed.", "python3", harbor, "server", "install-tls-policy"))
    worked.append(attempt(system, "The server-wide Apache configs were not installed.", "python3", harbor, "server", "install-apache-conf"))
    if system.path("/etc/cron.d/vns-keep-alive").is_file() or system.path(INITIALIZED).is_file():
        # The keep-alive job runs the release in use, no longer /vns
        worked.append(attempt(system, "The keep-alive job was not updated.", "python3", f"{LIB}/keep_alive.py", "--install"))
    system.step("Bring the sites up to date")
    worked.append(attempt(system, "Harbor's copy of the sites' state was not updated.", "python3", harbor, "state", "sync"))
    worked.append(attempt(system, "Not every site was migrated: sudo harbor migrate --dry-run tells why.", "python3", harbor, "migrate"))
    worked.append(attempt(system, "The sa password was not kept in Harbor.", "python3", harbor, "sql", "import-sa-password"))
    worked.append(attempt(system, "The backup jobs were not changed to harbor commands.", "python3", harbor, "server", "cron", "upgrade"))
    # The certificates acme.sh kept in /root/.acme.sh (before 1.0.67) moved to where its cron job renews them
    worked.append(attempt(system, "Not every certificate was moved to where acme.sh renews it: sudo harbor server adopt-certificates --dry-run tells why.",
                          "python3", harbor, "server", "adopt-certificates"))
    # Harbor's image signing key (1.0.46) taken off every service: it broke sites' images made by hand. Not restarted
    attempt(system, "Harbor's image signing key was not taken off every site's service.", "python3", f"{LIB}/harden_service.py", "--all")
    # Every server an environment runs on is in Servers: one made before that was needed is added (its own step, as
    # it must not hold the rest of the update)
    attempt(system, "The servers the environments run on were not all put in Servers: sudo harbor servers adopt tells why.", "python3", harbor, "servers", "adopt")
    move_sql_ready(system)
    # VNS 1.x stays in /vns (its custom scripts are there), but its harbor is the one in use: anything that still calls
    # /vns/bin/harbor runs this release
    legacy = system.path("/vns/bin/harbor")
    if legacy.exists() and not legacy.is_symlink():
        legacy.unlink()
        legacy.symlink_to(COMMAND)
        system.say(f"/vns/bin/harbor runs {COMMAND} now.")
    for name in sites:
        system.step(f"Update the admin site {name}")
        admin.update(system, name)
    if not sites:
        system.say("No admin site was found to update.")
    # /vns goes only when all of the update worked: else it stays for the next update to try again
    if all(worked):
        remove_vns(system)
    elif system.path(VNS).is_dir():
        system.warn(f"{VNS} stays until an update has worked in full: see what failed above.")


# ---- /vns, where VNS 1.x lived (phase 7)
VNS = "/vns"
# Where something the server runs could still name /vns: cron, services, Apache
NAMED_IN = ("/etc/cron.d", "/etc/crontab", "/var/spool/cron/crontabs", "/etc/systemd/system", "/etc/apache2/sites-available",
            "/etc/apache2/conf-available")
NAMES_VNS = re.compile(r"(?<![\w./-])/vns(?=/|\s|[\"']|$)", re.M)


def without_comments(text):
    """A config's lines without its comments: a comment that names /vns runs nothing"""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", ";")))


def names_vns(system):
    """What the server runs that still names /vns: files in NAMED_IN, and root's crontab"""
    found = []
    for place in NAMED_IN:
        top = system.path(place)
        for path in ([top] if top.is_file() else sorted(top.rglob("*")) if top.is_dir() else []):
            try:
                if path.is_file() and NAMES_VNS.search(without_comments(path.read_text(errors="replace"))):
                    found.append("/" + str(path.relative_to(system.path("/"))))
            except OSError:
                continue
    table = system.run("crontab", "-l", check=False, capture=True)
    if table.ok and NAMES_VNS.search(without_comments(table.stdout)):
        found.append("root's crontab")
    return found


def started_from_vns(system):
    """Whether this update was started by harbor in /vns (the admin of VNS 1.x): its process waits for the update to
    finish, and needs its code until then"""
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return False
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            parent = next((line.split()[1] for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("PPid:")), "1")
        except OSError:
            return False
        if re.search(r"(?<![\w./-])/vns/", command):
            return True
        pid = int(parent)
    return False


# The check before a site starts that its SQL Server answers, as the services run it
SQL_READY = "/opt/harbor/current/lib/sql_ready.py"
OLD_SQL_READY = re.compile(r'(?<=ExecStartPre=/usr/bin/python3 )"?(?:/vns/apache/lib|/opt/harbor/releases/[^/"\s]+/lib)/sql_ready\.py"?')


def move_sql_ready(system):
    """The sites' services run the SQL check from the release in use, no longer from /vns or from one release. It
    counts from the site's next start: nothing is restarted."""
    moved = []
    folder = system.path("/etc/systemd/system")
    for service in sorted(folder.glob("*.service")) if folder.is_dir() else []:
        if service.is_symlink() or not service.is_file():
            continue
        text = service.read_text(errors="replace")
        updated = OLD_SQL_READY.sub(f'"{SQL_READY}"', text)
        if updated != text:
            service.write_text(updated)
            moved.append(service.name)
    if moved:
        system.say(f"The SQL check before a site starts runs from {SQL_READY} in {len(moved)} service(s).")
        system.run("systemctl", "daemon-reload", check=False)


def remove_vns(system):
    """/vns goes once nothing uses it: backed up first (/var/lib/harbor/backups/vns-<time>.tar.gz, root only). It stays,
    and why is told, while cron, a service or Apache still names it, or while it holds the sa password Harbor lacks."""
    from datetime import datetime
    from harbor.sql.databases import SA_SECRET
    from harbor.sql.server import sa_scripts
    from harbor.state.store import read_json
    folder = system.path(VNS)
    if not folder.is_dir() or folder.is_symlink():
        return
    if started_from_vns(system):
        system.say(f"{VNS} goes at the next update: this one was started from it, which needs its code until it ends.")
        return
    found = names_vns(system)
    if found:
        system.warn(f"{VNS} stays: it is still named in {', '.join(found)}. Move what is there, then update again.")
        return
    if not (read_json(system.path(SA_SECRET)) or {}).get("sa_password") and (folder / "sql").is_dir():
        holding = [path for path in sa_scripts(system, folder / "sql") if "'@SA_PASSWORD@'" not in path.read_text(errors="replace")]
        if holding:
            system.warn(f"{VNS} stays: its scripts hold the sa password, which Harbor does not have yet "
                        "(harbor sql import-sa-password).")
            return
    system.plan([f"Remove {VNS}: VNS 1.x is no longer used"])
    system.step(f"Remove {VNS}: VNS 1.x is no longer used")
    backups = system.path("/var/lib/harbor/backups")
    backups.mkdir(parents=True, exist_ok=True)
    backups.chmod(0o700)
    archive = f"/var/lib/harbor/backups/vns-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    if not system.run("tar", "-czf", archive, "-C", "/", VNS.strip("/"), check=False).ok:
        system.warn(f"{VNS} stays: it could not be backed up first.")
        return
    if system.path(archive).is_file():
        system.path(archive).chmod(0o600)
    shutil.rmtree(folder)
    system.say(f"{VNS} is removed. Its backup: {archive}")


def describe(system):
    system.say(json.dumps(check(system), indent=2))
