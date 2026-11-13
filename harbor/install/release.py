"""Harbor installed as releases (phase 3a of dev/HARBOR.md).

  /opt/harbor/releases/<version>/   bin, harbor, apache and sql, as a copy of the repository has them; never changed
  /opt/harbor/current               the release in use
  /usr/local/bin/harbor             current/bin/harbor

A release is copied whole into a folder of its own first and only then named, and current is changed in one step, so a
server never runs half a release. The three newest are kept, so an update can be undone (rollback).
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from harbor.core.system import Stop

ROOT = "/opt/harbor"
RELEASES = f"{ROOT}/releases"
CURRENT = f"{ROOT}/current"
COMMAND = "/usr/local/bin/harbor"
KEEP = 3
# What Harbor runs; the rest of a repository (tests, dev tools, the NuGet package) is not part of a release
PARTS = ("bin", "harbor", "apache", "sql", "VERSION", "REPOSITORY")
LEFT_OUT = ("tests", "__pycache__", "*.pyc", ".DS_Store")


def describe(source):
    """(version, revision) of a copy of the repository: VERSION, and the commit it is (.vns-version, or git)"""
    source = Path(source)
    version = (source / "VERSION").read_text().strip() if (source / "VERSION").is_file() else "0.0.0"
    revision = ""
    marker = source / ".vns-version"
    if marker.is_file():
        revision = marker.read_text().strip().split()[0] if marker.read_text().strip() else ""
    if not revision:
        found = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True)
        revision = found.stdout.strip() if found.returncode == 0 else ""
    if not re.fullmatch(r"[0-9.]+", version):
        raise Stop(f"Not a version: {version}")
    return version, revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else ""


def release_name(version, revision):
    return f"{version}-{revision[:12]}" if revision else version


def releases(system):
    """Every installed release, newest first: {name, version, revision, installed, current}"""
    folder = system.path(RELEASES)
    current = os.path.basename(os.readlink(system.path(CURRENT))) if os.path.islink(system.path(CURRENT)) else ""
    found = []
    for path in folder.iterdir() if folder.is_dir() else []:
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            info = json.loads((path / "release.json").read_text())
        except (OSError, ValueError):
            info = {}
        found.append({"name": path.name, "version": info.get("version", ""), "revision": info.get("revision", ""),
                      "installed": info.get("installed", ""), "current": path.name == current})
    return sorted(found, key=lambda item: (item["installed"], item["name"]), reverse=True)


def link(system, path, target):
    """path points at target, changed in one step"""
    path = system.path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new")
    if os.path.lexists(temporary):
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, path)


def install(system, source, keep=KEEP):
    """A release from a copy of the repository, made the one in use. Installing the same commit again only uses it."""
    source = Path(source)
    if not (source / "bin/harbor").is_file() or not (source / "harbor/cli.py").is_file():
        raise Stop(f"{source} is not a copy of Harbor.")
    version, revision = describe(source)
    name = release_name(version, revision)
    target = system.path(f"{RELEASES}/{name}")
    # A working copy deployed for testing (dev/deploy.sh) has changes the commit does not: it is always copied again
    working_copy = (source / ".vns-dev").is_file()
    if working_copy:
        name += "-dev"
        target = system.path(f"{RELEASES}/{name}")
    if not target.is_dir() or not revision or working_copy:
        # Copied whole into a folder of its own first, then named: a release is there completely or not at all
        staging = system.path(f"{RELEASES}/.{name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for part in PARTS:
            if (source / part).is_dir():
                shutil.copytree(source / part, staging / part, symlinks=True, ignore=shutil.ignore_patterns(*LEFT_OUT))
            elif (source / part).is_file():
                shutil.copy2(source / part, staging / part)
        (staging / "release.json").write_text(json.dumps({"version": version, "revision": revision,
                                                          "installed": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2) + "\n")
        for path in [staging, *staging.rglob("*")]:
            if not path.is_symlink():
                mode = path.stat().st_mode
                # Readable for everyone, changed by nobody but root
                path.chmod((mode | 0o444 | (0o111 if path.is_dir() or mode & 0o100 else 0)) & ~0o022)
        if target.is_dir():
            shutil.rmtree(target)
        os.replace(staging, target)
        system.say(f"Release {name} installed in {RELEASES}")
    link(system, CURRENT, f"releases/{name}")
    link(system, COMMAND, f"{CURRENT}/bin/harbor")
    system.say(f"Harbor {name} is in use ({CURRENT}).")
    prune(system, keep)
    return name


def use(system, name, keep=KEEP):
    """A release that is installed made the one in use (a release copied here from another server)"""
    if not name or "/" in name or not system.path(f"{RELEASES}/{name}/bin/harbor").is_file():
        raise Stop(f"No release {name} in {RELEASES}.")
    link(system, CURRENT, f"releases/{name}")
    link(system, COMMAND, f"{CURRENT}/bin/harbor")
    system.say(f"Harbor {name} is in use ({CURRENT}).")
    prune(system, keep)


def prune(system, keep=KEEP):
    """Only the newest releases stay, and always the one in use"""
    for item in releases(system)[keep:]:
        if not item["current"]:
            shutil.rmtree(system.path(f"{RELEASES}/{item['name']}"), ignore_errors=True)
            system.say(f"Release {item['name']} removed")


def rollback(system):
    """The release installed before the one in use is used again"""
    found = releases(system)
    current = next((index for index, item in enumerate(found) if item["current"]), None)
    if current is None:
        raise Stop("No release of Harbor is in use.")
    if current + 1 >= len(found):
        raise Stop("There is no earlier release to go back to.")
    earlier = found[current + 1]["name"]
    link(system, CURRENT, f"releases/{earlier}")
    system.say(f"Harbor {earlier} is in use again ({CURRENT}).")


def version(system):
    found = next((item for item in releases(system) if item["current"]), None)
    from harbor.core.paths import INSTALL
    system.say(f"Harbor {found['name']} ({CURRENT})" if found else f"Harbor runs from {INSTALL} (no release installed)")
