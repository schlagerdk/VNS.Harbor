"""Harbor's own copy of the state that is in the site folders today. Phase 2 of dev/HARBOR.md.

  /var/lib/harbor/sites/<site>/environments/<id>.json   an environment's settings, without its passwords
  /var/lib/harbor/sites/<site>/data_sync.json           Sync setup
  /var/lib/harbor/sites/<site>/publish/<scope>.json     publish status and package update checks
  /etc/harbor/secrets/sites/<site>/<id>.json            an environment's passwords (folders 0700, files 0600)

While the generated scripts in a site folder exist they read their settings from themselves, so the site folder is the
truth: sync() makes this copy equal to it, and takes away what is gone there. Phase 6 makes this the truth instead.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile

from harbor.core.templates import Site
from harbor.environments import settings

STATE = "/var/lib/harbor/sites"
SECRETS = "/etc/harbor/secrets/sites"
SECRET_KEYS = ("PASSWORD", "SMB_PASSWORD")


def write_json(path, data, private=False):
    """Written whole or not at all; a secret only readable by root"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text() == text:
        return False
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def site_state(system, name):
    return system.path(f"{STATE}/{name}")


def site_secrets(system, name):
    return system.path(f"{SECRETS}/{name}")


def mirror_folder(source, target, pattern="*.json"):
    """target holds the files source has, equal, and nothing else"""
    names = {path.name for path in sorted(Path(source).glob(pattern)) if path.is_file()} if Path(source).is_dir() else set()
    for name in names:
        write_json(target / name, json.loads((Path(source) / name).read_text()))
    if target.is_dir():
        for path in target.glob(pattern):
            if path.name not in names:
                path.unlink()
        if not any(target.iterdir()):
            target.rmdir()


def sync(system, name):
    """Harbor's copy of one site's state, made equal to its folder. Returns what was not readable, to be told."""
    site = Site(name)
    folder = system.path(site.site_root)
    state, secrets = site_state(system, name), site_secrets(system, name)
    if not folder.is_dir():
        shutil.rmtree(state, ignore_errors=True)
        shutil.rmtree(secrets, ignore_errors=True)
        return []
    problems = []
    if (state / "migrated.json").is_file():
        # Migrated (phase 6): the copy is the truth, there is nothing in the site folder to make it from
        return []
    # Only what the site folder has: the copy is made from it, never from itself
    ids = settings.script_ids(system, site.site_root)
    kept = set()
    for target_id in ids:
        try:
            values = settings.parse(system.path(settings.settings_script(site.site_root, target_id)).read_text())
        except (OSError, settings.SettingsError) as error:
            # Left as it was here: the script may be fixed by hand
            problems.append(f"{name} {target_id}: {error}")
            kept.add(target_id)
            continue
        kept.add(target_id)
        write_json(state / "environments" / f"{target_id}.json", {key: value for key, value in values.items() if key not in SECRET_KEYS})
        hidden = {key: values[key] for key in SECRET_KEYS if values.get(key)}
        if hidden:
            write_json(secrets / f"{target_id}.json", hidden, private=True)
        else:
            (secrets / f"{target_id}.json").unlink(missing_ok=True)
    for folder_name, root in (("environments", state), ("", secrets)):
        place = root / folder_name if folder_name else root
        if place.is_dir():
            for path in place.glob("*.json"):
                if path.stem not in kept:
                    path.unlink()
    sync_setup = folder / "scripts/data_sync.json"
    if sync_setup.is_file() and read_json(sync_setup) is not None:
        write_json(state / "data_sync.json", read_json(sync_setup))
    elif sync_setup.is_file():
        problems.append(f"{name}: scripts/data_sync.json is not valid JSON")
    else:
        (state / "data_sync.json").unlink(missing_ok=True)
    try:
        mirror_folder(folder / ".vns-publish", state / "publish")
    except ValueError as error:
        problems.append(f"{name}: .vns-publish: {error}")
    # Only root may look into the secrets, all the way down
    for place in (system.path("/etc/harbor/secrets"), system.path(SECRETS), secrets):
        if place.is_dir():
            place.chmod(0o700)
    # Nothing left: no folder of its own
    for place in (state / "environments", state, secrets):
        if place.is_dir() and not any(place.iterdir()):
            place.rmdir()
    return problems


def sync_all(system):
    """Every site in /var/www, and the state of a site that is no longer there goes"""
    web = system.path(Site("x").root_path)
    names = sorted(path.name for path in web.iterdir() if path.is_dir() and not path.is_symlink()) if web.is_dir() else []
    problems = []
    for name in names:
        problems += sync(system, name)
    for root in (system.path(STATE), system.path(SECRETS)):
        if root.is_dir():
            for path in root.iterdir():
                if path.name not in names:
                    shutil.rmtree(path, ignore_errors=True)
    return problems


def sync_command(system, name=""):
    problems = sync(system, name) if name else sync_all(system)
    for problem in problems:
        system.warn(f"Not synced: {problem}")
