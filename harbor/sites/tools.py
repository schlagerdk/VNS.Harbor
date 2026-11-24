"""The tools a site's scripts folder had: from the site templates of VNS 1.x (phase 2d).

Each template now calls one of these, with the site's name (and, for publish, the build configuration it was made with)
baked in when it was rendered, so a site's scripts hold nothing else. Scripts rendered before keep working as they are
until phase 6 removes them.
"""
from pathlib import Path
import os
import re
import shutil
import tempfile

from harbor.core.bindings import certificate_domains
from harbor.core.dotnet import runtime_identifier
from harbor.core.site_config import ACME, ACME_FOLDERS, ACME_SERVER, require_acme
from harbor.core.system import CommandFailed, Stop
from harbor.sites.actions import site_named
from harbor.core.paths import BIN, LIB

SCHEMA_FILES = ("appsettings-schema.json", "appsettings-schema.usync.json", "appsettings-schema.Umbraco.Cms.json", "umbraco-package-schema.json")


def yes_unless_no(answer):
    """[Y/n]: anything but an answer starting with n is yes"""
    return not answer.strip()[:1] in ("n", "N")


def working_folder(system, site):
    """The folder the site's service runs from, or the site folder"""
    service = system.path(site.service_path)
    text = service.read_text() if service.is_file() else ""
    working = next((line[len("WorkingDirectory="):] for line in text.splitlines() if line.startswith("WorkingDirectory=")), "")
    return working.rstrip("/") or site.site_root


def service_environment(system, site):
    """The ASP.NET Core environment the site's service runs with (Development, Test, Staging, Production), or nothing"""
    service = system.path(site.service_path)
    text = service.read_text() if service.is_file() else ""
    return next((line[len("Environment=ASPNETCORE_ENVIRONMENT="):].strip() for line in text.splitlines()
                 if line.startswith("Environment=ASPNETCORE_ENVIRONMENT=")), "")


def configuration_for(environment):
    """Staging and Production are Release builds; Development and Test are Debug builds"""
    return "Release" if environment in ("Staging", "Production") else "Debug" if environment else ""


def restart_apache(system):
    system.run("systemctl", "restart", "apache2")


def check_service(system, name):
    site = site_named(system, name)
    system.run("systemctl", "status", site.service_name, "--no-pager")


def restart_service(system, name):
    site = site_named(system, name)
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "restart", site.service_name)
    system.sleep(2)
    system.run("systemctl", "status", site.service_name, "--no-pager", check=False)
    if system.run("systemctl", "is-active", "--quiet", site.service_name, check=False).ok:
        system.say(f"OK: {site.service_name} is running")
    else:
        system.say(f"FAILED: {site.service_name} is not running. Last log lines:")
        system.run("journalctl", "-u", site.service_name, "-n", "20", "--no-pager", check=False)
        raise Stop("", 1)


def edit_conf(system, name):
    site = site_named(system, name)
    system.run("nano", site.config_path, check=False)
    if yes_unless_no(system.ask("Test config and reload apache? [Y/n]: ")):
        if system.run("apache2ctl", "configtest", check=False).ok:
            system.run("systemctl", "reload", "apache2")


def edit_service(system, name):
    site = site_named(system, name)
    system.run("nano", site.service_path, check=False)
    if yes_unless_no(system.ask("Reload daemon and restart service? [Y/n]: ")):
        system.run("systemctl", "daemon-reload")
        system.run("systemctl", "restart", site.service_name)
        system.run("systemctl", "status", site.service_name, "--no-pager")


def clear_logs(system, name):
    """Umbraco's log folder, with the site stopped while it goes"""
    site = site_named(system, name)
    system.run("systemctl", "stop", site.service_name, check=False)
    shutil.rmtree(system.path(f"{working_folder(system, site)}/umbraco/Logs"), ignore_errors=True)
    system.run("systemctl", "start", site.service_name, check=False)
    system.run("systemctl", "status", site.service_name, "--no-pager", check=False)


def pinned_to_latest(text, latest):
    """requirements.txt with each package pinned with == moved to its latest version (latest: name to version); other
    lines (ranges, comments, options) as they are, as pip --upgrade follows those itself"""
    def norm(value):
        return re.sub(r"[-_.]+", "-", value).lower()
    found = {norm(name): version for name, version in latest.items()}
    changed = []

    def line(match):
        name, extras, version, rest = match.group(1), match.group(2) or "", match.group(3), match.group(4)
        new = found.get(norm(name))
        if not new or new == version:
            return match.group(0)
        changed.append(f"{name} {version} → {new}")
        return f"{name}{extras}=={new}{rest}"
    text = re.sub(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([^\s;#]+)(.*)$", line, text)
    return text, changed


def update_packages(system, name):
    """A Python site's packages, upgraded as the site's own user. A package pinned with == in requirements.txt is moved
    to its latest version there first: else pip --upgrade keeps it where it is, and the update did nothing."""
    import json
    site = site_named(system, name)
    pip = ["sudo", "-u", "www-data", "./venv/bin/pip"]
    requirements = system.path(f"{site.site_root}/app/requirements.txt")
    listed = system.run(*pip, "list", "--outdated", "--format=json", "--disable-pip-version-check", "--no-cache-dir",
                        check=False, capture=True, cwd=site.site_root)
    try:
        latest = {item["name"]: item["latest_version"] for item in json.loads(listed.stdout or "[]") if isinstance(item, dict)} if listed.ok else {}
    except (ValueError, KeyError, TypeError):
        latest = {}
    if requirements.is_file() and latest:
        text, changed = pinned_to_latest(requirements.read_text(), latest)
        if changed:
            requirements.write_text(text)
            system.say("requirements.txt moved to the latest versions: " + ", ".join(changed))
    system.run(*pip, "install", "--upgrade", "--no-cache-dir", "-r", "app/requirements.txt", check=False, cwd=site.site_root)
    system.run("systemctl", "restart", site.service_name, check=False)
    system.run("systemctl", "status", site.service_name, "--no-pager", check=False)


def fix_app_permissions(system, name):
    """A Python site's app folder, owned by www-data: folders 755, files 644"""
    site = site_named(system, name)
    app = f"{site.site_root}/app"
    if not system.path(app).is_dir():
        raise Stop(f"Folder not found: {app}")
    system.run("chown", "-R", "www-data:www-data", app)
    system.run("find", app, "-type", "d", "-exec", "chmod", "755", "{}", ";")
    system.run("find", app, "-type", "f", "-exec", "chmod", "644", "{}", ";")
    system.say(f"Permissions fixed on {app}")


def reissue_certificate(system, name):
    """The certificate for the site's binding and aliases issued again, its config as it is"""
    site = site_named(system, name)
    config = system.path(site.config_path)
    binding = next((line.split()[1] for line in config.read_text().splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "") \
        if config.is_file() else ""
    if not binding:
        raise Stop(f"Could not determine ServerName in {site.config_path}")
    try:
        domains = certificate_domains(config.read_text(), binding, site.config_path)
        require_acme(system)
    except (ValueError, RuntimeError) as error:
        raise Stop(str(error)) from None
    issue = f"Issue certificate for {' '.join(domains)}"
    system.plan([issue, "Install the certificate"])
    system.step(issue)
    system.run(ACME, *ACME_FOLDERS, "--issue", "--apache", "--server", ACME_SERVER, "--force", *(argument for domain in domains for argument in ("-d", domain)))
    # Installed where the config reads it, so each renewal reloads Apache (and a certificate acme.sh did not know is
    # renewed from now on)
    system.step("Install the certificate")
    system.run(ACME, *ACME_FOLDERS, "--install-cert", "-d", binding.lower(), "--key-file", site.key_file, "--fullchain-file", site.certificate_file,
               "--reloadcmd", "systemctl reload apache2")


def outdated(system, name):
    """The project's outdated NuGet packages, and an upgrade if wanted"""
    site = site_named(system, name)
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise Stop("dotnet SDK not found. Please install it first.")
    # dotnet-outdated is an apphost that needs DOTNET_ROOT when .NET is not in /usr/share/dotnet
    env = {} if os.environ.get("DOTNET_ROOT") else {"DOTNET_ROOT": os.path.dirname(os.path.realpath(dotnet))}
    system.plan(["Running dotnet outdated..."])
    system.step("Running dotnet outdated...")
    output = system.run("dotnet", "outdated", capture=True, check=False, env=env, cwd=site.site_root)
    system.say((output.stdout + output.stderr).rstrip("\n"))
    if "No outdated dependencies were detected" in output.stdout + output.stderr:
        system.say("\nDone.")
        return
    if system.ask("\nDo you want to upgrade the packages? (y/n): ").strip() in ("y", "Y"):
        system.plan(["Running dotnet outdated --upgrade..."])
        system.step("Running dotnet outdated --upgrade...")
        system.run("dotnet", "outdated", "--upgrade", env=env, cwd=site.site_root)
        system.say("Upgrade completed.")
    else:
        system.say("No upgrade performed.")
    system.say("\nDone.")


def publish(system, name, configuration="", folder="", environment_name=""):
    """The site built into its publish folder and started again; recorded as published when it worked. folder and
    environment_name are what a very old publish script had (update_admin_site.sh keeps them when it upgrades it)."""
    site = site_named(system, name)
    configuration_given = configuration
    # A migrated site (phase 6) has no publish script to hold them: what it held is in Harbor's state
    from harbor.environments import settings
    if settings.migrated(system, site.site_root):
        from harbor.state.store import read_json
        saved = read_json(system.path(f"{settings.STATE}/{site.name}/site.json")) or {}
        configuration = configuration or saved.get("configuration", "")
        folder = folder or saved.get("folder", "")
        environment_name = environment_name or saved.get("environment_name", "")
    # The environment the site runs as decides, when it has one; what the site was made with, before it
    configuration = configuration_given or configuration_for(environment_name or service_environment(system, site)) or configuration or "Debug"
    if configuration not in ("Debug", "Release"):
        raise Stop("Usage: publish.sh [Debug|Release]")
    if os.environ.get("VNS_PUBLISH_TRACKED") != "test":
        # Run again inside publish_status.py, which records the publish when it works
        harbor = BIN
        extra = [*(["--folder", folder] if folder else []), *(["--environment-name", environment_name] if environment_name else [])]
        result = system.run("python3", str(LIB / "publish_status.py"), "run", site.site_root, "test", "python3", str(harbor), "site",
                            "publish", site.name, configuration, *extra, check=False)
        if not result.ok:
            raise Stop("", result.returncode)
        return
    projects = sorted(system.path(site.site_root).glob("*.csproj"))
    if len(projects) != 1:
        raise Stop(f"{site.name} needs exactly one project.")
    project = projects[0]
    working = f"{site.site_root}/{folder}" if folder else working_folder(system, site)
    folder = os.path.relpath(working, site.site_root) if working != site.site_root else "."
    unit = site.service_name
    stopped = False
    # Which site, as it reads in Publish all among its environments
    named = environment_name or service_environment(system, site)
    build = f"Publish {site.name} ({named + ', ' if named else ''}{configuration})"
    system.plan([f"Stop {unit}", build, "Clean up", f"Start {unit}", f"Check {unit} runs"])
    try:
        system.step(f"Stop {unit}")
        system.run("systemctl", "stop", unit)
        stopped = True
        system.step(build)
        shutil.rmtree(system.path(f"{working}/runtimes"), ignore_errors=True)
        system.run("python3", str(LIB / "build_as.py"), "--", "dotnet", "publish", project.name, "-c", configuration, f"-p:PublishDir={folder}",
                   *([f"-p:EnvironmentName={environment_name}"] if environment_name else []),
                   f"-p:CustomAfterMicrosoftCommonTargets={LIB}/publish.targets", "--runtime", runtime_identifier(system.output("uname", "-m")),
                   "--self-contained", "false", "--nologo", cwd=site.site_root)
        system.step("Clean up")
        system.say("Delete bin")
        shutil.rmtree(system.path(f"{site.site_root}/bin"), ignore_errors=True)
        if "Umbraco.Cms" in project.read_text(errors="replace"):
            system.say("Delete Umbraco schema files and App_Plugins")
            for base in (site.site_root, working):
                for schema in SCHEMA_FILES:
                    system.path(f"{base}/{schema}").unlink(missing_ok=True)
            shutil.rmtree(system.path(f"{site.site_root}/App_Plugins"), ignore_errors=True)
        system.run("chown", "-R", "www-data:www-data", folder, cwd=site.site_root)
        system.run("python3", str(LIB / "sql_startup.py"), unit)
        # Harbor's image signing key from 1.0.46 taken off (harden_service.py)
        system.run("python3", str(LIB / "harden_service.py"), unit, check=False)
        system.step(f"Start {unit}")
        system.run("systemctl", "start", unit)
        stopped = False
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None
    finally:
        # Always bring the site back up, also if the publish fails
        if stopped:
            system.run("systemctl", "start", unit, check=False)
    system.step(f"Check {unit} runs")
    keep_connection(system, site, "main")
    system.sleep(2)
    system.run("systemctl", "status", unit, "--no-pager", check=False)
    if system.run("systemctl", "is-active", "--quiet", unit, check=False).ok:
        system.say(f"OK: {unit} is running")
    else:
        system.say(f"FAILED: {unit} is not running. Last log lines:")
        system.run("journalctl", "-u", unit, "-n", "20", "--no-pager", check=False)
        raise Stop("", 1)


def keep_connection(system, site, environment):
    """A site with the VNS.Harbor package gets its connection to Harbor again where it runs (vns-harbor.json, root:www-data)"""
    from harbor.umbraco import backoffice
    if not backoffice.uses_package(system, site):
        return
    try:
        backoffice.connect(system, site.name, environment)
    except (Stop, OSError) as error:
        system.warn(f"The site's connection to Harbor was not written: {error}")


# ---- an environment's logs
CLEAR_REMOTE_LOGS = '''set -e
systemctl stop '{folder}'
_dir='/var/www/{folder}/umbraco/Logs'
if [[ -d "${{_dir}}" ]]; then
	rm -rf "${{_dir}}"
	echo "Deleted ${{_dir}}"
else
	echo "Nothing to delete: ${{_dir}} not found"
fi
systemctl start '{folder}'
systemctl status '{folder}' --no-pager
'''


def clear_environment_logs(system, name, target="prod"):
    """Umbraco's logs where the environment is: a folder here, a Linux server over SSH, or IIS (taken offline briefly,
    as IIS keeps the log files open)"""
    from harbor.environments import settings
    from harbor.environments.windows import Share
    site = site_named(system, name)
    environment = settings.load(system, site.site_root, target)
    if environment.windows:
        try:
            share = Share(system, environment).connect()
        except RuntimeError as error:
            raise Stop(str(error)) from None
        logs = f"{share.target}/umbraco/Logs"
        if not share.rclone("lsf", logs, "--max-depth", "1", check=False, capture=True).ok:
            system.say("Nothing to delete: umbraco/Logs not found")
            return
        try:
            share.take_offline()
            share.rclone("purge", logs)
            system.say("Deleted umbraco/Logs")
        finally:
            # Always bring the site back up, also if deleting fails
            share.bring_online()
        return
    if environment.LOCAL_PATH:
        # Local folder: no service is managed, only the log folder is deleted
        logs = f"{environment.root}/umbraco/Logs"
        if system.path(logs).is_dir():
            shutil.rmtree(system.path(logs))
            system.say(f"Deleted {logs}")
        else:
            system.say(f"Nothing to delete: {logs} not found")
        return
    if not environment.IP or not environment.USER or not environment.FOLDER:
        raise Stop("Set PROD_IP, PROD_USER and PROD_FOLDER (or PROD_LOCAL_PATH) at the top of this script")
    result = settings.remote_exec(system, environment, "bash -s", input=CLEAR_REMOTE_LOGS.format(folder=environment.FOLDER), check=False)
    if not result.ok:
        raise Stop("", result.returncode)
