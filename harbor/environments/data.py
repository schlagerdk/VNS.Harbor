"""Data sync between a main site and its environments. From data_sync.sh.

  settings: set uSync in the appsettings.json of the main site or one environment by its data sync settings
    (scripts/data_sync.json, saved by the admin), then restart it so uSync reads them.
  sync: sync data from one site to others, when both have data sync on, the source may send and the destination may
    receive. Settings are synced from the main site first, so the destination has the same document types. Data is
    merged: content and media are added and updated, never deleted. uSync at the destination imports it while the site
    runs, asked through the VNS.Harbor package; with an older package the sync says it is not imported.

lib/data_sync.py decides who may sync what; the admin uses it too, so it stays where it is and harbor loads it.
"""
import importlib.util
import os
from pathlib import Path
import re
import shlex
import tempfile

from harbor.core.names import service_name
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.environments.sync import Sync, display_name, sync_environments
from harbor.environments.windows import Share
from harbor.sites.actions import site_named
from harbor.core.paths import LIB



def rules():
    spec = importlib.util.spec_from_file_location("vns_data_sync", LIB / "data_sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Failed(Exception):
    """A step that failed for one destination; its message is told already"""


class DataSync:
    def __init__(self, system, site):
        self.system = system
        self.site = site
        self.root = str(system.path(site.site_root))
        self.rules = rules()
        self.main_folder = Sync(system, site, "", False).main_folder

    def config(self):
        try:
            return self.rules.load(self.root)
        except ValueError as error:
            self.system.warn(str(error))
            raise Failed() from None

    def valid(self, site_id):
        targets = settings.target_ids(self.system, self.site.site_root)
        if site_id != "main" and not (re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", site_id or "") and site_id in targets):
            raise Stop(f"Unknown site or environment: {site_id}")

    def kinds(self, source, destination):
        try:
            return self.rules.kinds(self.config(), source, destination)
        except ValueError as error:
            self.system.warn(str(error))
            raise Failed() from None

    def package(self):
        """Whether the site's project has the VNS.Harbor package, through which Harbor has uSync import what it syncs
        (no import at startup); a site without it (Umbraco 13) imports at startup, restarted after a sync"""
        if not hasattr(self, "_package"):
            from harbor.umbraco.backoffice import uses_package
            self._package = uses_package(self.system, self.site)
        return self._package

    def appsettings(self, site_id, paths, shown=None):
        """Set uSync in the files, as data_sync.py appsettings: a line for each, Updated or Unchanged"""
        try:
            settings_of = self.config().get(site_id)
            lines = [("Updated " if self.rules.apply_file(path, settings_of, shown, self.package()) else "Unchanged ") + os.path.basename(path) for path in paths]
        except (OSError, ValueError) as error:
            self.system.warn(str(error))
            raise Failed() from None
        result = "\n".join(lines)
        self.system.say(result)
        return result

    def apply_settings(self, site_id, force=False, restart=True):
        """Edit the appsettings.json of one site in place, by its saved settings, and restart it when it changed. With
        force it is restarted anyway. Without restart the file is written and the site left running (what changes
        there, such as ImportAtStartup, is read when it next starts)."""
        system, site = self.system, self.site
        if site_id == "main":
            # The project's copy too, as publishing the main site copies it to the folder the site runs from
            paths = []
            for path in (f"{site.site_root}/appsettings.json", f"{self.main_folder()}/appsettings.json"):
                path = str(system.path(path))
                if Path(path).is_file() and path not in paths:
                    paths.append(path)
            if not paths:
                system.warn("The main site has no appsettings.json.")
                raise Failed()
            result = self.appsettings("main", paths)
            system.run("chown", "www-data:www-data", *paths, check=False, capture=True)
            if not restart:
                return
            if not force and "Updated" not in result:
                system.say(f"Nothing changed, so {site.service_name} is not restarted.")
                return
            system.plan([f"Restart {site.service_name}"])
            system.step(f"Restart {site.service_name}")
            system.run("systemctl", "restart", site.service_name)
            return
        environment = settings.load(system, site.site_root, site_id)
        with tempfile.TemporaryDirectory() as work:
            file = f"{work}/appsettings.json"
            if environment.windows:
                try:
                    share = Share(system, environment).connect()
                except RuntimeError as error:
                    system.warn(str(error))
                    raise Failed() from None
                Path(file).write_text(share.rclone("cat", f"{share.target}/appsettings.json", capture=True).stdout)
                result = self.appsettings(site_id, [file], f"appsettings.json on {display_name(system, site, site_id)}")
                if not force and "Updated" not in result:
                    system.say("Nothing changed, so the IIS app is not restarted.")
                    return
                share.rclone("rcat", f"{share.target}/appsettings.json", input=Path(file).read_text())
                if not restart:
                    return
                # IIS restarts the app when web.config changes
                system.plan(["Restart the IIS app"])
                system.step("Restart the IIS app")
                share.rclone("touch", f"{share.target}/web.config")
                return
            path = shlex.quote(f"{environment.root}/appsettings.json")
            Path(file).write_text(settings.remote_exec(system, environment, f"cat {path}", capture=True).stdout)
            result = self.appsettings(site_id, [file], f"appsettings.json on {display_name(system, site, site_id)}")
            if not force and "Updated" not in result:
                system.say("Nothing changed, so the site is not restarted.")
                return
            settings.remote_exec(system, environment, f"tee {path} >/dev/null", input=Path(file).read_text(), capture=True)
            settings.remote_exec(system, environment, f"chown www-data:www-data {path}")
            if not restart:
                return
            unit = service_name(environment.FOLDER)
            system.plan([f"Restart {unit} on {environment.description}"])
            system.step(f"Restart {unit} on {environment.description}")
            settings.remote_exec(system, environment, f"systemctl restart {shlex.quote(unit)}")

    def has_exported_content(self, site_id):
        """Whether uSync has written content in a site's folder, where data sync takes it from. uSync writes content
        when it is saved, and everything at startup on a site that may send, so content made before has to be written once."""
        if site_id == "main":
            return any(self.system.path(self.main_folder()).glob("uSync/*/Content/*"))
        environment = settings.load(self.system, self.site.site_root, site_id)
        if environment.windows:
            return True
        return settings.remote_exec(self.system, environment, f"compgen -G {shlex.quote(environment.root)}'/uSync/*/Content/*' >/dev/null",
                                    check=False, capture=True).ok

    def settings_behind(self, destination, source="main", kinds=("content", "media")):
        """Why the destination's settings are not those of the main site, or "": data is only merged into a site whose
        document types match. Only data is sent; Sync settings (or Sync everything) sends the settings first.
        A destination that may not receive settings (Sync setup) keeps its own, which must already match."""
        from harbor.environments import history
        if destination == "main":
            return ""
        try:
            self.rules.settings_allowed(self.config(), "main", destination)
        except ValueError:
            return ""
        return history.settings_behind(self.system, self.site, destination, source=source, kinds=tuple(kinds))

    def steps(self, source, destination, dry_run):
        """The steps of one destination: what it gets, the data, and its import"""
        kinds = self.kinds(source, destination)
        # The sites by the environment they are, as the admin and the dashboard name them
        source_name, destination_name = display_name(self.system, self.site, source), display_name(self.system, self.site, destination)
        return [f"{destination_name}: {' and '.join(kinds)} from {source_name}. Nothing is deleted on {destination_name}.",
                f"Data from {source_name} to {destination_name}", *([] if dry_run else [f"Import on {destination_name}"])]

    def sync_one(self, source, destination, dry_run, settings_first=False):
        """One destination: the data merged in, then imported (without a restart where the package can). The settings must be in sync first; with
        settings_first (Sync everything) they are synced first when they are behind."""
        system = self.system
        kinds = self.kinds(source, destination)
        destination_name = display_name(system, self.site, destination)
        steps = self.steps(source, destination, dry_run)
        system.step(steps[0])
        def sync(*args, **options):
            try:
                sync_environments(system, self.site.name, *args, dry_run=dry_run, restart=False, lanes=False, **options)
            except Stop as error:
                if str(error):
                    system.warn(str(error))
                raise Failed() from None
        behind = self.settings_behind(destination, source, kinds)
        if behind and settings_first:
            first = f"Settings from {display_name(system, self.site, 'main')} to {destination_name}: {behind[0].lower() + behind[1:]}"
            system.plan([first])
            system.step(first)
            sync("main", destination)
            behind = "" if dry_run else self.settings_behind(destination, source, kinds)
        if behind:
            system.warn(f"{behind}. Sync settings first, or use Sync everything: data is only merged into a site with the same document types.")
            raise Failed()
        system.step(steps[1])
        sync(source, destination, data=",".join(kinds))
        if dry_run:
            system.say(f"DRY RUN: {destination_name} is not restarted.")
            return
        system.step(steps[2])
        # uSync there imports the settings and the data now, while the site runs (the VNS.Harbor package): no restart
        from harbor.umbraco.backoffice import usync_import
        done = usync_import(system, self.site.name, destination, ("Settings", "Content"), label=destination_name)
        if done is True:
            return
        if done is False:
            raise Failed()
        if self.package():
            # Its package there is from before that: said plainly, as the data is not in use until someone imports it
            system.warn(f"{destination_name}: the data is there, but NOT imported yet: import it in uSync in its backoffice "
                        "(the VNS.Harbor package 18.3.3.1 or newer there imports it by itself)")
            return
        # A site without the package (Umbraco 13): it imports what it received at startup. Its settings are applied
        # again on the way, so a destination edited by hand still imports.
        try:
            self.apply_settings(destination, force=True)
        except (CommandFailed, Stop):
            raise Failed() from None
        system.say(f"uSync on {destination_name} imports the data as it starts. Its log in the backoffice shows the result.")


def data_sync(system, name, action, source="", destinations="", option=""):
    site = site_named(system, name)
    if not system.path(f"{site.site_root}/scripts").is_dir() and not settings.migrated(system, site.site_root):
        raise Stop(f"Unknown site: {site.name}")
    data = DataSync(system, site)
    if action == "settings":
        data.valid(source)
        system.plan([f"Sync setup for {source}"])
        system.step(f"Sync setup for {source}")
        try:
            data.apply_settings(source)
        except Failed:
            raise Stop("", 1) from None
        except CommandFailed as error:
            raise Stop("", error.result.returncode or 1) from None
        system.say("Done.")
        return
    if action != "sync":
        raise Stop("Usage: data_sync.sh <site> settings <main|environment> | sync <from> <to> [--dry-run|--settings-first]")
    if option not in ("", "--dry-run", "--settings-first"):
        raise Stop(f"Unknown option: {option}")
    dry_run = option == "--dry-run"
    data.valid(source)
    if not destinations:
        raise Stop("Choose a destination.")
    named = [item for item in destinations.split(",") if item]
    # Every destination is checked before anything is synced
    wants_content = False
    for destination in named:
        data.valid(destination)
        try:
            wants_content = "content" in data.kinds(source, destination) or wants_content
        except Failed:
            raise Stop("", 1) from None
    # Content is synced from what uSync has written: without it, only media would go, and that would look like a sync
    if wants_content and not data.has_exported_content(source):
        raise Stop(f"{source} has no content written by uSync yet, so there is no content to sync.\n"
                   "uSync writes content when it is saved, and all of it when a site that may send data starts.\n"
                   f"Save Sync setup on {source}, which restarts it, or restart it, then sync again.")
    system.plan([step for destination in named for step in data.steps(source, destination, dry_run)])
    failed = []
    for destination in named:
        try:
            data.sync_one(source, destination, dry_run, settings_first=option == "--settings-first")
        except Failed:
            system.warn(f"{destination}: FAILED")
            failed.append(destination)
        except CommandFailed:
            system.warn(f"{destination}: FAILED")
            failed.append(destination)
    if failed:
        raise Stop(f"Data sync failed for: {' '.join(failed)}")
    system.say("Done.")


# ---- no import at startup (1.0.92)
IMPORT_OFF = "/var/lib/harbor/import-at-startup-off"


def sites_with_usync(system, names=None):
    """The main sites here with uSync and the VNS.Harbor package: built here (an Umbraco project with the package) or
    published from a machine (local-publish.json, its build with VNS.Harbor.dll), with a uSync folder. An environment's
    own folder is not one: its main site names it."""
    from harbor.core.templates import Site
    web = system.path("/var/www")
    found = []
    for folder in sorted(web.iterdir()) if web.is_dir() else []:
        if not folder.is_dir() or folder.is_symlink() or (names and folder.name not in names):
            continue
        # Only a site with the VNS.Harbor package: one without (Umbraco 13) keeps importing at startup
        projects = [project.read_text(errors="replace") for project in folder.glob("*.csproj")]
        if projects and not any(re.search(r'<PackageReference\s+Include="VNS\.Harbor"', text, re.I) for text in projects):
            continue
        built = any("Umbraco.Cms" in text for text in projects)
        published = system.path(f"{settings.STATE}/{folder.name}/local-publish.json").is_file()
        # Published from a machine: its uSync folder where it runs, at the top or one folder down (such as _publish)
        published = published and any((path / "VNS.Harbor.dll").is_file() for path in (folder, *folder.iterdir()) if path.is_dir())
        if built or (published and ((folder / "uSync").is_dir() or any(folder.glob("*/uSync")))):
            found.append(Site(folder.name))
    return found


def import_at_startup_off(system, names=None):
    """Every Umbraco site with the VNS.Harbor package here, and each of its environments: uSync's ImportAtStartup None in
    its appsettings.json (an Import at startup in its Sync setup does nothing for it). Nothing is restarted: uSync reads it
    when the site next starts, and Harbor has uSync import what it syncs or publishes. Done once (a marker), until a
    site or an environment could not be reached: then again with the next update."""
    import json
    marker = system.path(IMPORT_OFF)
    if names is None and marker.is_file():
        system.say("ImportAtStartup is off on the sites already.")
        return
    missed = []
    for site in sites_with_usync(system, names):
        data = DataSync(system, site)
        for site_id in ["main", *settings.target_ids(system, site.site_root)]:
            try:
                data.apply_settings(site_id, restart=False)
            except (Failed, CommandFailed, Stop, OSError, RuntimeError):
                missed.append(f"{site.name} ({site_id})")
    if missed:
        system.warn("Not reached, so ImportAtStartup is left there until the next update: " + ", ".join(missed))
        return
    if names is None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"done": True}) + "\n")
    system.say("ImportAtStartup is off on the sites: Harbor has uSync import what it syncs or publishes.")
