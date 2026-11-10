"""Sync between a main site and its environments: who may send and receive settings, content and media.

Each site, the main site ("main") and every environment (its id), has its own settings, saved in
<site>/scripts/data_sync.json on the server that manages the site:

  enabled  data sync is on for the site
  send     the site may be a source
  receive  the site may be a destination
  content  the site takes part with content, which includes Dictionary
  media    the site takes part with media, which includes wwwroot/media
  settings_send     the site may send its settings (uSync settings, Views, css, scripts). By default only the
                    main site may: settings come from the main site, and environments do not pass them around.
  settings_receive  the site may receive settings: on unless turned off. The main site never receives them.

Data can be synced from A to B when both have data sync on, A may send and B may receive. Only what both
take part with is synced. A sync always merges: content is added and updated, never deleted.

  data_sync.py kinds SITE_ROOT SOURCE DESTINATION   Print what may be synced, or the reason it may not
  data_sync.py settings SITE_ROOT SOURCE DESTINATION   Exit 0 when settings may be synced, or print the reason
  data_sync.py appsettings SITE_ROOT ID FILE...     Set uSync in each appsettings.json by the site's settings
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

KINDS = ("content", "media")
FLAGS = ("enabled", "send", "receive") + KINDS
SETTINGS_FLAGS = ("settings_send", "settings_receive")
HANDLERS = {"content": "ContentHandler", "media": "MediaHandler"}
ID_RE = re.compile(r"main|[a-z0-9][a-z0-9-]{0,62}")


def config_path(site_root):
    return Path(site_root) / "scripts" / "data_sync.json"


def clean(settings, site_id=""):
    """The settings of one site. Unset, only the main site sends settings, and every environment receives them."""
    settings = settings if isinstance(settings, dict) else {}
    result = {flag: settings.get(flag) is True for flag in FLAGS}
    result["settings_send"] = settings.get("settings_send", site_id == "main") is True
    result["settings_receive"] = settings.get("settings_receive", True) is True
    return result


def load(site_root):
    try:
        data = json.loads(config_path(site_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read {config_path(site_root)}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{config_path(site_root)} is not a settings object")
    return {key: clean(value, key) for key, value in data.items() if isinstance(key, str) and ID_RE.fullmatch(key)}


def save(site_root, site_id, settings):
    if not isinstance(site_id, str) or not ID_RE.fullmatch(site_id):
        raise ValueError("Unknown site")
    data = load(site_root)
    data[site_id] = clean(settings, site_id)
    path = config_path(site_root)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".data_sync.", suffix=".json")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return data[site_id]


def kinds(config, source, destination):
    """What may be synced from source to destination. Raises ValueError with the reason when nothing may."""
    if source == destination:
        raise ValueError("The source and destination are the same")
    sender, receiver = clean(config.get(source)), clean(config.get(destination))
    for name, settings in ((source, sender), (destination, receiver)):
        if not settings["enabled"]:
            raise ValueError(f"Data sync is off for {name}")
    if not sender["send"]:
        raise ValueError(f"{source} may not send data")
    if not receiver["receive"]:
        raise ValueError(f"{destination} may not receive data")
    shared = [kind for kind in KINDS if sender[kind] and receiver[kind]]
    if not shared:
        raise ValueError(f"{source} and {destination} have no content or media in common")
    return shared


def settings_allowed(config, source, destination):
    """Raises ValueError with the reason when settings may not be synced from source to destination"""
    if source == destination:
        raise ValueError("The source and destination are the same")
    if destination == "main":
        raise ValueError("The main site does not receive settings")
    if not clean(config.get(source), source)["settings_send"]:
        raise ValueError(f"{source} may not send settings")
    if not clean(config.get(destination), destination)["settings_receive"]:
        raise ValueError(f"{destination} may not receive settings")


def apply_appsettings(data, settings):
    """Set uSync for one site: the content and media handlers by what it takes part with, export on save
    for a sender, and import at startup for a receiver. A site with data sync off gets the handlers off,
    and the values set here are reverted. Returns the changed object."""
    settings = clean(settings)
    on = settings["enabled"]
    usync = data.setdefault("uSync", {})
    general = usync.setdefault("Settings", {})
    default = usync.setdefault("Sets", {}).setdefault("Default", {})
    disabled = [handler for handler in default.get("DisabledHandlers", []) if isinstance(handler, str)]
    for kind, handler in HANDLERS.items():
        if on and settings[kind]:
            disabled = [name for name in disabled if name != handler]
        elif handler not in disabled:
            disabled.append(handler)
    default["DisabledHandlers"] = disabled
    if on:
        general["ExportOnSave"] = "All" if settings["send"] else "Settings"
        general["ImportAtStartup"] = "All" if settings["receive"] else "Settings"
        general["UiEnabledGroups"] = "Settings,Content"
    else:
        if general.get("ImportAtStartup") == "All":
            general["ImportAtStartup"] = "Settings"
        if general.get("ExportOnSave") == "Settings":
            general["ExportOnSave"] = "All"
        if general.get("UiEnabledGroups") == "Settings,Content":
            general["UiEnabledGroups"] = "Settings"
    return data


def apply_file(path, settings):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not plain JSON, so it is not changed: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a settings object")
    updated = json.dumps(apply_appsettings(data, settings), indent=2, ensure_ascii=False) + "\n"
    if updated == text:
        return False
    Path(path).write_text(updated, encoding="utf-8")
    return True


def main(args):
    try:
        if len(args) == 4 and args[0] == "kinds":
            print(" ".join(kinds(load(args[1]), args[2], args[3])))
        elif len(args) == 4 and args[0] == "settings":
            settings_allowed(load(args[1]), args[2], args[3])
        elif len(args) >= 4 and args[0] == "appsettings":
            settings = load(args[1]).get(args[2])
            for path in args[3:]:
                print(("Updated " if apply_file(path, settings) else "Unchanged ") + os.path.basename(path))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
