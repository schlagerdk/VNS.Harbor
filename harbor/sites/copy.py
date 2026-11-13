"""Copy a site to a new folder here, with its own service, port and Apache config. From copy_site.sh.

apache/lib/copy_site.py copies the files and rewrites them to the new name; it also runs on other servers (move_site.sh),
so it stays where it is and harbor calls it.
"""
import importlib.util
import json
import os
from pathlib import Path
import re

from harbor.core.bindings import validate_binding
from harbor.core.names import normalize_dns_name, normalize_site_name
from harbor.core.ports import allocate_port
from harbor.core.site_config import issue_certificate
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site

LIB = Path(__file__).resolve().parents[2] / "apache/lib"


def copy_module():
    import sys
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    spec = importlib.util.spec_from_file_location("vns_copy_site", LIB / "copy_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_site(system, source_name, target_name, binding, database="", project=None, connection=None):
    """project and connection default to VNS_COPY_PROJECT and VNS_COPY_SQL, as the script read them"""
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
    reservation = None
    try:
        if system.path(source.service_path).is_file():
            reservation = allocate_port(system, target_name)
        port = str(reservation.port) if reservation else ""
        https = bool(re.search(r"(?mi)^[ \t]*SSLEngine[ \t]+on", system.path(source.config_path).read_text()))
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
        if https:
            config = system.path(site.config_path)
            final = config.read_text()
            site.doc_root = site.site_root
            system.step(f"Issue a certificate for {binding}")
            try:
                issue_certificate(system, site, binding)
            except RuntimeError as error:
                raise Stop(str(error)) from None
            config.write_text(final)
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
                if not system.run("bash", f"{site.site_root}/scripts/{script.name}", check=False).ok:
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
