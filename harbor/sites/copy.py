"""Copy a site to a new folder here, with its own service, port and Apache config. From copy_site.sh.

lib/copy_site.py copies the files and rewrites them to the new name; it also runs on other servers (move_site.sh),
so it stays where it is and harbor calls it.
"""
import importlib.util
import json
import os
from pathlib import Path
import re

from harbor.core.bindings import validate_binding
from harbor.core.names import normalize_dns_name, normalize_site_name, service_name
from harbor.core.ports import allocate_port
from harbor.core.site_config import configure_umbraco_https, enable_https_config, http_only, issue_certificate
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.core.paths import BIN, LIB



def copy_module():
    import sys
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    spec = importlib.util.spec_from_file_location("vns_copy_site", LIB / "copy_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def carry_migrated_state(system, source_name, target_name):
    """A copy of a migrated site (phase 6) is migrated too: it has no scripts folder either. It is a site of its own,
    so it takes the source's publish options and Sync setup for the site itself, never its environments."""
    from harbor.state.store import STATE, read_json, write_json
    source, target = system.path(f"{STATE}/{source_name}"), system.path(f"{STATE}/{target_name}")
    if not (source / "migrated.json").is_file():
        return
    write_json(target / "migrated.json", {**(read_json(source / "migrated.json") or {}), "copied_from": source_name})
    if read_json(source / "site.json"):
        write_json(target / "site.json", read_json(source / "site.json"))
    setup = read_json(source / "data_sync.json")
    if isinstance(setup, dict) and "main" in setup:
        write_json(target / "data_sync.json", {"main": setup["main"]})


def copy_site(system, source_name, target_name, binding, database="", project=None, connection=None, stop_source=False, remove_source=""):
    """project and connection default to VNS_COPY_PROJECT and VNS_COPY_SQL, as the script read them.

    stop_source stops the source while it is copied, so a SQLite database is copied as it is, and starts it again.
    remove_source makes it a move: the source is stopped, and once the copy runs, its service and Apache config are
    removed ("config", the folder stays) or the whole site ("all"). Only a site without environments can be moved
    so, as the copy leaves the environments out."""
    try:
        source_name = normalize_site_name(source_name)
        target_name = normalize_site_name(target_name)
        binding = normalize_dns_name(binding, fully_qualified=True)
        validate_binding(binding)
    except ValueError as error:
        raise Stop(str(error)) from None
    if project is None:
        project = os.environ.get("VNS_COPY_PROJECT", "")
    if connection is None and os.environ.get("VNS_COPY_SQL"):
        connection = json.loads(os.environ.pop("VNS_COPY_SQL"))
    site = Site(target_name, binding=binding)
    source = Site(source_name)
    managed = system.path(f"/var/lib/vns-managed/{source_name}")
    if managed.is_file():
        lines = managed.read_text().splitlines() + ["", ""]
        raise Stop(f"{source_name} is an environment of the main site {lines[0]} on {lines[1]}. Make the copy from there.")
    if remove_source not in ("", "config", "all"):
        raise Stop("Remove the source with config or all.")
    if remove_source and settings.target_ids(system, source.site_root):
        raise Stop(f"{source_name} has environments, which a copy leaves out. Move them first, or copy it and delete it afterwards.")
    # The environments here that go with the copy, as environments of it (VNS_COPY_ENVIRONMENTS: [{id, binding}])
    copies = environment_copies(system, source, site, json.loads(os.environ.pop("VNS_COPY_ENVIRONMENTS", "") or "[]"))
    if copies and remove_source:
        raise Stop("A move here takes no environments: copy the site with them, and delete it afterwards.")
    unit = source.service_name if system.path(source.service_path).is_file() else ""
    stopped = bool(unit) and (stop_source or bool(remove_source)) and system.run("systemctl", "is-active", "--quiet", unit, check=False).ok
    # All the steps, as copy_files takes them
    https = copy_https(system, source)
    build = bool(project) and not system.path(f"{source.site_root}/{project}.csproj").is_file()
    system.plan([*([f"Stop {source_name} while it is copied"] if stopped else []), f"Copy {source_name} to {target_name}",
                 *([f"Issue a certificate for {binding}"] if https else []),
                 *(([f"Build {project}"] if build else [f"Start {target_name}"]) if system.path(source.service_path).is_file() else []),
                 *[f"Copy {copy['label']} to {copy['binding']}" for copy in copies],
                 *([f"Start {source_name} again"] if stopped and not remove_source else [])])
    if stopped:
        system.step(f"Stop {source_name} while it is copied")
        system.run("systemctl", "stop", unit)
    try:
        copy_files(system, site, source, source_name, target_name, binding, database, project, connection)
        for copy in copies:
            copy_environment(system, target_name, copy, project, stop_source)
    except Stop:
        if stopped:
            system.run("systemctl", "start", unit, check=False)
        raise
    if stopped and not remove_source:
        system.step(f"Start {source_name} again")
        system.run("systemctl", "start", unit, check=False)
    if remove_source:
        remove(system, source_name, remove_source)


def environment_copies(system, source, site, chosen):
    """The environments here that go with a copy here, checked before anything is copied: each with its new folder and
    binding. One on another server is not copied: two main sites would publish to one site"""
    if not chosen:
        return []
    if not isinstance(chosen, list):
        raise Stop("Choose the environments to copy.")
    if not settings.migrated(system, source.site_root):
        raise Stop(f"{source.name} still has its scripts: migrate it first (harbor migrate {source.name}), then copy it with its environments.")
    found, folders, bindings = [], set(), {site.binding.lower()}
    for item in chosen:
        target_id, binding = (item.get("id"), item.get("binding")) if isinstance(item, dict) else (None, None)
        if target_id not in settings.target_ids(system, source.site_root):
            raise Stop(f"{source.name} has no environment {target_id}.")
        environment = settings.load(system, source.site_root, target_id)
        if not environment.LOCAL_PATH:
            raise Stop(f"{environment.BINDING} is not on this server: only an environment here is copied with the site.")
        try:
            binding = normalize_dns_name(binding or "", fully_qualified=True)
            validate_binding(binding)
        except ValueError as error:
            raise Stop(f"{environment.BINDING}: {error}") from None
        # Its folder named as the copy is: staging-shop.dk becomes staging-copy.shop.dk
        folder = environment.FOLDER.replace(source.name, site.name) if source.name in environment.FOLDER else f"{target_id}-{site.name}"
        try:
            folder = normalize_site_name(folder)
        except ValueError as error:
            raise Stop(f"{folder}: {error}") from None
        root = environment.root.rstrip("/")
        new_root = site.site_root + root[len(source.site_root):] if root.startswith(f"{source.site_root}/") else f"{site.root_path}/{folder}"
        if (folder in folders or binding.lower() in bindings or system.path(f"/etc/systemd/system/{folder}.service").exists()
                or system.path(f"/etc/apache2/sites-available/{folder}.conf").exists() or (not new_root.startswith(f"{site.site_root}/") and system.path(new_root).exists())):
            raise Stop(f"{folder} or {binding} is taken: choose another binding for {environment.BINDING}.")
        for path in system.path("/etc/apache2/sites-available").glob("*.conf"):
            if re.search(r"(?mi)^\s*(?:ServerName|ServerAlias)\s+.*(?<!\S)" + re.escape(binding) + r"(?!\S)", path.read_text()):
                raise Stop(f"{binding} is used by {path.stem} already.")
        folders.add(folder)
        bindings.add(binding.lower())
        found.append({"id": target_id, "label": environment.other.get("ENVIRONMENT") or target_id.capitalize(), "binding": binding, "folder": folder,
                      "root": root, "new_root": new_root, "unit": service_name(environment.FOLDER)})
    return found


def copy_environment(system, target_name, copy, project, stop_source):
    """An environment copied as one of the copy's: its folder as it is (data, settings and build), registered with its
    new folder and binding, then set up and started without a build (with one when the copy has a new project name, as
    its dll is named after it). On HTTP: Issue certificate gives it one when its DNS points here"""
    system.step(f"Copy {copy['label']} to {copy['binding']}")
    running = stop_source and system.run("systemctl", "is-active", "--quiet", copy["unit"], check=False).ok
    if running:
        system.run("systemctl", "stop", copy["unit"])
    try:
        system.path(copy["new_root"]).mkdir(parents=True, exist_ok=True)
        system.run("rsync", "-aH", f"{system.path(copy['root'])}/", f"{system.path(copy['new_root'])}/")
    finally:
        if running:
            system.run("systemctl", "start", copy["unit"], check=False)
    created = system.run("python3", str(BIN), "env", "create", target_name, "local", check=False, env={
        "VNS_PROD_MODAL": "1", "VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": copy["new_root"], "VNS_PROD_FOLDER": copy["folder"],
        "VNS_PROD_BINDING": copy["binding"], "VNS_PROD_ENVIRONMENT": copy["label"], "VNS_PROD_HTTPS": "n", "VNS_PROD_ID": copy["id"],
        "VNS_PROD_NO_PUBLISH": "1"})
    if not created.ok:
        raise Stop(f"{copy['label']} was copied to {copy['new_root']}, but could not be made an environment of {target_name}.", created.returncode)
    published = system.run("python3", str(BIN), "env", "publish", target_name, check=False,
                           env={"VNS_TARGET": copy["id"], **({} if project else {"VNS_MIGRATE_ONLY": "1"})})
    if not published.ok:
        raise Stop(f"{copy['label']} is an environment of {target_name}, but did not start: see its service.", published.returncode)


def remove(system, name, what):
    """The source of a move, once the copy runs: its service and Apache config, or all of it"""
    from harbor.sites.delete import Deletion, delete_site
    if what == "all":
        delete_site(system, name, "y", "all")
        return
    site = Site(name)
    binding = next((line.split()[1] for line in system.path(site.config_path).read_text().splitlines()
                    if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "") if system.path(site.config_path).is_file() else ""
    deletion = Deletion(system, site, "y")
    system.plan([*deletion.service_steps(name), *(deletion.certificate_steps(binding, {name}) if binding and "." in binding else [])])
    deletion.service_and_config(name)
    # Its certificate goes unless the copy kept the binding
    if binding and "." in binding:
        deletion.certificate(binding, site.certificate_directory)
    system.run("systemctl", "daemon-reload", check=False)
    system.run("apache2ctl", "configtest", check=False)
    system.run("systemctl", "reload", "apache2", check=False)
    system.say(f"{name} no longer runs here. Its folder {site.site_root} is kept.")


def copy_https(system, source):
    """Whether the copy gets HTTPS: as chosen (VNS_COPY_HTTPS y or n, the admin's Set up with HTTPS, offered when DNS
    points here), else as the source has it"""
    chosen = os.environ.get("VNS_COPY_HTTPS", "")
    if chosen in ("y", "n"):
        return chosen == "y"
    return system.path(source.config_path).is_file() and bool(re.search(r"(?mi)^[ \t]*SSLEngine[ \t]+on", system.path(source.config_path).read_text()))


def copy_files(system, site, source, source_name, target_name, binding, database, project, connection):
    reservation = None
    try:
        if system.path(source.service_path).is_file():
            reservation = allocate_port(system, target_name)
        port = str(reservation.port) if reservation else ""
        https = copy_https(system, source)
        # A renamed project has no dll with the new name yet: it is built instead of just started
        build = bool(project) and not system.path(f"{source.site_root}/{project}.csproj").is_file()
        system.step(f"Copy {source_name} to {target_name}")
        try:
            copy_module().prepare(source_name, target_name, binding, port, web_root=system.path(site.root_path),
                                  configs=system.path("/etc/apache2/sites-available"), services=system.path("/etc/systemd/system"),
                                  database=database, connection=connection, project=project)
        except (OSError, ValueError) as error:
            raise Stop(f"Copy site failed: {error}") from None
        system.path(f"/var/lib/vns-roots/{target_name}").mkdir(parents=True, exist_ok=True)
        carry_migrated_state(system, source_name, target_name)
        # The copy's own connection to Harbor, before it starts (the package reads it then): its vns-harbor.json came with
        # its files and named the original, whose environments its dashboard showed. An environment copied with it gets
        # its own as it is set up
        from harbor.umbraco import backoffice
        backoffice.reconnect(system, target_name)
        config = system.path(site.config_path)
        secure = bool(re.search(r"(?mi)^[ \t]*SSLEngine[ \t]+on", config.read_text()))
        if secure and not https:
            # A copy without HTTPS of a site with it: its HTTPS site on port 80, without the certificate it has not got
            config.write_text(http_only(config.read_text()))
        if https:
            final = config.read_text()
            site.doc_root = site.site_root
            system.step(f"Issue a certificate for {binding}")
            try:
                issue_certificate(system, site, binding)
            except RuntimeError as error:
                raise Stop(str(error)) from None
            config.write_text(final)
            if not secure:
                # The source had HTTP only: its config made HTTPS with the new certificate
                try:
                    enable_https_config(system, site, binding)
                except RuntimeError as error:
                    raise Stop(str(error)) from None
        # Umbraco's own idea of its address follows: its URL and UseHttps in the copy's settings
        working = re.search(r"(?m)^WorkingDirectory=(.+)$", system.path(site.service_path).read_text()) if system.path(site.service_path).is_file() else None
        settings_file = f"{working[1].strip()}/appsettings.json" if working else ""
        if settings_file and system.path(settings_file).is_file() and secure != https:
            try:
                configure_umbraco_https(system, settings_file, binding, https)
            except (OSError, ValueError):
                system.warn(f"Umbraco's address in {settings_file} was not changed to {'https' if https else 'http'}://{binding}")
        system.run("apache2ctl", "configtest")
        system.run("a2ensite", f"{target_name}.conf")
        service = system.path(site.service_path)
        if service.is_file():
            system.run("systemctl", "daemon-reload")
            if re.search(r"(?m)^ExecStart=.*/dotnet ", service.read_text()):
                system.run("python3", str(LIB / "sql_startup.py"), site.service_name)
            system.run("systemctl", "enable", site.service_name)
            if build:
                system.step(f"Build {project}")
                script = system.path(f"{site.site_root}/scripts/publish.sh")
                if not script.is_file():
                    script = system.path(f"{site.site_root}/scripts/publish_local.sh")
                # A migrated site has no publish script: harbor publishes it, as the script would
                argv = (["bash", f"{site.site_root}/scripts/{script.name}"] if script.is_file()
                        else ["python3", str(BIN), "site", "publish", target_name])
                if not system.run(*argv, check=False).ok:
                    raise Stop(f"The copy could not be built. It is in {site.site_root}: fix the build and run its Publish.")
            else:
                system.step(f"Start {target_name}")
                system.run("systemctl", "start", site.service_name)
            system.run("systemctl", "is-active", "--quiet", site.service_name)
        system.run("systemctl", "reload", "apache2")
    except (Stop, CommandFailed) as error:
        message = str(error)
        if message:
            system.warn(message)
        system.warn("FAILED: Copy is incomplete. Inspect the new site before retrying; the source was not changed.")
        raise Stop("", error.code if isinstance(error, Stop) else (error.result.returncode or 1)) from None
    finally:
        if reservation:
            reservation.release()
    system.say(f"Site copied: {source_name} -> {target_name} ({binding})")
    if database or connection is not None:
        system.say("SQL connections were updated. The database and login must already exist; neither was created.")
    else:
        system.say("Database connections were copied unchanged; no database was duplicated.")
