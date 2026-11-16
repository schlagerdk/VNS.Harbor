"""What has been synced between a site and its environments, and whether a site has changed since: the dashboard in
Umbraco shows when each kind was last synced, and what has not been sent yet.

  /var/lib/harbor/sites/<site>/sync-history.json   the newest syncs first: when, who, from where to where, what, and
                                                   a fingerprint of each kind as it was sent

A fingerprint covers what a sync of that kind takes from its source, as harbor/environments/sync.py does:
  settings  uSync's settings folders and usync.config in each uSync version folder, Views, wwwroot/css, wwwroot/scripts
  content   uSync's Content and Dictionary
  media     uSync's Media, and the files in wwwroot/media (by name, size and time: they are never rewritten)
The uSync files and the code are fingerprinted by their contents: uSync writes them again when a site starts, with the
same contents. A site has changes not synced to an environment when its fingerprint differs from the one sent there
last. That is only known for a site whose files are on this server: the main site, and environments in a folder here.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re

from harbor.state.store import read_json, write_json

KINDS = ("settings", "content", "media")
KEEP = 200
SETTINGS_FOLDERS = ("ContentTypes", "DataTypes", "Languages", "MediaTypes", "MemberTypes", "RelationTypes", "Templates")
CODE_FOLDERS = ("Views", "wwwroot/css", "wwwroot/scripts")
VERSION = re.compile(r"v[0-9]+|Root")


def path(system, site):
    from harbor.environments.settings import STATE
    return system.path(f"{STATE}/{site}/sync-history.json")


def entries(system, site):
    """The syncs of a site, newest first"""
    found = read_json(path(system, site))
    return [entry for entry in found if isinstance(entry, dict)] if isinstance(found, list) else []


def kinds_of(data):
    """The kinds a sync takes: settings, or the data it was given (content, media)"""
    return [kind for kind in (data or "").split(",") if kind in ("content", "media")] or ["settings"]


def record(system, site, source, destination, kinds, ok, prints, who=""):
    """One sync to one destination, kept with what was sent"""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "from": source, "to": destination,
             "kinds": list(kinds), "ok": bool(ok), "who": who or os.environ.get("HARBOR_SYNC_WHO", "")}
    if ok:
        entry["fingerprints"] = {kind: prints.get(kind) for kind in kinds if prints.get(kind)}
    # The sync itself has worked or not: a history that cannot be written is told, never a failed sync
    try:
        target = path(system, site)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, [entry, *entries(system, site)][:KEEP])
    except OSError as error:
        system.say(f"The sync was not written in Harbor's sync history: {error}")
    return entry


def _files(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(item for item in folder.rglob("*") if item.is_file() and not item.is_symlink())


def fingerprint(source, kind):
    """What a sync of this kind would take from source, in one value"""
    source = Path(source)
    by_contents, by_size = [], []
    usync = source / "uSync"
    for version in sorted(item.name for item in usync.iterdir() if item.is_dir()) if usync.is_dir() else []:
        if not VERSION.fullmatch(version):
            continue
        folders = {"settings": SETTINGS_FOLDERS, "content": ("Content", "Dictionary"), "media": ("Media",)}[kind]
        for name in folders:
            by_contents += _files(usync / version / name)
        if kind == "settings" and (usync / version / "usync.config").is_file():
            by_contents.append(usync / version / "usync.config")
    if kind == "settings":
        for name in CODE_FOLDERS:
            by_contents += _files(source / name)
    if kind == "media":
        by_size = _files(source / "wwwroot/media")
    digest = hashlib.sha256()
    for item in sorted(by_contents):
        digest.update(str(item.relative_to(source)).encode() + b"\0")
        try:
            digest.update(hashlib.sha256(item.read_bytes()).digest())
        except OSError:
            continue
    for item in sorted(by_size):
        try:
            stat = item.stat()
        except OSError:
            continue
        digest.update(f"{item.relative_to(source)}\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def fingerprints(source, kinds=KINDS):
    return {kind: fingerprint(source, kind) for kind in kinds}


def local_folder(system, site, site_id):
    """Where a site's files are on this server, or "" when they are elsewhere (another server, Windows)"""
    from harbor.environments import settings
    from harbor.environments.sync import Sync
    if site_id == "main":
        return Sync(system, site, "", False).main_folder()
    try:
        environment = settings.load(system, site.site_root, site_id)
    except settings.SettingsError:
        return ""
    return environment.root if environment.LOCAL_PATH and not environment.windows else ""


def settings_behind(system, site, destination, main_prints=None):
    """Why a site's settings are not those of the main site, or "": data is only merged into a site whose document
    types match (harbor env data-sync, and the sync buttons in the admin and the dashboard). site is the Site; the
    caller checks first that the destination may receive settings at all."""
    from harbor.environments.sync import display_name
    if main_prints is None:
        folder = local_folder(system, site, "main")
        main_prints = {"settings": fingerprint(system.path(folder), "settings")} if folder and system.path(folder).is_dir() else {}
    found = between(system, site.name, "main", destination, ["settings"], main_prints)["settings"]
    if found["changed"] is False:
        return ""
    main_name, destination_name = display_name(system, site, "main"), display_name(system, site, destination)
    if found["changed"]:
        return f"{main_name} has settings that are not synced to {destination_name}"
    # Never synced there as far as Harbor knows, or only failed: one settings sync makes it known
    return f"Settings have not been synced from {main_name} to {destination_name} yet"


def between(system, site, source, destination, kinds, now=None):
    """For each kind: the last sync from source to destination ({time, ok, who}), and whether source has changed
    since the last one that worked (True, False, or None when that is not known: never synced, or not on this server)"""
    found = {}
    history = [entry for entry in entries(system, site) if entry.get("from") == source and entry.get("to") == destination]
    for kind in kinds:
        last = next((entry for entry in history if kind in entry.get("kinds", [])), None)
        sent = next((entry for entry in history if kind in entry.get("kinds", []) and entry.get("ok")), None)
        current = (now or {}).get(kind)
        sent_print = (sent or {}).get("fingerprints", {}).get(kind)
        found[kind] = {"last": {key: last.get(key) for key in ("time", "ok", "who")} if last else None,
                       "changed": (current != sent_print) if current and sent_print else None}
    return found
