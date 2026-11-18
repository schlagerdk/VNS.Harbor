"""Migrate a site to another Linux server, or copy it there, with its environments on this server. From move_site.sh.

Environments on other servers or on Windows stay where they are: the site keeps its settings for them. Everything is set
up with HTTP on the new server, as DNS may not point there yet. With VNS_MOVE_HTTPS=y each site then gets a certificate
there; one whose DNS does not point there yet stays on HTTP, and Issue certificate gives it one when it does. The whole
folders go, also the builds, so nothing is built and the sites run the same version. A SQL Server on this machine stays
here: the copies' connections to it are pointed at this server's address.
A migration stops the sites here and removes their services and Apache configs once the new server runs them, and
VNS_MOVE_DELETE_LOCAL=y also deletes their files. A copy (VNS_MOVE_COPY=1) leaves them as they are; VNS_COPY_STOP=y stops
them while they are copied. If a step fails, the sites here run as before and what was made on the new server is removed.
VNS_MOVE_FOLDER, _BINDING and _PROJECT give the site a new folder, binding and project name there, as Copy site does here.
A new project name is built there, and only a site without environments can get one, as they run its build.

The new server sets the sites up with `move_site.sh --receive`, whichever VNS it has: receive() is that, with harbor.
"""
import os
from pathlib import Path
import re
import shutil
import tempfile

from harbor.core.bindings import validate_binding, validate_project_name
from harbor.core.names import service_name
from harbor.core.ports import allocate_port
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.environments.register import bash_quote, site_name
from harbor.core.paths import BIN, LIB
from harbor.state.store import SECRETS, STATE

ROOTS = "/var/lib/vns-roots"
SYSTEMD = "/etc/systemd/system"
SITES = "/etc/apache2/sites-available"
INCOMING = "/var/lib/vns-incoming"


def server_name_of(text):
    return next((line.split()[1] for line in text.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")


# ---- on the new server: one <name>.conf, and <name>.service when it has one, for every site that arrives
def receive_state(system, folder, root_path):
    """A migrated site's state from the old server, under its name here: environments in its folder follow it"""
    names = folder / "harbor-site"
    if not names.is_file():
        return
    source, target = (names.read_text().splitlines() + ["", ""])[:2]
    source, target = site_name(source), site_name(target)
    state = system.path(f"{STATE}/{target}")
    if state.exists():
        raise Stop(f"Harbor already has a site {target} on this server.")
    shutil.copytree(folder / "harbor-state", state)
    for path in (state / "environments").glob("*.json"):
        path.write_text(path.read_text().replace(f"{root_path}/{source}/", f"{root_path}/{target}/"))
    if (folder / "harbor-secrets").is_dir():
        secrets = system.path(f"{SECRETS}/{target}")
        shutil.copytree(folder / "harbor-secrets", secrets)
        for place in (system.path("/etc/harbor/secrets"), system.path(SECRETS), secrets):
            place.chmod(0o700)
        for path in secrets.glob("*.json"):
            path.chmod(0o600)


def reopen_folder(system, folder):
    """A site folder that came closed to others (its server had users, harbor/server/users.py) without the ACL that let
    www-data in: www-data must reach it here, so it is open to others again; closed again below when this server has users"""
    root = system.path(folder)
    if root.is_dir() and not root.is_symlink() and not root.stat().st_mode & 0o001:
        root.chmod(root.stat().st_mode | 0o005)


def close_folders_again(system):
    """This server's own users (if any) see only their sites: the folders that came are closed as the others are here"""
    from harbor.server.users import GROUP, keep_sites_closed, members
    if members(system, GROUP):
        keep_sites_closed(system, True)


def receive(system, incoming, sql_server=""):
    root_path = Site("x").root_path
    folder = system.path(incoming) if incoming else None
    if not incoming or not folder.is_dir():
        raise Stop(f"{incoming} was not found.")

    def python(*argv, quiet=False):
        result = system.run("python3", *argv, capture=quiet)
        return result
    # The site gets its new folder, binding or project name, like a copy here. Its environments in its folder follow.
    build = project = ""
    rename = folder / "rename"
    if rename.is_file():
        lines = rename.read_text().splitlines() + ["", "", "", ""]
        source, target, binding, project = lines[:4]
        source, target = site_name(source), site_name(target)
        system.say(f"Rename {source} to {target} ({binding})")
        options = ["--conf", str(folder / f"{source}.conf")]
        if (folder / f"{source}.service").is_file():
            options += ["--service", str(folder / f"{source}.service")]
        if project:
            options += ["--project", project]
        python(str(LIB / "copy_site.py"), "rename", str(system.path(f"{root_path}/{source}")), str(system.path(f"{root_path}/{target}")), binding, *options)
        if source != target:
            (folder / f"{source}.conf").rename(folder / f"{target}.conf")
            if (folder / f"{source}.service").is_file():
                (folder / f"{source}.service").rename(folder / f"{target}.service")
            for file in sorted([*folder.glob("*.conf"), *folder.glob("*.service")]):
                if file.is_file():
                    file.write_text(file.read_text().replace(f"{root_path}/{source}/", f"{root_path}/{target}/"))
        # A new project name has no build yet
        if project:
            build = target
    receive_state(system, folder, root_path)
    # All the steps: each site set up (and built, when its project was renamed), then the certificates asked for
    steps = []
    for conf in sorted(folder.glob("*.conf")):
        if conf.is_file():
            name = site_name(conf.name[:-len(".conf")])
            steps.append(f"Set up {name} with HTTP")
            if name == build and (folder / f"{name}.service").is_file():
                steps.append(f"Build {project}")
    if (folder / "certificates").is_file():
        lines = (folder / "certificates").read_text().splitlines()
        main = site_name(lines[0] if lines else "")
        steps += [f"Issue a certificate for {main}" if site_id == "main" else f"Issue a certificate for the environment {site_id}"
                  for site_id in lines[1:] if site_id == "main" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", site_id)]
    system.plan(steps)
    units = []
    for conf in sorted(folder.glob("*.conf")):
        if not conf.is_file():
            continue
        name = site_name(conf.name[:-len(".conf")])
        site = Site(name)
        if os.path.lexists(system.path(site.config_path)) or os.path.lexists(system.path(site.service_path)):
            raise Stop(f"{name} is already set up on this server.")
        reopen_folder(system, site.site_root)
        system.step(f"Set up {name} with HTTP")
        python(str(LIB / "move_site.py"), "http-conf", str(conf))
        binding = server_name_of(conf.read_text())
        folders = [site.site_root]
        service = folder / f"{name}.service"
        reservation = None
        try:
            if service.is_file():
                # The port it had may be taken here
                reservation = allocate_port(system, name)
                python(str(LIB / "move_site.py"), "port", str(reservation.port), str(service), str(conf))
                working = next((line[len("WorkingDirectory="):] for line in service.read_text().splitlines() if line.startswith("WorkingDirectory=")), "")
                working = working[:-1] if working.endswith("/") else working
                if working and working != site.site_root:
                    folders.append(working)
            for path in folders:
                if not system.path(path).is_dir():
                    continue
                if binding:
                    python(str(LIB / "move_site.py"), "http-settings", str(system.path(path)), binding, quiet=True)
                if not sql_server:
                    continue
                repointed = python(str(LIB / "copy_site.py"), "repoint-sql", str(system.path(path)), sql_server, quiet=True).stdout.rstrip("\n")
                if repointed != "0":
                    system.say(f"The SQL connection in {path} now points to {sql_server}, which must accept connections from this server.")
            shutil.copy(conf, system.path(site.config_path))
            system.run("a2ensite", f"{name}.conf", capture=True)
            if service.is_file():
                shutil.copy(service, system.path(site.service_path))
                system.path(f"{ROOTS}/{name}").mkdir(parents=True, exist_ok=True)
                system.run("systemctl", "daemon-reload")
                if re.search(r"(?m)^ExecStart=.*/dotnet ", service.read_text()):
                    if not system.run("python3", str(LIB / "sql_startup.py"), site.service_name, check=False).ok:
                        system.warn(f"Could not make {site.service_name} wait for SQL Server at boot.")
                system.run("systemctl", "enable", site.service_name, check=False, capture=True)
                if name == build:
                    system.step(f"Build {project}")
                    script = f"{site.site_root}/scripts/publish.sh"
                    if not system.path(script).is_file():
                        script = f"{site.site_root}/scripts/publish_local.sh"
                    # A migrated site has no publish script: harbor publishes it, as the script would
                    argv = ["bash", script] if system.path(script).is_file() else ["python3", str(BIN), "site", "publish", name]
                    if not system.run(*argv, check=False).ok:
                        raise Stop(f"{name} could not be built on this server.")
                else:
                    system.run("systemctl", "start", site.service_name)
                units.append(site.service_name)
        finally:
            if reservation:
                reservation.release()
    # Environments on this server are published with HTTP from now on, until a certificate is issued
    scripts = folder / "http-scripts"
    if scripts.is_file():
        for script in scripts.read_text().splitlines():
            if script.startswith("environment:"):
                main = (folder / "harbor-site").read_text().splitlines()[1]
                settings.update(system, f"{root_path}/{main}", script[len("environment:"):], {"HTTPS": "n"})
                continue
            path = system.path(script) if script.startswith("/") else None
            if path and path.is_file():
                path.write_text(re.sub(r"(?m)^PROD_HTTPS=\S*", "PROD_HTTPS='n'", path.read_text()))
    close_folders_again(system)
    system.run("apache2ctl", "configtest")
    system.run("systemctl", "reload", "apache2")
    for unit in units:
        if not system.run("systemctl", "is-active", "--quiet", unit, check=False).ok:
            raise Stop(f"{unit} did not start.")
    # HTTPS was asked for: a certificate for each site, now that it runs here. One whose DNS does not point here yet
    # stays on HTTP, as the move has worked.
    certificates = folder / "certificates"
    if certificates.is_file():
        from harbor.environments.actions import issue_certificate as issue_for_environment
        from harbor.sites.actions import issue_certificate as issue_for_site
        lines = certificates.read_text().splitlines()
        main = site_name(lines[0] if lines else "")
        failed = []
        for site_id in lines[1:]:
            try:
                if site_id == "main":
                    system.step(f"Issue a certificate for {main}")
                    issue_for_site(system, main, yes=True)
                    continue
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", site_id):
                    continue
                system.step(f"Issue a certificate for the environment {site_id}")
                issue_for_environment(system, main, site_id, yes=True)
                settings.update(system, f"{root_path}/{main}", site_id, {"HTTPS": "y"})
            except (Stop, CommandFailed) as error:
                if isinstance(error, Stop) and str(error):
                    system.warn(str(error))
                failed.append(main if site_id == "main" else site_id)
        if failed:
            system.warn(f"WARNING: no certificate for {' '.join(failed)}, as DNS does not point to this server yet. "
                        "They answer on HTTP: use Issue certificate on them when it does.")
    shutil.rmtree(folder, ignore_errors=True)


# ---- on this server
def move_site(system, name):
    site = Site(site_name(name))
    copy = os.environ.get("VNS_MOVE_COPY", "")
    verb = "Copy" if copy else "Migrate"
    if not system.path(site.site_root).is_dir():
        raise Stop(f"The folder {site.site_root} does not exist.")
    if not system.path(site.config_path).is_file():
        raise Stop(f"{site.name} has no Apache config.")
    managed = system.path(f"/var/lib/vns-managed/{site.name}")
    if managed.is_file():
        lines = managed.read_text().splitlines() + ["", ""]
        raise Stop(f"{site.name} is an environment of a main site on {lines[1]}. Move it from there.")

    host = os.environ.get("VNS_MOVE_IP", "")
    user = os.environ.get("VNS_MOVE_USER") or "root"
    password = os.environ.pop("VNS_MOVE_PASSWORD", "")
    port = os.environ.get("VNS_MOVE_SSH_PORT") or "22"
    if not host:
        raise Stop("Enter the new server.")
    if not port.isdigit():
        raise Stop("The SSH port must be a number.")

    # The site's name, binding and project on the new server
    old_binding = server_name_of(system.path(site.config_path).read_text())
    new_site = site_name(os.environ.get("VNS_MOVE_FOLDER") or site.name)
    new_binding = os.environ.get("VNS_MOVE_BINDING") or old_binding
    project = os.environ.get("VNS_MOVE_PROJECT", "")
    try:
        validate_binding(new_binding)
        projects = sorted(system.path(site.site_root).glob("*.csproj"))
        if project and projects and project == projects[0].stem:
            project = ""
        if project:
            validate_project_name(project)
    except ValueError as error:
        raise Stop(str(error)) from None
    targets = settings.target_ids(system, site.site_root)
    if project and targets:
        raise Stop(f"{site.name} has environments, which run the build it makes, so its project cannot get a new name.")
    rename = new_site != site.name or new_binding.lower() != old_binding.lower() or bool(project)

    # The sites that go: the site, and its environments on this server. Their names are their folders, services and
    # configs, here and on the new server, where the site can have another.
    names, new_names = [site.name], [new_site]
    folders, new_folders = [site.site_root], [f"{site.root_path}/{new_site}"]
    http_scripts, certificates, kept = [], ["main"], []
    for target_id in targets:
        environment = settings.load(system, site.site_root, target_id)
        if environment.windows:
            kept.append(f"{environment.BINDING} on Windows ({environment.WINDOWS_SHARE})")
            continue
        if not environment.LOCAL_PATH:
            kept.append(f"{environment.BINDING} on {environment.IP}")
            continue
        if settings.migrated(system, site.site_root):
            # Its settings travel in Harbor's state: the receiving server sets HTTP there
            http_scripts.append(f"environment:{target_id}")
        else:
            script = f"{Path(environment.script).parent}/publish_prod.sh"
            http_scripts.append(
                f"{site.root_path}/{new_site}/{script[len(site.site_root) + 1:] if script.startswith(site.site_root + '/') else script}")
        if system.path(f"{SITES}/{environment.FOLDER}.conf").is_file():
            names.append(environment.FOLDER)
            new_names.append(environment.FOLDER)
            certificates.append(target_id)
        # One in the site's folder goes with it
        if not environment.root.startswith(f"{site.site_root}/") and system.path(environment.root).is_dir():
            folders.append(environment.root)
            new_folders.append(environment.root)

    ssh = ["ssh", "-p", port, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
           "-o", "ServerAliveCountMax=2"]
    env = {}
    if password:
        if not system.which("sshpass"):
            raise Stop("sshpass is required for password login: sudo apt install sshpass")
        env["SSHPASS"] = password
        ssh = ["sshpass", "-e", *ssh]
    else:
        # Harbor's own key too: a server added in Servers needs no password
        from harbor.environments.settings import harbor_key
        ssh += ["-o", "BatchMode=yes", *harbor_key()]
    sudo = "" if user == "root" else "sudo -n"

    def on_new_server(command, check=True):
        return system.run(*ssh, f"{user}@{host}", f"{sudo} {command}", env=env, check=check)

    def copy_to_new_server(source, destination):
        system.run("rsync", "-aHz", "-e", " ".join(ssh), f"--rsync-path={sudo + ' ' if sudo else ''}rsync", source, f"{user}@{host}:{destination}", env=env)

    stop_here = not copy or os.environ.get("VNS_COPY_STOP") == "y"
    system.plan([f"Check {host}", *(["Stop the sites here, so their databases and files stay consistent while they are copied"] if stop_here else []),
                 *(f"Copy {source} to {host}:{destination}" for source, destination in zip(folders, new_folders)),
                 f"Set up the sites on {host}", *([] if copy else ["Remove the sites here"])])
    # ---- before anything is stopped: the new server must be ready, and have none of the sites
    system.step(f"Check {host}")
    # The new server's own harbor sets the sites up (harbor site receive)
    check = f"set -e; test -x /usr/local/bin/harbor || {{ echo {bash_quote(f'{host} has no Harbor: install it there under Server → Servers.')} >&2; exit 1; }}"
    check += (f"; test \"$(uname -m)\" = {bash_quote(system.output('uname', '-m'))} || {{ echo '{host} has another CPU architecture than this server, "
              "so the builds cannot run there.' >&2; exit 1; }")
    for old, new in zip(names, new_names):
        unit = service_name(new)
        check += (f"; ! test -e {bash_quote(f'{SYSTEMD}/{unit}.service')} -o -e {bash_quote(f'{SITES}/{new}.conf')} || "
                  f"{{ echo {bash_quote(f'{new} already exists on {host}.')} >&2; exit 1; }}")
        local_service = system.path(f"{SYSTEMD}/{service_name(old)}.service")
        if local_service.is_file() and re.search(r"(?m)^ExecStart=.*/usr/lib/dotnet/dotnet ", local_service.read_text()):
            check += f"; test -x /usr/lib/dotnet/dotnet || {{ echo '.NET is not installed on {host}.' >&2; exit 1; }}"
    for folder in new_folders:
        check += f"; ! test -e {bash_quote(folder)} || {{ echo {bash_quote(f'{folder} already exists on {host}.')} >&2; exit 1; }}"
    try:
        on_new_server(f"bash -c {bash_quote(check)}")
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None

    # The source's own SQL Server stays: the copies are pointed at this server
    addresses = system.output("hostname", "-I", check=False).split()
    sql_server = (addresses[0] if addresses else "") or system.output("hostname", "-f", check=False) or system.output("hostname", check=False)

    staging = Path(tempfile.mkdtemp())
    incoming = f"{INCOMING}/{site.name}-{os.getpid()}"
    stopped = []
    started = done = False
    try:
        for each in names:
            unit = service_name(each)
            shutil.copy(system.path(f"{SITES}/{each}.conf"), staging / f"{each}.conf")
            if system.path(f"{SYSTEMD}/{unit}.service").is_file():
                shutil.copy(system.path(f"{SYSTEMD}/{unit}.service"), staging / f"{each}.service")
        if http_scripts:
            (staging / "http-scripts").write_text("".join(f"{line}\n" for line in http_scripts))
        if rename:
            (staging / "rename").write_text(f"{site.name}\n{new_site}\n{new_binding}\n{project}\n")
        if settings.migrated(system, site.site_root):
            # A migrated site (phase 6) has its settings in Harbor's state, not its folder: they go with it
            (staging / "harbor-site").write_text(f"{site.name}\n{new_site}\n")
            shutil.copytree(system.path(f"{STATE}/{site.name}"), staging / "harbor-state")
            if system.path(f"{SECRETS}/{site.name}").is_dir():
                shutil.copytree(system.path(f"{SECRETS}/{site.name}"), staging / "harbor-secrets")
        if os.environ.get("VNS_MOVE_HTTPS") == "y":
            (staging / "certificates").write_text("".join(f"{line}\n" for line in [new_site, *certificates]))

        if stop_here:
            system.step("Stop the sites here, so their databases and files stay consistent while they are copied")
            for each in names:
                unit = service_name(each)
                if not system.path(f"{SYSTEMD}/{unit}.service").is_file():
                    continue
                if not system.run("systemctl", "is-active", "--quiet", unit, check=False).ok:
                    continue
                stopped.append(unit)
                system.run("systemctl", "stop", unit)

        started = True
        for source, destination in zip(folders, new_folders):
            system.step(f"Copy {source} to {host}:{destination}")
            on_new_server(f"mkdir -p {bash_quote(destination)}")
            copy_to_new_server(f"{system.path(source)}/", f"{destination}/")
        on_new_server(f"mkdir -p {bash_quote(incoming)}")
        copy_to_new_server(f"{staging}/", f"{incoming}/")
        system.step(f"Set up the sites on {host}")
        on_new_server(f"/usr/local/bin/harbor site receive {bash_quote(incoming)} {bash_quote(sql_server)}")
        done = True
    except BaseException as error:
        if not done:
            system.warn(f"The {verb.lower()} failed, so {site.name} runs here as before.")
            if started:
                system.warn(f"Remove what was made on {host}")
                cleanup = ""
                for each in new_names:
                    unit = service_name(each)
                    cleanup += f"systemctl stop {bash_quote(unit)} 2>/dev/null; systemctl disable {bash_quote(unit)} 2>/dev/null; "
                    cleanup += (f"rm -f {bash_quote(f'{SYSTEMD}/{unit}.service')} {bash_quote(f'/etc/apache2/sites-enabled/{each}.conf')} "
                                f"{bash_quote(f'{SITES}/{each}.conf')}; ")
                    cleanup += f"rm -rf {bash_quote(f'{ROOTS}/{each}')}; "
                for folder in new_folders:
                    cleanup += f"rm -rf {bash_quote(folder)}; "
                if settings.migrated(system, site.site_root):
                    # Its Harbor state there too, or another try finds the site there
                    cleanup += f"rm -rf {bash_quote(f'{STATE}/{new_site}')} {bash_quote(f'{SECRETS}/{new_site}')}; "
                cleanup += f"rm -rf {bash_quote(incoming)}; systemctl daemon-reload; apache2ctl configtest >/dev/null 2>&1 && systemctl reload apache2; true"
                if not on_new_server(f"bash -c {bash_quote(cleanup)}", check=False).ok:
                    system.warn(f"Could not clean up {host}. Remove the sites there by hand.")
            for unit in stopped:
                if not system.run("systemctl", "start", unit, check=False).ok:
                    system.warn(f"WARNING: could not start {unit} again.")
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, CommandFailed):
            raise Stop("", error.result.returncode or 1) from None
        raise
    shutil.rmtree(staging, ignore_errors=True)

    if copy:
        for unit in stopped:
            system.run("systemctl", "start", unit)
        system.say(f"{site.name} is copied to {host} as {new_site} ({new_binding}). It is not changed here.")
    else:
        system.step("Remove the sites here")
        for each in names:
            unit = service_name(each)
            system.run("systemctl", "disable", unit, check=False, capture=True)
            system.path(f"{SYSTEMD}/{unit}.service").unlink(missing_ok=True)
            system.run("a2dissite", f"{each}.conf", check=False, capture=True)
            system.path(f"{SITES}/{each}.conf").unlink(missing_ok=True)
            shutil.rmtree(system.path(f"{ROOTS}/{each}"), ignore_errors=True)
        system.run("systemctl", "daemon-reload")
        system.run("apache2ctl", "configtest")
        system.run("systemctl", "reload", "apache2")
        if os.environ.get("VNS_MOVE_DELETE_LOCAL") == "y":
            for folder in folders:
                system.say(f"Delete {folder}")
                shutil.rmtree(system.path(folder), ignore_errors=True)
        else:
            system.say(f"The files are kept here: {' '.join(folders)}")
        system.say(f"{site.name} has moved to {host} as {new_site} ({new_binding}). A certificate on this server is not removed.")
    if os.environ.get("VNS_MOVE_HTTPS") == "y":
        system.say(f"On {host}: {' '.join(new_names)}. A site without a certificate there answers on HTTP until Issue certificate gives it one.")
    else:
        system.say(f"On {host} the sites answer on HTTP: {' '.join(new_names)}. When DNS points there, use Issue certificate on each of them.")
    if kept:
        system.say(f"Environments on other servers are not moved; {site.name} still publishes to them: {' '.join(kept)}")
