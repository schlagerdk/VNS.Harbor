"""Migrate or copy an environment of a site to another folder or server. From move_env.sh.

The destination comes in VNS_PROD_TARGET, as the admin's dialog sends it:
  local    VNS_PROD_LOCAL_PATH=/var/www/<name>, _BINDING, _ENVIRONMENT, _HTTPS
  remote   VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT, _FOLDER, _BINDING, _ENVIRONMENT, _HTTPS
  windows  VNS_PROD_SMB_SHARE, _SMB_USER, _SMB_PASSWORD, _SMB_DOMAIN, _BINDING, _ENVIRONMENT, _HTTPS
  same     VNS_PROD_FOLDER, _BINDING, _ENVIRONMENT, _HTTPS. Another folder on the source's remote server
The environment can be on this server or on a remote Linux server.
On this server to this server, the folder is moved and the service and Apache config follow it. Otherwise the whole
folder is copied, also the build, so the site runs the same version afterwards. It is only built for Windows and for
another CPU architecture. A database in SQL Server stays where it is: a connection to the source's own machine is
pointed at that machine. A SQLite database is part of the files.
The source's service and Apache config are removed only when that has worked. VNS_MOVE_DELETE_LOCAL=y then also
deletes its files. If a step fails, the source runs as before, and a new folder on Linux is removed again.
With VNS_ENV_COPY=1 the environment is copied, and the copy becomes a new environment: the source stays as it is, and
is only stopped during the copy with VNS_COPY_STOP=y. VNS_COPY_DATABASE and VNS_COPY_SQL set a new connection for the
copy. A Windows server can be copied from. Only settings and content can be fetched from it, so the copy is always built.

What runs on another server is sent as the script sent it. The site's create_prod.sh and publish_prod.sh run as before.
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from harbor.core.bindings import server_aliases_from_config
from harbor.core.names import normalize_site_name, service_name
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.environments.register import bash_quote, run_create_prod, site_name
from harbor.environments.windows import Share
from harbor.core.paths import BIN, LIB
from harbor.state.store import STATE

ROOTS = "/var/lib/vns-roots"
REMOVE_REMOTE_SOURCE = r'''systemctl stop "${UNIT}" || true
systemctl disable "${UNIT}" || true
rm -f "/etc/systemd/system/${UNIT}.service" "/etc/apache2/sites-enabled/${NAME}.conf" "/etc/apache2/sites-available/${NAME}.conf"
rm -rf "/var/lib/vns-roots/${NAME}"
rm -f "/var/lib/vns-managed/${NAME}"
[[ ${DELETE_FILES} != y ]] || rm -rf "${ROOT:?}"
systemctl daemon-reload
apache2ctl configtest
systemctl reload apache2
'''
REQUEST = ("TARGET", "LOCAL_PATH", "IP", "USER", "PASSWORD", "SSH_PORT", "FOLDER", "BINDING", "ENVIRONMENT", "HTTPS", "SMB_SHARE",
           "SMB_USER", "SMB_PASSWORD", "SMB_DOMAIN")


class Failed(Exception):
    """A step failed; what is told is told, and the exit code is kept"""

    def __init__(self, code=1):
        super().__init__()
        self.code = code


class Move:
    def __init__(self, system, name, old_id):
        self.system = system
        self.site = Site(site_name(name))
        if not old_id:
            raise Stop("Choose the environment to move.")
        self.old_id = old_id
        self.source = settings.load(system, self.site.site_root, old_id)
        self.copy = os.environ.get("VNS_ENV_COPY", "")
        self.source_windows = self.source.windows
        self.source_remote = not self.source.windows and not self.source.LOCAL_PATH
        if self.source_windows and not self.copy:
            raise Stop("An environment on a Windows server cannot be migrated, as its IIS site cannot be stopped or removed from here. It can be copied.")
        self.old_root = self.source.root
        self.old_name = self.source.FOLDER
        self.old_unit = service_name(self.old_name)
        self.old_service = f"/etc/systemd/system/{self.old_unit}.service"
        self.old_conf = f"/etc/apache2/sites-available/{self.old_name}.conf"
        self.old_scripts = str(Path(self.source.script).parent)
        # What the dialog asked for, as the script read it from its environment
        self.request = {key: os.environ.get(f"VNS_PROD_{key}", "") for key in REQUEST}
        self.destination = self.request["TARGET"]
        if self.destination == "same":
            # The saved login of the source: it never passes through the browser
            if not self.source_remote:
                raise Stop("Only an environment on a remote Linux server can use its own server.")
            self.request.update(TARGET="remote", IP=self.source.IP, USER=self.source.USER, PASSWORD=self.source.PASSWORD,
                                SSH_PORT=self.source.SSH_PORT)
            self.destination = "remote"
        if not self.source_windows:
            if self.source_remote:
                if not settings.remote_exec(system, self.source, f"test -d {bash_quote(self.old_root)}", check=False).ok:
                    raise Stop(f"{self.old_root} was not found on {self.source.IP}.")
            else:
                if not system.path(self.old_root).is_dir():
                    raise Stop(f"The folder {self.old_root} does not exist.")
                if not system.path(self.old_service).is_file():
                    raise Stop(f"The service of {self.old_name} does not exist.")
                if not system.path(self.old_conf).is_file():
                    raise Stop(f"The Apache config of {self.old_name} does not exist.")

    # ---- helpers
    def variables(self, **extra):
        """What the site's scripts get, as they inherited it from the script: the request and the environment moved"""
        values = {f"VNS_PROD_{key}": value for key, value in self.request.items() if value or key in ("PASSWORD", "SMB_PASSWORD")}
        return {**values, "VNS_TARGET": self.old_id, **extra}

    def run(self, *argv, **options):
        result = self.system.run(*argv, check=False, **options)
        if not result.ok:
            raise Failed(result.returncode)
        return result

    def environment(self, target_id):
        return settings.load(self.system, self.site.site_root, target_id)

    @staticmethod
    def ssh(environment):
        argv = ["ssh", "-p", environment.SSH_PORT, "-o", "StrictHostKeyChecking=accept-new"]
        env = {}
        if environment.PASSWORD:
            env["SSHPASS"] = environment.PASSWORD
            argv = ["sshpass", "-e", *argv]
        else:
            argv += settings.harbor_key()
        sudo = "" if environment.USER == "root" else "sudo -n"
        rsync = ["rsync", "-az", "-e", " ".join(argv), f"--rsync-path={sudo + ' ' if sudo else ''}rsync"]
        return argv, env, sudo, rsync

    def share(self, environment):
        try:
            return Share(self.system, environment).connect()
        except RuntimeError as error:
            self.system.warn(str(error))
            raise Failed() from None

    def source_control(self, action):
        if self.source_windows:
            return
        if self.source_remote:
            settings.remote_exec(self.system, self.source, f"systemctl {action} {bash_quote(self.old_unit)}")
        else:
            self.system.run("systemctl", action, self.old_unit)

    def local(self, path):
        return str(self.system.path(path))

    # ---- to another folder on this server
    def move_local(self):
        system, site = self.system, self.site
        new_root = self.request["LOCAL_PATH"][:-1] if self.request["LOCAL_PATH"].endswith("/") else self.request["LOCAL_PATH"]
        new_name = os.path.basename(new_root)
        if new_root != f"{site.root_path}/{new_name}":
            raise Stop(f"The new folder must be {site.root_path}/<name>.")
        new_name = site_name(new_name)
        new_unit = service_name(new_name)
        new_service = f"/etc/systemd/system/{new_unit}.service"
        new_conf = f"/etc/apache2/sites-available/{new_name}.conf"
        if any(os.path.lexists(system.path(path)) for path in (new_root, new_service, new_conf)):
            raise Stop(f"{new_name} already exists on this server.")
        backup = Path(tempfile.mkdtemp())
        shutil.copy(system.path(self.old_service), backup / "service")
        shutil.copy(system.path(self.old_conf), backup / "conf")
        # A migrated site has no scripts: its settings are in Harbor's copy
        has_scripts = system.path(self.old_scripts).is_dir()
        if has_scripts:
            shutil.copytree(system.path(self.old_scripts), backup / "scripts", symlinks=True)
        before = settings.values(system, site.site_root, self.source.id)

        def quiet(*argv):
            system.run(*argv, check=False, capture=True)

        def undo():
            system.warn(f"The move failed. Restoring {self.old_name}.")
            quiet("systemctl", "stop", new_unit)
            quiet("systemctl", "disable", new_unit)
            system.path(new_service).unlink(missing_ok=True)
            quiet("a2dissite", f"{new_name}.conf")
            system.path(new_conf).unlink(missing_ok=True)
            if system.path(new_root).is_dir() and not os.path.lexists(system.path(self.old_root)):
                shutil.move(system.path(new_root), system.path(self.old_root))
            if system.path(f"{ROOTS}/{new_name}").is_dir() and not os.path.lexists(system.path(f"{ROOTS}/{self.old_name}")):
                shutil.move(system.path(f"{ROOTS}/{new_name}"), system.path(f"{ROOTS}/{self.old_name}"))
            shutil.copy(backup / "service", system.path(self.old_service))
            shutil.copy(backup / "conf", system.path(self.old_conf))
            if has_scripts:
                shutil.copytree(backup / "scripts", system.path(self.old_scripts), symlinks=True, dirs_exist_ok=True)
            elif before is not None:
                settings.write_copy(system, site.site_root, self.source.id, before)
            quiet("systemctl", "daemon-reload")
            quiet("systemctl", "enable", self.old_unit)
            quiet("a2ensite", f"{self.old_name}.conf")
            if system.run("apache2ctl", "configtest", check=False, capture=True).ok:
                quiet("systemctl", "reload", "apache2")
            system.run("systemctl", "start", self.old_unit, check=False)

        try:
            system.step(f"Stop {self.old_name}")
            system.run("systemctl", "stop", self.old_unit)
            system.say(f"Move {self.old_root} to {new_root}")
            shutil.move(system.path(self.old_root), system.path(new_root))
            if system.path(f"{ROOTS}/{self.old_name}").is_dir():
                shutil.move(system.path(f"{ROOTS}/{self.old_name}"), system.path(f"{ROOTS}/{new_name}"))
            system.step("Update the service and the Apache config")
            quiet("systemctl", "disable", self.old_unit)
            quiet("a2dissite", f"{self.old_name}.conf")
            # Only the folder and the name used for roots and logs change. The binding is left alone, also if it contains the name.
            old, new = re.escape(self.old_name), new_name
            service = (backup / "service").read_text().replace(self.old_root, new_root)
            service = re.sub(rf"(?m)^(Description=.*\[){old}\]", lambda match: f"{match[1]}{new}]", service)
            service = re.sub(rf"(?m)^(SyslogIdentifier=){old}$", lambda match: f"{match[1]}{new}", service)
            service = re.sub(rf"vns-roots/{old}\b", lambda match: f"vns-roots/{new}", service)
            system.path(new_service).write_text(service)
            conf = (backup / "conf").read_text().replace(self.old_root, new_root)
            conf = re.sub(rf"(/var/log/apache2/){old}-", lambda match: f"{match[1]}{new}-", conf)
            system.path(new_conf).write_text(conf)
            system.path(self.old_service).unlink(missing_ok=True)
            system.path(self.old_conf).unlink(missing_ok=True)
            system.run("systemctl", "daemon-reload")
            system.run("systemctl", "enable", new_unit, capture=True)
            system.run("a2ensite", f"{new_name}.conf", capture=True)
            system.run("apache2ctl", "configtest")
            system.run("systemctl", "reload", "apache2")
            system.run("systemctl", "start", new_unit)
            system.step("Update the environment's settings")
            # Where its settings are: its script, or Harbor's copy once the site is migrated
            settings.update(system, site.site_root, self.source.id, {"LOCAL_PATH": new_root, "FOLDER": new_name})
            for script in ("sync_prod.sh", "clear_logs_prod.sh"):
                path = system.path(f"{self.old_scripts}/{script}")
                if not path.is_file():
                    continue
                text = re.sub(r"(?m)^PROD_LOCAL_PATH=\S*", lambda match: f"PROD_LOCAL_PATH='{new_root}'", path.read_text())
                path.write_text(re.sub(r"(?m)^PROD_FOLDER=\S*", lambda match: f"PROD_FOLDER='{new_name}'", text))
        except BaseException as error:
            undo()
            shutil.rmtree(backup, ignore_errors=True)
            if isinstance(error, CommandFailed):
                raise Stop("", error.result.returncode or 1) from None
            raise
        shutil.rmtree(backup, ignore_errors=True)
        system.say(f"{self.old_name} now runs as {new_name} from {new_root}. The port is unchanged.")

        new_conf_text = system.path(new_conf).read_text()
        old_binding = next((line.split()[1] for line in new_conf_text.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")
        new_binding = self.request["BINDING"]
        if new_binding and new_binding.lower() != old_binding.lower():
            from harbor.environments.bindings import change_binding
            system.step(f"Change the binding to {new_binding}")
            aliases = " ".join(server_aliases_from_config(new_conf_text, old_binding))
            try:
                change_binding(system, site.name, new_binding, aliases, yes=True, target=self.old_id)
            except (Stop, CommandFailed) as error:
                if str(error) and isinstance(error, Stop):
                    system.warn(str(error))
                raise Stop("The site was moved, but its binding could not be changed. Use Change bindings on the environment.") from None
            # change_binding_prod changes appsettings.json
            scheme = "https" if re.search(r"(?m)^[ \t]*SSLEngine[ \t]+On([ \t]|$)", system.path(new_conf).read_text()) else "http"
            for file in sorted(system.path(new_root).glob("appsettings*.json")):
                if file.is_file():
                    file.write_text(re.sub(r'("UmbracoApplicationUrl"\s*:\s*")[^"]*', lambda match: f"{match[1]}{scheme}://{new_binding}", file.read_text()))
            system.run("systemctl", "restart", new_unit)

    # ---- to a remote Linux or Windows server
    def fetch_windows_source(self):
        """What a Windows site has that its build does not: its settings, its SQLite database and its files. Of umbraco
        only the database in umbraco/Data: logs, locks and TEMP are the running site's own, and a file IIS holds open
        cannot be read. Filters, not --include with --exclude, whose order rclone does not promise."""
        share = self.share(self.source)
        share.rclone("copy", share.target, self.content, "--filter", "- /appsettings-schema*.json", "--filter", "+ /appsettings*.json",
                     "--filter", "- *", "--modify-window", "2s", "--stats", "0")
        if share.rclone("lsf", f"{share.target}/umbraco/Data", "--max-depth", "1", check=False, capture=True).ok:
            self.system.say("Copy the SQLite database (umbraco/Data)")
            share.rclone("copy", f"{share.target}/umbraco/Data", f"{self.content}/umbraco/Data", "--max-depth", "1",
                         *[argument for pattern in ("+ *.sqlite.db", "+ *.sqlite.db-wal", "+ *.sqlite.db-shm", "- *") for argument in ("--filter", pattern)],
                         "--modify-window", "2s", "--stats", "0")
        for folder in ("uSync", "Views", "wwwroot", "App_Data"):
            if not share.rclone("lsf", f"{share.target}/{folder}", "--max-depth", "1", check=False, capture=True).ok:
                continue
            self.system.say(f"Copy {folder}")
            share.rclone("copy", f"{share.target}/{folder}", f"{self.content}/{folder}", "--create-empty-src-dirs",
                         *[argument for pattern in ("- /TEMP/**", "- /Logs/**", "- *.lock", "- *.partial", "+ **") for argument in ("--filter", pattern)],
                         "--modify-window", "2s", "--stats", "0")

    def fetch_source(self):
        """The source's folder as rsync sees it"""
        ssh, env, sudo, rsync = self.ssh(self.source)
        self.system.run(*rsync, f"{self.source.USER}@{self.source.IP}:{self.old_root}/", f"{self.content}/", env=env)

    def copy_everything(self, target_id):
        """The whole folder, to this server or a remote Linux server"""
        environment = self.environment(target_id)
        if environment.LOCAL_PATH:
            self.system.path(environment.root).mkdir(parents=True, exist_ok=True)
            self.system.run("rsync", "-a", f"{self.content}/", f"{self.local(environment.root)}/")
            return
        ssh, env, sudo, rsync = self.ssh(environment)
        host = f"{environment.USER}@{environment.IP}"
        self.system.run(*ssh, host, f"{sudo} mkdir -p {bash_quote(environment.root)}", env=env)
        self.system.run(*rsync, f"{self.content}/", f"{host}:{environment.root}/", env=env)

    def repoint_destination_sql(self, target_id):
        """A SQL Server on the source's machine stays there: the settings at the destination are pointed at that
        machine. Only the copy changes. The server must accept connections from the destination."""
        system = self.system
        environment = self.environment(target_id)
        same_server = not environment.windows and (
            (environment.LOCAL_PATH and not self.source_remote and not self.source_windows)
            or (not environment.LOCAL_PATH and environment.IP == self.source_host))
        new_connection = bool(os.environ.get("VNS_COPY_DATABASE") or os.environ.get("VNS_COPY_SQL"))
        if same_server and not new_connection:
            return
        with tempfile.TemporaryDirectory() as folder:
            files = sorted(Path(self.content).glob("appsettings*.json"))
            if not files:
                return
            for file in files:
                shutil.copy(file, folder)
            changed = False
            if new_connection:
                self.run("python3", str(LIB / "copy_site.py"), "update-connections", folder, os.environ.get("VNS_COPY_DATABASE", ""))
                changed = True
            # A connection entered by hand is used as it is. A new database name on the source's own SQL Server still needs its address.
            if not os.environ.get("VNS_COPY_SQL") and not same_server:
                repointed = self.run("python3", str(LIB / "copy_site.py"), "repoint-sql", folder, self.source_host, capture=True).stdout.rstrip("\n")
                if repointed != "0":
                    changed = True
                    system.say(f"The SQL connection now points to {self.source_host}. That SQL Server must accept connections from the new server.")
            if not environment.windows and not changed:
                return
            if environment.windows:
                # Publishing to Linux sets the URL on the server. On Windows it is set here, as a publish keeps existing settings.
                scheme = "http" if environment.HTTPS == "n" else "https"
                system.say(f"Set UmbracoApplicationUrl to {scheme}://{environment.BINDING}")
                for file in sorted(Path(folder).glob("appsettings*.json")):
                    self.run("python3", str(LIB / "windows_prod.py"), "set-url", str(file), f"{scheme}://{environment.BINDING}", environment.HTTPS)
                share = self.share(environment)
                share.rclone("copy", folder, share.target, "--modify-window", "2s", "--stats", "0")
            elif environment.LOCAL_PATH:
                for file in sorted(Path(folder).glob("appsettings*.json")):
                    shutil.copy(file, system.path(environment.root))
            else:
                ssh, env, sudo, rsync = self.ssh(environment)
                system.run(*rsync, f"{folder}/", f"{environment.USER}@{environment.IP}:{environment.root}/", env=env)

    def destination_is_free(self, target_id):
        """Never mix a migration or copy into a site that is already at the destination"""
        environment = self.environment(target_id)
        if environment.windows:
            share = self.share(environment)
            listing = share.rclone("lsf", share.target, "--max-depth", "1", "--files-only", check=False, capture=True).stdout
            if any(line.lower() == "appsettings.json" for line in listing.splitlines()):
                raise Stop(f"{environment.WINDOWS_SHARE} already holds a site. Empty the folder or choose another.")
        elif not environment.LOCAL_PATH:
            if settings.remote_exec(self.system, environment, f"test -e {bash_quote(environment.root)}", check=False).ok:
                raise Stop(f"{environment.root} already exists on {environment.IP}. Choose another folder.")

    def remove_new_linux_folder(self, target_id):
        environment = self.environment(target_id)
        if environment.windows:
            self.system.warn(f"Files already copied to {environment.WINDOWS_SHARE} are left there. Empty the folder before you try again.")
        elif environment.LOCAL_PATH:
            shutil.rmtree(self.system.path(environment.root), ignore_errors=True)
        elif not settings.remote_exec(self.system, environment, f"rm -rf {bash_quote(environment.root)}", check=False).ok:
            self.system.warn(f"Could not remove {environment.root} on {environment.IP}.")

    def server_architecture(self, target_id):
        environment = self.environment(target_id)
        if environment.LOCAL_PATH:
            return self.system.output("uname", "-m")
        return settings.remote_exec(self.system, environment, "uname -m", capture=True).stdout.rstrip("\n")

    def copy_content(self, target_id):
        """Settings and content, to a Windows server, which runs a build of its own"""
        environment = self.environment(target_id)
        folders = [folder for folder in ("umbraco", "uSync", "Views", "wwwroot", "App_Data") if Path(self.content, folder).is_dir()]
        share = self.share(environment)
        share.rclone("copy", self.content, share.target, "--include", "/appsettings*.json", "--exclude", "/appsettings-schema*.json",
                     "--modify-window", "2s", "--stats", "0")
        for folder in folders:
            self.system.say(f"Copy {folder}")
            share.rclone("copy", f"{self.content}/{folder}", f"{share.target}/{folder}", "--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0")

    def issue_wanted_certificate(self, target_id):
        """HTTPS was asked for on another server: it was set up with HTTP, and gets its certificate now that it runs
        there. If DNS does not point there yet, it stays on HTTP, as the move has worked."""
        if os.environ.get("VNS_MOVE_HTTPS") != "y" or self.destination == "windows":
            return
        from harbor.environments.actions import issue_certificate
        self.system.step(f"Issue a certificate for {self.request['BINDING'] or 'the binding'}")
        try:
            issue_certificate(self.system, self.site.name, target_id, yes=True)
        except (Stop, CommandFailed) as error:
            if isinstance(error, Stop) and str(error):
                self.system.warn(str(error))
            self.system.warn(f"WARNING: no certificate yet, as DNS for {self.request['BINDING'] or 'the binding'} does not point to the new server. "
                             "It answers on HTTP: use Issue certificate on it when DNS points there.")
            return
        settings.update(self.system, self.site.site_root, target_id, {"HTTPS": "y"})

    def new_id(self, number=2):
        name = (self.request["ENVIRONMENT"] or "production").lower()
        found = name
        taken = set(settings.target_ids(self.system, self.site.site_root))
        while os.path.lexists(self.system.path(f"{self.site.site_root}/scripts/targets/{found}")) or found in taken:
            found = f"{name}-{number}"
            number += 1
        return found

    def move_to_server(self):
        system, site = self.system, self.site
        moving = self.new_id() if self.copy else f"{self.old_id}-moving"
        final_id = self.new_id() if self.old_id == "prod" and not self.copy else self.old_id
        new_scripts = f"{site.site_root}/scripts/targets/{moving}"
        if os.path.lexists(system.path(new_scripts)):
            raise Stop(f"An earlier move left {new_scripts}. Remove it first.")
        if self.destination == "local":
            new_root = self.request["LOCAL_PATH"][:-1] if self.request["LOCAL_PATH"].endswith("/") else self.request["LOCAL_PATH"]
            new_name = os.path.basename(new_root)
            # A folder of its own under /var/www, or a subfolder of the main site's, as for a new environment
            if os.path.lexists(system.path(new_root)) or (new_root != f"{site.root_path}/{new_name}" and os.path.dirname(new_root) != site.site_root):
                raise Stop(f"The new folder must be a new folder directly under {site.root_path}, or in {site.site_root}.")
        elif (self.destination == "remote" and self.source_remote and self.request["IP"] == self.source.IP
              and self.request["FOLDER"] == self.old_name):
            raise Stop("The environment is already in that folder on that server.")
        self.content = self.local(self.old_root)
        fetch = None
        if self.source_windows:
            share = self.source.WINDOWS_SHARE
            self.source_host = (share[2:] if share.startswith("//") else share).split("/", 1)[0]
        elif self.source_remote:
            self.source_host = self.source.IP
        else:
            addresses = system.output("hostname", "-I", check=False).split()
            self.source_host = (addresses[0] if addresses else "") or system.output("hostname", "-f", check=False) or system.output("hostname", check=False)

        moved = source_stopped = destination_started = False
        try:
            # Also checks the login, before anything is stopped
            main = settings.load_main_site(system, site)
            site.project_name, site.binding, site.admin_mail = main.project_name, main.binding, main.admin_mail
            site.publish_folder, site.csharp_kind = main.publish_folder, main.csharp_kind
            created = run_create_prod(system, site, self.variables(VNS_PROD_ID=moving, VNS_PROD_NO_PUBLISH="1", VNS_PROD_MODAL="1"))
            if not created.ok:
                raise Failed(created.returncode)
            # The passwords are in the new environment's settings now: the site's scripts are not given them again
            self.request.update(PASSWORD="", SMB_PASSWORD="")
            self.destination_is_free(moving)

            if not self.copy or os.environ.get("VNS_COPY_STOP") == "y":
                system.step(f"Stop {self.old_name}, so its database and files stay consistent while they are copied")
                source_stopped = True
                self.source_control("stop")
            destination_started = True
            if self.source_remote or self.source_windows:
                # The servers have no access to each other, so the files go through this server
                fetch = tempfile.mkdtemp()
                self.content = fetch
                system.step(f"Fetch {self.old_root}")
                self.fetch_windows_source() if self.source_windows else self.fetch_source()

            if self.destination == "windows":
                # Windows runs another build, so only settings and content are copied
                self.copy_content(moving)
                self.repoint_destination_sql(moving)
                if self.copy and source_stopped:
                    source_stopped = False
                    self.source_control("start")
                system.step("Publish to the new server")
                self.publish(moving, VNS_MOVING="1")
            else:
                system.step("Copy everything to the destination")
                self.copy_everything(moving)
                self.repoint_destination_sql(moving)
                if self.copy and source_stopped:
                    source_stopped = False
                    self.source_control("start")
                architecture = ""
                if self.source_remote:
                    architecture = settings.remote_exec(system, self.source, "uname -m", capture=True).stdout.rstrip("\n")
                elif not self.source_windows:
                    architecture = system.output("uname", "-m")
                if self.source_windows:
                    system.step("A Windows build cannot run on Linux, so the project is built for it")
                    self.publish(moving, VNS_MOVING="1")
                elif architecture == self.server_architecture(moving):
                    system.step("Create the service and Apache config")
                    self.publish(moving, VNS_PUBLISH_TRACKED=f"target-{moving}", VNS_MIGRATE_ONLY="1")
                else:
                    system.step("The destination has another CPU architecture, so the project is built for it")
                    self.publish(moving, VNS_MOVING="1")
            moved = True
        except BaseException as error:
            if not moved:
                system.warn(f"The copy failed. {self.old_name} is not changed." if self.copy
                            else f"The migration failed, so {self.old_name} keeps running where it was.")
                if destination_started:
                    try:
                        self.remove_new_linux_folder(moving)
                    except (Stop, Failed, CommandFailed, OSError):
                        pass
                shutil.rmtree(system.path(new_scripts), ignore_errors=True)
                system.path(f"{site.site_root}/.vns-publish/target-{moving}.json").unlink(missing_ok=True)
                if settings.migrated(system, site.site_root):
                    settings.remove(system, site.site_root, moving)
                    system.path(f"{STATE}/{site.name}/publish/target-{moving}.json").unlink(missing_ok=True)
                if source_stopped:
                    try:
                        self.source_control("start")
                    except (Stop, CommandFailed):
                        system.warn(f"WARNING: could not start {self.old_name} again.")
            if fetch:
                shutil.rmtree(fetch, ignore_errors=True)
            if isinstance(error, Failed):
                raise Stop("", error.code) from None
            if isinstance(error, CommandFailed):
                raise Stop("", error.result.returncode or 1) from None
            raise

        if self.copy:
            if fetch:
                shutil.rmtree(fetch, ignore_errors=True)
            system.say(f"{self.old_name} is copied, and the copy is the environment {moving}. {self.old_name} is not changed.")
            self.issue_wanted_certificate(moving)
            return

        system.step(f"Remove {self.old_name} from where it was")
        delete_files = os.environ.get("VNS_MOVE_DELETE_LOCAL", "")
        if self.source_remote:
            header = f"UNIT={bash_quote(self.old_unit)}\nNAME={bash_quote(self.old_name)}\nROOT={bash_quote(self.old_root)}\nDELETE_FILES={bash_quote(delete_files)}\n"
            settings.remote_exec(system, self.source, "bash -s", input=header + REMOVE_REMOTE_SOURCE)
            if delete_files != "y":
                system.say(f"The files are kept in {self.old_root} on {self.source.IP}.")
        else:
            self.remove_local_service()
            if delete_files == "y":
                system.say(f"Delete the local files in {self.old_root}")
                shutil.rmtree(system.path(self.old_root), ignore_errors=True)
            else:
                system.say(f"The local files are kept in {self.old_root}.")

        if settings.migrated(system, site.site_root):
            # In Harbor's copy: the new environment takes the old one's id, with its publish status
            settings.write_copy(system, site.site_root, final_id, settings.values(system, site.site_root, moving))
            settings.remove(system, site.site_root, moving)
            if final_id != self.old_id:
                settings.remove(system, site.site_root, self.old_id)
            status = system.path(f"{STATE}/{site.name}/publish/target-{moving}.json")
            if status.is_file():
                os.replace(status, system.path(f"{STATE}/{site.name}/publish/target-{final_id}.json"))
            if fetch:
                shutil.rmtree(fetch, ignore_errors=True)
            self.issue_wanted_certificate(final_id)
            system.say(f"{self.old_name} has moved. Point DNS for {self.request['BINDING'] or 'the binding'} to the new server if you have not done so.")
            system.say("A certificate on this server for the old binding is not removed.")
            return
        publishes = f"{site.site_root}/.vns-publish"
        if self.old_id == "prod":
            for script in ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"):
                system.path(f"{site.site_root}/scripts/{script}").unlink(missing_ok=True)
            if system.path(f"{publishes}/prod.json").exists():
                os.replace(system.path(f"{publishes}/prod.json"), system.path(f"{publishes}/target-{final_id}.json"))
        else:
            shutil.rmtree(system.path(self.old_scripts), ignore_errors=True)
        shutil.move(system.path(new_scripts), system.path(f"{site.site_root}/scripts/targets/{final_id}"))
        if system.path(f"{publishes}/target-{moving}.json").is_file():
            os.replace(system.path(f"{publishes}/target-{moving}.json"), system.path(f"{publishes}/target-{final_id}.json"))
        if fetch:
            shutil.rmtree(fetch, ignore_errors=True)
        self.issue_wanted_certificate(final_id)
        system.say(f"{self.old_name} has moved. Point DNS for {self.request['BINDING'] or 'the binding'} to the new server if you have not done so.")
        system.say("A certificate on this server for the old binding is not removed.")

    def publish(self, target_id, **variables):
        """The new environment published: by its script, or by harbor for a migrated site (the script only calls it)"""
        site = self.site
        if settings.migrated(self.system, site.site_root):
            self.run("python3", str(BIN), "env", "publish", site.name, env=self.variables(VNS_TARGET=target_id, **variables))
        else:
            self.run("bash", f"{site.site_root}/scripts/targets/{target_id}/publish_prod.sh", env=self.variables(**variables))

    def remove_local_service(self):
        system = self.system
        system.run("systemctl", "stop", self.old_unit, check=False)
        system.run("systemctl", "disable", self.old_unit, check=False)
        system.path(self.old_service).unlink(missing_ok=True)
        system.run("a2dissite", f"{self.old_name}.conf", check=False, capture=True)
        system.path(self.old_conf).unlink(missing_ok=True)
        shutil.rmtree(system.path(f"{ROOTS}/{self.old_name}"), ignore_errors=True)
        system.run("systemctl", "daemon-reload")
        system.run("apache2ctl", "configtest")
        system.run("systemctl", "reload", "apache2")


def move_environment(system, name, target_id):
    move = Move(system, name, target_id)
    if move.destination == "local":
        if move.source_remote or move.source_windows or move.copy:
            move.move_to_server()
        else:
            move.move_local()
    elif move.destination in ("remote", "windows"):
        move.move_to_server()
    else:
        raise Stop("The destination must be local, remote or windows.")
