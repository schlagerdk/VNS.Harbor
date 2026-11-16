"""Whether the content and media that have changed on a site need settings that are not synced yet: a text changed on
a page and a new data type nobody uses have nothing to do with each other, but a page made on a new template needs
that template on the destination first.

uSync writes everything as XML that points at the rest:
  Content   <ContentType>alias</ContentType>, <Template Key="…">, Culture="da-DK", and block values with contentTypeKey
  Media     <ContentType>alias</ContentType> (a media type)
  A document or media type: <Composition Key="…">, and each property's <Definition> (a data type)
  A data type: its config, which names the element types of a block list or grid by their keys

What changed is told by the files themselves: at each sync Harbor keeps each file's hash as it was sent
(sync-manifests.json beside the sync history), and the files that differ now are the changes. A content or media file
that changed needs the settings it uses, all the way down (compositions, data types, element types); when one of them
has changed since the settings were synced there, the settings go first. A changed document type or data type that a
changed page uses always counts: that is what breaks an import.
"""
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ElementTree

from harbor.state.store import read_json, write_json

VERSION = re.compile(r"v[0-9]+|Root")
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SETTINGS_FOLDERS = ("ContentTypes", "DataTypes", "Languages", "MediaTypes", "MemberTypes", "RelationTypes", "Templates")
DATA_FOLDERS = {"content": ("Content", "Dictionary"), "media": ("Media",)}
# What an item is, by its folder, as the reasons name it
NAMES = {"ContentTypes": "the document type", "MediaTypes": "the media type", "DataTypes": "the data type",
         "Templates": "the template", "Languages": "the language", "MemberTypes": "the member type", "RelationTypes": "the relation type"}


def manifest(source, kinds):
    """Each uSync file a sync of these kinds takes, with its hash: {relative path: sha256}"""
    source = Path(source)
    found = {}
    usync = source / "uSync"
    for version in sorted(item.name for item in usync.iterdir() if item.is_dir()) if usync.is_dir() else []:
        if not VERSION.fullmatch(version):
            continue
        folders = [folder for kind in kinds for folder in (SETTINGS_FOLDERS if kind == "settings" else DATA_FOLDERS.get(kind, ()))]
        for folder in folders:
            for item in sorted((usync / version / folder).rglob("*.config")) if (usync / version / folder).is_dir() else []:
                try:
                    found[str(item.relative_to(source))] = hashlib.sha256(item.read_bytes()).hexdigest()
                except OSError:
                    continue
    return found


def manifests_path(system, site):
    from harbor.environments.settings import STATE
    return system.path(f"{STATE}/{site}/sync-manifests.json")


def keep(system, site, source, destination, kinds, files):
    """What a sync that worked sent, file by file, for the next comparison"""
    path = manifests_path(system, site)
    found = read_json(path)
    found = found if isinstance(found, dict) else {}
    pair = found.setdefault(f"{source}>{destination}", {})
    for kind in kinds:
        pair[kind] = {name: digest for name, digest in files.items() if _kind_of(name) == kind}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, found)
    except OSError:
        pass


def sent(system, site, source, destination, kind):
    """The files sent last time a sync of this kind worked, or None when Harbor does not know them"""
    found = read_json(manifests_path(system, site))
    files = (found or {}).get(f"{source}>{destination}", {}).get(kind) if isinstance(found, dict) else None
    return files if isinstance(files, dict) else None


def _kind_of(name):
    folder = Path(name).parts[2] if len(Path(name).parts) > 2 else ""
    if folder in SETTINGS_FOLDERS:
        return "settings"
    return next((kind for kind, folders in DATA_FOLDERS.items() if folder in folders), "")


def _read(path):
    try:
        return ElementTree.fromstring(Path(path).read_bytes())
    except (OSError, ElementTree.ParseError):
        return None


class Settings:
    """The settings of a site as uSync wrote them: each item by its key, what it uses, and its name"""

    def __init__(self, source):
        self.items = {}       # key -> (folder, name, path)
        self.uses = {}        # key -> set of keys
        self.by_alias = {}    # (folder, alias) -> key
        self.by_path = {}     # relative path -> key
        source = Path(source)
        usync = source / "uSync"
        for version in sorted(item.name for item in usync.iterdir() if item.is_dir()) if usync.is_dir() else []:
            if not VERSION.fullmatch(version):
                continue
            for folder in SETTINGS_FOLDERS:
                for path in sorted((usync / version / folder).rglob("*.config")) if (usync / version / folder).is_dir() else []:
                    self._add(folder, path, source)

    def _add(self, folder, path, source):
        root = _read(path)
        if root is None:
            return
        key = (root.get("Key") or "").lower()
        if not key:
            return
        alias = root.get("Alias") or ""
        name = root.findtext("Info/Name") or root.findtext("Name") or alias
        self.items[key] = (folder, name, str(path.relative_to(source)))
        self.by_alias[(folder, alias.lower())] = key
        self.by_path[str(path.relative_to(source))] = key
        uses = set()
        if folder in ("ContentTypes", "MediaTypes", "MemberTypes"):
            uses |= {(item.get("Key") or "").lower() for item in root.iter("Composition")}
            uses |= {(item.text or "").strip().lower() for item in root.iter("Definition")}
        elif folder == "DataTypes":
            # A block list or grid names its element types by key in its config
            uses |= {match.lower() for match in GUID.findall(root.findtext("Config") or "")}
        self.uses[key] = {item for item in uses if item}

    def closure(self, keys):
        """The items these use, and what those use, all the way down"""
        found, waiting = set(), [key for key in keys if key]
        while waiting:
            key = waiting.pop()
            if key in found:
                continue
            found.add(key)
            waiting.extend(self.uses.get(key, ()))
        return found & set(self.items)

    def describe(self, key):
        folder, name, _ = self.items[key]
        return f"{NAMES.get(folder, 'the setting')} {name}"


def needs(settings, path):
    """What a content or media file uses directly: its type, its template, its cultures and element types in its values"""
    root = _read(path)
    if root is None:
        return set(), ""
    name = root.findtext("Info/NodeName") or ""
    node = root.find("Info/NodeName")
    name = (node.get("Default") if node is not None else "") or root.get("Alias") or Path(path).stem
    keys = set()
    alias = (root.findtext("Info/ContentType") or "").strip().lower()
    folder = "MediaTypes" if root.tag == "Media" else "ContentTypes"
    if alias and (folder, alias) in settings.by_alias:
        keys.add(settings.by_alias[(folder, alias)])
    template = root.find("Info/Template")
    if template is not None and template.get("Key"):
        keys.add(template.get("Key").lower())
    for element in root.iter():
        culture = element.get("Culture")
        if culture and ("Languages", culture.lower()) in settings.by_alias:
            keys.add(settings.by_alias[("Languages", culture.lower())])
    # Element types in block values (contentTypeKey), and any other key of a setting a value names
    for value in root.iter("Value"):
        keys |= {match.lower() for match in GUID.findall(value.text or "") if match.lower() in settings.items}
    return keys, name


def changed(now, before):
    """The files that are new or differ from what was sent"""
    return sorted(name for name, digest in now.items() if (before or {}).get(name) != digest)


def reasons(settings_source, data_source, settings_sent, data_sent, kinds, destination_name, limit=3):
    """Why the data that changed needs settings that are not synced yet: a list of sentences, empty when it does not.
    None when it is not known (the settings were never sent file by file)."""
    if settings_sent is None:
        return None
    settings = Settings(settings_source)
    settings_now = manifest(settings_source, ["settings"])
    changed_settings = {settings.by_path[name] for name in changed(settings_now, settings_sent) if name in settings.by_path}
    if not changed_settings:
        return []
    data_now = manifest(data_source, kinds)
    found = []
    for name in changed(data_now, data_sent):
        uses, node = needs(settings, Path(data_source) / name)
        missing = settings.closure(uses) & changed_settings
        if missing:
            item = sorted(settings.describe(key) for key in missing)[0]
            found.append(f"{node} uses {item}, which has changed since it was synced to {destination_name}")
            if len(found) >= limit:
                break
    return found
