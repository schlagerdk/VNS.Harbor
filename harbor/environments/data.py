"""Data sync between a main site and its environments. From data_sync.sh.

  settings: set uSync in the appsettings.json of the main site or one environment by its data sync settings
    (scripts/data_sync.json, saved by the admin), then restart it so uSync reads them.
  sync: sync data from one site to others, when both have data sync on, the source may send and the destination may
    receive. Settings are synced from the main site first, so the destination has the same document types. Data is
    merged: content and media are added and updated, never deleted. The destination is restarted, and uSync imports
    the data at startup.

apache/lib/data_sync.py decides who may sync what; the admin uses it too, so it stays where it is and harbor loads it.
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
from harbor.environments.sync import Sync, sync_environments
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

    def appsettings(self, site_id, paths):
        """Set uSync in the files, as data_sync.py appsettings: a line for each, Updated or Unchanged"""
        try:
            settings_of = self.config().get(site_id)
            lines = [("Updated " if self.rules.apply_file(path, settings_of) else "Unchanged ") + os.path.basename(path) for path in paths]
        except (OSError, ValueError) as error:
            self.system.warn(str(error))
            raise Failed() from None
        result = "\n".join(lines)
        self.system.say(result)
        return result

    def apply_settings(self, site_id, force=False):
        """Edit the appsettings.json of one site in place, by its saved settings, and restart it when it changed. With
        force it is restarted anyway, so uSync imports what it just received."""
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
            if not force and "Updated" not in result:
                system.say(f"Nothing changed, so {site.service_name} is not restarted.")
                return
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
                result = self.appsettings(site_id, [file])
                if not force and "Updated" not in result:
                    system.say("Nothing changed, so the IIS app is not restarted.")
                    return
                share.rclone("rcat", f"{share.target}/appsettings.json", input=Path(file).read_text())
                # IIS restarts the app when web.config changes
                system.step("Restart the IIS app")
                share.rclone("touch", f"{share.target}/web.config")
                return
            path = shlex.quote(f"{environment.root}/appsettings.json")
            Path(file).write_text(settings.remote_exec(system, environment, f"cat {path}", capture=True).stdout)
            result = self.appsettings(site_id, [file])
            if not force and "Updated" not in result:
                system.say("Nothing changed, so the site is not restarted.")
                return
            settings.remote_exec(system, environment, f"tee {path} >/dev/null", input=Path(file).read_text(), capture=True)
            settings.remote_exec(system, environment, f"chown www-data:www-data {path}")
            unit = service_name(environment.FOLDER)
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

    def sync_one(self, source, destination, dry_run):
        """One destination: settings from the main site, then the data merged in, then a restart to import it"""
        system = self.system
        kinds = self.kinds(source, destination)
        system.step(f"{destination}: {' and '.join(kinds)} from {source}. Nothing is deleted on {destination}.")

        def sync(*args, **options):
            try:
                sync_environments(system, self.site.name, *args, dry_run=dry_run, **options)
            except Stop as error:
                if str(error):
                    system.warn(str(error))
                raise Failed() from None
        # Settings first, so the document types match, unless Sync setup says the destination does not take them
        if destination != "main":
            try:
                self.rules.settings_allowed(self.config(), "main", destination)
                allowed, reason = True, ""
            except ValueError as error:
                allowed, reason = False, str(error)
            if allowed:
                system.step(f"Settings from the main site to {destination}")
                sync("main", destination)
            else:
                system.say(f"Settings are not synced first: {reason}. Its document types must already match.")
        system.step(f"Data from {source} to {destination}")
        sync(source, destination, data=",".join(kinds))
        if dry_run:
            system.say(f"DRY RUN: {destination} is not restarted.")
            return
        # The destination imports what it received at startup. Its settings are applied again on the way, so a
        # destination edited by hand still imports.
        system.step(f"Import on {destination}")
        try:
            self.apply_settings(destination, force=True)
        except (CommandFailed, Stop):
            raise Failed() from None
        system.say(f"uSync on {destination} imports the data as it starts. Its log in the backoffice shows the result.")


def data_sync(system, name, action, source="", destinations="", option=""):
    site = site_named(system, name)
    if not system.path(f"{site.site_root}/scripts").is_dir():
        raise Stop(f"Unknown site: {site.name}")
    data = DataSync(system, site)
    if action == "settings":
        data.valid(source)
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
        raise Stop("Usage: data_sync.sh <site> settings <main|environment> | sync <from> <to> [--dry-run]")
    if option not in ("", "--dry-run"):
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
    failed = []
    for destination in named:
        try:
            data.sync_one(source, destination, dry_run)
        except Failed:
            system.warn(f"{destination}: FAILED")
            failed.append(destination)
        except CommandFailed:
            system.warn(f"{destination}: FAILED")
            failed.append(destination)
    if failed:
        raise Stop(f"Data sync failed for: {' '.join(failed)}")
    system.say("Done.")
