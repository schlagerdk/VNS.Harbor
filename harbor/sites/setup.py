"""Set up a folder that was put in /var/www by hand, like Create site, without overwriting what is in it. From setup_site.sh.

  a C# project (one .csproj): the site's scripts, and a build in _publish unless one is there already
  a build (a dll with its .runtimeconfig.json, in the folder or in _publish): run as it is, nothing is built
  anything else: a static HTML site
Only the service, the Apache config, the certificate and missing scripts are made. The folder's files are not changed,
except that the folder the site runs from is owned by www-data, and a project gets its publish folder left out of its build.
"""
import os
from pathlib import Path
import re

from harbor.core.bindings import exclude_from_build, site_binding, validate_project_name
from harbor.core.dotnet import runtime_identifier
from harbor.core.names import normalize_site_name
from harbor.core.ports import allocate_port
from harbor.core.site_config import (default_site, install_apache_config, install_service, issue_certificate,
                                     make_default_site)
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site, render
from harbor.server.apache import own_name
from harbor.core.paths import LIB, SITE_TEMPLATES

SOLUTION = SITE_TEMPLATES


def install_missing_scripts(system, site, *kinds):
    """The tools of a kind of site: their names kept for the admin's buttons (migrate.SITE_SCRIPTS)"""
    from harbor.sites.create import install_scripts
    install_scripts(system, site, *kinds)


def what_is_in(system, site):
    """('project'|'build'|'html', project name, publish folder, kind)"""
    root = system.path(site.site_root)
    projects = sorted(root.glob("*.csproj"))
    if projects:
        if len(projects) != 1:
            raise Stop(f"{site.name} has more than one .csproj. A site needs exactly one project.")
        name = projects[0].stem
        try:
            validate_project_name(name)
        except ValueError as error:
            raise Stop(str(error)) from None
        assembly = re.search(r"<AssemblyName>(.*)</AssemblyName>", projects[0].read_text(errors="replace"))
        if assembly and assembly[1] and assembly[1] != name:
            raise Stop(f"{name}.csproj builds {assembly[1]}.dll. A site's project must build a dll with its own name.")
        kind = "umbraco" if "Umbraco.Cms" in projects[0].read_text(errors="replace") else "empty"
        return "project", name, "_publish", kind
    for folder in (".", "_publish"):
        path = root / folder if folder != "." else root
        if not path.is_dir():
            continue
        runtime = next(iter(sorted(path.glob("*.runtimeconfig.json"))), None)
        if runtime is None:
            continue
        name = runtime.name[:-len(".runtimeconfig.json")]
        if not (path / f"{name}.dll").is_file():
            continue
        deps = path / f"{name}.deps.json"
        kind = "umbraco" if deps.is_file() and '"Umbraco.Cms/' in deps.read_text(errors="replace") else "empty"
        return "build", name, folder, kind
    if (root / "app/app.py").is_file():
        raise Stop(f"{site.name} holds a Python app. Setting up a Python site from a folder is not supported: create it with Create site.")
    return "html", "", ".", ""


def setup_site(system, name="", binding="", certificate="", environment=""):
    if not name:
        name = system.ask("Enter folder name: ").strip()
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    site = Site(name)
    root = system.path(site.site_root)
    if not root.is_dir() or root.is_symlink():
        raise Stop(f"{site.site_root} is not a folder.")
    if system.path(site.service_path).exists():
        raise Stop(f"{name} is already set up: it has a service.")

    kind_of_folder, project, publish, kind = what_is_in(system, site)
    site.project_name = project
    site.publish_folder = publish
    site.csharp_kind = kind
    if system.path(site.config_path).exists():
        if kind_of_folder == "html":
            raise Stop(f"{name} is already set up: it has an Apache config.")
        system.say(f"{name} has an Apache config but no service: the setup is finished and the config is replaced")
    shown = "C#" if kind == "empty" else kind
    if kind_of_folder == "project":
        system.say(f"Found: the {shown} project {project}.csproj")
    elif kind_of_folder == "build":
        system.say(f"Found: the {shown} build {project}.dll in {'the folder' if publish == '.' else publish}")
    else:
        system.say("Found: static files, set up as an HTML site")

    if not certificate:
        certificate = system.ask("Create an HTTPS certificate? [Y/n]: ").strip() or "y"
    if certificate.lower() in ("y", "yes"):
        certificate = "y"
    elif certificate.lower() in ("n", "no"):
        certificate = "n"
    else:
        raise Stop("Certificate option must be y or n.")
    if not binding:
        if "." in name:
            binding = system.ask(f"Enter site URL binding (* = answer any name) [{name}]: ").strip() or name
        else:
            binding = system.ask("Enter site URL binding (* = answer any name): ").strip()
    answers_any = binding == "*"
    if answers_any:
        current = default_site(system)
        if current:
            raise Stop(f"{current} already answers any name. Only one site can.")
        binding = own_name(system).lower()
        if certificate == "y" and "." not in binding:
            raise Stop(f"This server's name, {binding}, has no dot, so it cannot get a certificate.")
    try:
        binding = site_binding(binding, http_only=certificate == "n")
    except ValueError as error:
        raise Stop(str(error)) from None
    site.binding = binding
    mine = os.path.realpath(system.path(site.config_path))
    for enabled in sorted(system.path("/etc/apache2/sites-enabled").glob("*.conf")):
        if not os.path.lexists(enabled) or os.path.realpath(enabled) == mine or not enabled.exists():
            continue
        for line in enabled.read_text(errors="replace").splitlines():
            fields = line.split()
            if fields[:1] in (["ServerName"], ["ServerAlias"]) and binding in (field.lower() for field in fields[1:]):
                raise Stop(f"{binding} is already used by {'/' + str(enabled.relative_to(system.root)).lstrip('/')}")
    if kind_of_folder != "html":
        environment = environment or ("Production" if kind_of_folder == "build" else "Test")
        if environment not in ("Development", "Test", "Staging", "Production"):
            raise Stop("Environment must be Development, Test, Staging or Production.")
        site.environment = environment

    reservation = None
    try:
        if kind_of_folder == "html":
            site.doc_root = site.site_root
            if certificate == "y":
                system.step(f"Issue a certificate for {binding}")
                issue_certificate(system, site, binding)
            install_apache_config(system, site, SOLUTION / "html/apache.conf.tpl", certificate=certificate == "y")
            install_missing_scripts(system, site, "common")
        else:
            kind_folder = SOLUTION / ("csharp/umbraco" if kind == "umbraco" else "csharp")
            site.doc_root = f"{site.publish_root}/wwwroot"
            site.build_config = "Release" if environment == "Production" else "Debug"
            reservation = allocate_port(system, name)
            site.port_http = str(reservation.port)
            system.path(site.doc_root).mkdir(parents=True, exist_ok=True)
            system.path(f"/var/lib/vns-roots/{name}").mkdir(parents=True, exist_ok=True)
            if certificate == "y":
                system.step(f"Issue a certificate for {binding}")
                issue_certificate(system, site, binding)
            # A project's own tools (publish, outdated, create_prod, change_env) only for a project: a build is run as it is
            install_missing_scripts(system, site, "common", "service", *(["umbraco"] if kind == "umbraco" else []))
            if kind_of_folder == "project":
                install_missing_scripts(system, site, "csharp")
                csproj = system.path(f"{site.site_root}/{project}.csproj")
                if f"{publish}/**" not in csproj.read_text():
                    system.say(f"Exclude {publish} from the project's build")
                    csproj.write_text(exclude_from_build(csproj.read_text(), publish))
                if system.path(f"{site.publish_root}/{project}.dll").is_file():
                    system.say(f"{publish} already holds a build: it is used as it is")
                else:
                    system.step(f"Build the project into {publish}")
                    identifier = runtime_identifier(system.output("uname", "-m"))
                    system.run("python3", str(LIB / "publish_status.py"), "run", site.site_root, "test", "python3",
                               str(LIB / "build_as.py"), "--", "dotnet", "publish", f"{site.site_root}/{project}.csproj",
                               "-c", site.build_config, f"-p:PublishDir={publish}",
                               f"-p:CustomAfterMicrosoftCommonTargets={LIB}/publish.targets", "--runtime", identifier,
                               "--self-contained", "false", "--nologo", cwd=site.site_root)
            # The service runs as www-data and writes inside the folder it runs from
            system.run("chown", "-R", "www-data:www-data", site.publish_root)
            # Its scripts stay root's, as the admin runs them as root: also where the site runs from its own folder
            scripts = system.path(f"{site.site_root}/scripts")
            if scripts.is_dir():
                system.run("chown", "-R", "root:root", str(scripts))
                system.run("chmod", "-R", "go-w", str(scripts))
            conf = kind_folder / "apache.conf.tpl"
            service = kind_folder / "service.tpl"
            install_apache_config(system, site, conf if conf.is_file() else SOLUTION / "csharp/apache.conf.tpl", certificate=certificate == "y")
            install_service(system, site, service if service.is_file() else SOLUTION / "csharp/service.tpl")
    except (CommandFailed, RuntimeError, ValueError) as error:
        raise Stop(str(error)) from None
    finally:
        if reservation:
            reservation.release()
    if answers_any:
        try:
            make_default_site(system, site)
        except RuntimeError as error:
            raise Stop(str(error)) from None
    scheme = "https" if certificate == "y" else "http"
    system.say(f"Site set up: {scheme}://{binding}/ (folder: {name})")
    if answers_any:
        system.say("It also answers any name that no other site has.")
