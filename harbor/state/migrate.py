"""The migration (phase 6 of dev/HARBOR.md): Harbor's own copy becomes the truth, and the generated scripts leave the
site folders.

  6a  plan(): what the migration would do with a site, changing nothing (harbor migrate --dry-run)
  6d  migrate(): it done, with a backup; rollback() puts the site folder back as it was

What a site folder holds today, and where it goes:
  scripts/publish_prod.sh, scripts/targets/<id>/publish_prod.sh  an environment's settings: Harbor's copy has them
  scripts/data_sync.json                                         Sync setup: Harbor's copy has it
  .vns-publish/*.json                                            publish status: Harbor's copy has it
  a system script (the wrapper, or any version a template had)  the backup: Harbor does its work
  a system script changed by hand                                the backup, and told, so the change can be seen
  any other file in scripts/                                     the site's own: it stays where it is
In the end no site has .vns-publish or Harbor's scripts; a scripts folder is left only for the site's own scripts
(decided 2026-11-13).
"""
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
import shutil
import tarfile
from pathlib import Path
import re

from harbor.core.templates import Site
from harbor.environments import settings
from harbor.core.system import Stop
from harbor.state.store import SECRET_KEYS, read_json, site_secrets, site_state, sync, write_json

BACKUPS = "/var/lib/harbor/backups"
TARGET_SCRIPTS = ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh")


# The tools a site's scripts folder had, by kind (the site templates of VNS 1.x): a site
# keeps their names in its site.json, and the admin shows them as its actions and runs the harbor command each called
SITE_SCRIPTS = {
    "common": ("edit_conf.sh", "issue_certificat.sh", "restart_apache.sh"),
    "service": ("check_service.sh", "edit_service.sh", "restart_service.sh"),
    "python": ("fix_permissions.sh", "update_packages.sh"),
    "csharp": ("change_env.sh", "create_prod.sh", "outdated.sh", "publish.sh"),
    "umbraco": ("clear_logs.sh",),
}


def system_script_names():
    """Every script name the site templates put in a site's scripts folder"""
    now = {name for names in SITE_SCRIPTS.values() for name in names} | set(TARGET_SCRIPTS) | {"set_password.sh"}
    # And the names they had before (publish_local.sh became publish.sh)
    return sorted(now | set(json.loads(Path(__file__).with_name("script_history.json").read_text())))


def calls_harbor(text):
    return "/bin/harbor" in text


def normal(text):
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")


# A script renamed by Harbor: publish.sh became publish_local.sh when the first environment was added
SAME_SCRIPT = {"publish_local.sh": "publish.sh", "clear_logs_local.sh": "clear_logs.sh"}


@lru_cache(maxsize=None)
def versions(name):
    """The versions a template script has had (dev/script_history.py), each as a pattern: the placeholders such as
    @SITE@ stand for what was filled in, on that line"""
    history = json.loads(Path(__file__).with_name("script_history.json").read_text())
    return [re.compile("".join(re.escape(part) if index % 2 == 0 else r"[^\n]*?"
                               for index, part in enumerate(re.split(r"(@[A-Z_]+@)", normal(text)))))
            for text in history.get(name, []) + history.get(SAME_SCRIPT.get(name, ""), [])]


def harbors(path):
    """A script Harbor made: the wrapper that calls harbor, or one of its template's versions as it was rendered"""
    text = path.read_text(errors="replace")
    return calls_harbor(text) or any(pattern.fullmatch(normal(text)) for pattern in versions(path.name))


def plan(system, name):
    """What the migration would do with one site, and whether it may"""
    site = Site(name)
    folder = system.path(site.site_root)
    result = {"site": name, "environments": [], "sync_setup": "none", "publish_status": 0,
              "backup": [], "changed": [], "own": [], "problems": []}
    if not folder.is_dir():
        result["problems"].append("The site's folder is missing.")
        return finish(result)
    state, secrets = site_state(system, name), site_secrets(system, name)
    # Every environment's settings must be in Harbor's copy, the same as in its script
    for target_id in settings.script_ids(system, site.site_root):
        script = settings.settings_script(site.site_root, target_id)
        try:
            values = settings.parse(system.path(script).read_text())
        except (OSError, settings.SettingsError) as error:
            result["problems"].append(f"{target_id}: its settings cannot be read ({error})")
            continue
        copy = read_json(state / "environments" / f"{target_id}.json") or {}
        hidden = read_json(secrets / f"{target_id}.json") or {}
        wanted = {key: value for key, value in values.items() if key not in SECRET_KEYS}
        missing_secrets = [key for key in SECRET_KEYS if values.get(key) and hidden.get(key) != values[key]]
        same = copy == wanted and not missing_secrets
        result["environments"].append({"id": target_id, "in_copy": bool(copy), "same": same})
        if not same:
            result["problems"].append(f"{target_id}: Harbor's copy differs from the script (run harbor state sync {name})")
    sync_setup = folder / "scripts/data_sync.json"
    if sync_setup.is_file():
        same = read_json(sync_setup) is not None and read_json(sync_setup) == read_json(state / "data_sync.json")
        result["sync_setup"] = "same" if same else "differs"
        if not same:
            result["problems"].append(f"Sync setup: Harbor's copy differs (run harbor state sync {name})")
    publish = folder / ".vns-publish"
    if publish.is_dir():
        files = sorted(publish.glob("*.json"))
        result["publish_status"] = len(files)
        if any(read_json(path) != read_json(state / "publish" / path.name) for path in files):
            result["problems"].append(f"Publish status: Harbor's copy differs (run harbor state sync {name})")
    # The scripts folder: what goes, what stays
    names = set(system_script_names())
    scripts = folder / "scripts"
    for path in sorted(scripts.rglob("*")) if scripts.is_dir() else []:
        if not path.is_file():
            continue
        relative = str(path.relative_to(folder))
        if path.name == "data_sync.json" and path.parent == scripts:
            result["backup"].append(relative)
        elif path.name == "publish_prod.sh" and (path.parent == scripts or path.parent.parent == scripts / "targets"):
            # Its settings are in Harbor's copy; the script itself is Harbor's
            result["backup"].append(relative)
        elif path.name in names and (path.parent == scripts or path.parent.parent == scripts / "targets"):
            (result["backup"] if harbors(path) else result["changed"]).append(relative)
        else:
            result["own"].append(relative)
    if publish.is_dir():
        result["backup"].append(".vns-publish/")
    return finish(result)


def finish(result):
    result["ready"] = not result["problems"]
    return result


def sites(system):
    from harbor.install.admin import is_admin_site
    web = system.path("/var/www")
    return [path.name for path in sorted(web.iterdir()) if path.is_dir() and not path.is_symlink()
            and (path / "scripts").is_dir() and not is_admin_site(path) and not (path / "app/manage.py").exists()] if web.is_dir() else []


def dry_run(system, name=""):
    """The plan for one site or every site, told; True when every one is ready"""
    plans = [plan(system, item) for item in ([name] if name else sites(system))]
    for item in plans:
        system.say(f"{item['site']}: {'ready' if item['ready'] else 'NOT ready'}")
        if item["environments"]:
            system.say("  environments: " + ", ".join(f"{env['id']}{'' if env['same'] else ' (differs)'}" for env in item["environments"]))
        for key, label in (("backup", "to the backup"), ("changed", "to the backup, changed by hand"), ("own", "the site's own, stays")):
            for path in item[key]:
                system.say(f"  {label}: {path}")
        for problem in item["problems"]:
            system.warn(f"  {problem}")
    return plans


# ---- 6d: the migration itself
def publish_options(text):
    """What a site's publish script held: its default configuration, and the folder and EnvironmentName an old one had"""
    found = {}
    configuration = re.search(r"\$\{1:-(Debug|Release)\}", text)
    if configuration:
        found["configuration"] = configuration[1]
    for key, option in (("folder", "--folder"), ("environment_name", "--environment-name")):
        match = re.search(rf"{option}\s+(['\"]?)([^'\"\s]+)\1", text)
        if match:
            found[key] = match[2]
    return found


def site_record(folder, item):
    """site.json: the publish options, and the scripts the site had, so the admin shows the same buttons"""
    scripts = folder / "scripts"
    record = {}
    for name in ("publish_local.sh", "publish.sh"):
        if (scripts / name).is_file():
            record.update(publish_options((scripts / name).read_text(errors="replace")))
            break
    names = set(system_script_names())
    record["scripts"] = sorted(path.name for path in scripts.glob("*.sh") if path.name in names and path.name not in TARGET_SCRIPTS) \
        if scripts.is_dir() else []
    record["targets"] = {}
    for environment in item["environments"]:
        place = scripts if environment["id"] == "prod" else scripts / "targets" / environment["id"]
        record["targets"][environment["id"]] = [name for name in TARGET_SCRIPTS if (place / name).is_file()]
    return record


def migrate(system, name):
    """One site migrated: Harbor's copy becomes its truth and its folder has no scripts or .vns-publish any more"""
    site = Site(name)
    folder = system.path(site.site_root)
    state = site_state(system, name)
    if (state / "migrated.json").is_file():
        system.say(f"{name} is migrated already.")
        return
    # Harbor's copy equal to the folder, then checked: a site that is not ready is left as it is
    for problem in sync(system, name):
        system.warn(problem)
    item = plan(system, name)
    if not item["ready"]:
        raise Stop(f"{name} is not ready: " + "; ".join(item["problems"]))
    if not (folder / "scripts").exists() and not (folder / ".vns-publish").exists():
        system.say(f"{name} has nothing to migrate.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backups = system.path(f"{BACKUPS}/{name}")
    backups.mkdir(parents=True, exist_ok=True)
    for place in (system.path(BACKUPS), backups):
        place.chmod(0o700)
    backup = backups / f"{stamp}.tar.gz"
    system.plan([f"Back up the scripts of {name}", f"Take Harbor's scripts out of {name}"])
    system.step(f"Back up the scripts of {name}")
    with tarfile.open(backup, "w:gz") as archive:
        for part in ("scripts", ".vns-publish"):
            if (folder / part).exists():
                archive.add(folder / part, arcname=part)
    backup.chmod(0o600)
    write_json(state / "site.json", site_record(folder, item))
    write_json(state / "migrated.json", {"time": stamp, "backup": "/" + str(backup.relative_to(system.root)),
                                         "changed": item["changed"], "own": item["own"]})
    system.step(f"Take Harbor's scripts out of {name}")
    # Harbor's scripts and settings go; the site's own scripts stay where they are
    for relative in item["backup"] + item["changed"]:
        path = folder / relative.rstrip("/")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    scripts = folder / "scripts"
    for place in sorted([path for path in scripts.rglob("*") if path.is_dir()], key=lambda path: len(path.parts), reverse=True) + [scripts]:
        if place.is_dir() and not place.is_symlink() and not any(place.iterdir()):
            place.rmdir()
    for path in item["changed"]:
        system.warn(f"{name}: {path} was changed by hand. Harbor does its work now; the change is in {backup}.")
    system.say(f"{name} is migrated." + (" Its own scripts stay in its scripts folder." if item["own"] else ""))


def rollback(system, name):
    """A migrated site's folder as it was before: its scripts and .vns-publish from the backup; the site folder is the
    truth again. What was changed in Harbor's copy since (a new environment, say) is not in the scripts."""
    site = Site(name)
    state = site_state(system, name)
    marker = read_json(state / "migrated.json")
    if not isinstance(marker, dict):
        raise Stop(f"{name} is not migrated.")
    backup = system.path(marker.get("backup", ""))
    if not backup.is_file():
        raise Stop(f"The backup of {name} is gone: {marker.get('backup', '')}")
    folder = system.path(site.site_root)
    system.plan([f"Put the scripts of {name} back"])
    system.step(f"Put the scripts of {name} back")
    with tarfile.open(backup) as archive:
        members = [member for member in archive.getmembers() if member.name.split("/")[0] in ("scripts", ".vns-publish")
                   and not member.name.startswith("/") and ".." not in member.name.split("/")]
        # The tar filter where Python has it (3.12, and later 3.10 updates): no paths out of the folder, modes kept
        archive.extractall(folder, members=members, **({"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}))
    for path in ("migrated.json", "site.json"):
        (state / path).unlink(missing_ok=True)
    for problem in sync(system, name):
        system.warn(problem)
    system.say(f"{name} has its scripts again; they are its truth, as before the migration.")


SERVER = "/var/lib/harbor/migrated.json"


def server_migrated(system):
    """Every site here migrated: a new site is made migrated too, with no scripts folder"""
    return system.path(SERVER).is_file()


def born_migrated(system, site, names):
    """A site made on a migrated server: what its scripts would have been is recorded in Harbor, not written"""
    state = site_state(system, site.name)
    record = read_json(state / "site.json") or {}
    record["scripts"] = sorted(set(record.get("scripts", [])) | set(names))
    record.setdefault("targets", {})
    if getattr(site, "build_config", ""):
        record["configuration"] = site.build_config
    write_json(state / "site.json", record)
    if not (state / "migrated.json").is_file():
        write_json(state / "migrated.json", {"time": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), "made": True})


def migrate_all(system, name=""):
    """Every site (or one), each on its own: one that is not ready, or fails, is told and left as it is. When every site
    is migrated, so is the server: a site made from then on has no scripts folder either."""
    failed = []
    for item in [name] if name else sites(system):
        try:
            migrate(system, item)
        except (Stop, OSError) as error:
            system.warn(f"{item}: {error}")
            failed.append(item)
    if failed:
        raise Stop(f"Not migrated: {', '.join(failed)}", 2)
    if not name:
        write_json(system.path(SERVER), {"time": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")})
        system.say("Every site is migrated: new sites are made without a scripts folder.")
