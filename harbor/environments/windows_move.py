"""Environments on a live Windows server (IIS) moved to a Linux server Harbor knows (harbor servers), while Windows
keeps running them: the main site stays here, and its environment is built from its project for the Linux server,
with the settings and files of the one on Windows, on HTTP with a temporary alias. It is kept the same as the live site
until the address moves, then taken over.

  list       every site's environments on Windows, and where each was moved to
  move       an environment copied to a Linux server as a new environment of its site (harbor env move, as a copy):
             built from the main site's project, with appsettings, umbraco, uSync, Views, wwwroot and App_Data from
             Windows; the Windows environment stays, and is still the one that is live
  refresh    the live data fetched again from Windows: the SQLite database, media and uSync. With pause, the site on
             Windows shows its offline page (app_offline.htm) while its database is copied
  take-over  after the address has moved: the alias removed, a certificate issued there, and the Windows environment
             removed from Harbor (its IIS site is not touched)

The server is reached with Harbor's own key (harbor servers add), so no password is kept with the environment.

  /var/lib/harbor/sites/<site>/windows-move.json   {new environment: {windows, server, alias}}
"""
import os
import shlex
import shutil
import tempfile
from unittest import mock

from harbor.core.names import service_name
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.state.store import read_json, site_state, write_json

# What refresh fetches from Windows: the data that changes while the site is live. SQLite with its journal files.
DATABASE = ("umbraco/Data", ("*.sqlite.db", "*.sqlite.db-wal", "*.sqlite.db-shm"))
FOLDERS = ("wwwroot/media", "uSync")


def links_path(system, site):
    return site_state(system, site) / "windows-move.json"


def links(system, site):
    """The environments moved from Windows that still exist: one deleted since is no longer a move, so the one on
    Windows can be moved again"""
    found = read_json(links_path(system, site))
    if not isinstance(found, dict):
        return {}
    from harbor.core.templates import Site
    try:
        existing = set(settings.target_ids(system, Site(site).site_root))
    except (settings.SettingsError, OSError):
        return found
    return {new_id: entry for new_id, entry in found.items() if new_id in existing}


def _sites(system):
    from harbor.install.admin import web_root
    root = system.path(web_root())
    return sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")) if root.is_dir() else []


def candidates(system):
    """Every site's environments on Windows: [{site, target, binding, environment, share, moved_to}]"""
    from harbor.core.templates import Site
    found = []
    for name in _sites(system):
        site = Site(name)
        try:
            targets = settings.target_ids(system, site.site_root)
        except (settings.SettingsError, OSError):
            continue
        moved = {entry.get("windows"): new_id for new_id, entry in links(system, name).items() if isinstance(entry, dict)}
        for target in targets:
            try:
                environment = settings.load(system, site.site_root, target)
            except settings.SettingsError:
                continue
            if not environment.windows:
                continue
            found.append({"site": name, "target": target, "binding": environment.BINDING, "environment": environment.other.get("ENVIRONMENT", ""),
                          "share": environment.WINDOWS_SHARE, "moved_to": moved.get(target, "")})
    return found


def move(system, name, target, server_name, alias=""):
    """One environment on Windows copied to the Linux server, as a new environment of its site"""
    from harbor.core.templates import Site
    from harbor.environments.move import move_environment
    from harbor.server import servers
    site = Site(name)
    environment = settings.load(system, site.site_root, target)
    if not environment.windows:
        raise Stop(f"{environment.BINDING or target} is not on Windows.")
    if target in {entry.get("windows") for entry in links(system, name).values() if isinstance(entry, dict)}:
        raise Stop(f"{environment.BINDING} has been copied already.")
    entry = servers.server(system, server_name)
    before = set(settings.target_ids(system, site.site_root))
    # As the admin's Copy site asks it, to a remote Linux server, on HTTP: the address still points to Windows
    request = {"VNS_ENV_COPY": "1", "VNS_PROD_TARGET": "remote", "VNS_PROD_IP": entry["host"], "VNS_PROD_USER": entry["user"],
               "VNS_PROD_PASSWORD": "", "VNS_PROD_SSH_PORT": str(entry["port"]),
               # Named as its folder on Windows (poda.dk), not by its binding (www.poda.dk): /var/www/<folder> and its service
               "VNS_PROD_FOLDER": environment.FOLDER or environment.BINDING,
               "VNS_PROD_BINDING": environment.BINDING, "VNS_PROD_ENVIRONMENT": environment.other.get("ENVIRONMENT", "") or "Production",
               "VNS_PROD_HTTPS": "n"}
    with mock.patch.dict(os.environ, request):
        move_environment(system, name, target)
    new = sorted(set(settings.target_ids(system, site.site_root)) - before)
    if len(new) != 1:
        raise Stop("The copy did not become one new environment.")
    new_id = new[0]
    found = links(system, name)
    found[new_id] = {"windows": target, "server": server_name, "alias": alias}
    links_path(system, name).parent.mkdir(parents=True, exist_ok=True)
    write_json(links_path(system, name), found)
    if alias:
        from harbor.environments.bindings import change_binding
        system.step(f"Add the temporary alias {alias}")
        change_binding(system, name, environment.BINDING, alias, yes=True, target=new_id)
    system.say(f"{environment.BINDING} runs on {server_name} on HTTP, as the environment {new_id}. Windows still runs it: "
               "Refresh from Windows fetches its data again; take over when the address has moved.")
    return new_id


def move_all(system, items, server_name):
    """Several environments, one at a time: one that fails is told, and the rest go on"""
    failed = []
    for item in items:
        system.step(f"==== {item.get('site')}: {item.get('target')}")
        try:
            move(system, item["site"], item["target"], server_name, item.get("alias", ""))
        except (Stop, CommandFailed) as error:
            system.warn(f"{item.get('site')} ({item.get('target')}): FAILED{': ' + str(error) if str(error) else ''}")
            failed.append(item.get("site", ""))
    if failed:
        raise Stop(f"Not copied: {', '.join(failed)}")


def _link(system, name, new_id):
    entry = links(system, name).get(new_id)
    if not isinstance(entry, dict) or not entry.get("windows"):
        raise Stop(f"{new_id} of {name} was not copied from Windows (or has been taken over).")
    return entry


def refresh(system, name, new_id, pause=False):
    """The live data fetched again from Windows into the environment on Linux, which is stopped while its database is
    replaced. The files go through this server: the two servers have no access to each other."""
    from harbor.core.templates import Site
    from harbor.environments.windows import Share
    site = Site(name)
    entry = _link(system, name, new_id)
    windows = settings.load(system, site.site_root, entry["windows"])
    linux = settings.load(system, site.site_root, new_id)
    try:
        share = Share(system, windows).connect()
    except RuntimeError as error:
        raise Stop(str(error)) from None
    work = tempfile.mkdtemp()
    unit = service_name(linux.FOLDER)
    stopped = False
    try:
        includes = [argument for pattern in DATABASE[1] for argument in ("--include", pattern)]
        has_database = share.rclone("lsf", f"{share.target}/{DATABASE[0]}", *includes, check=False, capture=True).stdout.strip()
        for folder in FOLDERS:
            if share.rclone("lsf", f"{share.target}/{folder}", "--max-depth", "1", check=False, capture=True).ok:
                system.step(f"Fetch {folder}")
                share.rclone("copy", f"{share.target}/{folder}", f"{work}/{folder}", "--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0")
        if has_database:
            if pause:
                share.take_offline()
            try:
                system.step("Fetch the SQLite database")
                share.rclone("copy", f"{share.target}/{DATABASE[0]}", f"{work}/{DATABASE[0]}", *includes, "--modify-window", "2s", "--stats", "0")
            finally:
                if pause:
                    try:
                        share.bring_online()
                    except RuntimeError as error:
                        system.warn(str(error))
        argv, env = settings.ssh_command(linux)
        sudo = "" if linux.USER == "root" else "sudo -n "
        root = shlex.quote(linux.root)
        if has_database:
            system.step(f"Stop {linux.BINDING} on {linux.IP} while its database is replaced")
            settings.remote_exec(system, linux, f"systemctl stop {shlex.quote(unit)}; rm -f {root}/{DATABASE[0]}/*.sqlite.db-wal {root}/{DATABASE[0]}/*.sqlite.db-shm")
            stopped = True
        system.step(f"Copy the data to {linux.IP}")
        system.run("rsync", "-az", "-e", " ".join(argv), f"--rsync-path={sudo}rsync", f"{work}/", f"{linux.USER}@{linux.IP}:{linux.root}/", env=env)
        settings.remote_exec(system, linux, f"chown -R www-data:www-data {' '.join(f'{root}/{folder}' for folder in (DATABASE[0], *FOLDERS))} 2>/dev/null || true")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if stopped:
            system.step(f"Start {linux.BINDING} on {linux.IP}")
            settings.remote_exec(system, linux, f"systemctl start {shlex.quote(unit)}", check=False)
    system.say(f"{linux.BINDING} on {linux.IP} has the live site's data as it was now.")


def refresh_all(system, pause=False):
    moved = [(name, new_id) for name in _sites(system) for new_id in links(system, name)]
    if not moved:
        raise Stop("No environment has been copied from Windows (or all have been taken over).")
    failed = []
    for name, new_id in moved:
        system.step(f"==== {name}: {new_id}")
        try:
            refresh(system, name, new_id, pause=pause)
        except (Stop, CommandFailed) as error:
            system.warn(f"{name} ({new_id}): FAILED{': ' + str(error) if str(error) else ''}")
            failed.append(name)
    if failed:
        raise Stop(f"Not refreshed: {', '.join(failed)}")


def take_over(system, name, new_id):
    """After the address has moved: the alias removed, a certificate issued on Linux, and the environment on Windows
    removed from Harbor. Its IIS site and files on Windows are not touched."""
    from harbor.core.templates import Site
    from harbor.environments.actions import issue_certificate
    from harbor.environments.bindings import change_binding
    from harbor.sites.delete import delete_site
    site = Site(name)
    entry = _link(system, name, new_id)
    linux = settings.load(system, site.site_root, new_id)
    system.step("Remove the temporary alias")
    change_binding(system, name, linux.BINDING, "", yes=True, target=new_id)
    issue_certificate(system, name, new_id, yes=True)
    system.step("Remove the environment on Windows from Harbor (its IIS site is not touched)")
    delete_site(system, name, "n", "prod", entry["windows"])
    found = links(system, name)
    found.pop(new_id, None)
    write_json(links_path(system, name), found)
    system.say(f"{linux.BINDING} has been taken over: it runs on {linux.IP} on HTTPS.")
