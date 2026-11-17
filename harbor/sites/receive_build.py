"""A C# site published from a developer's own machine: no project here, it runs the build it is sent, as an
environment of a site on another server does. The machine runs dotnet publish (linux-x64, framework-dependent),
rsyncs the build to /var/lib/harbor/incoming/<site>/ over SSH, then runs harbor site receive-build <site> here.

  The first time   the site is made from the build: folder, service, Apache config and, when asked, a certificate
                   (as harbor site setup does for a build put in /var/www by hand), with the build's appsettings
  Later            the build is copied into the folder the site runs from and the site is restarted. What the site
                   keeps there is never touched: appsettings*.json, umbraco/Data, umbraco/Logs, wwwroot/media,
                   App_Data, its connection to Harbor. Nothing is deleted in App_Plugins or uSync

  /var/lib/harbor/sites/<site>/local-publish.json   the site is published from a machine: its binding and when
"""
from datetime import datetime, timezone

from harbor.core.names import normalize_site_name
from harbor.core.system import Stop
from harbor.state.store import read_json, site_state, write_json

INCOMING = "/var/lib/harbor/incoming"
# Never sent over what the site keeps (rsync leaves excluded paths alone, also with --delete)
KEPT = ("/appsettings*.json", "/umbraco/Data/", "/umbraco/Logs/", "/umbraco/mediacache/", "/wwwroot/media/", "/App_Data/",
        "/app_data/", "/vns-harbor.json", "/logs/", "/scripts/", "/.vns-publish/")
# Updated, but nothing deleted there: packages' own files, and settings a later sync may have added
PROTECTED = ("/App_Plugins/**", "/wwwroot/App_Plugins/**", "/uSync/**")


def create_external(system, name, project, binding="", https="n", environment="Production"):
    """A site that is published from an external source, made now and running: an empty web project of that name is
    built here (as the build user, never root), its build put where a publish from a machine lands, and the project
    removed. The site is then set up from that build, as the first publish would. A project published to it later must
    build the same dll ({project}.dll)."""
    import shutil
    from harbor.core.dotnet import runtime_identifier
    from harbor.core.paths import LIB
    from harbor.core.templates import Site
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    site = Site(name)
    root = system.path(site.site_root)
    if root.exists():
        raise Stop(f"{site.site_root} exists already.")
    root.mkdir(parents=True)
    try:
        system.step(f"Create an empty web project {project}, built to start the site with")
        system.run("dotnet", "new", "web", "--name", project, "--output", ".", "--force", cwd=site.site_root)
        system.step(f"Build {project}.dll")
        identifier = runtime_identifier(system.output("uname", "-m"))
        system.run("python3", str(LIB / "build_as.py"), "--", "dotnet", "publish", f"{site.site_root}/{project}.csproj", "-c", "Release",
                   "-p:PublishDir=_publish", "--runtime", identifier, "--self-contained", "false", "--nologo", cwd=site.site_root)
        incoming = system.path(f"{INCOMING}/{name}")
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / "_publish"), str(incoming))
    finally:
        # Only the build is kept: the folder is made again from it, with no project
        shutil.rmtree(root, ignore_errors=True)
    receive_build(system, name, binding, https, environment)
    system.say(f"Publish {project} from your machine to replace it: it must build {project}.dll.")


def marker(system, name):
    return site_state(system, name) / "local-publish.json"


def receive_build(system, name, binding="", https="n", environment="Production"):
    from harbor.core.templates import Site
    from harbor.sites.tools import working_folder
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    incoming = system.path(f"{INCOMING}/{name}")
    if not any(incoming.glob("*.runtimeconfig.json")):
        raise Stop(f"No build in {INCOMING}/{name}: run the publish script on your machine, which sends it there.")
    site = Site(name)
    root = system.path(site.site_root)
    if not root.exists():
        from harbor.sites.setup import setup_site
        system.step(f"First publish: set up {name} from the build")
        root.mkdir(parents=True)
        system.run("rsync", "-a", f"{incoming}/", f"{root}/")
        setup_site(system, name, binding or name, "y" if https == "y" else "n", environment or "Production")
        found = {}
    else:
        if any(root.glob("*.csproj")):
            raise Stop(f"{name} has a project here: it is built here, not published from a machine.")
        found = read_json(marker(system, name))
        if not isinstance(found, dict):
            found = {}
        folder = working_folder(system, site)
        system.step(f"Copy the new build into {folder}")
        arguments = [*(argument for pattern in PROTECTED for argument in ("--filter", f"P {pattern}")),
                     *(argument for pattern in KEPT for argument in ("--exclude", pattern))]
        # By content: a new build can have the same sizes and times as the one there
        system.run("rsync", "-a", "--checksum", "--delete", *arguments, f"{incoming}/", f"{system.path(folder)}/")
        system.run("chown", "-R", "www-data:www-data", str(system.path(folder)), check=False)
        from harbor.sites.tools import keep_connection
        keep_connection(system, site, "main")
        system.step(f"Restart {name}")
        system.run("systemctl", "restart", site.service_name, check=False)
        system.run("systemctl", "status", site.service_name, "--no-pager", check=False)
    marker(system, name).parent.mkdir(parents=True, exist_ok=True)
    write_json(marker(system, name), {**found, "binding": binding or found.get("binding") or name,
                                       "published": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    system.say(f"{name} runs the build from your machine.")
