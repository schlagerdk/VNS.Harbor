"""Sites moved from a Windows server (IIS) to this one, while the Windows server stays live: the sites are fetched over
SMB, built here and set up on HTTP with their own bindings, kept the same as the live sites until the server's address
moves, and then taken over.

  list       the folders in a share (//server/share/folder): which hold a project, which only a build, and
             which static files (an HTML site, such as a CDN)
  import     a folder with a project: copied here without its builds, set up as a site (HTTP, Production), and its data
             fetched into the folder it runs from. A folder of static files is copied as it is, as an HTML site
  refresh    the data fetched again from the live site: the SQLite database, media and uSync; for an HTML site, all its
             files. With pause, the site on Windows is taken offline (app_offline.htm) while its database is copied
  take-over  after the address has moved: the temporary aliases removed, a certificate issued (HTTPS), and the Windows
             source forgotten. Nothing on Windows is changed

A site with only a build (.NET, with its runtimeconfig.json) is not imported: it is an environment of a site with its
project, and Copy from Windows (harbor env windows-move) copies it, built from that project. A classic ASP.NET site
(Umbraco 7 and older, .NET Framework) runs on Windows only, and is not imported either.

  /var/lib/harbor/sites/<site>/windows-source.json        where it came from: the share, the folder, the login
  /etc/harbor/secrets/sites/<site>/windows-source.json    the login's password (root only)
"""
import re
from types import SimpleNamespace

from harbor.core.names import normalize_site_name
from harbor.core.system import CommandFailed, Stop
from harbor.state.store import read_json, site_secrets, site_state, write_json

# Not copied when a site is imported: builds and what the site writes while it runs
LEFT_OUT = ("/bin/**", "/obj/**", "/*.dll", "/*.pdb", "/*.exe", "/runtimes/**", "/refs/**", "/web.config", "/app_offline.htm",
            "/umbraco/Data/TEMP/**", "/umbraco/Logs/**", "/App_Data/TEMP/**", "/App_Data/Logs/**", "/.vs/**", "*.user", "*.lock", "*.partial")
# What refresh fetches: the data that changes while the site is live. SQLite with its journal files.
DATA = (("umbraco/Data", ("*.sqlite.db", "*.sqlite.db-wal", "*.sqlite.db-shm")), ("wwwroot/media", ()), ("uSync", ()))
UMBRACO_VERSION = re.compile(r'<PackageReference\s+Include="Umbraco\.Cms"\s+Version="([^"]+)"', re.I)
# A build's deps.json: the Umbraco it was built with, and the runtime it was published for (win-x64 is Windows only)
BUILT_UMBRACO = re.compile(r'"Umbraco\.Cms/([0-9][^"]*)"')
RUNTIME_TARGET = re.compile(r'"runtimeTarget"\s*:\s*\{\s*"name"\s*:\s*"[^"/]*/?([^"]*)"')


def share(system, unc, user, password, domain="", folder=""):
    """A folder in a Windows share, logged in"""
    from harbor.environments.windows import Share
    path = unc.rstrip("/") + (f"/{folder}" if folder else "")
    environment = SimpleNamespace(WINDOWS_SHARE=path, SMB_USER=user, SMB_PASSWORD=password, SMB_DOMAIN=domain, other={"OFFLINE_WAIT": "5"})
    try:
        return Share(system, environment).connect()
    except RuntimeError as error:
        raise Stop(str(error)) from None


def list_folders(system, unc, user, password, domain=""):
    """Each folder in the share: what it holds (project, build or other), its Umbraco version when it has a project,
    and the site here it was imported as"""
    root = share(system, unc, user, password, domain)
    folders = [line.rstrip("/") for line in root.rclone("lsf", root.target, "--dirs-only", "--max-depth", "1", capture=True).stdout.splitlines() if line.strip()]
    imported = {(entry.get("unc", "").rstrip("/").lower(), entry.get("folder", "").lower()): name for name, entry in _imported(system)}
    found = []
    for folder in sorted(folders, key=str.lower):
        files = root.rclone("lsf", f"{root.target}/{folder}", "--files-only", "--max-depth", "1", check=False, capture=True).stdout.splitlines()
        projects = [name for name in files if name.lower().endswith(".csproj")]
        lower = {name.lower() for name in files}
        # A classic ASP.NET site (Umbraco 7 and older, .NET Framework) has Global.asax and its dlls in bin: it runs on
        # Windows only. Static files only with a start page: a backup or an empty folder is not a site.
        runtimes = [name for name in files if name.lower().endswith(".runtimeconfig.json")]
        kind = ("project" if projects else "framework" if "global.asax" in lower
                else "build" if runtimes
                else "framework" if any(name.endswith(".dll") for name in lower)
                else "html" if lower & {"index.html", "index.htm", "default.htm", "default.html"} else "other")
        umbraco = windows_only = ""
        if kind == "build":
            deps = root.rclone("cat", f"{root.target}/{folder}/{runtimes[0][:-len('.runtimeconfig.json')]}.deps.json", check=False, capture=True).stdout
            match = BUILT_UMBRACO.search(deps)
            umbraco = match[1] if match else ""
            target = RUNTIME_TARGET.search(deps)
            windows_only = target[1] if target and target[1].lower().startswith("win") else ""
        if len(projects) == 1:
            text = root.rclone("cat", f"{root.target}/{folder}/{projects[0]}", check=False, capture=True).stdout
            match = UMBRACO_VERSION.search(text)
            umbraco = match[1] if match else ""
        if kind == "build":
            projects = []
        found.append({"folder": folder, "kind": kind, "project": projects[0][:-len(".csproj")] if len(projects) == 1 else "",
                      "projects": len(projects), "umbraco": umbraco, "windows_only": windows_only,
                      "imported": imported.get((unc.rstrip("/").lower(), folder.lower()), "")})
    return found


def _imported(system):
    from harbor.environments.settings import STATE
    folder = system.path(STATE)
    for path in sorted(folder.glob("*/windows-source.json")) if folder.is_dir() else []:
        entry = read_json(path)
        if isinstance(entry, dict):
            yield path.parent.name, entry


def source(system, name):
    """Where a site came from, with the password, or Stop when it was not imported from Windows"""
    entry = read_json(site_state(system, name) / "windows-source.json")
    secret = read_json(site_secrets(system, name) / "windows-source.json")
    if not isinstance(entry, dict) or not entry.get("unc"):
        raise Stop(f"{name} was not imported from Windows (or has been taken over).")
    return {**entry, "password": (secret or {}).get("password", "")}


def keep_source(system, name, unc, folder, user, password, domain, kind="project"):
    write_json(site_state(system, name) / "windows-source.json", {"unc": unc.rstrip("/"), "folder": folder, "user": user, "domain": domain, "kind": kind})
    secrets = site_secrets(system, name)
    secrets.mkdir(parents=True, exist_ok=True)
    secrets.chmod(0o700)
    write_json(secrets / "windows-source.json", {"password": password}, private=True)


def import_site(system, unc, folder, binding, user, password, domain="", aliases=""):
    """A Windows site with a project, here: copied without its builds, set up on HTTP as Production, and its data fetched"""
    from harbor.core.templates import Site
    from harbor.sites.setup import setup_site
    try:
        name = normalize_site_name(binding)
    except ValueError:
        raise Stop("Give the site's binding: a DNS name of letters, numbers, dots and hyphens.") from None
    site = Site(name)
    if system.path(site.site_root).exists():
        raise Stop(f"{site.site_root} exists already.")
    remote = share(system, unc, user, password, domain, folder)
    files = remote.rclone("lsf", remote.target, "--files-only", "--max-depth", "1", capture=True).stdout.splitlines()
    lower = {item.lower() for item in files}
    project = any(item.endswith(".csproj") for item in lower)
    build = not project and any(item.endswith(".runtimeconfig.json") for item in lower)
    if build:
        raise Stop(f"{folder} is only a build: copy it with Copy from Windows, as an environment of the site with its project.")
    if not project and ("global.asax" in lower or any(item.endswith(".dll") for item in lower)):
        raise Stop(f"{folder} is a classic ASP.NET site (.NET Framework): it runs on Windows only.")
    kind = "project" if project else "html"
    system.step(f"Copy {folder} from Windows" + {"project": "", "html": ", as static files"}[kind])
    # A project without its builds; static files as they are. IIS's own files never.
    left_out = LEFT_OUT if project else ("/web.config", "/app_offline.htm")
    arguments = [argument for pattern in left_out for argument in ("--exclude", pattern)]
    system.path(site.site_root).mkdir(parents=True)
    try:
        remote.rclone("copy", remote.target, str(system.path(site.site_root)), *arguments, "--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0")
    except CommandFailed:
        import shutil
        shutil.rmtree(system.path(site.site_root), ignore_errors=True)
        raise Stop(f"{folder} could not be copied from Windows.") from None
    keep_source(system, name, unc, folder, user, password, domain, kind)
    system.step(f"Set up {name} on HTTP" + (", as an HTML site" if kind == "html" else ", as Production"))
    setup_site(system, name, binding, "n", "Production")
    if aliases:
        from harbor.sites.bindings import change_binding
        system.step(f"Add the temporary aliases {aliases}")
        change_binding(system, name, binding, aliases, yes=True)
    refresh(system, name)
    system.say(f"{name} runs here on HTTP. Refresh fetches the live site's data again; take over when the address has moved.")


def import_all(system, unc, sites, user, password, domain=""):
    """Several folders, one at a time: one that fails is told, and the rest go on"""
    failed = []
    for item in sites:
        folder, binding = item.get("folder", ""), item.get("binding") or item.get("folder", "")
        system.step(f"==== {folder} as {binding}")
        try:
            import_site(system, unc, folder, binding, user, password, domain, item.get("aliases", ""))
        except (Stop, CommandFailed) as error:
            system.warn(f"{folder}: FAILED{': ' + str(error) if str(error) else ''}")
            failed.append(folder)
    if failed:
        raise Stop(f"Not imported: {', '.join(failed)}")
    system.say(f"Imported {len(sites)} site(s). Refresh fetches their data again; take over when the address has moved.")


def imported(system):
    """The sites here that were imported from Windows and are not taken over yet"""
    return sorted(name for name, _ in _imported(system))


def refresh_all(system, pause=False):
    """Every site imported from Windows, one at a time"""
    names = imported(system)
    if not names:
        raise Stop("No site here was imported from Windows (or all have been taken over).")
    failed = []
    for name in names:
        system.step(f"==== {name}")
        try:
            refresh(system, name, pause=pause)
        except (Stop, CommandFailed) as error:
            system.warn(f"{name}: FAILED{': ' + str(error) if str(error) else ''}")
            failed.append(name)
    if failed:
        raise Stop(f"Not refreshed: {', '.join(failed)}")


def run_folder(system, name):
    """Where the site runs from: its data belongs there"""
    from harbor.core.templates import Site
    from harbor.sites.tools import working_folder
    return working_folder(system, Site(name))


def refresh(system, name, pause=False):
    """The live site's data fetched again: the SQLite database (stopping the site here while it is replaced), media and
    uSync. With pause, the site on Windows shows its offline page while its database is copied."""
    from harbor.core.templates import Site
    found = source(system, name)
    remote = share(system, found["unc"], found["user"], found["password"], found.get("domain", ""), found["folder"])
    site = Site(name)
    here = run_folder(system, name)
    if found.get("kind") == "html":
        # Static files: all of them, as they are on Windows (a file deleted there stays here, as with media)
        system.step("Copy the site's files")
        remote.rclone("copy", remote.target, str(system.path(here)), "--exclude", "/web.config", "--exclude", "/app_offline.htm",
                      "--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0")
        system.run("chown", "-R", "www-data:www-data", str(system.path(here)), check=False)
        system.say(f"{name} has the live site's files as they were now.")
        return
    database = DATA[0]
    has_database = remote.rclone("lsf", f"{remote.target}/{database[0]}", *[a for p in database[1] for a in ("--include", p)],
                                 check=False, capture=True).stdout.strip()
    stopped = False
    try:
        if has_database:
            if pause:
                remote.take_offline()
            system.step("Stop the site here while its database is replaced")
            system.run("systemctl", "stop", site.service_name, check=False)
            stopped = True
            target = system.path(f"{here}/{database[0]}")
            target.mkdir(parents=True, exist_ok=True)
            # A journal that is not on Windows any more must not be applied to the new database here
            for stale in target.glob("*.sqlite.db-*"):
                stale.unlink()
            system.step("Copy the SQLite database")
            remote.rclone("copy", f"{remote.target}/{database[0]}", str(target), *[a for p in database[1] for a in ("--include", p)],
                          "--modify-window", "2s", "--stats", "0")
        for folder, _ in DATA[1:]:
            if not remote.rclone("lsf", f"{remote.target}/{folder}", "--max-depth", "1", check=False, capture=True).ok:
                continue
            system.step(f"Copy {folder}")
            remote.rclone("copy", f"{remote.target}/{folder}", str(system.path(f"{here}/{folder}")), "--create-empty-src-dirs",
                          "--modify-window", "2s", "--stats", "0")
    finally:
        if pause:
            try:
                remote.bring_online()
            except RuntimeError as error:
                system.warn(str(error))
        if stopped or not has_database:
            system.run("chown", "-R", "www-data:www-data", *(str(system.path(f"{here}/{folder}")) for folder, _ in DATA
                                                              if system.path(f"{here}/{folder}").exists()), check=False)
        if stopped:
            system.step("Start the site here")
            system.run("systemctl", "start", site.service_name, check=False)
    system.say(f"{name} has the live site's data as it was now.")


def take_over(system, name):
    """After the address has moved to this server: only the binding (the temporary aliases go), a certificate (HTTPS),
    and the Windows source forgotten. Nothing on Windows is changed."""
    from harbor.core.templates import Site
    from harbor.sites.actions import issue_certificate
    from harbor.sites.bindings import change_binding
    source(system, name)
    site = Site(name)
    config = system.path(site.config_path)
    binding = next((line.split()[1] for line in config.read_text().splitlines() if line.split()[:1] == ["ServerName"]), name) if config.is_file() else name
    system.step("Remove the temporary aliases")
    change_binding(system, name, binding, "", yes=True)
    issue_certificate(system, name, yes=True)
    forget(system, name)
    system.say(f"{name} has been taken over: it runs here on HTTPS.")


def forget(system, name):
    for path in (site_state(system, name) / "windows-source.json", site_secrets(system, name) / "windows-source.json"):
        if path.exists():
            path.unlink()
