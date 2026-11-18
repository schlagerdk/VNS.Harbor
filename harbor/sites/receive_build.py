"""A C# site published from a developer's own machine: no project here, it runs the build it is sent, as an
environment of a site on another server does. The machine's script (shown by Publish from your machine) runs
dotnet publish for this server's architecture (framework-dependent) and sends the build, with a scripts folder beside
it, as one tar over one SSH login: harbor site receive-build <site> --stdin. One publish of a site at a time (a lock).

  The first time   the site is made from the build: folder, service, Apache config and, when asked, a certificate
                   (as harbor site setup does for a build put in /var/www by hand), with the build's appsettings
  Later            the build is checked (it has the dll the service runs, and none of what the site keeps here), the
                   build there kept for harbor site rollback, the new one copied into the folder the site runs from,
                   and the site restarted and checked: it runs and answers on its port, or the publish fails
  Never touched    umbraco/Data, umbraco/Logs, wwwroot/media, App_Data, its connection to Harbor, the paths the site
                   keeps (local-publish.json "keep", such as /wwwroot/), and appsettings*.json unless the site takes
                   appsettings.json from its build ("appsettings": "build"; appsettings.<Environment>.json stays).
                   Nothing is deleted in App_Plugins or uSync. The folder's own rights stay as they are
  Its scripts      the .sh files of the scripts folder sent along, in the site's scripts folder, owned by root

  /var/lib/harbor/sites/<site>/local-publish.json   its binding, what it keeps, and its publishes: when, by whom, which commit
  /var/lib/harbor/previous/<site>/                  the build before the last publish (harbor site rollback)
"""
from datetime import datetime, timezone
import re

from harbor.core.names import normalize_site_name
from harbor.core.system import Stop
from harbor.state.store import read_json, site_state, write_json

INCOMING = "/var/lib/harbor/incoming"
PREVIOUS = "/var/lib/harbor/previous"
# What the site has here and a build never brings (rsync leaves excluded paths alone, also with --delete)
SERVER_DATA = ("/umbraco/Data/", "/umbraco/Logs/", "/umbraco/mediacache/", "/wwwroot/media/", "/App_Data/", "/app_data/",
               "/vns-harbor.json", "/logs/", "/scripts/", "/.vns-publish/",
               # What Python writes as it runs (compiled modules): never a site's own, nor a reason to stop a publish
               "__pycache__/", "*.pyc")
# A build with one of these is a project that publishes what belongs on the server: stopped, not left out quietly
NEVER_IN_BUILD = ("/App_Data/", "/app_data/", "/umbraco/Data/")
# Updated, but nothing deleted there: packages' own files, and settings a later sync may have added
PROTECTED = ("/App_Plugins/**", "/wwwroot/App_Plugins/**", "/uSync/**")
# A path a site keeps from its publishes as well, such as /wwwroot/ where its files are made here
KEEP_PATH = re.compile(r"/[A-Za-z0-9_.*-]+(?:/[A-Za-z0-9_.*-]+)*/?")
HISTORY = 20


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
    system.plan([f"Create an empty web project {project}, built to start the site with", f"Build {project}.dll"])
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


def keep_paths(paths):
    """The paths a site keeps, checked: rooted at the site's folder, no way out of it"""
    found = []
    for path in paths or []:
        path = path.strip()
        # none: nothing kept, as a choice (the first publish to a site with files asks for one)
        if not path or path == "none":
            continue
        if not KEEP_PATH.fullmatch(path) or ".." in path.split("/"):
            raise Stop(f"{path} is not a path in the site's folder, as /wwwroot/ is.")
        if path not in found:
            found.append(path)
    return found


def excluded(kept, appsettings):
    """What a copy of a build leaves alone: the site's data, what it keeps, and its appsettings (all of them, or only
    the ones for an environment when it takes appsettings.json from its build)"""
    settings = "/appsettings.*.json" if appsettings == "build" else "/appsettings*.json"
    return [argument for pattern in (*SERVER_DATA, *kept, settings) for argument in ("--exclude", pattern)]


def service_dll(system, site):
    """The dll the site's service starts (ExecStart=… dotnet …/Name.dll), or ''"""
    service = system.path(site.service_path)
    match = re.search(r"(?m)^ExecStart=\S*dotnet\s+(\S+?\.dll)\b", service.read_text() if service.is_file() else "")
    return match[1].rsplit("/", 1)[-1] if match else ""


def answer_port(system, site):
    """The port the site's service answers on (its --urls), or ''"""
    service = system.path(site.service_path)
    text = service.read_text() if service.is_file() else ""
    # dotnet … --urls http://localhost:PORT, or gunicorn --bind 127.0.0.1:PORT
    match = re.search(r"--urls[ =\"']+https?://[^:/\s\"']+:(\d+)", text) or re.search(r"--bind[ =]+[0-9.]+:(\d+)", text)
    return match[1] if match else ""


def check_running(system, site, wait=60, failed=""):
    """After a restart: the service runs and answers on its port (any answer but a server error), or Stop with its log,
    so the script on the machine fails too. A site that starts slowly (Umbraco) has a minute"""
    port = answer_port(system, site)
    system.step(f"Check that {site.name} runs")
    if port:
        system.say(f"It must answer on port {port}")
    active, code = False, ""
    for attempt in range(max(1, wait // 3)):
        active = system.run("systemctl", "is-active", "--quiet", site.service_name, check=False).ok
        code = system.output("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
                             f"http://127.0.0.1:{port}/", check=False).strip() if active and port else ""
        if active and (not port or code.isdigit() and 0 < int(code) < 500):
            system.say(f"{site.name} runs" + (f" and answers ({code})" if port else "") + ".")
            return
        system.sleep(3)
    system.run("journalctl", "-u", site.service_name, "-n", "30", "--no-pager", check=False)
    raise Stop(f"{site.name} does not " + ("run" if not active else f"answer on port {port} ({code or 'no answer'})")
               + f" after the publish. Its log is above.{failed}")


def unpack(system, name, stream):
    """The tar the machine sent (build/ and scripts/), unpacked into the site's incoming folders, emptied first.
    Only plain files and folders inside them (tarfile's data filter): no links out, no absolute paths"""
    import shutil
    import tarfile
    targets = {"build": system.path(f"{INCOMING}/{name}"), "scripts": system.path(f"{INCOMING}/{name}.scripts")}
    for folder in targets.values():
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            parts = [part for part in member.name.split("/") if part not in ("", ".")]
            if not parts or parts[0] not in targets or len(parts) == 1:
                continue
            member.name = "/".join(parts[1:])
            try:
                archive.extract(member, targets[parts[0]], filter="data")
            except tarfile.FilterError as error:
                raise Stop(f"The build has {member.name}, which points out of it ({error}). Nothing was changed.") from None


def receive_build(system, name, binding="", https="n", environment="Production", keep=None, appsettings=None, stream=None,
                  by="", commit="", dirty=""):
    """keep: the paths the site keeps from now on (None: the ones it kept). appsettings: build or server (None: as
    before). stream: the machine's tar (else the build rsynced to the incoming folder, as older scripts did)"""
    import fcntl
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    lock_path = system.path(f"{INCOMING}/{name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop(f"{name} is being published already. Try again when that is done.") from None
        system.plan([*(["Unpack the build"] if stream is not None else []), *publish_steps(system, name, keep)])
        if stream is not None:
            system.step("Unpack the build")
            unpack(system, name, stream)
        publish(system, name, binding, https, environment, keep, appsettings, {"by": by, "commit": commit, "dirty": dirty == "y"})


def site_kind(system, site):
    """What a site here runs, as its publish puts it in place: csharp (a build, its service runs its dll), python (its
    app folder, run by gunicorn) or html (static files, no service). '' when there is no site yet"""
    root = system.path(site.site_root)
    if not root.exists():
        return ""
    if (root / "app/app.py").is_file() or (root / "venv").is_dir():
        return "python"
    if system.path(site.service_path).is_file() or any(root.glob("*.runtimeconfig.json")) or any(root.glob("_publish/*.runtimeconfig.json")):
        return "csharp"
    return "html"


def destination(system, site, kind):
    """The folder a publish of a kind of site goes into"""
    from harbor.sites.tools import working_folder
    if kind == "python":
        return system.path(f"{site.site_root}/app")
    if kind == "html":
        return system.path(site.site_root)
    return system.path(working_folder(system, site))


def check_build(system, site, kind, incoming, kept):
    """The build is what the site runs, and has none of what it keeps on the server: else nothing is changed"""
    if kind == "python" and not (incoming / "app.py").is_file():
        raise Stop(f"The build has no app.py, which {site.name} runs (gunicorn app:app). Send the app folder.")
    if kind == "html" and not any(incoming.iterdir()):
        raise Stop("The build is empty.")
    if kind == "csharp":
        dll = service_dll(system, site)
        if dll and not (incoming / dll).is_file():
            raise Stop(f"The build has no {dll}, which {site.name}'s service starts. Nothing was changed: the project must build {dll}.")
    # What the site keeps here must not be in its build: it would be left out, and that is a fault in the project
    for path in (*(NEVER_IN_BUILD if kind == "csharp" else ()), *kept):
        if any(incoming.glob(path.strip("/"))):
            raise Stop(f"The build has {path}, which this site keeps on the server, so it would be left out. "
                       "Nothing was changed: the project should not publish it.")


def copy_step(kind, folder, kept):
    return f"Copy the new {'build' if kind == 'csharp' else 'files'} into {folder}" + (f", keeping {', '.join(kept)}" if kept else "")


def publish_steps(system, name, keep):
    """The steps of a publish, before the build is unpacked. A Python site's packages, and the site's own scripts,
    are steps of their own when they come (the build decides)"""
    from harbor.core.templates import Site
    site = Site(name)
    if not system.path(site.site_root).exists():
        return [f"First publish: set up {name} from the build", f"Check that {name} runs"]
    kind = site_kind(system, site)
    found = read_json(marker(system, name))
    kept = keep_paths(keep) if keep is not None else keep_paths(found.get("keep") if isinstance(found, dict) else None)
    return [copy_step(kind, destination(system, site, kind), kept), *([] if kind == "html" else [f"Restart {name}", f"Check that {name} runs"])]


def publish(system, name, binding, https, environment, keep, appsettings, who):
    from harbor.core.templates import Site
    incoming = system.path(f"{INCOMING}/{name}")
    site = Site(name)
    root = system.path(site.site_root)
    kind = site_kind(system, site)
    found = read_json(marker(system, name))
    if not isinstance(found, dict):
        found = {}
    kept = keep_paths(keep) if keep is not None else keep_paths(found.get("keep"))
    appsettings = appsettings or found.get("appsettings") or "server"
    if not root.exists():
        # Only a C# site is made by its first publish; a Python or HTML site is made in Harbor first
        if not any(incoming.glob("*.runtimeconfig.json")):
            raise Stop(f"No site {name} here, and the build is not a C# build to make one from. Make the site in Harbor first.")
        check_build(system, site, "csharp", incoming, kept)
        from harbor.sites.setup import setup_site
        system.step(f"First publish: set up {name} from the build")
        root.mkdir(parents=True)
        system.run("rsync", "-a", "--no-perms", "--chmod=D755,F644", f"{incoming}/", f"{root}/")
        own_scripts(system, name, site)
        setup_site(system, name, binding or name, "y" if https == "y" else "n", environment or "Production")
        own_scripts(system, name, site)
        check_running(system, site)
    else:
        if any(root.glob("*.csproj")):
            raise Stop(f"{name} has a project here: it is built here, not published from a machine.")
        if not incoming.is_dir() or (kind == "csharp" and not any(incoming.glob("*.runtimeconfig.json"))):
            raise Stop(f"No build in {INCOMING}/{name}: run the publish script on your machine, which sends it there.")
        check_build(system, site, kind, incoming, kept)
        folder = destination(system, site, kind)
        folder.mkdir(parents=True, exist_ok=True)
        arguments = excluded(kept, appsettings)
        # What is there now, for harbor site rollback: without what the site has here
        previous = system.path(f"{PREVIOUS}/{name}")
        previous.mkdir(parents=True, exist_ok=True)
        before = (folder / "requirements.txt").read_bytes() if (folder / "requirements.txt").is_file() else b""
        # The first publish to a site that has files it would delete: only with a choice of what it keeps
        if keep is None and "keep" not in found:
            doomed = would_delete(system, incoming, folder, arguments)
            if doomed:
                shown = ", ".join(doomed[:8]) + (f" and {len(doomed) - 8} more" if len(doomed) > 8 else "")
                raise Stop(f"This publish would delete {len(doomed)} file(s) the site has and the build does not: {shown}. "
                           "Nothing was changed. Choose what the site keeps: KEEP=(/wwwroot/) for a folder, or KEEP=(none) to let them go "
                           "(--keep none). The choice is kept for later publishes.")
        # What it runs now, for harbor site rollback: only when the publish before worked, so two failures in a row
        # still leave the last one that ran
        if found.get("ok", True):
            system.run("rsync", "-a", "--delete", *arguments, f"{folder}/", f"{previous}/")
        system.step(copy_step(kind, folder, kept))
        try:
            put_in_place(system, site, incoming, folder, arguments)
            restart(system, site, kind, folder, before, f" harbor site rollback {name} puts what it had before back.")
        except Stop:
            remember(system, name, found, binding, kept, appsettings, who, ok=False)
            raise
    remember(system, name, found, binding, kept, appsettings, who, ok=True)
    system.say(f"{name} runs what you published from your machine.")


def remember(system, name, found, binding, kept, appsettings, who, ok):
    """The publish in local-publish.json: what the site keeps, and when, by whom, which commit, and whether it worked"""
    publishes = [entry for entry in found.get("publishes", []) if isinstance(entry, dict)]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = {"time": now, **{key: value for key, value in who.items() if value}, **({} if ok else {"failed": True})}
    marker(system, name).parent.mkdir(parents=True, exist_ok=True)
    # The binding is the one it was made with: a later publish does not change it (Change bindings does)
    write_json(marker(system, name), {**found, "binding": found.get("binding") or binding or name, "keep": kept,
                                       "appsettings": appsettings, "published": now, "ok": ok,
                                       "publishes": ([entry] + publishes)[:HISTORY]})


def would_delete(system, source, folder, arguments):
    """The files a copy would delete (rsync's dry run), as paths in the folder"""
    protect = [argument for pattern in PROTECTED for argument in ("--filter", f"P {pattern}")]
    result = system.run("rsync", "-a", "--delete", "--dry-run", "--itemize-changes", *protect, *arguments,
                        f"{source}/", f"{folder}/", check=False, capture=True)
    return [line.split(" ", 1)[1].strip() for line in (result.stdout or "").splitlines()
            if line.startswith("*deleting") and not line.rstrip().endswith("/")]


def restart(system, site, kind, folder, requirements_before, failed=""):
    """After a publish: a Python site's packages when its requirements changed, then the site restarted and checked.
    A static site has nothing to restart"""
    if kind == "html":
        return
    if kind == "python" and (folder / "requirements.txt").is_file() and (folder / "requirements.txt").read_bytes() != requirements_before:
        system.plan(["Install the packages in requirements.txt"])
        system.step("Install the packages in requirements.txt")
        system.run("sudo", "-u", "www-data", "./venv/bin/pip", "install", "--upgrade", "-r", "app/requirements.txt", cwd=site.site_root)
    system.step(f"Restart {site.name}")
    if kind in ("csharp", "umbraco"):
        system.run("python3", f"{site.harbor_dir}/lib/harden_service.py", site.service_name, check=False)
    system.run("systemctl", "restart", site.service_name, check=False)
    check_running(system, site, failed=failed)


def put_in_place(system, site, source, folder, arguments):
    """Files into the folder they run from: by content (a new build can have the same sizes and times), what is no
    longer sent removed, the site's data and kept paths left alone, and the folder's own rights kept"""
    protect = [argument for pattern in PROTECTED for argument in ("--filter", f"P {pattern}")]
    system.run("rsync", "-a", "--checksum", "--delete", "--no-perms", "--no-owner", "--no-group", *protect, *arguments,
               f"{source}/", f"{folder}/")
    system.run("chown", "-R", "www-data:www-data", str(folder), check=False)
    own_scripts(system, site.name, site)
    from harbor.sites.tools import keep_connection
    keep_connection(system, site, "main")


def rollback(system, name):
    """What the site had before its last publish put back, the site restarted and checked"""
    from harbor.core.templates import Site
    name = normalize_site_name(name)
    site = Site(name)
    kind = site_kind(system, site)
    previous = system.path(f"{PREVIOUS}/{name}")
    if not kind or not previous.is_dir() or not any(previous.iterdir()):
        raise Stop(f"{name} has nothing from before its last publish to go back to.")
    found = read_json(marker(system, name)) or {}
    if kind == "csharp":
        dll = service_dll(system, site)
        if dll and not (previous / dll).is_file():
            raise Stop(f"What it had before has no {dll}, which {name}'s service starts. Nothing was changed.")
    folder = destination(system, site, kind)
    before = (folder / "requirements.txt").read_bytes() if (folder / "requirements.txt").is_file() else b""
    system.plan([f"Put what {name} had before its last publish back into {folder}", *([] if kind == "html" else [f"Restart {name}", f"Check that {name} runs"])])
    system.step(f"Put what {name} had before its last publish back into {folder}")
    put_in_place(system, site, previous, folder, excluded(keep_paths(found.get("keep")), found.get("appsettings") or "server"))
    restart(system, site, kind, folder, before)
    system.say(f"{name} runs what it had before its last publish.")


def scripts_folder(system, name):
    """Where a site published from a machine keeps its own scripts: in Harbor's state, which only root can write. Not
    in the site's folder, which its app user (www-data) owns and could swap a scripts folder into"""
    return site_state(system, name) / "scripts"


def own_scripts(system, name, site):
    """The site's own scripts as the machine sent them (.sh only), in a folder only root can write, as the admin runs
    them as root. One the machine sent before and no longer sends is removed; one written on the server stays, as do
    the scripts in the site's own scripts folder (made root's here as well)"""
    target = scripts_folder(system, name)
    sent = system.path(f"{INCOMING}/{name}.scripts")
    if sent.is_dir():
        record = site_state(system, name) / "published-scripts.json"
        before = read_json(record)
        before = set(before) if isinstance(before, list) else set()
        now = {path.name for path in sent.glob("*.sh") if path.is_file()}
        if now or before & {path.name for path in target.glob("*.sh")}:
            system.plan(["Put the site's own scripts where only root can change them"])
            system.step("Put the site's own scripts where only root can change them")
        target.mkdir(parents=True, exist_ok=True)
        system.run("rsync", "-a", "--no-perms", "--chmod=D755,F755", "--include", "*.sh", "--exclude", "*", f"{sent}/", f"{target}/")
        for gone in sorted(before - now):
            (target / gone).unlink(missing_ok=True)
        write_json(record, sorted(now))
    if target.is_dir():
        system.run("chown", "-R", "root:root", str(target), check=False)
        system.run("chmod", "-R", "go-w", str(target), check=False)
    legacy = system.path(f"{site.site_root}/scripts")
    if legacy.is_dir():
        system.run("chown", "-R", "root:root", str(legacy), check=False)
        system.run("chmod", "-R", "go-w", str(legacy), check=False)
