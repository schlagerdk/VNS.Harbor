"""The environments of a site and their settings, and running commands where one is. From lib/prod.sh: prod_targets,
load_main_site, load_prod_settings, prod_exec and prod_run.

Each environment has its settings in <site>/scripts/targets/<id>/publish_prod.sh, or in <site>/scripts/publish_prod.sh
for one made before several were possible (id "prod"). Only the PROD_* assignments in it are read, as the script does.
"""
from dataclasses import dataclass, field, fields
import json
import re
import shlex

TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ASSIGNMENT = re.compile(r"PROD_[A-Z_]+=")


from harbor.core.system import Stop  # noqa: E402


class SettingsError(Stop, ValueError):
    """Settings that cannot be used: the command stops with the message, as the script did"""


@dataclass
class Environment:
    """An environment's settings. What the script does not set is empty, and the SSH port 22."""
    id: str = "prod"
    script: str = ""
    LOCAL_PATH: str = ""
    IP: str = ""
    USER: str = ""
    PASSWORD: str = field(default="", repr=False)
    SSH_PORT: str = "22"
    FOLDER: str = ""
    BINDING: str = ""
    PORT_HTTP: str = ""
    ADMIN_MAIL: str = ""
    HTTPS: str = ""
    TARGET: str = ""
    WINDOWS_SHARE: str = ""
    SMB_USER: str = ""
    SMB_PASSWORD: str = field(default="", repr=False)
    SMB_DOMAIN: str = ""
    other: dict = field(default_factory=dict)

    @property
    def windows(self):
        return self.TARGET == "windows"

    @property
    def local(self):
        return not self.windows and bool(self.LOCAL_PATH)

    @property
    def description(self):
        """Where it is, as the script's _target says"""
        if self.windows:
            return self.WINDOWS_SHARE
        if self.LOCAL_PATH:
            return "this server"
        return f"{self.USER}@{self.IP}:{self.SSH_PORT}"

    @property
    def root(self):
        """Its folder where it runs, as the script's _root"""
        if self.windows:
            return self.WINDOWS_SHARE
        if self.LOCAL_PATH:
            # As ${PROD_LOCAL_PATH%/}: one trailing slash goes
            return self.LOCAL_PATH[:-1] if self.LOCAL_PATH.endswith("/") else self.LOCAL_PATH
        return f"/var/www/{self.FOLDER}"


SETTINGS = {item.name for item in fields(Environment)} - {"id", "script", "other"}


def script_ids(system, site_root):
    """The environments that have a script in the site folder: the older single one first, then scripts/targets"""
    root = system.path(site_root)
    ids = ["prod"] if (root / "scripts/publish_prod.sh").is_file() else []
    targets = root / "scripts/targets"
    if targets.is_dir():
        ids += sorted(path.name for path in targets.iterdir() if path.is_dir() and (path / "publish_prod.sh").is_file())
    return ids


def target_ids(system, site_root):
    """The id of every environment of a site. While the site folder has any of their scripts it is the truth;
    without, Harbor's copy is (harbor/state)."""
    ids = script_ids(system, site_root)
    if ids:
        return ids
    copy = system.path(f"{STATE}/{site_name(site_root)}/environments")
    found = sorted(path.stem for path in copy.glob("*.json")) if copy.is_dir() else []
    return (["prod"] if "prod" in found else []) + [item for item in found if item != "prod" and TARGET.fullmatch(item)]


def settings_script(site_root, target="prod"):
    if target != "prod":
        if not TARGET.fullmatch(target):
            raise SettingsError(f"Invalid environment: {target}")
        return f"{site_root}/scripts/targets/{target}/publish_prod.sh"
    return f"{site_root}/scripts/publish_prod.sh"


def parse(text):
    """The PROD_* values a script sets, as bash reads them: quotes, escapes and comments as in the shell"""
    values = {}
    for line in text.splitlines():
        if not ASSIGNMENT.match(line):
            continue
        try:
            words = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise SettingsError(f"Could not read the line {line.split('=', 1)[0]}: {error}") from None
        for word in words:
            name, equals, value = word.partition("=")
            if equals and re.fullmatch(r"PROD_[A-Z_]+", name):
                values[name[len("PROD_"):]] = value
    return values


# Harbor's copy of the state (harbor/state/store.py): read when the site folder no longer has the script
STATE = "/var/lib/harbor/sites"
SECRETS = "/etc/harbor/secrets/sites"


def site_name(site_root):
    return str(site_root).rstrip("/").rsplit("/", 1)[-1]


def values(system, site_root, target="prod"):
    """An environment's settings, its passwords too: from its script in the site folder while that is there, else from
    Harbor's copy. None when it has neither."""
    path = system.path(settings_script(site_root, target))
    if path.is_file():
        return parse(path.read_text())
    name = site_name(site_root)
    copy = system.path(f"{STATE}/{name}/environments/{target}.json")
    if not copy.is_file():
        return None
    try:
        found = json.loads(copy.read_text())
        secret = system.path(f"{SECRETS}/{name}/{target}.json")
        if secret.is_file():
            found.update(json.loads(secret.read_text()))
    except (OSError, ValueError) as error:
        raise SettingsError(f"Could not read Harbor's settings of {name} {target}: {error}") from None
    return {key: str(value) for key, value in found.items()}


def load(system, site_root, target="prod"):
    """An environment's settings, as load_prod_settings reads them"""
    script = settings_script(site_root, target)
    found = values(system, site_root, target)
    if found is None:
        raise SettingsError(f"This site has no production site (missing {script}).")
    environment = Environment(id=target, script=script)
    for name, value in found.items():
        if name in SETTINGS:
            setattr(environment, name, value)
        else:
            environment.other[name] = value
    if not environment.FOLDER or not environment.BINDING:
        raise SettingsError(f"PROD_FOLDER and PROD_BINDING must be set in {script}")
    return environment


def ssh_command(environment, tty=False):
    """The ssh to an environment's server, and the variables it needs: the password goes in SSHPASS, never in argv"""
    argv = ["ssh", "-p", environment.SSH_PORT, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    if tty:
        argv.append("-tt")
    env = {}
    if environment.PASSWORD:
        env["SSHPASS"] = environment.PASSWORD
        argv = ["sshpass", "-e", *argv]
    else:
        argv += ["-o", "BatchMode=yes"]
    return argv, env


def remote_exec(system, environment, command, tty=False, input=None, check=True, capture=False):
    """Run a shell command as root where the environment is: here with bash, else over SSH (with sudo -n when not root)"""
    if environment.windows:
        raise SettingsError("This action is not available for a Windows production site. It has no SSH access: use IIS on the server.")
    if environment.LOCAL_PATH:
        return system.run("sudo", "bash", "-c", command, check=check, capture=capture, input=input)
    argv, env = ssh_command(environment, tty)
    sudo = "" if environment.USER == "root" else "sudo -n"
    return system.run(*argv, f"{environment.USER}@{environment.IP}", f"{sudo} {command}", check=check, capture=capture,
                      input=input, env=env)


def remote_run(system, environment, variables, script, check=True):
    """Run a bash script as root where the environment is, with the given variables (prod_run)"""
    return remote_exec(system, environment, f"env {variables} bash -s", input=script, check=check)


@dataclass
class MainSite:
    """What create_prod.sh is rendered with, read from the main site (load_main_site)"""
    project_name: str
    binding: str
    admin_mail: str
    publish_folder: str
    csharp_kind: str


def load_main_site(system, site):
    projects = sorted(system.path(site.site_root).glob("*.csproj"))
    if len(projects) != 1:
        raise SettingsError(f"{site.name} needs exactly one project.")
    config = system.path(site.config_path).read_text() if system.path(site.config_path).is_file() else ""
    binding = next((line.split()[1] for line in config.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")
    if not binding:
        raise SettingsError(f"{site.name} has no ServerName.")
    mail = next((line.split()[1] for line in config.splitlines() if line.split()[:1] == ["ServerAdmin"] and len(line.split()) > 1), "")
    service = system.path(site.service_path)
    working = ""
    if service.is_file():
        working = next((line[len("WorkingDirectory="):] for line in service.read_text().splitlines() if line.startswith("WorkingDirectory=")), "")
    working = working.rstrip("/")
    publish = "." if not working or working == site.site_root else working.rsplit("/", 1)[-1]
    kind = "umbraco" if "Umbraco.Cms" in projects[0].read_text(errors="replace") else "empty"
    return MainSite(projects[0].stem, binding, mail or site.admin_mail, publish, kind)
