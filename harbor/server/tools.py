"""What the server has for the sites, and the tools to change it (phase 4 of dev/HARBOR.md): SQL Server, sqlcmd, the sa
password in sql/, the backup and log shrink cron jobs, .NET and Node.js. The admin's Server tools window shows status()
and runs the rest as tasks.
"""
import re
import shutil
import tempfile

from harbor.core.dotnet import DOTNET_DIR, ensure_channel
from harbor.core.system import Stop
from harbor.state.store import read_json
from harbor.sql.databases import PLACEHOLDER, SA_SECRET, SQL_DIR, SQLCMD
from harbor.sql.server import FALLBACK_UBUNTU, SUPPORTED_UBUNTU, installed, os_release, sa_scripts

NODESOURCE = "https://deb.nodesource.com/setup_{}.x"
HARBOR = "/usr/local/bin/harbor"
# The jobs Harbor adds, as harbor commands (they were /vns/sql/cron/*.sh, which go with the cutover); others in root's
# crontab (such as the custom cron/delete_old_2dm_data.sh) are left alone
JOBS = {"shrink_logs": f"0 */4 * * * {HARBOR} sql shrink-logs >> /var/log/sql_shrink_logs.log 2>&1",
        "backup_everything": f"15 */4 * * * {HARBOR} sql backup-everything >> /var/log/sql_backup_everything.log 2>&1"}


def package_version(system, package):
    return system.output("dpkg-query", "-W", "-f=${Version}", package, check=False) if installed(system, package) else ""


def crontab(system):
    found = system.run("crontab", "-l", check=False, capture=True)
    return found.stdout if found.ok else ""


def job_lines(text, name):
    """A job's lines: the harbor command, or the script in /vns/sql/cron it was"""
    command = f"harbor sql {name.replace('_', '-')}"
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


def status(system, sql_folder=SQL_DIR):
    """Everything the Server tools window shows"""
    try:
        release = os_release(system)
    except OSError:
        release = {}
    scripts = sa_scripts(system, sql_folder) if sql_folder.is_dir() else []
    table = crontab(system)
    mine = {line for name in JOBS for line in job_lines(table, name)}
    others = [line for line in table.splitlines() if line.strip() and not line.lstrip().startswith("#") and line not in mine]
    node = system.output("node", "--version", check=False) if system.which("node") else ""
    return {
        "os": {"name": release.get("PRETTY_NAME", ""), "id": release.get("ID", ""), "version": release.get("VERSION_ID", "")},
        "sql_server": {"installed": installed(system, "mssql-server"), "version": package_version(system, "mssql-server"),
                       "running": system.output("systemctl", "is-active", "mssql-server", check=False) == "active",
                       # Microsoft publishes SQL Server for some Ubuntu versions only: on another it is installed from these
                       "packages_for": "" if release.get("VERSION_ID", "") in SUPPORTED_UBUNTU else FALLBACK_UBUNTU},
        "sql_tools": {"installed": system.path(SQLCMD).is_file(), "version": package_version(system, "mssql-tools18")},
        "sa_password": {"scripts": len(scripts),
                        "set": bool((read_json(system.path(SA_SECRET)) or {}).get("sa_password"))
                        or (bool(scripts) and not any(f"'{PLACEHOLDER}'" in path.read_text(errors="replace") for path in scripts))},
        "cron": {"jobs": {name: bool(job_lines(table, name)) for name in JOBS}, "others": others},
        "dotnet": dotnet(system),
        "node": {"version": node.lstrip("v"), "npm": system.output("npm", "--version", check=False) if node else ""},
    }


# ---- cron
def cron(system, on):
    """The backup and log shrink jobs in root's crontab once each, or taken out; other jobs stay as they are"""
    table = crontab(system)
    lines = table.splitlines()
    if on:
        missing = [line for name, line in JOBS.items() if not job_lines(table, name)]
        if not missing:
            system.say("The backup and log shrink jobs are in root's crontab.")
            return
        system.plan(["Add the backup and log shrink jobs to root's crontab"])
        system.step("Add the backup and log shrink jobs to root's crontab")
        lines += missing
    else:
        mine = {line for name in JOBS for line in job_lines(table, name)}
        if not mine:
            system.say("The backup and log shrink jobs are not in root's crontab.")
            return
        system.plan(["Take the backup and log shrink jobs out of root's crontab"])
        system.step("Take the backup and log shrink jobs out of root's crontab")
        lines = [line for line in lines if line not in mine]
    system.run("crontab", "-", input="".join(line + "\n" for line in lines))
    system.say("Backups every 4 hours (at :15), logs shrunk every 4 hours." if on else "No backups or log shrinks are run by cron now.")


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
    ensure_channel(system, channel, force=update)
    # The site services run /usr/bin/dotnet
    link = system.path("/usr/bin/dotnet")
    if not link.exists() and not link.is_symlink():
        link.symlink_to(f"{DOTNET_DIR}/dotnet")
    after = {item["channel"]: item for item in dotnet(system)}.get(channel)
    if not after or not after["sdk"] or not after["runtime"]:
        raise Stop(f".NET {channel} was not installed in {DOTNET_DIR}.")
    if before and (before["sdk"], before["runtime"]) == (after["sdk"], after["runtime"]):
        system.say(f".NET {channel} is up to date (SDK {after['sdk']}, runtime {after['runtime']}).")
    else:
        system.say(f".NET {channel}: SDK {after['sdk']}, runtime {after['runtime']}. Sites start on the new runtime when they restart.")


def sites_using(system, channel):
    """The sites here that build or run on a .NET channel: their project (TargetFramework, an SDK pinned in global.json)
    or what they publish (runtimeconfig.json)"""
    major = channel.split(".")[0]
    framework = re.compile(rf"<TargetFrameworks?>[^<]*\bnet{major}\.0\b")
    runtime = re.compile(rf'"(?:tfm|version)"\s*:\s*"(?:net)?{major}\.0[\.\d]*"')
    web = system.path("/var/www")
    found = []
    for site in sorted(web.iterdir()) if web.is_dir() else []:
        if not site.is_dir() or site.is_symlink():
            continue
        files = [*site.glob("*.csproj"), *site.glob("global.json"), *site.glob("*.runtimeconfig.json"), *site.glob("*/*.runtimeconfig.json")]
        for path in files:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if (framework if path.suffix == ".csproj" else runtime).search(text) or (path.name == "global.json" and f'"{major}.0.' in text):
                found.append(site.name)
                break
    return found


def dotnet_remove(system, channel):
    """A channel's SDKs and runtimes taken out of /usr/lib/dotnet. Never the newest (its host runs the others), and never
    one a site here uses."""
    channel = channel_of(channel)
    installed_channels = [item["channel"] for item in dotnet(system)]
    if channel not in installed_channels:
        raise Stop(f".NET {channel} is not installed.")
    newest = max(installed_channels, key=lambda item: int(item.split(".")[0]))
    if channel == newest:
        raise Stop(f".NET {channel} is the newest here: its host runs the other versions, so it stays.")
    using = sites_using(system, channel)
    if using:
        raise Stop(f".NET {channel} is used by {', '.join(using)}: move them to another version first.")
    system.plan([f"Remove .NET {channel}"])
    system.step(f"Remove .NET {channel}")
    root = system.path(DOTNET_DIR)
    prefix = channel.split(".")[0] + ".0."
    removed = 0
    for parent in ("sdk", "sdk-manifests", "templates", "host/fxr", "shared/Microsoft.NETCore.App", "shared/Microsoft.AspNetCore.App",
                   "packs/Microsoft.NETCore.App.Ref", "packs/Microsoft.AspNetCore.App.Ref", "packs/Microsoft.NETCore.App.Host.linux-x64",
                   "packs/Microsoft.NETCore.App.Host.linux-arm64"):
        folder = root / parent
        for path in sorted(folder.glob(prefix + "*")) if folder.is_dir() else []:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed += 1
    system.say(f".NET {channel} is removed ({removed} folders). The other versions stay.")


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


def crontab_entries(system):
    """Root's crontab as its lines: each job with its schedule and command (and which of Harbor's jobs it is), and the
    other lines (variables, comments) as they are"""
    entries = []
    for line in crontab(system).splitlines():
        parts = job_parts(line)
        if parts:
            harbor = next((name for name in JOBS if job_lines(line, name)), "")
            entries.append({"kind": "job", "schedule": parts[0], "command": parts[1], "harbor": harbor})
        elif line.strip():
            entries.append({"kind": "other", "line": line})
    return entries


def crontab_save(system, entries):
    """Root's crontab made of these entries: jobs ({schedule, command}) checked, the other lines kept as they are"""
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
        lines.append(f"{schedule} {command}")
    system.plan(["Save root's crontab"])
    system.step("Save root's crontab")
    system.run("crontab", "-", input="".join(line + "\n" for line in lines))
    system.say(f"Root's crontab has {sum(1 for entry in entries if entry.get('kind') != 'other')} jobs.")


# ---- Node.js
def node_install(system, major):
    """Node.js of a major version from NodeSource's repository, replacing another major version"""
    if not re.fullmatch(r"[0-9]{2}", str(major)) or int(major) < 18:
        raise Stop(f"Not a Node.js version: {major} (such as 22 or 24)")
    system.plan([f"Add the Node.js {major} repository", f"Install Node.js {major}"])
    system.step(f"Add the Node.js {major} repository")
    with tempfile.NamedTemporaryFile(suffix=".sh") as setup:
        system.run("curl", "-fsSL", NODESOURCE.format(major), "-o", setup.name)
        system.run("bash", setup.name)
    system.step(f"Install Node.js {major}")
    system.run("apt-get", "install", "-y", "nodejs")
    version = system.output("node", "--version", check=False)
    if not version.startswith(f"v{major}."):
        raise Stop(f"Node.js {major} was not installed (node --version: {version or 'none'}).")
    system.say(f"Node.js {version.lstrip('v')} with npm {system.output('npm', '--version', check=False)}")


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
