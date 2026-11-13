"""Harbor updates itself from its releases on GitHub (cutover step 2c of dev/HARBOR.md).

  REPOSITORY           where updates come from: VNS Harbor's repository (since VNS.Linux 1.3.0)
  <install>/REPOSITORY where the installed code came from (schlagerdk/VNS.Linux for VNS 1.x)

An update is finished by the code it brings, never by the code that runs it: `self update` fetches the release and runs
`harbor self finish-update` from the fetched copy. An updater replaced by the update it runs is what kept 1.2.0 from
moving a server on 1.1.x over; this way a release always installs itself as it means to.

When the installed code came from another repository (VNS 1.x), any release in REPOSITORY is newer, as VNS Harbor
starts its own versions. Without a release there, nothing is updated: never a branch.
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
LEGACY = "schlagerdk/VNS.Linux"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def installed_repository(root=INSTALL):
    path = Path(root) / "REPOSITORY"
    found = path.read_text().strip() if path.is_file() else ""
    return found or LEGACY


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
    """The newest release (or ref, such as v1.0.0) fetched and installed by its own code"""
    if ref and not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", ref):
        raise Stop(f"Not a release: {ref}")
    if not ref:
        found = newer(release_versions(system))
        if not found:
            versions = release_versions(system)
            raise Stop("There is no release to update to yet." if not versions else f"Harbor is up to date ({installed_version() or versions[0]}).")
        ref = "v" + found[0]
    folder = Path(tempfile.mkdtemp(prefix="harbor-update-"))
    try:
        system.step(f"Fetch {ref} from github.com/{REPOSITORY}")
        system.run("git", "clone", "--quiet", "--depth", "1", "--branch", ref, f"https://github.com/{REPOSITORY}.git", str(folder / "source"))
        source = folder / "source"
        revision = system.output("git", "-C", str(source), "rev-parse", "HEAD", check=False)
        (source / ".vns-version").write_text(revision + "\n")
        if not (source / "bin/harbor").is_file():
            raise Stop(f"{ref} has no bin/harbor: it is not a release of Harbor.")
        system.step(f"Install {ref}")
        if not (source / "harbor/install/update.py").is_file():
            # A release from before 1.3.0 cannot finish itself: it is installed as its own update.sh did
            finish_update(system, source)
            return
        # The fetched code finishes the update, as it means to
        result = system.run("python3", str(source / "bin/harbor"), "self", "finish-update", str(source), check=False)
        if not result.ok:
            raise Stop("", result.returncode or 1)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def admin_site(system):
    """The admin site on this server (the service that runs /var/www/<site>/app with gunicorn)"""
    services = system.path("/etc/systemd/system")
    for path in sorted(services.glob("*.service")) if services.is_dir() else []:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^WorkingDirectory=/var/www/([^/\n]+)/app$", text)
        if match and "gunicorn" in text:
            return match[1]
    return ""


def finish_update(system, source):
    """This release, installed from a copy of it (run from that copy by `self update`). VNS 1.x still lives in /vns:
    its update.sh puts it there and installs Harbor, and the admin site gets the new code."""
    source = Path(source)
    if (source / "update.sh").is_file():
        # With the source given, update.sh copies it to /vns and installs Harbor; it never fetches again
        system.run("bash", str(source / "update.sh"), str(source))
        admin = admin_site(system)
        if admin:
            system.step(f"Update the admin site {admin}")
            system.run("bash", "/vns/apache/update_admin_site.sh", admin)
        else:
            system.say("No admin site was found to update.")
        return
    raise Stop("This release has no installer for this server yet.")


def describe(system):
    system.say(json.dumps(check(system), indent=2))
