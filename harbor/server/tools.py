"""What the server has for the sites, and the tools to change it (phase 4 of dev/HARBOR.md): SQL Server, sqlcmd, the sa
password in sql/, the backup and log shrink cron jobs, .NET and Node.js. The admin's Server tools window shows status()
and runs the rest as tasks.
"""
import json
import os
import re
import shutil
import tempfile
import time

from harbor.core.dotnet import DOTNET_DIR, ensure_channel
from harbor.server import samba, ssh
from harbor.core.system import Stop
from harbor.state.store import read_json, write_json
from harbor.sql.databases import PLACEHOLDER, SA_SECRET, SQL_DIR, SQLCMD
from harbor.sql import postgres
from harbor.sql.server import FALLBACK_UBUNTU, SUPPORTED_UBUNTU, installed, os_release, sa_scripts

NODESOURCE = "https://deb.nodesource.com/setup_{}.x"
HARBOR = "/usr/local/bin/harbor"
# The jobs Harbor adds, as harbor commands (they were /vns/sql/cron/*.sh, which go with the cutover); others in root's
# crontab (such as the custom cron/delete_old_2dm_data.sh) are left alone
JOBS = {"shrink_logs": f"0 */4 * * * {HARBOR} sql shrink-logs >> /var/log/sql_shrink_logs.log 2>&1",
        "backup_everything": f"15 */4 * * * {HARBOR} sql backup-everything >> /var/log/sql_backup_everything.log 2>&1",
        # PostgreSQL's of its own (phase 10), a quarter of an hour after Microsoft SQL Server's
        "postgres_backup": f"30 */4 * * * {HARBOR} postgres backup-everything >> /var/log/postgres_backup_everything.log 2>&1"}
# The harbor command each job runs
JOB_COMMANDS = {"shrink_logs": "harbor sql shrink-logs", "backup_everything": "harbor sql backup-everything",
                "postgres_backup": "harbor postgres backup-everything"}
# Microsoft SQL Server's jobs: those turned on and off together, as they were before PostgreSQL had one
MSSQL_JOBS = ("shrink_logs", "backup_everything")


def package_version(system, package):
    return system.output("dpkg-query", "-W", "-f=${Version}", package, check=False) if installed(system, package) else ""


def crontab(system):
    found = system.run("crontab", "-l", check=False, capture=True)
    return found.stdout if found.ok else ""


def job_lines(text, name):
    """A job's lines: the harbor command, or the script in /vns/sql/cron it was"""
    command = JOB_COMMANDS[name]
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")
            and (f"/sql/cron/{name}.sh" in line or re.search(rf"(^|[\s/]){re.escape(command)}(\s|$)", line))]


def dotnet(system):
    """Per channel (such as 10.0): its newest SDK and ASP.NET Core runtime in the folder the site services use"""
    program = f"{DOTNET_DIR}/dotnet"
    if not system.path(program).is_file():
        return []
    sdks = system.output(program, "--list-sdks", check=False)
    runtimes = system.output(program, "--list-runtimes", check=False)
    channels = {}
    for version in re.findall(r"(?m)^([0-9]+\.[0-9]+\.[0-9]+\S*) \[", sdks):
        channels.setdefault(".".join(version.split(".")[:2]), {"sdk": [], "runtime": []})["sdk"].append(version)
    for version in re.findall(r"(?m)^Microsoft\.AspNetCore\.App ([0-9]+\.[0-9]+\.[0-9]+\S*) \[", runtimes):
        channels.setdefault(".".join(version.split(".")[:2]), {"sdk": [], "runtime": []})["runtime"].append(version)

    def key(version):
        return [int(part) if part.isdigit() else -1 for part in re.split(r"[.-]", version)]
    return [{"channel": channel, "sdk": max(found["sdk"], key=key, default=""), "runtime": max(found["runtime"], key=key, default="")}
            for channel, found in sorted(channels.items(), key=lambda item: key(item[0]), reverse=True)]


RELEASES = "https://builds.dotnet.microsoft.com/dotnet/release-metadata/{}/releases.json"
RELEASES_CACHE = "/var/lib/vns/dotnet-releases.json"
RELEASES_AGE = 6 * 60 * 60


def apt_candidate(system, package):
    """(installed, the version apt would install) of a package, as apt's lists have them now; "" for none"""
    found = system.output("apt-cache", "policy", package, check=False)
    installed = re.search(r"(?m)^\s*Installed:\s*(\S+)", found or "")
    candidate = re.search(r"(?m)^\s*Candidate:\s*(\S+)", found or "")
    clean = lambda match: "" if not match or match.group(1) == "(none)" else match.group(1)
    return clean(installed), clean(candidate)


def newer_package(system, package):
    """The version apt would update a package to, or "" when it is up to date (or not installed)"""
    installed, candidate = apt_candidate(system, package)
    if not installed or not candidate or candidate == installed:
        return ""
    return candidate if system.run("dpkg", "--compare-versions", candidate, "gt", installed, check=False, capture=True).ok else ""


def dotnet_latest(system, channels):
    """The newest SDK and runtime of each .NET channel, from Microsoft's release list, kept for six hours so Server
    tools stays quick. A channel it cannot tell is left out: its Update button is shown as it was."""
    import time
    path = system.path(RELEASES_CACHE)
    try:
        cache = json.loads(path.read_text()) if path.is_file() else {}
    except ValueError:
        cache = {}
    now = time.time()
    found, changed = {}, False
    for channel in channels:
        entry = cache.get(channel) or {}
        if now - entry.get("at", 0) > RELEASES_AGE:
            fetched = system.run("curl", "-fsS", "--max-time", "6", RELEASES.format(channel), check=False, capture=True)
            try:
                data = json.loads(fetched.stdout) if fetched.ok else {}
                entry = {"at": now, "sdk": data.get("latest-sdk", ""), "runtime": data.get("latest-runtime", "")}
                changed = True
            except ValueError:
                entry = {}
        if entry.get("sdk") or entry.get("runtime"):
            found[channel] = {"sdk": entry.get("sdk", ""), "runtime": entry.get("runtime", "")}
            cache[channel] = entry
    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache))
        except OSError:
            pass
    return found


def version_key(version):
    return [int(part) if part.isdigit() else -1 for part in re.split(r"[.-]", version or "")]


def dotnet_updates(system, items):
    """Each installed channel with what an update would bring: latest_sdk and latest_runtime when newer, update true or
    false, or update None when it cannot be told"""
    latest = dotnet_latest(system, [item["channel"] for item in items])
    result = []
    for item in items:
        known = latest.get(item["channel"])
        if not known:
            result.append({**item, "update": None})
            continue
        sdk = known["sdk"] if known["sdk"] and version_key(known["sdk"]) > version_key(item["sdk"]) else ""
        runtime = known["runtime"] if known["runtime"] and version_key(known["runtime"]) > version_key(item["runtime"]) else ""
        result.append({**item, "update": bool(sdk or runtime), "latest_sdk": sdk, "latest_runtime": runtime})
    return result


def status(system, sql_folder=SQL_DIR):
    """Everything the Server tools window shows"""
    try:
        release = os_release(system)
    except OSError:
        release = {}
    scripts = sa_scripts(system, sql_folder) if sql_folder.is_dir() else []
    table = crontab(system)
    found = [entry for entry in crontab_entries(system, table) if entry["kind"] == "job"]
    mine = {line for name in JOBS for line in job_lines(table, name)}
    others = [line for line in table.splitlines() if line.strip() and not line.lstrip().startswith("#") and line not in mine]
    node = system.output("node", "--version", check=False) if system.which("node") else ""
    usage = dotnet_usage(system)
    return {
        "os": {"name": release.get("PRETTY_NAME", ""), "id": release.get("ID", ""), "version": release.get("VERSION_ID", "")},
        "sql_server": {"installed": installed(system, "mssql-server"), "version": package_version(system, "mssql-server"),
                       "newer": newer_package(system, "mssql-server"),
                       "running": system.output("systemctl", "is-active", "mssql-server", check=False) == "active",
                       # Microsoft publishes SQL Server for some Ubuntu versions only: on another it is installed from these
                       "packages_for": "" if release.get("VERSION_ID", "") in SUPPORTED_UBUNTU else FALLBACK_UBUNTU},
        "sql_tools": {"installed": system.path(SQLCMD).is_file(), "version": package_version(system, "mssql-tools18"),
                      "newer": newer_package(system, "mssql-tools18")},
        # PostgreSQL beside it (phase 10): the server, its tools, and the postgres password Harbor keeps
        # The version of the server itself (postgresql-18: 18.6-…), not of the package that brings it (18+290)
        "postgres": {"installed": installed(system, "postgresql"),
                     "version": next((package_version(system, f"postgresql-{major}").split("-")[0] for major in reversed(postgres.clusters(system))
                                      if package_version(system, f"postgresql-{major}")), package_version(system, "postgresql").split("+")[0]),
                     "newer": newer_package(system, "postgresql"),
                     "running": system.output("systemctl", "is-active", "postgresql", check=False) == "active"},
        "postgres_tools": {"installed": postgres.tools_installed(system),
                           # psql (PostgreSQL) 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1): 18.6
                           "version": (re.findall(r"\d+(?:\.\d+)+", system.output("psql", "--version", check=False)) or [""])[0] if postgres.tools_installed(system) else "",
                           "newer": newer_package(system, "postgresql-client")},
        "postgres_password": {"set": bool(postgres.password(system))},
        "sa_password": {"scripts": len(scripts),
                        "set": bool((read_json(system.path(SA_SECRET)) or {}).get("sa_password"))
                        or (bool(scripts) and not any(f"'{PLACEHOLDER}'" in path.read_text(errors="replace") for path in scripts))},
        "cron": {"jobs": {name: bool(job_lines(table, name)) for name in JOBS}, "others": others,
                 # Each job's schedule and label as they are in root's crontab (edited there or in Scheduled jobs), or
                 # the schedule Harbor gives it when it is turned on
                 "schedules": {name: next((entry["schedule"] for entry in found if entry.get("harbor") == name), " ".join(JOBS[name].split()[:5]))
                               for name in JOBS},
                 "labels": {name: next((entry["label"] for entry in found if entry.get("harbor") == name and entry["label"]), "") for name in JOBS}},
        "dotnet": [{**item, "sites": usage.get(item["channel"].split(".")[0], [])} for item in dotnet_updates(system, dotnet(system))],
        "node": {"version": node.lstrip("v"), "npm": system.output("npm", "--version", check=False) if node else "",
                 "newer": newer_package(system, "nodejs") if node else "",
                 "sites": node_usage(system)},
        "samba": samba.status(system),
        "ssh": ssh.status(system),
    }


# ---- cron
def cron(system, on, only=None):
    """The backup and log shrink jobs in root's crontab once each (or only one of them), or taken out; other jobs stay
    as they are"""
    table = crontab(system)
    lines = table.splitlines()
    if on:
        missing = [line for name, line in JOBS.items() if not job_lines(table, name) and (name == only or only is None and name in MSSQL_JOBS)]
        if not missing:
            system.say("The backup and log shrink jobs are in root's crontab." if only is None else "The job is in root's crontab.")
            return
        step = "Add the backup and log shrink jobs to root's crontab" if only is None else f"Add the job {HARBOR_LABELS[only]} to root's crontab"
        system.plan([step])
        system.step(step)
        lines += missing
    else:
        mine = {line for name in JOBS for line in job_lines(table, name) if name == only or only is None and name in MSSQL_JOBS}
        if not mine:
            system.say("The backup and log shrink jobs are not in root's crontab.")
            return
        system.plan(["Take the backup and log shrink jobs out of root's crontab"])
        system.step("Take the backup and log shrink jobs out of root's crontab")
        # A job's label and what of it is watched (the comment lines just above it) go with it
        above = cron_watch().WATCH
        gone = set()
        for index, line in enumerate(lines):
            if line in mine:
                gone.add(index)
                before = index - 1
                while before >= 0 and (LABEL.match(lines[before]) or above.match(lines[before])):
                    gone.add(before)
                    before -= 1
        lines = [line for index, line in enumerate(lines) if index not in gone]
    system.run("crontab", "-", input="".join(line + "\n" for line in lines))
    if on:
        # When each was added, as when a job is saved in Scheduled jobs: cron has not run it before, and that is not
        # a time it should have
        state = cron_state(system)
        now = int(time.time())
        for line in missing:
            parts = job_parts(line)
            if parts:
                state["saved"][parts[1]] = now
        save_cron_state(system, state, {job_parts(line)[1] for line in lines if job_parts(line)})
    system.say(("Backups every 4 hours (at :15), logs shrunk every 4 hours." if only is None else "The job is in root's crontab.") if on
               else "No backups or log shrinks are run by cron now.")


# ---- .NET
def cron_upgrade(system):
    """The backup and log shrink jobs as harbor commands, where they still run a script in /vns/sql/cron: the same
    times and log files. Other jobs, such as the custom ones, stay as they are."""
    table = crontab(system)
    lines = table.splitlines()
    changed = []
    for index, line in enumerate(lines):
        for name in JOBS:
            if not line.lstrip().startswith("#") and f"/sql/cron/{name}.sh" in line:
                lines[index] = re.sub(rf"\S*/sql/cron/{name}\.sh", f"{HARBOR} sql {name.replace('_', '-')}", line)
                changed.append(name)
    if not changed:
        system.say("The backup and log shrink jobs run harbor already, or are not there.")
        return
    system.plan(["Let the backup and log shrink jobs run harbor"])
    system.step("Let the backup and log shrink jobs run harbor")
    system.run("crontab", "-", input="".join(line + "\n" for line in lines))
    system.say(f"{', '.join(changed)} run harbor now.")


def channel_of(text):
    if not re.fullmatch(r"[0-9]{1,2}\.0", text or "") or int(text.split(".")[0]) < 8:
        raise Stop(f"Not a .NET channel: {text} (such as 10.0, 8.0 or newer)")
    return text


def dotnet_install(system, channel, update=False):
    """A channel's SDK and ASP.NET Core runtime, or their newest patches when it has them already"""
    channel = channel_of(channel)
    before = {item["channel"]: item for item in dotnet(system)}.get(channel)
    if before and not update:
        system.say(f".NET {channel} is installed (SDK {before['sdk'] or '-'}, runtime {before['runtime'] or '-'}).")
        return
    system.plan([f"{'Update' if before else 'Install'} .NET {channel}"])
    system.step(f"{'Update' if before else 'Install'} .NET {channel}")
    # Also what the sites need around it (/usr/bin/dotnet and the rest), with the first .NET
    ensure_channel(system, channel, force=update)
    after = {item["channel"]: item for item in dotnet(system)}.get(channel)
    if not after or not after["sdk"] or not after["runtime"]:
        raise Stop(f".NET {channel} was not installed in {DOTNET_DIR}.")
    if before and (before["sdk"], before["runtime"]) == (after["sdk"], after["runtime"]):
        system.say(f".NET {channel} is up to date (SDK {after['sdk']}, runtime {after['runtime']}).")
    else:
        system.say(f".NET {channel}: SDK {after['sdk']}, runtime {after['runtime']}. Sites start on the new runtime when they restart.")


def site_folders(system):
    """The sites' folders in /var/www (not links to them)"""
    web = system.path("/var/www")
    return [site for site in sorted(web.iterdir()) if site.is_dir() and not site.is_symlink()] if web.is_dir() else []


def dotnet_usage(system):
    """Per .NET major version, the sites here that build or run on it: by their project (TargetFramework, an SDK
    pinned in global.json) or by what they publish (runtimeconfig.json). One pass over /var/www for every version."""
    framework = re.compile(r"<TargetFrameworks?>([^<]*)<")
    runtime = re.compile(r'"(?:tfm|version)"\s*:\s*"(?:net)?([0-9]+)\.0[.\d]*"')
    pinned = re.compile(r'"version"\s*:\s*"([0-9]+)\.0\.')
    usage = {}
    for site in site_folders(system):
        found = {}
        for path in [*site.glob("*.csproj"), *site.glob("global.json"), *site.glob("*.runtimeconfig.json"), *site.glob("*/*.runtimeconfig.json")]:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if path.suffix == ".csproj":
                majors = re.findall(r"\bnet([0-9]+)\.0\b", " ".join(framework.findall(text)))
                how = "project"
            elif path.name == "global.json":
                majors, how = pinned.findall(text), "project"
            else:
                majors, how = runtime.findall(text), "published"
            for major in majors:
                # The project says more than what is published from it
                if found.get(major) != "project":
                    found[major] = how
        for major, how in found.items():
            usage.setdefault(major, []).append({"site": site.name, "how": how})
    return usage


def sites_using(system, channel):
    """The sites here that build or run on a .NET channel"""
    return [item["site"] for item in dotnet_usage(system).get(channel.split(".")[0], [])]


NODE_PROGRAM = re.compile(r"(^|[\s/=])(node|nodejs|npm|npx|pm2)(\s|$)")


def node_usage(system):
    """The sites here that use Node.js: a package.json in the site (not in node_modules), whose engines say which
    versions it wants, or a service of the site that runs node, npm or pm2"""
    found = {}
    for site in site_folders(system):
        for pattern in ("package.json", "*/package.json", "*/*/package.json"):
            for path in sorted(site.glob(pattern)):
                if "node_modules" in path.parts:
                    continue
                try:
                    engines = (json.loads(path.read_text(errors="replace")).get("engines") or {}).get("node", "")
                except (OSError, ValueError, AttributeError):
                    engines = ""
                entry = found.setdefault(site.name, {"site": site.name, "how": "package.json", "path": str(path.relative_to(site)),
                                                     "engines": engines if isinstance(engines, str) else ""})
                if not entry["engines"] and isinstance(engines, str) and engines:
                    entry["engines"] = engines
    units = system.path("/etc/systemd/system")
    for unit in sorted(units.glob("*.service")) if units.is_dir() else []:
        try:
            lines = unit.read_text(errors="replace").splitlines()
        except OSError:
            continue
        if any(line.strip().startswith("ExecStart") and NODE_PROGRAM.search(line.split("=", 1)[-1]) for line in lines):
            name = unit.name[:-len(".service")]
            found.setdefault(name, {"site": name, "how": "service", "path": unit.name, "engines": ""})
    return sorted(found.values(), key=lambda item: item["site"])


def dotnet_remove(system, channel):
    """A channel's SDKs and runtimes taken out of /usr/lib/dotnet. Never one a site here uses, nor the only one. The
    newest host (host/fxr) stays, also when its version goes: dotnet runs every version with the newest host there is,
    and a newer host runs older versions."""
    channel = channel_of(channel)
    installed_channels = [item["channel"] for item in dotnet(system)]
    if channel not in installed_channels:
        raise Stop(f".NET {channel} is not installed.")
    if len(installed_channels) == 1:
        raise Stop(f".NET {channel} is the only version here, so it stays.")
    using = sites_using(system, channel)
    if using:
        raise Stop(f".NET {channel} is used by {', '.join(using)}: move them to another version first.")
    system.plan([f"Remove .NET {channel}"])
    system.step(f"Remove .NET {channel}")
    root = system.path(DOTNET_DIR)
    prefix = channel.split(".")[0] + ".0."
    hosts = [path for path in (root / "host/fxr").iterdir() if path.is_dir()] if (root / "host/fxr").is_dir() else []

    def version_key(path):
        return [int(part) if part.isdigit() else -1 for part in re.split(r"[.-]", path.name)]
    newest_host = max(hosts, key=version_key, default=None)
    removed = 0
    for parent in ("sdk", "sdk-manifests", "templates", "host/fxr", "shared/Microsoft.NETCore.App", "shared/Microsoft.AspNetCore.App",
                   "packs/Microsoft.NETCore.App.Ref", "packs/Microsoft.AspNetCore.App.Ref", "packs/Microsoft.NETCore.App.Host.linux-x64",
                   "packs/Microsoft.NETCore.App.Host.linux-arm64"):
        folder = root / parent
        for path in sorted(folder.glob(prefix + "*")) if folder.is_dir() else []:
            if path.is_dir() and not path.is_symlink() and path != newest_host:
                shutil.rmtree(path)
                removed += 1
    kept = f" Its host {newest_host.name} stays: the other versions run on it." if newest_host and newest_host.name.startswith(prefix) else ""
    system.say(f".NET {channel} is removed ({removed} folders). The other versions stay.{kept}")


# ---- cron: root's crontab, line by line
SPECIAL = ("@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly")
FIELD = re.compile(r"[0-9A-Za-z*/,\-]+")


def job_parts(line):
    """(schedule, command) of a crontab line that runs something, or None (a comment, a variable, empty)"""
    text = line.strip()
    if not text or text.startswith("#") or re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", text):
        return None
    words = text.split(None, 1 if text.startswith("@") else 5)
    if text.startswith("@"):
        return (words[0], words[1]) if len(words) == 2 and words[0] in SPECIAL else None
    if len(words) == 6 and all(FIELD.fullmatch(word) for word in words[:5]):
        return " ".join(words[:5]), words[5]
    return None


# What cron's journal cannot tell of a job: when its command was saved here (cron has not run a new or changed command
# before then), and when it was run with Run, which does not go through cron
CRON_STATE = "/var/lib/harbor/cron.json"


def cron_state(system):
    state = read_json(system.path(CRON_STATE))
    state = state if isinstance(state, dict) else {}
    return {"saved": state.get("saved") if isinstance(state.get("saved"), dict) else {},
            "by_hand": state.get("by_hand") if isinstance(state.get("by_hand"), dict) else {}}


def save_cron_state(system, state, commands):
    """Kept only for the commands root's crontab has"""
    write_json(system.path(CRON_STATE), {key: {command: value for command, value in state[key].items() if command in commands}
                                        for key in ("saved", "by_hand")})


# What the admin calls Harbor's own jobs until they are given a label
HARBOR_LABELS = {"backup_everything": "Microsoft SQL Server backup", "shrink_logs": "Microsoft SQL Server log shrink",
                 "postgres_backup": "PostgreSQL backup"}
# A job's label: a comment line of its own just above it, so it stays in root's crontab with the job
LABEL = re.compile(r"^#\s*Harbor label:\s*(.*?)\s*$")


def clean_label(text):
    return " ".join(str(text or "").split())[:80]


def cron_watch():
    """lib/cron_watch.py: the watch line above a job, and how its runs ended while the alerts are on"""
    import importlib.util
    from harbor.core.paths import LIB
    spec = importlib.util.spec_from_file_location("vns_cron_watch", LIB / "cron_watch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def crontab_entries(system, text=None, with_runs=False):
    """Root's crontab as its lines (or the crontab given): each job with its schedule, command, label, what of it is
    watched (Email alerts) and which of Harbor's jobs it is, and the other lines (variables, comments) as they are.
    with_runs: each job's last run as harbor-cron-shell noted it, and whether its alert is on."""
    watcher = cron_watch()
    entries = []
    label = watch = None

    def dangling():
        # A label or watch line not followed by a job is kept as the comment it is
        if label is not None:
            entries.append({"kind": "other", "line": f"# Harbor label: {label}"})
        if watch is not None:
            entries.append({"kind": "other", "line": f"# Harbor watch: {watcher.watch_text(watch)}"})

    for line in (crontab(system) if text is None else text).splitlines():
        parts = job_parts(line)
        if parts:
            harbor = next((name for name in JOBS if job_lines(line, name)), "")
            entries.append({"kind": "job", "schedule": parts[0], "command": parts[1], "harbor": harbor, "label": label or "", "watch": watch or {}})
            label = watch = None
            continue
        found, watched = LABEL.match(line), watcher.WATCH.match(line)
        if found and label is None:
            label = clean_label(found.group(1))
        elif watched and watch is None:
            watch = watcher.parse_watch(watched.group(1))
        else:
            dangling()
            label = watch = None
            if found:
                label = clean_label(found.group(1))
            elif watched:
                watch = watcher.parse_watch(watched.group(1))
            elif line.strip():
                entries.append({"kind": "other", "line": line})
    dangling()
    if with_runs:
        state = system.path(str(watcher.STATE))
        known = watcher.runs(state)
        failing = watcher.read_json(state / "alerts.json", {}).get("open") or {}
        for entry in entries:
            if entry["kind"] == "job":
                key = watcher.key_of(entry["command"])
                entry["last"] = (known.get(key, {}).get("runs") or [None])[0]
                entry["failing"] = (failing.get(key) or {}).get("problems") or None
    return entries


def crontab_save(system, entries):
    """Root's crontab made of these entries: jobs ({schedule, command, label, watch}) checked, the other lines kept as
    they are"""
    watcher = cron_watch()
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise Stop("Not a crontab entry.")
        if entry.get("kind") == "other":
            line = str(entry.get("line", ""))
            if "\n" in line or (job_parts(line) and True):
                raise Stop("A line that is not a job must stay as it was.")
            lines.append(line)
            continue
        schedule = " ".join(str(entry.get("schedule", "")).split())
        command = str(entry.get("command", "")).strip()
        if not command or "\n" in command or "\r" in command:
            raise Stop("A job needs a command on one line.")
        if not job_parts(f"{schedule} {command}"):
            raise Stop(f"Not a schedule: {schedule} (five fields, such as 15 */4 * * *, or @daily, @reboot)")
        label = clean_label(entry.get("label"))
        if label:
            lines.append(f"# Harbor label: {label}")
        watch = watcher.watch_text(entry.get("watch"))
        if watch:
            lines.append(f"# Harbor watch: {watch}")
        lines.append(f"{schedule} {command}")
    system.plan(["Save root's crontab"])
    system.step("Save root's crontab")
    before = {entry["command"] for entry in crontab_entries(system) if entry["kind"] == "job"}
    system.run("crontab", "-", input="".join(line + "\n" for line in lines))
    state = cron_state(system)
    now = int(time.time())
    commands = {job_parts(line)[1] for line in lines if job_parts(line)}
    for command in commands - before:
        state["saved"][command] = now
    save_cron_state(system, state, commands)
    # The jobs' logs rotated, while that is on: a job added or changed has its log in it
    if system.path(LOGROTATE).is_file():
        logrotate(system, True, quiet=True)
    system.say(f"Root's crontab has {sum(1 for entry in entries if entry.get('kind') != 'other')} jobs.")


ERROR_LINE = re.compile(r"\b(error|errors|failed|failure|fatal|exception|traceback|denied|cannot|could not|unable to)\b", re.I)
RUN_LINE = re.compile(r"^(\S+) \S+ CRON\[\d+\]: \(root\) CMD \((.*)\)$")


def job_output(command):
    """Where a job's output goes: ("file", path) for > or >> to a file, ("tag", tag) for | logger -t tag, or None"""
    found = re.search(r"(?<![0-9&])>>?\s*(['\"]?)(/[^\s'\"]+)\1", command)
    if found:
        # Thrown away: nothing to show
        return None if found.group(2) == "/dev/null" else ("file", found.group(2))
    found = re.search(r"\|\s*(?:/usr/bin/)?logger\b[^|]*?\s-t\s*(['\"]?)([^\s'\"]+)\1", command)
    return ("tag", found.group(2)) if found else None


def tail_text(path, lines=80, size=65536):
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - size))
        text = stream.read().decode("utf-8", "replace")
    return text.splitlines()[-lines:]


def crontab_log(system, command, days=14):
    """What is known of a job: when cron started it (its journal, the last days), and the end of its output with the
    lines that look like errors. cron does not log how a job ended, so its output is what tells."""
    command = str(command).strip()
    found = system.run("journalctl", "-u", "cron", "--since", f"-{days}d", "-o", "short-iso", "--no-pager", "--case-sensitive=true",
                       "-g", "CMD \\(" + re.escape(command[:900]), check=False, capture=True)
    runs = [match.group(1) for match in map(RUN_LINE.match, (found.stdout if found.ok else "").splitlines())
            if match and match.group(2) == command]
    output = {"kind": "", "where": "", "modified": None, "lines": []}
    target = job_output(command)
    if target and target[0] == "file":
        path = system.path(target[1])
        output.update(kind="file", where=target[1])
        try:
            output.update(modified=int(path.stat().st_mtime), lines=tail_text(path))
        except OSError:
            pass
    elif target:
        logged = system.run("journalctl", "-t", target[1], "-n", "80", "-o", "short-iso", "--no-pager", check=False, capture=True)
        output.update(kind="tag", where=target[1], lines=(logged.stdout if logged.ok else "").splitlines()[-80:])
    # SQL Server's line after every DBCC says "error" without being one
    errors = [line for line in output["lines"] if ERROR_LINE.search(line) and not line.startswith("DBCC execution completed.")][-20:]
    state = cron_state(system)
    # How its last runs ended, as harbor-cron-shell noted them while the Email alerts are on
    watcher = cron_watch()
    noted = watcher.runs(system.path(str(watcher.STATE))).get(watcher.key_of(command), {}).get("runs") or []
    return {"command": command, "days": days, "runs": runs[-50:][::-1], "runs_count": len(runs), "output": output, "errors": errors,
            "saved": state["saved"].get(command), "by_hand": state["by_hand"].get(command) or [], "noted": noted[:10]}


def crontab_clear_log(system, command):
    """Empties the file a job of root's crontab writes its output to (> or >> a file): only for a job that is there,
    and only a plain file, not a link to another"""
    command = str(command).strip()
    if not any(entry.get("kind") == "job" and entry["command"] == command for entry in crontab_entries(system)):
        raise Stop("That job is not in root's crontab.")
    target = job_output(command)
    if not target or target[0] != "file":
        raise Stop("The job does not write its output to a file.")
    path = system.path(target[1])
    if path.is_symlink() or not path.is_file():
        raise Stop(f"{target[1]} is not a plain file.")
    with open(path, "r+b") as stream:
        stream.truncate(0)
    system.say(f"{target[1]} is empty now.")


def cron_command(command):
    """A crontab command as cron runs it: the first % that is not escaped ends the command and the rest is its
    stdin, with each further % a new line; \\% is a plain %"""
    text, given, index = "", None, 0
    while index < len(command):
        char = command[index]
        if char == "\\" and command[index + 1:index + 2] == "%":
            text += "%"
            index += 2
            continue
        if char == "%":
            given = command[index + 1:].replace("%", "\n") + "\n"
            break
        text += char
        index += 1
    return text, given


def crontab_run(system, command):
    """A job of root's crontab run now, as cron runs it: by /bin/sh as root in its home, with cron's few variables
    and the crontab's own (PATH=, MAILTO= and the like). Its output goes where the command sends it."""
    command = str(command).strip()
    entries = crontab_entries(system)
    job = next((entry for entry in entries if entry.get("kind") == "job" and entry["command"] == command), None)
    if not job:
        raise Stop("That job is not in root's crontab.")
    name = job["label"] or HARBOR_LABELS.get(job["harbor"], "") or command
    env = {"HOME": "/root", "LOGNAME": "root", "USER": "root", "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin"}
    for entry in entries:
        found = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", entry.get("line", "")) if entry.get("kind") == "other" else None
        if found:
            value = found.group(2)
            env[found.group(1)] = value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value
    text, given = cron_command(command)
    system.plan([f"Run {name}"])
    system.step(f"Run {name}")
    target = job_output(command)
    started = int(time.time())
    # env -i: only cron's variables, not the admin's (HARBOR_PROGRESS would put the window's steps in the job's log)
    result = system.run("/usr/bin/env", "-i", *(f"{key}={value}" for key, value in env.items()), "/bin/sh", "-c", text,
                        input=given or "", cwd=env["HOME"], check=False)
    state = cron_state(system)
    state["by_hand"][command] = ([{"at": started, "code": result.returncode}] + list(state["by_hand"].get(command) or []))[:10]
    save_cron_state(system, state, {entry["command"] for entry in entries if entry.get("kind") == "job"})
    where = f" Its output is in {target[1]}." if target and target[0] == "file" else \
        f" Its output is in the journal (logger -t {target[1]})." if target else ""
    if not result.ok:
        raise Stop(f"The job ended with exit code {result.returncode}.{where}", result.returncode)
    system.say(f"The job ran and ended well (exit code 0).{where}")


# ---- the job logs kept small (Scheduled jobs): logrotate for the file each job writes its output to
LOGROTATE = "/etc/logrotate.d/vns-harbor-jobs"
LOGROTATE_NOTE = "# VNS Harbor: the log files of root's scheduled jobs (Scheduled jobs, Keep logs small)"


def rotated_elsewhere(system, path):
    """Whether another logrotate config has this file already (logrotate refuses a file named twice)"""
    import fnmatch
    folder = system.path("/etc/logrotate.d")
    configs = [system.path("/etc/logrotate.conf"), *(sorted(folder.iterdir()) if folder.is_dir() else [])]
    for config in configs:
        if config.name == LOGROTATE.rsplit("/", 1)[1] or not config.is_file():
            continue
        try:
            text = config.read_text(errors="replace")
        except OSError:
            continue
        for word in re.findall(r"(?m)^\s*(\S[^{]*?)\s*\{?\s*$", text):
            for pattern in re.findall(r"\"([^\"]+)\"|(/\S+)", word):
                pattern = pattern[0] or pattern[1]
                if pattern.startswith("/") and fnmatch.fnmatch(path, pattern):
                    return True
    return False


def job_logs(system, entries=None):
    """The files root's jobs write their output to (> or >> a file), each once, but those another config rotates"""
    entries = crontab_entries(system) if entries is None else entries
    found = []
    for entry in entries:
        target = job_output(entry["command"]) if entry.get("kind") == "job" else None
        if target and target[0] == "file" and target[1] not in found and target[1].startswith("/var/log/"):
            found.append(target[1])
    return [path for path in found if not rotated_elsewhere(system, path)]


def logrotate_state(system):
    config = system.path(LOGROTATE)
    return {"on": config.is_file(), "files": job_logs(system)}


def logrotate(system, on, entries=None, quiet=False):
    """Each job's log rotated weekly, the last 8 kept compressed (on), or left as it is (off). Written again when the jobs
    change, while it is on."""
    config = system.path(LOGROTATE)
    if not on:
        if config.is_file():
            if not quiet:
                system.plan(["Stop rotating the jobs' logs"])
                system.step("Stop rotating the jobs' logs")
            config.unlink()
        if not quiet:
            system.say("The jobs' logs are left as they are now.")
        return
    files = job_logs(system, entries)
    if not quiet:
        system.plan(["Rotate the jobs' logs"])
        system.step("Rotate the jobs' logs")
    quoted = [f'"{path}"' if re.search(r"\s", path) else path for path in files]
    text = (LOGROTATE_NOTE + "\n" + "\n".join(quoted) + "\n{\n\tweekly\n\trotate 8\n\tcompress\n\tdelaycompress\n\tmissingok\n"
            "\tnotifempty\n\tcopytruncate\n}\n") if files else LOGROTATE_NOTE + "\n# No job writes a log of its own yet.\n"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")
    os.chmod(config, 0o644)
    if not quiet:
        system.say(f"{len(files)} log{'' if len(files) == 1 else 's'} rotated weekly, the last 8 kept compressed.")


# ---- the OS updates
def unattended_on(system):
    """unattended-upgrades installed and on: the security updates installed by themselves, as Ubuntu chooses them"""
    steps = ["Install unattended-upgrades", "Turn on the automatic security updates"]
    system.plan(steps)
    system.step(steps[0])
    if not installed(system, "unattended-upgrades"):
        system.run("apt-get", "install", "-y", "unattended-upgrades", env={"DEBIAN_FRONTEND": "noninteractive"})
    system.step(steps[1])
    path = system.path("/etc/apt/apt.conf.d/20auto-upgrades")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n', encoding="utf-8")
    system.say("Security updates are installed by themselves, once a day.")


def os_update(system, scope, health=None):
    """The OS updates installed now: the security updates (security) or every update (all), without questions (a
    changed config file keeps the one here), waiting for apt's lock while unattended-upgrades holds it. Packages are
    never removed. What waits afterwards, and a restart asked for, is read again for Health."""
    import importlib.util
    from harbor.core.paths import LIB
    if scope not in ("security", "all"):
        raise Stop("Install the security updates or all of them.")
    if health is None:
        spec = importlib.util.spec_from_file_location("vns_server_health", LIB / "server_health.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
    steps = ["Finish an update that was stopped", "Read the package lists", "Install the security updates" if scope == "security" else "Install the updates",
             "Read what waits now"]
    system.plan(steps)
    # Microsoft's packages (msodbcsql18, mssql-tools18) are updated only with their licence accepted, as it was when
    # Harbor installed them
    env = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a", "ACCEPT_EULA": "Y"}
    apt = ["apt-get", "-y", "-o", "DPkg::Lock::Timeout=600", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
    # Packages unpacked by an update that stopped halfway are configured first: apt does nothing else until they are
    system.step(steps[0])
    system.run("dpkg", "--force-confdef", "--force-confold", "--configure", "-a", env=env)
    system.step(steps[1])
    system.run("apt-get", "-o", "DPkg::Lock::Timeout=600", "update", env=env)
    system.step(steps[2])
    if scope == "security":
        names = [item["name"] for item in health.updates()["packages"] if item["security"]]
        if names:
            system.run(*apt, "install", "--only-upgrade", *names, env=env)
    else:
        names = None
        system.run(*apt, "upgrade", "--with-new-pkgs", env=env)
    system.step(steps[3])
    # Microsoft's own list may have come with the update, with the repository Harbor named in its own
    from harbor.sql.server import drop_duplicate_list
    drop_duplicate_list(system)
    found = health.updates()
    health.write_json(health.STATE / "updates.json", found)
    done = "No security update waited." if names == [] else f"{len(names)} security update{'' if len(names) == 1 else 's'} installed." if names else "The updates are installed."
    left = f" {found['count']} update{'' if found['count'] == 1 else 's'} still wait{'s' if found['count'] == 1 else ''}." if found["count"] else " Nothing waits now."
    system.say(done + left + (" The server asks to be restarted." if found["reboot"] else ""))


# ---- Node.js
def node_install(system, major):
    """Node.js of a major version from NodeSource's repository. There is one nodejs package: another major version is
    replaced by it, also a newer one (apt does not go down a version by itself, so the version is named). Packages
    installed with npm -g stay in /usr/lib/node_modules."""
    if not re.fullmatch(r"[0-9]{2}", str(major)) or int(major) < 18:
        raise Stop(f"Not a Node.js version: {major} (such as 22 or 24)")
    before = system.output("node", "--version", check=False) if system.which("node") else ""
    install = f"Install Node.js {major}" + (f" in place of {before.lstrip('v')}" if before and not before.startswith(f"v{major}.") else "")
    system.plan([f"Add the Node.js {major} repository", install])
    system.step(f"Add the Node.js {major} repository")
    with tempfile.NamedTemporaryFile(suffix=".sh") as setup:
        system.run("curl", "-fsSL", NODESOURCE.format(major), "-o", setup.name)
        system.run("bash", setup.name)
    system.step(install)
    # The newest of the major version the repository has: named, so it replaces a newer one too
    offered = re.findall(r"(?m)^\s*nodejs\s*\|\s*(\S+)\s*\|", system.output("apt-cache", "madison", "nodejs", check=False))
    wanted = next((version for version in offered if version.startswith(f"{major}.")), "")
    system.run("apt-get", "install", "-y", "--allow-downgrades", f"nodejs={wanted}" if wanted else "nodejs")
    version = system.output("node", "--version", check=False)
    if not version.startswith(f"v{major}."):
        raise Stop(f"Node.js {major} was not installed (node --version: {version or 'none'}).")
    replaced = f", in place of {before.lstrip('v')}" if before and before != version else ""
    system.say(f"Node.js {version.lstrip('v')} with npm {system.output('npm', '--version', check=False)}{replaced}")


def node_remove(system):
    if not installed(system, "nodejs"):
        system.say("Node.js is not installed.")
        return
    system.plan(["Remove Node.js"])
    system.step("Remove Node.js")
    system.run("apt-get", "remove", "--purge", "-y", "nodejs")
    for listing in system.path("/etc/apt/sources.list.d").glob("nodesource*"):
        listing.unlink()
    system.say("Node.js removed")
