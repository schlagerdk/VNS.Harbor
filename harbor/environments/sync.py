"""Sync settings from the main site, or from one environment, to one or all of the site's other environments; or
data (content and media) from one site to another. From sync_env.sh.

Only settings are synced: in each uSync version folder (v9, v17, v18, Root ...) the folders in SETTINGS_FOLDERS and
usync.config, plus Views, wwwroot/css and wwwroot/scripts. Each is mirrored on its own. Content, Media, Dictionary,
Domains and any folder not listed are never touched, so data on a destination is kept. A source folder that does not
exist is skipped: it never empties the destination.

With data it syncs data instead: in each uSync version folder Content and Dictionary for content, Media and
wwwroot/media for media. Data is merged: files are added and updated, never deleted. data_sync checks first that the
two sites may sync data, and it is what the admin runs.

Every path given to rsync and rclone is the server's own (system.path), so the tests can run them on a fake server.
"""
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.environments.windows import Share
from harbor.sites.actions import site_named

LIB = Path(__file__).resolve().parents[2] / "apache/lib"
SETTINGS_FOLDERS = ("ContentTypes", "DataTypes", "Languages", "MediaTypes", "MemberTypes", "RelationTypes", "Templates")
CODE_FOLDERS = ("Views", "wwwroot/css", "wwwroot/scripts")
USAGE = "Usage: sync_env.sh <site> <main|environment> <environment|all> [--data <content,media>] [--dry-run]"


class Failed(Exception):
    """A step that failed in one environment: the sync goes on with the next"""


class Sync:
    def __init__(self, system, site, data, dry_run):
        self.system = system
        self.site = site
        self.data = data
        self.dry_run = ["--dry-run"] if dry_run else []
        self.what = f"data ({data.replace(',', ' and ')})" if data else "settings"
        # What is copied from an environment: uSync, plus the code folders for settings or the media files for data
        self.pull_folders = ["uSync", *CODE_FOLDERS]
        if data:
            self.pull_folders = ["uSync"] + (["wwwroot/media"] if "media" in data else [])

    def main_folder(self):
        """The main site's files are in the folder its service runs from"""
        service = self.system.path(self.site.service_path)
        text = service.read_text() if service.is_file() else ""
        working = next((line[len("WorkingDirectory="):] for line in text.splitlines() if line.startswith("WorkingDirectory=")), "")
        working = working[:-1] if working.endswith("/") else working
        if working:
            return working
        if self.system.path(f"{self.site.site_root}/_publish").is_dir():
            return f"{self.site.site_root}/_publish"
        return self.site.site_root

    def local(self, path):
        return str(self.system.path(path))

    @staticmethod
    def ssh(environment):
        """ssh as the script's sync used it: its own options, the password in SSHPASS"""
        argv = ["ssh", "-p", environment.SSH_PORT, "-o", "StrictHostKeyChecking=accept-new"]
        env = {}
        if environment.PASSWORD:
            env["SSHPASS"] = environment.PASSWORD
            argv = ["sshpass", "-e", *argv]
        sudo = "" if environment.USER == "root" else "sudo -n"
        return argv, env, sudo

    def share(self, environment):
        try:
            return Share(self.system, environment).connect()
        except RuntimeError as error:
            self.system.warn(str(error))
            raise Failed() from None

    # ---- copy an environment's folders here. Any error stops the sync: a partial copy must not be mirrored.
    def pull(self, target, source):
        system = self.system
        environment = settings.load(system, self.site.site_root, target)
        if environment.windows:
            share = self.share(environment)
            for folder in self.pull_folders:
                if not share.rclone("lsf", f"{share.target}/{folder}", "--max-depth", "1", check=False, capture=True).ok:
                    continue
                share.rclone("copy", f"{share.target}/{folder}", f"{source}/{folder}", "--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0")
        elif environment.LOCAL_PATH:
            for folder in self.pull_folders:
                if not self.system.path(f"{environment.root}/{folder}").is_dir():
                    continue
                Path(source, folder).mkdir(parents=True, exist_ok=True)
                system.run("rsync", "-a", f"{self.local(environment.root)}/{folder}/", f"{source}/{folder}/")
        else:
            ssh, env, sudo = self.ssh(environment)
            host = f"{environment.USER}@{environment.IP}"
            for folder in self.pull_folders:
                if not system.run(*ssh, host, f"{sudo} test -d '{environment.root}/{folder}'", env=env, check=False).ok:
                    continue
                Path(source, folder).mkdir(parents=True, exist_ok=True)
                system.run("rsync", "-az", "-e", " ".join(ssh), f"--rsync-path={sudo + ' ' if sudo else ''}rsync",
                           f"{host}:{environment.root}/{folder}/", f"{source}/{folder}/", env=env)

    # ---- mirror each listed folder on its own, so nothing outside them is deleted
    def push(self, target, source, mirror, files):
        system = self.system
        if target == "main":
            # The main site is on this server, in the folder its service runs from
            environment = settings.Environment(LOCAL_PATH=self.main_folder())
        else:
            environment = settings.load(system, self.site.site_root, target)
        root = environment.root
        mode, delete = ("copy", []) if self.data else ("sync", ["--delete"])

        def run(*argv, env=None):
            if not system.run(*argv, env=env, check=False).ok:
                raise Failed()
        if environment.windows:
            share = self.share(environment)
            for path in mirror:
                system.say(path)
                if not share.rclone(mode, f"{source}/{path}", f"{share.target}/{path}", "--create-empty-src-dirs", "--modify-window", "2s",
                                    "--stats", "0", *self.dry_run, check=False).ok:
                    raise Failed()
            for path in files:
                if not share.rclone("copyto", f"{source}/{path}", f"{share.target}/{path}", "--modify-window", "2s", "--stats", "0",
                                    *self.dry_run, check=False).ok:
                    raise Failed()
        elif environment.LOCAL_PATH:
            for path in mirror:
                system.say(path)
                if not self.dry_run:
                    system.path(f"{root}/{path}").mkdir(parents=True, exist_ok=True)
                run("rsync", "-a", *delete, *self.dry_run, f"{source}/{path}/", f"{self.local(root)}/{path}/")
            for path in files:
                run("rsync", "-a", *self.dry_run, f"{source}/{path}", f"{self.local(root)}/{path}")
            system.run("chown", "-R", "www-data:www-data", *(f"{self.local(root)}/{folder}" for folder in ("uSync", "Views", "wwwroot")),
                       check=False, capture=True)
        else:
            ssh, env, sudo = self.ssh(environment)
            host = f"{environment.USER}@{environment.IP}"
            rsync = ["rsync", "-az", "-e", " ".join(ssh), f"--rsync-path={sudo + ' ' if sudo else ''}rsync", *self.dry_run]
            for path in mirror:
                system.say(path)
                run(*ssh, host, f"{sudo} mkdir -p {shlex.quote(f'{root}/{path}')}", env=env)
                run(*rsync, *delete, f"{source}/{path}/", f"{host}:{root}/{path}/", env=env)
            for path in files:
                run(*rsync, f"{source}/{path}", f"{host}:{root}/{path}", env=env)
            folders = " ".join(shlex.quote(f"{root}/{folder}") for folder in ("uSync", "Views", "wwwroot"))
            run(*ssh, host, f"{sudo} chown -R www-data:www-data {folders} 2>/dev/null || true", env=env)

    def missing(self, target):
        """True only when the server answers that the folder is gone. An unreachable server fails in the sync instead."""
        if target == "main":
            return False
        environment = settings.load(self.system, self.site.site_root, target)
        if environment.LOCAL_PATH or environment.windows:
            return False
        return settings.remote_exec(self.system, environment, f"test -d {shlex.quote(environment.root)}", check=False).returncode == 1


ORDER = ("main", "development", "test", "staging", "production")


def in_environment_order(system, site, ids):
    """Environments in the order they follow: the main site, Development, Test, Staging, Production, then the rest"""
    def rank(target_id):
        if target_id == "main":
            return 0, target_id
        try:
            environment = (settings.load(system, site.site_root, target_id).other.get("ENVIRONMENT") or "").lower()
        except settings.SettingsError:
            environment = ""
        return (ORDER.index(environment) if environment in ORDER else len(ORDER)), target_id
    return sorted(ids, key=rank)


def sync_environments(system, name, source_id, to, data="", dry_run=False):
    if dry_run:
        system.say("DRY RUN: nothing is changed on the destinations.")
    if data and not re.fullmatch(r"(content|media)(,(content|media))?", data):
        raise Stop("--data takes content, media or content,media")
    site = site_named(system, name)
    if not source_id or not to:
        raise Stop("Usage: sync_env.sh <site> <main|environment> <environment|all>")
    if source_id == to:
        raise Stop("The source and destination are the same.")
    targets = settings.target_ids(system, site.site_root)
    known = ["main", *targets]
    if source_id not in known:
        raise Stop(f"Unknown source: {source_id}")
    if data:
        # Data goes to one site at a time, and may also go to the main site
        if to == "all":
            raise Stop("Data is synced to one site at a time.")
        if to not in known:
            raise Stop(f"Unknown destination: {to}")
        destinations = [to]
    else:
        named = [item for item in to.split(",") if item]
        destinations = [target for target in targets if target != source_id and (to == "all" or target in named)]
        if to != "all":
            for item in named:
                if item == "main" or item not in known:
                    raise Stop(f"Unknown destination: {item}")
    if not destinations:
        raise Stop("No environment to sync to.")
    destinations = in_environment_order(system, site, destinations)

    # Who may send and receive settings is set in Sync setup. With all, a site that may not is skipped. A site named on
    # its own that may not stops the sync before anything changes.
    if not data:
        allowed = []
        for target in destinations:
            check = system.run("python3", str(LIB / "data_sync.py"), "settings", str(system.path(site.site_root)), source_id, target,
                               check=False, capture=True)
            reason = (check.stdout + check.stderr).rstrip("\n")
            if check.ok:
                allowed.append(target)
            elif to == "all":
                system.say(f"{target}: skipped, {reason}")
            else:
                raise Stop(f"{reason}. Change it in Sync setup.")
        if not allowed:
            raise Stop(f"No environment may receive settings from {source_id}. Change it in Sync setup.")
        destinations = allowed

    sync = Sync(system, site, data, dry_run)
    work = None
    try:
        if source_id == "main":
            source = sync.local(sync.main_folder())
        else:
            work = tempfile.mkdtemp()
            source = work
            system.step(f"Copy {sync.what} from {source_id}")
            try:
                sync.pull(source_id, source)
            except CommandFailed as error:
                raise Stop("", error.result.returncode or 1) from None
            except Failed:
                raise Stop("", 1) from None

        # What is synced: only what exists at the source
        version_folders = list(SETTINGS_FOLDERS)
        code_folders = list(CODE_FOLDERS)
        if data:
            version_folders = (["Content", "Dictionary"] if "content" in data else []) + (["Media"] if "media" in data else [])
            code_folders = ["wwwroot/media"] if "media" in data else []
        mirror, files = [], []
        usync = Path(source, "uSync")
        for version in sorted(path.name for path in usync.iterdir() if path.is_dir()) if usync.is_dir() else []:
            if not re.fullmatch(r"v[0-9]+|Root", version):
                continue
            mirror += [f"uSync/{version}/{folder}" for folder in version_folders if Path(source, "uSync", version, folder).is_dir()]
            if not data and Path(source, "uSync", version, "usync.config").is_file():
                files.append(f"uSync/{version}/usync.config")
        mirror += [folder for folder in code_folders if Path(source, folder).is_dir()]
        if not mirror:
            raise Stop(f"Nothing to sync in {source}.")

        failed = 0
        for target in destinations:
            if sync.missing(target):
                system.say(f"{target}: SKIPPED (the site no longer exists on the server)")
                continue
            system.step(f"Sync {sync.what} to {target}")
            try:
                sync.push(target, source, mirror, files)
            except (Failed, Stop):
                system.warn(f"{target}: FAILED")
                failed += 1
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    if failed:
        raise Stop(f"Sync finished with errors in {failed} environment(s).")
    if not data:
        system.say("Sync completed. uSync imports the settings when it is set to import at startup, or from the backoffice.")
