"""A site exported for a customer (Settings, Export Site): the site and its environments, built for Windows or Linux,
with its databases, without anything of VNS Harbor, in one zip to download or send as a link. Laid out as the site is
on its server, so it can be put on another and built there to the folder wanted:

  <site>/<project>.csproj ...     the project, without the VNS.Harbor package or Harbor's NuGet source; each folder below
                                  left out of its build
  <site>/_publish, _test, ...     each environment: a fresh build for the OS chosen, with its own settings, Umbraco data,
                                  uSync, Views, App_Data and media. One from another server is _<environment>, or
                                  _<environment>_external when a folder here has that name
  <site>/databases/<folder>.bak   (Microsoft SQL Server) or .dump (PostgreSQL), the environment's database
  <site>/scripts/publish.sh|.ps1  builds the project and publishes it to the environment chosen, keeping its own files

The environments' settings have the database server and password in their connection strings blanked, and the mail
server and sender as placeholders (example.com) the site starts with: the customer fills in their own.

Each export is kept in /var/lib/harbor/exports/<id>: meta.json, the zip, and, when the site was deleted from Harbor with
it, a snapshot that Restore puts back as it was. The zip is deleted 72 hours after it was first downloaded, and the
snapshot with it (harbor site export-cleanup, run every minute by keep-alive).

The connections to the databases' servers come in VNS_EXPORT_DATABASES ({environment folder: connection}), never on a
command line."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import time
import zipfile

from harbor.core.names import service_name
from harbor.core.paths import LIB
from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

EXPORTS = "/var/lib/harbor/exports"
WEB_ROOT = "/var/www"
KEEP_AFTER_DOWNLOAD = 72 * 3600
OSES = {"windows": "win-x64", "linux": "linux-x64"}
# A site that is not built (HTML, Python) runs on either: exported once, for any
ANY = "any"
# Never from a site's own folder: what is made again where it runs (a Python site's virtual environment, caches)
MADE_AGAIN = ("venv", ".venv", "__pycache__", "node_modules")
# An environment's own, kept from where it runs over the fresh build (as a publish keeps them): its settings and data
OWN = ("umbraco", "uSync", "Views", "App_Data", "app_data")
# Never in an export: Harbor's own files, logs, caches and what a build makes again
HARBOR_FILES = ("vns-harbor.json", ".harbor-published", ".vns-publish", "scripts")
LEFT_OUT = ("umbraco/Logs", "umbraco/Data/TEMP", "umbraco/Data/Lucene")
SOURCE_LEFT_OUT = {"bin", "obj", "_publish", "scripts", ".vns-publish", ".git", "node_modules", ".vs", "vns-harbor.json"}


# ---- the exports kept here
def folder_of(system, identifier):
	if not re.fullmatch(r"[0-9a-f]{16}", identifier or ""):
		raise Stop("That is not an export.")
	return system.path(f"{EXPORTS}/{identifier}")


def meta(system, identifier):
	found = read_json(folder_of(system, identifier) / "meta.json")
	if not isinstance(found, dict):
		raise Stop("That export is not here any more.")
	return found


def save(system, item):
	write_json(folder_of(system, item["id"]) / "meta.json", item, private=True)


def exports(system):
	"""Every export here, newest first (never its token's secret beyond what the admin shows)"""
	root = system.path(EXPORTS)
	found = []
	for path in sorted(root.glob("*/meta.json")) if root.is_dir() else []:
		item = read_json(path)
		if isinstance(item, dict):
			found.append(item)
	return sorted(found, key=lambda item: item.get("at") or 0, reverse=True)


# ---- what is exported
def units(system, name, targets):
	"""The main site and the environments chosen: (folder name in the zip, kind, unit, where it runs, environment,
	target)"""
	from harbor.environments import settings
	from harbor.sites.actions import site_named
	site = site_named(system, name)
	root = site.site_root
	if not system.path(root).is_dir():
		raise Stop(f"{name} is not a site here.")
	found = []
	if "main" in targets:
		found.append((name, "local", name, running_folder(system, name, root), None, "main"))
	for target in settings.target_ids(system, root):
		environment = settings.load(system, root, target)
		# By its id, or by its folder (unit:<folder>): an environment's server asks for its own by the name it has there
		if target not in targets and f"unit:{environment.FOLDER}" not in targets:
			continue
		if environment.windows:
			system.warn(f"{environment.FOLDER} runs on Windows: it is not exported (export it from its IIS server)")
			continue
		if environment.LOCAL_PATH:
			found.append((environment.FOLDER, "local", environment.FOLDER, running_folder(system, environment.FOLDER, environment.LOCAL_PATH), environment, target))
		else:
			found.append((environment.FOLDER, "remote", environment.FOLDER, f"/var/www/{environment.FOLDER}", environment, target))
	if not found:
		raise Stop("Choose what to export.")
	return site, found


def running_folder(system, unit, fallback):
	"""The folder a site's service runs in (its publish folder), else its own"""
	service = system.path(f"/etc/systemd/system/{service_name(unit)}.service")
	if service.is_file():
		match = re.search(r"(?m)^WorkingDirectory=(.+)$", service.read_text())
		if match:
			return os.path.normpath(match[1].strip().strip('"'))
	return os.path.normpath(fallback)


# ---- Harbor taken out
EXCLUDED = re.compile(r'[ \t]*<!--\s*Exclude[^>]*?build[^>]*?-->[ \t]*\r?\n[ \t]*<ItemGroup>(?:\s*<(?:Content|Compile|None|EmbeddedResource)\s+Remove="[^"]*"\s*/>)*\s*</ItemGroup>[ \t]*\r?\n?')


FOLDERS_MARK = "<!-- The environments' folders, the databases and the scripts are not part of the build -->"
FOLDERS_EXCLUDED = re.compile(r"[ \t]*" + re.escape(FOLDERS_MARK) + r"\s*<PropertyGroup>\s*<DefaultItemExcludes>[^<]*</DefaultItemExcludes>\s*</PropertyGroup>[ \t]*\r?\n?")


def strip_project(folder, folders=()):
	"""The project without VNS Harbor: its package reference, Harbor's NuGet source and Harbor's files. Each of the
	folders (its environments', the databases', the scripts') is left out of its build, a block each as Harbor writes
	them, in place of those Harbor wrote"""
	for csproj in Path(folder).glob("*.csproj"):
		text = csproj.read_text(encoding="utf-8-sig", errors="replace")
		changed = re.sub(r'[ \t]*<PackageReference\s+Include="VNS\.Harbor"[^>]*?(?:/>|>.*?</PackageReference>)[ \t]*\r?\n?', "", text, flags=re.S | re.I)
		changed = EXCLUDED.sub("", changed)
		changed = FOLDERS_EXCLUDED.sub("", changed)
		# Only English and Danish of the packages' languages, as Harbor builds it (its publish.targets): the customer's
		# own builds too, with the publish script. One the project names itself stays
		if "<SatelliteResourceLanguages" not in changed:
			changed = re.sub(r"(<PropertyGroup>[ \t]*\r?\n)", lambda match: match[1] + "    <SatelliteResourceLanguages>en;da</SatelliteResourceLanguages>\n", changed, count=1)
		# One left empty by an earlier export or by hand
		changed = re.sub(r"[ \t]*<ItemGroup>\s*</ItemGroup>[ \t]*\r?\n?", "", changed)
		if folders:
			# Never picked up by the build at all (MSBuild's DefaultItemExcludes, as Umbraco leaves out its umbraco\Data):
			# an environment that has run holds Umbraco's generated models (umbraco\Data\TEMP\InMemoryAuto), which a
			# Windows build otherwise compiled with the project's own. And a block each, as Harbor writes them
			excludes = ";".join(f"{name}\\**" for name in folders)
			blocks = (f"  {FOLDERS_MARK}\n  <PropertyGroup>\n    <DefaultItemExcludes>$(DefaultItemExcludes);{excludes}</DefaultItemExcludes>\n"
			          "  </PropertyGroup>\n")
			blocks += "".join(f'  <!-- Exclude {name} from build -->\n  <ItemGroup>\n    <Content Remove="{name}/**" />\n'
			                 f'    <Compile Remove="{name}/**" />\n    <None Remove="{name}/**" />\n  </ItemGroup>\n' for name in folders)
			changed = re.sub(r"\s*</Project>\s*\Z", lambda match: "\n" + blocks + "</Project>\n", changed, count=1)
		if changed != text:
			csproj.write_text(changed, encoding="utf-8")
	for config in Path(folder).glob("[Nn]u[Gg]et.config"):
		text = config.read_text(encoding="utf-8-sig", errors="replace")
		changed = re.sub(r"[ \t]*<!--[^>]*VNS\.Harbor[^>]*-->[ \t]*\r?\n?", "", text)
		changed = re.sub(r'[ \t]*<add\s+key="vns-local"[^>]*/>[ \t]*\r?\n?', "", changed)
		# Left with nuget.org alone (Harbor's local feed was put there while VNS.Harbor was tested), it says what dotnet
		# does anyway: it goes. One with sources of the customer's own stays
		sources = re.findall(r'<add\s+key="[^"]*"\s+value="([^"]*)"', changed)
		others = re.sub(r"(?s)<packageSources>.*?</packageSources>|<\?xml[^>]*\?>|<!--.*?-->|</?configuration>|\s", "", changed)
		if all("api.nuget.org" in source for source in sources) and not others:
			config.unlink()
		elif changed != text:
			config.write_text(changed, encoding="utf-8")
	strip_folder(folder)


def strip_folder(folder):
	"""Harbor's own files out of a site's folder, and what is no customer's: logs and caches"""
	root = Path(folder)
	for name in (*HARBOR_FILES, *LEFT_OUT):
		path = root / name
		if path.is_dir() and not path.is_symlink():
			shutil.rmtree(path)
		elif os.path.lexists(path):
			path.unlink()
	for path in (root / "wwwroot/App_Plugins/VNS.Harbor", root / "App_Plugins/VNS.Harbor"):
		if path.is_dir():
			shutil.rmtree(path)


SERVER_FIELD = re.compile(r'(?i)((?:^|;)\s*(?:server|data source|datasource|host|address|addr|network address)\s*=)\s*("[^"]*"|[^;]*)')
PASSWORD_FIELD = re.compile(r'(?i)((?:^|;)\s*(?:password|pwd)\s*=)\s*("[^"]*"|[^;]*)')


def clean_connection(value):
	"""A connection string to a server without the server and password: the customer's own go there"""
	return PASSWORD_FIELD.sub(r"\1", SERVER_FIELD.sub(r"\1", value))


# Of Umbraco's Smtp, what is ours, given placeholders the customer replaces: Umbraco checks From (and Notifications' Email)
# is an address as it starts, so an empty one stops the site. SecureSocketOptions, Port and DeliveryMethod stay
MAIL_FIELDS = {"From": "noreply@example.com", "Host": "smtp.example.com", "Name": "Your company", "Username": "", "Password": ""}
NOTIFICATION_FIELDS = {"Email": "noreply@example.com"}


def blank_values(text, block, keys):
	"""In each object named block in a settings file's text, the string values of keys (a dict: key to its new value)
	set, the rest of the text (its comments too) as it was"""
	result, index = [], 0
	for found in re.finditer(r'"' + re.escape(block) + r'"\s*:\s*\{', text):
		if found.start() < index:
			continue
		start = found.end()
		# The brace that closes it, outside strings
		depth, end, inside = 1, start, False
		while end < len(text) and depth:
			char = text[end]
			if inside:
				if char == "\\":
					end += 1
				elif char == '"':
					inside = False
			elif char == '"':
				inside = True
			elif char in "{[":
				depth += 1
			elif char in "}]":
				depth -= 1
			end += 1
		body = text[start:end]
		for key, value in keys.items():
			body = re.sub(r'("' + re.escape(key) + r'"\s*:\s*)"(?:[^"\\]|\\.)*"', lambda match: match[1] + json.dumps(value), body)
		result.append(text[index:start] + body)
		index = end
	return "".join(result) + text[index:] if result else text


def plain_http(text):
	"""A settings file's text with Umbraco's UseHttps false and its UmbracoApplicationUrl on http, the rest as it was"""
	text = re.sub(r'("UseHttps"\s*:\s*)true\b', r"\1false", text)
	return re.sub(r'("UmbracoApplicationUrl"\s*:\s*")https://', r"\1http://", text)


def clean_settings(folder):
	"""An environment's appsettings*.json without what is ours: the database server and password in its connection
	strings (a SQLite file's stays as it is), and the mail server and sender (Umbraco's Smtp and Notifications), given
	placeholders (example.com) the site starts with, for the customer to replace with theirs. And plain http (Umbraco's
	UseHttps off, its UmbracoApplicationUrl http): the customer's server has no certificate for it until they give it one"""
	from harbor.sites import convert
	info = convert.info()
	for path in sorted(Path(folder).glob("appsettings*.json")):
		if "schema" in path.name or not path.is_file():
			continue
		raw = path.read_bytes()
		text = info.read_config_text(path)
		try:
			data = info.configuration_json(text)
		except ValueError:
			continue
		changed = text
		connections = data.get("ConnectionStrings") if isinstance(data.get("ConnectionStrings"), dict) else {}
		for key, value in connections.items():
			if not isinstance(value, str) or key.lower().endswith("_providername") or not value.strip():
				continue
			if info.connection_type(value, str(connections.get(key + "_ProviderName", "")).lower()) == "SQLite":
				continue
			cleaned = clean_connection(value)
			if cleaned != value:
				changed = convert.change_text(changed, key, cleaned, None) or changed
		# The mail server and our sender, given placeholders where they are: the customer fills in their own
		changed = blank_values(changed, "Smtp", MAIL_FIELDS)
		changed = blank_values(changed, "Notifications", NOTIFICATION_FIELDS)
		changed = plain_http(changed)
		if changed != text:
			convert.write_like(path, changed, raw)


# ---- the parts
def copy_source(system, site, target_folders, destination):
	"""The project's files (no builds, environments, Harbor's or version control's own) into destination"""
	root = system.path(site.site_root)
	inside = {os.path.realpath(folder) for folder in target_folders}
	def ignore(directory, names):
		left = set()
		for name in names:
			path = os.path.realpath(os.path.join(directory, name))
			if directory == str(root) and (name in SOURCE_LEFT_OUT or name.startswith("_")) or path in inside or os.path.islink(os.path.join(directory, name)):
				left.add(name)
		return left
	shutil.copytree(root, destination, ignore=ignore, symlinks=False)
	strip_project(destination)


def build(system, source, work, os_name, configuration="Release"):
	"""A fresh build of the project for the OS chosen, framework-dependent, as the build user (never root), Debug or
	Release as the environment it is for is built (Development and Test Debug, Staging and Production Release)"""
	projects = sorted(Path(source).glob("*.csproj"))
	if len(projects) != 1:
		return None
	project = work / "project"
	if not project.exists():
		shutil.copytree(source, project, symlinks=False)
	output = work / f"build-{configuration}"
	output.mkdir()
	# For IIS its web.config is made, though a project that runs on Linux turns that off
	windows = ["-p:IsTransformWebConfigDisabled=false"] if os_name == "windows" else []
	system.run("python3", str(LIB / "build_as.py"), "--", "dotnet", "publish", projects[0].name, "-c", configuration, f"-p:PublishDir={output}",
	           *windows, "--runtime", OSES[os_name], "--self-contained", "false", "--nologo", cwd=str(project))
	if not (output / f"{projects[0].stem}.dll").is_file():
		raise Stop(f"The build for {os_name.capitalize()} failed: {projects[0].stem}.dll is not in its output.")
	if os_name == "windows" and not (output / "web.config").is_file():
		(output / "web.config").write_text(default_web_config(projects[0].stem), encoding="utf-8")
	strip_folder(output)
	# The project's NuGet settings have nothing to do in a site that runs
	for config in output.glob("[Nn]u[Gg]et.config"):
		config.unlink()
	return output


def own_files(live, destination):
	"""An environment's own over its build: appsettings*.json, Umbraco's data, uSync, Views, App_Data, and wwwroot but
	the packages' App_Plugins (the build has those)"""
	live = Path(live)
	for path in live.glob("appsettings*.json"):
		if "schema" not in path.name:
			shutil.copy2(path, destination / path.name)
	for name in OWN:
		if (live / name).is_dir():
			shutil.copytree(live / name, destination / name, symlinks=False, dirs_exist_ok=True)
	if (live / "wwwroot").is_dir():
		for entry in (live / "wwwroot").iterdir():
			if entry.name == "App_Plugins" or entry.is_symlink():
				continue
			target = destination / "wwwroot" / entry.name
			if entry.is_dir():
				shutil.copytree(entry, target, symlinks=False, dirs_exist_ok=True)
			else:
				target.parent.mkdir(parents=True, exist_ok=True)
				shutil.copy2(entry, target)


def fetch_remote(system, environment, destination):
	"""An environment on another server, its whole folder copied here (rsync over its SSH)"""
	from harbor.environments import settings as env_settings
	argv, env = env_settings.ssh_command(environment)
	sudo = "" if environment.USER == "root" else "sudo -n "
	destination.mkdir(parents=True, exist_ok=True)
	result = system.run("rsync", "-a", "--exclude", "/umbraco/Logs/", "--exclude", "/umbraco/Data/TEMP/", "-e", " ".join(shlex.quote(part) for part in argv),
	                    f"--rsync-path={sudo}rsync", f"{environment.USER}@{environment.IP}:/var/www/{environment.FOLDER}/", f"{destination}/",
	                    env=env, check=False)
	if not result.ok:
		raise Stop(f"{environment.FOLDER} could not be copied from {environment.IP}.")


def backup_database(system, settings, database, destination):
	"""A copy-only backup of a database (Microsoft SQL Server) or a dump (PostgreSQL) into destination, here"""
	from harbor.sites import convert
	if settings.get("engine") == "postgres":
		pg = convert.converter().pg_admin()
		pg.run([pg.pg_dump(), "-Fc", "-d", database, "-f", str(destination)], settings)
		return
	sql = convert.converter().sql_admin()
	if not sql.backup_file_access(settings):
		raise Stop(f"No access to the backup folder of {settings.get('host')}: turn on SSH for backup files in its connection")
	path = sql.copy_backup(settings, database)
	try:
		pipe = sql.backup_file_pipe(settings, path, write=False)
		if pipe is None:
			shutil.copyfile(path, destination)
		else:
			with open(destination, "wb") as stream:
				result = subprocess.run(pipe[0], stdout=stream, stderr=subprocess.PIPE, env=pipe[1])
			if result.returncode:
				raise Stop("The backup could not be read from its server: " + result.stderr.decode(errors="replace").strip()[-300:])
	finally:
		sql.forget_copy_backup(settings, path)


# ---- the publish script that comes with it
SH = r'''#!/usr/bin/env bash
# Builds @PROJECT@ and publishes it to one of its environments' folders. What is the environment's own stays as it is:
# appsettings*.json, umbraco/, uSync/, Views/, App_Data/ and wwwroot/ (only its App_Plugins, the packages', is new).
# Run it from anywhere: bash scripts/publish.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# Each folder, the build it gets (Development and Test Debug, Staging and Production Release) and the environment it runs as
environments=(@FOLDERS@)
configurations=(@CONFIGURATIONS@)
names=(@NAMES@)
echo "Publish @PROJECT@ to which environment?"
select folder in "${environments[@]}"; do
	[ -n "${folder:-}" ] && break
done
index=$((REPLY - 1))
build="$(mktemp -d)"
# Afterwards, whatever happened: the build and what it leaves in the project (bin and the JSON schema files), as Harbor
# cleans up after its own builds. obj stays: the packages' restore and the build's state, which the next build and an
# editor use
trap 'rm -rf "$build" bin appsettings-schema*.json umbraco-package-schema.json' EXIT
dotnet publish "@PROJECT@.csproj" -c "${configurations[$index]}" -o "$build" --runtime linux-x64 --self-contained false
mkdir -p "$folder"
shopt -s dotglob nullglob
for item in "$build"/*; do
	name="$(basename "$item")"
	case "$name" in
		appsettings*.json|umbraco|uSync|Views|App_Data|app_data)
			[ -e "$folder/$name" ] && continue ;;
		wwwroot)
			if [ -d "$folder/wwwroot" ]; then
				if [ -d "$item/App_Plugins" ]; then
					rm -rf "$folder/wwwroot/App_Plugins"
					cp -a "$item/App_Plugins" "$folder/wwwroot/"
				fi
				continue
			fi ;;
	esac
	rm -rf "${folder:?}/$name"
	cp -a "$item" "$folder/"
done
echo "Published to $folder (${configurations[$index]}). Its service runs it as ${names[$index]}: ASPNETCORE_ENVIRONMENT=${names[$index]}. Restart it to run the new build."
'''

PS1 = r'''# Builds @PROJECT@ and publishes it to one of its environments' folders. What is the environment's own stays as it is:
# appsettings*.json, umbraco\, uSync\, Views\, App_Data\ and wwwroot\ (only its App_Plugins, the packages', is new).
# IIS lets go of the site while it is published (app_offline.htm). Run it from anywhere: powershell -File scripts\publish.ps1
@PAUSE@Set-Location (Join-Path $PSScriptRoot "..")
# Each folder, the build it gets (Development and Test Debug, Staging and Production Release) and the environment it runs as
$environments = @(@ENTRIES@)
for ($index = 0; $index -lt $environments.Count; $index++) { Write-Host "$($index + 1)) $($environments[$index].Folder) ($($environments[$index].Name))" }
$choice = Read-Host "Publish @PROJECT@ to which environment"
$chosen = $environments[[int]$choice - 1]
if (-not $chosen) { throw "Choose one of the numbers shown" }
$folder = $chosen.Folder
$build = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
dotnet publish "@PROJECT@.csproj" -c $chosen.Configuration -o $build --runtime win-x64 --self-contained false
if ($LASTEXITCODE -ne 0) { throw "The build failed" }
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$offline = Join-Path (Resolve-Path $folder) "app_offline.htm"
# The page visitors see meanwhile
[IO.File]::WriteAllBytes($offline, [Convert]::FromBase64String("@OFFLINE@"))
try {
	# IIS lets go of the site's files once it sees app_offline.htm
	Start-Sleep -Seconds 5
	# The build merged into the folder file by file (robocopy, retried while a file is still held), never a folder put
	# inside one of the same name. What is the environment's own is left out once it has it
	$leftOut = @()
	foreach ($name in @("umbraco", "uSync", "Views", "App_Data", "app_data", "wwwroot")) {
		if (Test-Path (Join-Path $folder $name)) { $leftOut += (Join-Path $build $name) }
	}
	$options = @("/E", "/R:10", "/W:3", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
	if ($leftOut.Count) { $options += "/XD"; $options += $leftOut }
	if (Test-Path (Join-Path $folder "appsettings.json")) { $options += "/XF"; $options += "appsettings*.json" }
	robocopy $build $folder @options | Out-Host
	if ($LASTEXITCODE -ge 8) { throw "Copying the build failed (robocopy $LASTEXITCODE)" }
	# Its wwwroot stays its own, but the packages' code in it (App_Plugins) is the build's, as it is
	$plugins = Join-Path $build "wwwroot\App_Plugins"
	if ((Test-Path (Join-Path $folder "wwwroot")) -and (Test-Path $plugins)) {
		robocopy $plugins (Join-Path $folder "wwwroot\App_Plugins") /MIR /R:10 /W:3 /NFL /NDL /NJH /NJS /NP | Out-Host
		if ($LASTEXITCODE -ge 8) { throw "Copying App_Plugins failed (robocopy $LASTEXITCODE)" }
	}
	$global:LASTEXITCODE = 0
	# The environment it runs as, in its web.config (the build's has none)
	$config = Join-Path $folder "web.config"
	if (Test-Path $config) {
		$xml = New-Object System.Xml.XmlDocument
		$xml.PreserveWhitespace = $true
		$xml.Load((Resolve-Path $config))
		$module = $xml.SelectSingleNode("//aspNetCore")
		if ($module) {
			$variables = $module.SelectSingleNode("environmentVariables")
			if (-not $variables) { $variables = $xml.CreateElement("environmentVariables"); [void]$module.AppendChild($variables) }
			foreach ($name in @("ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT")) {
				$variable = $variables.SelectSingleNode("environmentVariable[@name='$name']")
				if (-not $variable) { $variable = $xml.CreateElement("environmentVariable"); $variable.SetAttribute("name", $name); [void]$variables.AppendChild($variable) }
				$variable.SetAttribute("value", $chosen.Name)
			}
			$xml.Save((Resolve-Path $config))
		}
	}
} finally {
	Remove-Item -Force $offline -ErrorAction SilentlyContinue
	Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
	# What the build leaves in the project (bin and the JSON schema files), as Harbor cleans up after its own builds. obj
	# stays: the packages' restore and the build's state, which the next build and an editor use
	foreach ($name in @("bin", "umbraco-package-schema.json")) { Remove-Item -Recurse -Force $name -ErrorAction SilentlyContinue }
	Get-ChildItem -File -Filter "appsettings-schema*.json" | Remove-Item -Force -ErrorAction SilentlyContinue
}
Write-Host "Published to $folder ($($chosen.Configuration), $($chosen.Name))."
Done
'''


def publish_script(os_name, project, environments):
	"""scripts/publish.sh (Linux) or publish.ps1 (Windows, with CRLF as Windows has it). environments: [(folder,
	environment name)]: each gets the build its environment has (Debug or Release)"""
	if os_name == "windows":
		entries = ", ".join(f'@{{ Folder = "{folder}"; Configuration = "{configuration_of(name)}"; Name = "{name}" }}' for folder, name in environments)
		from harbor.sites.export_scripts import PS1_PAUSE, app_offline
		# The page IIS shows meanwhile, in the script itself (base64) rather than a file of its own beside it
		page = base64.b64encode(app_offline().encode("utf-8")).decode("ascii")
		text = PS1.replace("@PAUSE@", PS1_PAUSE).replace("@PROJECT@", project).replace("@ENTRIES@", entries).replace("@OFFLINE@", page)
		return "publish.ps1", text.replace("\n", "\r\n").encode("utf-8")
	text = (SH.replace("@PROJECT@", project).replace("@FOLDERS@", " ".join(shlex.quote(folder) for folder, _name in environments))
	        .replace("@CONFIGURATIONS@", " ".join(configuration_of(name) for _folder, name in environments))
	        .replace("@NAMES@", " ".join(shlex.quote(name) for _folder, name in environments)))
	return "publish.sh", text.encode("utf-8")


def environment_name(system, part):
	"""The environment an exported environment runs as (ASPNETCORE_ENVIRONMENT): its setting, else its service's, else
	Production for the main site and its own name for another"""
	label, kind, unit, live, environment, target = part
	named = environment.other.get("ENVIRONMENT", "") if environment else ""
	if not named and kind == "local":
		service = system.path(f"/etc/systemd/system/{service_name(unit)}.service")
		text = service.read_text() if service.is_file() else ""
		match = re.search(r"(?m)^Environment=(?:ASPNETCORE|DOTNET)_ENVIRONMENT=(\S+)", text)
		named = match[1] if match else ""
	return named or ("Production" if target in ("main", "prod") else target.capitalize())


def configuration_of(name):
	"""Development and Test are Debug builds, Staging and Production Release, as Harbor builds them"""
	from harbor.sites.tools import configuration_for
	return configuration_for(name) or "Release"


WEB_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <location path="." inheritInChildApplications="false">
    <system.webServer>
      <handlers>
        <add name="aspNetCore" path="*" verb="*" modules="AspNetCoreModuleV2" resourceType="Unspecified" />
      </handlers>
      <aspNetCore processPath="dotnet" arguments=".\\@DLL@.dll" stdoutLogEnabled="false" stdoutLogFile=".\\logs\\stdout" hostingModel="inprocess" />
    </system.webServer>
  </location>
</configuration>
"""


def default_web_config(dll):
	"""The web.config dotnet publish makes for IIS (the ASP.NET Core Module, in process), for a build that has none"""
	return WEB_CONFIG.replace("@DLL@", dll).replace("\n", "\r\n")


WEB_CONFIG_OFF = re.compile(r"[ \t]*<IsTransformWebConfigDisabled>\s*true\s*</IsTransformWebConfigDisabled>[ \t]*\r?\n?", re.I)


def web_config_on(folder):
	"""A project exported for Windows makes its web.config again as it is published (one that runs on Linux has it
	turned off): the customer's publish.ps1 needs it for IIS"""
	for csproj in Path(folder).glob("*.csproj"):
		raw = csproj.read_bytes()
		text = raw.decode("utf-8-sig")
		changed = WEB_CONFIG_OFF.sub("", text)
		if changed != text:
			# Its byte order mark kept, as the file had it
			csproj.write_bytes((b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + changed.encode("utf-8"))


# Already compressed (media, fonts, archives): stored as they are, as deflate makes them no smaller, only slower
STORED = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".ico", ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".ogg",
          ".woff", ".woff2", ".zip", ".gz", ".tgz", ".7z", ".rar", ".br", ".nupkg", ".pdf", ".docx", ".xlsx", ".pptx"}


def make_zip(tree, work, archive):
	"""The export's zip: deflate at its fastest level (about three times as fast as the default, a little larger) and what
	is compressed already stored, each file with its mode (the publish script can be run as it is)"""
	with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zipped:
		for path in sorted(tree.rglob("*")):
			if path.is_file() and not path.is_symlink():
				stored = path.suffix.lower() in STORED
				zipped.write(path, path.relative_to(work).as_posix(), compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
				             compresslevel=None if stored else 1)


def set_web_config(path, name):
	"""The environment in a Windows build's web.config (ASPNETCORE_ENVIRONMENT and DOTNET_ENVIRONMENT), as Harbor sets it"""
	from harbor.sites import convert
	convert._lib("windows_prod.py", "vns_export_windows").set_environment(path, name)


def folder_names(system, site, found):
	"""Each environment's folder in the export, as the site has it here (_publish, _test, ...); one from another server,
	or one that is not in the site's folder, _<environment> (and _<environment>_external when that is taken)"""
	root = os.path.realpath(system.path(site.site_root))
	named, used = [], set()
	for part in found:
		label, kind, unit, live, environment, target = part
		folder = None
		if kind == "local":
			real = os.path.realpath(system.path(live))
			relative = os.path.relpath(real, root) if real.startswith(root + os.sep) else ""
			if relative and os.sep not in relative and relative not in used:
				folder = relative
		if not folder:
			name = (environment.other.get("ENVIRONMENT", "") if environment else "") or ("publish" if target == "main" else target)
			base = "_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "site")
			folder, number = base, 2
			if folder in used or kind == "remote" and system.path(f"{site.site_root}/{folder}").exists():
				folder = f"{base}_external"
			while folder in used:
				folder, number = f"{base}_external{number}", number + 1
		used.add(folder)
		named.append((part, folder))
	return named


# ---- the export
# ---- an environment managed from another server: exported by the Harbor there, which has its project
def managed_note(system, name):
	"""(main site, its server's name) when this site is an environment of a site on another server, else None"""
	note = system.path(f"/var/lib/vns-managed/{name}")
	if not note.is_file():
		return None
	lines = [line.strip() for line in note.read_text(errors="replace").splitlines()]
	return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")


def manager_server(system, host):
	"""The server in Servers that a managed site's note names (by its full name, address or short name): (name, entry)"""
	from harbor.server import servers
	host = (host or "").strip().lower()
	for server_name, entry in servers.servers(system, "linux").items():
		if host and host in (str(entry.get("fqdn") or "").lower(), str(entry.get("host") or "").lower(), server_name.lower(),
		                     *[str(item).lower() for item in entry.get("names") or []]) or host.split(".")[0] == server_name.lower():
			return server_name, entry
	return None


REMOTE_HARBOR = 'h=/usr/local/bin/harbor; [ -x "$h" ] || h=/opt/harbor/current/bin/harbor\n'


def export_managed(system, name, note, os_name, by="", keep_hours=72):
	"""An environment of a site on another server, exported by the Harbor there: it builds the project for the OS chosen
	and takes this environment's own files from here, as it does for its environments. The export is kept there, under
	its site, and listed here too"""
	main, host = note
	found = manager_server(system, host)
	if not found:
		raise Stop(f"{name} is an environment of {main or 'a site'} on {host or 'another server'}, which has its project: add that server "
		           "under Servers to export it from here.")
	server_name, entry = found
	if os.environ.pop("VNS_EXPORT_DATABASES", ""):
		system.warn(f"Its database on a database server is not in the export: export it from {server_name}'s Export Site to take it along.")
	from harbor.server import servers
	command = ["site", "export", main, "--os", os_name, "--targets", f"unit:{name}", "--by", by or "", "--keep", str(int(keep_hours or 0))]
	system.plan([f"Ask {server_name} to export {name}, where {main} is"])
	system.step(f"Ask {server_name} to export {name}, where {main} is")
	# Its steps there (copy, build, export, zip) come here as it takes them, in the admin's window too
	progress = os.environ.get("HARBOR_PROGRESS", "")
	script = (REMOTE_HARBOR + (f"export HARBOR_PROGRESS={shlex.quote(progress)}\n" if progress else "unset HARBOR_PROGRESS\n")
	          + 'exec "$h" ' + " ".join(shlex.quote(part) for part in command) + "\n")
	result = servers.remote(system, entry, "bash -c " + shlex.quote(script), check=False, capture=False)
	if not result.ok:
		raise Stop(f"The export on {server_name} failed.", result.returncode or 1)
	system.say(f"The export is kept on {server_name}, under {main}, and listed here under {name}.")


def admin_site(system):
	"""The admin site's name here (its data folder in /var/www): its address is the download link's"""
	for path in sorted(system.path("/etc/systemd/system").glob("*.service")):
		match = re.search(r"(?m)^(?:WorkingDirectory|Environment=HARBOR_ADMIN_DATA)=/var/www/([^/\n]+)/app$", path.read_text(errors="replace"))
		if match and system.path(f"/var/www/{match[1]}/app/config.json").is_file():
			return match[1]
	return ""


def export_list(system, units):
	"""The exports here of the environments named (for their own server's admin): JSON, with each one's link"""
	admin = admin_site(system)
	wanted = set(units)
	found = []
	for item in exports(system):
		if item.get("state") == "snapshot" or not wanted & set(item.get("environments") or []):
			continue
		keep = item.get("keep", KEEP_AFTER_DOWNLOAD)
		found.append({key: item.get(key) for key in ("id", "site", "os", "at", "by", "environments", "size", "state", "downloaded", "file")}
		             | {"keep": keep, "expires": item["downloaded"] + keep if item.get("downloaded") and keep else None,
		                "link": f"https://{admin}/download/{item['token']}" if admin and item.get("token") and item.get("state") == "ready" else ""})
	print(json.dumps({"exports": found}))


def export_site(system, name, os_name, targets, delete=False, by="", keep_hours=72):
	"""harbor site export: the zip made (and the site deleted from Harbor when asked, a snapshot kept for Restore)"""
	from harbor.sites import convert
	if os_name not in (*OSES, ANY):
		raise Stop("Export for windows, linux or any.")
	# An environment of a site on another server: the Harbor there exports it (it has the project to build)
	note = managed_note(system, name)
	if note:
		if delete:
			raise Stop(f"{name} is an environment of a site on {note[1] or 'another server'}: delete it from there.")
		return export_managed(system, name, note, os_name, by, keep_hours)
	try:
		databases = json.loads(os.environ.pop("VNS_EXPORT_DATABASES", "") or "{}")
	except ValueError:
		databases = {}
	site, found = units(system, name, targets)
	identifier = secrets.token_hex(8)
	folder = folder_of(system, identifier)
	folder.mkdir(parents=True, mode=0o711)
	work = folder / "work"
	work.mkdir(mode=0o711)
	tree = work / name
	built_for = os_name != ANY
	has_project = built_for and bool(list(system.path(site.site_root).glob("*.csproj")))
	named = folder_names(system, site, found)
	names = {part[0]: environment_name(system, part) for part in found}
	configurations = sorted({configuration_of(names[part[0]]) for part in found}) if has_project else []
	steps = [*(["Copy the project without VNS Harbor", *[f"Build for {os_name.capitalize()} ({configuration})" for configuration in configurations]]
	           if has_project else ["Copy the site"]),
	         *[f"Export {part[0]} as {place}" for part, place in named], *(["Back up the databases"] if databases else []), "Make the zip",
	         *(["Keep a snapshot for Restore", f"Delete {name} from Harbor"] if delete else [])]
	system.plan(steps)
	# How long it lives once its link was first used: hours, or none (never deleted by itself)
	item = {"id": identifier, "site": name, "os": os_name, "at": int(time.time()), "by": by, "environments": [part[0] for part in found],
	        "token": secrets.token_urlsafe(32), "state": "making", "deleted": False, "keep": int(keep_hours) * 3600 if keep_hours else None}
	save(system, item)
	try:
		local_folders = [part[3] for part in found if part[1] == "local"]
		built = None
		project = ""
		if has_project:
			system.step(steps[0])
			copy_source(system, site, local_folders, tree)
			project = next(tree.glob("*.csproj")).stem
			strip_project(tree, [place for _part, place in named] + ["databases", "scripts"])
			if os_name == "windows":
				web_config_on(tree)
			clean_settings(tree)
			built = {}
			for configuration in configurations:
				system.step(f"Build for {os_name.capitalize()} ({configuration})")
				built[configuration] = build(system, tree, work, os_name, configuration)
		else:
			system.step(steps[0])
			# What runs is exported as it is: the site's folder (an HTML or Python site's environments here are in it)
			chosen = {os.path.realpath(system.path(part[3])) for part in found if part[1] == "local"}
			root = os.path.realpath(system.path(site.site_root))
			def ignore(directory, names):
				return {entry for entry in names if entry in MADE_AGAIN or os.path.islink(os.path.join(directory, entry))
				        or directory == root and entry.startswith("_") and os.path.realpath(os.path.join(directory, entry)) not in chosen}
			if root in chosen or os_name == ANY:
				shutil.copytree(root, tree, symlinks=False, ignore=ignore)
			else:
				tree.mkdir(parents=True)
			strip_folder(tree)
			if os_name == "windows":
				system.warn("There is no project here (published from elsewhere): it is not rebuilt for Windows, its files are as they run on Linux")
		for part, place in named:
			label, kind, unit, live, environment, _target = part
			system.step(f"Export {label} as {place}")
			destination = tree / place
			if kind == "remote":
				live = work / "remote" / label
				fetch_remote(system, environment, live)
			if built:
				shutil.copytree(built[configuration_of(names[label])], destination, symlinks=False)
				own_files(system.path(live), destination)
				# On Windows the environment it runs as is in its web.config, as Harbor sets it there
				if os_name == "windows" and (destination / "web.config").is_file():
					set_web_config(destination / "web.config", names[label])
			elif not destination.exists() and os.path.realpath(system.path(live)) != os.path.realpath(system.path(site.site_root)):
				shutil.copytree(system.path(live), destination, symlinks=False, ignore=shutil.ignore_patterns(*MADE_AGAIN))
			if destination.exists():
				strip_folder(destination)
				clean_settings(destination)
		if databases:
			system.step("Back up the databases")
			(tree / "databases").mkdir(exist_ok=True)
			places = {part[0]: place for part, place in named}
			for label, entry in databases.items():
				if label not in places or not isinstance(entry, dict) or not isinstance(entry.get("connection"), dict):
					continue
				suffix = ".dump" if entry["connection"].get("engine") == "postgres" else ".bak"
				system.say(f"Back up {entry.get('database')} of {label}")
				try:
					backup_database(system, entry["connection"], entry["database"], tree / "databases" / f"{places[label].lstrip('_')}{suffix}")
				except (Stop, RuntimeError, ValueError, OSError) as error:
					system.warn(f"{entry.get('database')} of {label} is not in the export: {error}")
		if built and project:
			# publish, and clear-logs, sync-settings, sync-data, check-packages, add-package and remove-package with it
			from harbor.sites.export_scripts import scripts as more_scripts
			environments = [(place, names[part[0]]) for part, place in named]
			(tree / "scripts").mkdir(exist_ok=True)
			for script, content in [publish_script(os_name, project, environments), *more_scripts(os_name, project, environments)]:
				(tree / "scripts" / script).write_bytes(content)
				os.chmod(tree / "scripts" / script, 0o755 if script.endswith((".sh", ".ps1")) else 0o644)
		# What the customer got, in Danish and English
		from harbor.sites.export_scripts import readme
		framework = ""
		for csproj in tree.glob("*.csproj"):
			match = re.search(r"<TargetFramework>([^<]+)</TargetFramework>", csproj.read_text(errors="replace"))
			framework = match[1].strip() if match else ""
		(tree / "README.txt").write_bytes(readme(name, os_name, [(place, names[part[0]]) for part, place in named],
		                                         sorted(path.name for path in (tree / "databases").glob("*")) if (tree / "databases").is_dir() else [],
		                                         framework, built=bool(built)))
		system.step("Make the zip")
		archive = folder / (f"{name}.zip" if os_name == ANY else f"{name}-{os_name}.zip")
		make_zip(tree, work, archive)
		os.chmod(archive, 0o600)
		item.update(file=archive.name, size=archive.stat().st_size, state="ready", folders={part[0]: place for part, place in named})
		save(system, item)
	except BaseException:
		shutil.rmtree(folder, ignore_errors=True)
		raise
	finally:
		shutil.rmtree(work, ignore_errors=True)
	if delete:
		system.step("Keep a snapshot for Restore")
		# Everything the deletion takes, whatever went in the zip: every environment, here and on the other servers, and
		# the databases chosen to go with it (backed up first, with their logins: Restore makes them again)
		from harbor.environments import settings
		_site, everything = units(system, name, ["main", *settings.target_ids(system, site.site_root)])
		keep_databases(system, identifier, everything)
		item["snapshot"] = snapshot(system, identifier, site, everything)
		save(system, item)
		system.step(f"Delete {name} from Harbor")
		from harbor.sites.delete import delete_site
		delete_site(system, name, delete_certificates="n", scope="all")
		item["deleted"] = True
		save(system, item)
	system.say(f"The export of {name} is ready: {archive.name}, {item['size'] // (1024 * 1024)} MB")
	convert.forget_gone(system)


# ---- deleted with it, and put back by Restore
def local_paths(system, unit, folder):
	"""What a site here is on this server: its folder, service, Apache config (and its enabled links) and Harbor's own"""
	paths = [folder, f"/etc/systemd/system/{service_name(unit)}.service", f"/var/lib/vns-managed/{unit}", f"/var/lib/vns-roots/{unit}"]
	for directory in ("/etc/apache2/sites-available", "/etc/apache2/sites-enabled"):
		paths += [str(path) for path in system.path(directory).glob(f"{unit}*.conf")]
	return [path for path in paths if os.path.lexists(system.path(path))]


def snapshot(system, identifier, site, found):
	"""Everything the deletion takes, kept: here as one tar (absolute paths), on another server by its own Harbor"""
	from harbor.sites import convert
	paths = [site.site_root, f"/var/lib/harbor/sites/{site.name}"]
	for label, kind, unit, live, environment, _target in found:
		if kind == "local":
			paths += local_paths(system, unit, live)
	paths += local_paths(system, site.name, site.site_root)
	unique = sorted({path for path in paths if os.path.lexists(system.path(path))})
	archive = folder_of(system, identifier) / "snapshot.tar.gz"
	with tarfile.open(archive, "w:gz") as tar:
		for path in unique:
			tar.add(system.path(path), arcname=path.lstrip("/"))
	os.chmod(archive, 0o600)
	remote = []
	for label, kind, unit, live, environment, target in found:
		if kind != "remote":
			continue
		result = convert._remote_run(system, environment, ("site", "export-snapshot", identifier, unit), progress=False, capture=True)
		if not result.ok:
			raise Stop(f"{unit} on {environment.IP} could not be kept for Restore: nothing is deleted. {result.stderr.strip()[-300:]}")
		remote.append({"unit": unit, "target": target, "host": environment.IP})
	return {"local": unique, "remote": remote}


def keep_databases(system, identifier, everything):
	"""The databases Delete takes with the site (VNS_DELETE_DATABASES, read by the deletion after), each backed up into
	the export's folder with its login and password, for Restore. One that cannot be backed up stops it: nothing is
	deleted then"""
	from harbor.sites import convert
	try:
		dropping = json.loads(os.environ.get("VNS_DELETE_DATABASES", "") or "[]")
	except ValueError:
		dropping = []
	if not dropping:
		return
	folder = folder_of(system, identifier)
	kept = folder / "databases"
	kept.mkdir(mode=0o700, exist_ok=True)
	parts = {part[2]: part for part in everything}
	records = []
	for entry in dropping:
		database, settings = entry.get("database"), entry.get("connection")
		if not database or not isinstance(settings, dict):
			continue
		part = parts.get(entry.get("unit"))
		login = password = ""
		try:
			if part and part[1] == "remote":
				found = convert._remote_json(system, part[4], ("site", "convert-relay", "login", part[2]))
				login, password = found.get("login", ""), found.get("password", "")
			elif part:
				folder_here, _ = convert.working_folder(system, part[2])
				login, password = convert.login_of(convert.site_connection(system, folder_here)["value"])
		except Stop:
			system.warn(f"The login of {database} is not known: Restore puts the database back without it")
		suffix = ".dump" if settings.get("engine") == "postgres" else ".bak"
		system.say(f"Keep {database} for Restore")
		try:
			backup_database(system, settings, database, kept / f"{database}{suffix}")
		except (Stop, RuntimeError, ValueError, OSError) as error:
			raise Stop(f"{database} could not be backed up, so nothing is deleted: {error}") from None
		records.append({"engine": "postgres" if settings.get("engine") == "postgres" else "mssql", "database": database, "login": login,
		                "password": password, "connection": settings, "file": f"{database}{suffix}"})
	# Root's alone: the logins' passwords and the connections are in it
	write_json(folder / "databases.json", records, private=True)


def restore_databases(system, identifier):
	"""The databases deleted with the site made again from their backups, each with its login, as they were"""
	from harbor.sites import convert
	folder = folder_of(system, identifier)
	records = read_json(folder / "databases.json") or []
	for entry in records:
		settings, database, login, password = entry["connection"], entry["database"], entry.get("login", ""), entry.get("password", "")
		path = folder / "databases" / entry["file"]
		system.say(f"Put {database} back on {settings.get('host')}")
		try:
			if entry["engine"] == "postgres":
				pg = convert.converter().pg_admin()
				if pg.query_json(settings, f"SELECT 1 AS found FROM pg_database WHERE datname = {pg.literal(database)}"):
					system.say(f"{database} is there already: it is left as it is")
					continue
				if login and not pg.role_exists(settings, login):
					pg.create_login(settings, login, password)
				pg.create_database(settings, database, login or None)
				if login:
					pg.own_schema(settings, database, login)
				pg.restore(settings, database, path, login or None)
				continue
			sql = convert.converter().sql_admin()
			if sql.query_json(settings, "SELECT name FROM sys.databases WHERE name=" + sql.literal(database)):
				system.say(f"{database} is there already: it is left as it is")
				continue
			separator = "\\" if "\\" in settings["backup_directory"] else "/"
			carried = settings["backup_directory"].rstrip("/\\") + separator + f"{database}.restore-{secrets.token_hex(4)}.bak"
			pipe = sql.backup_file_pipe(settings, carried, write=True)
			sql.file_operation(settings, "prepare_write", {carried: []})
			try:
				if pipe is None:
					shutil.copyfile(path, carried)
				else:
					with open(path, "rb") as stream:
						result = subprocess.run(pipe[0], stdin=stream, stderr=subprocess.PIPE, env=pipe[1])
					if result.returncode:
						raise RuntimeError("The backup could not be carried to its server: " + result.stderr.decode(errors="replace").strip()[-300:])
				sql.restore(settings, database, carried, 1, False)
			finally:
				convert._forget_backup(sql, settings, carried)
			if login and password:
				if sql.query_json(settings, "SELECT name FROM sys.server_principals WHERE name=" + sql.literal(login)):
					sql.execute(settings, f"USE {sql.identifier(database)}; IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name={sql.literal(login)}) "
					                      f"ALTER USER {sql.identifier(login)} WITH LOGIN={sql.identifier(login)};")
				else:
					sql.create_restore_login(settings, database, login, password)
		except (RuntimeError, ValueError, OSError) as error:
			system.warn(f"{database} could not be put back: {error}. Its backup is in the export until it is deleted.")


def snapshot_here(system, identifier, unit):
	"""harbor site export-snapshot (on an environment's server): its site kept for Restore"""
	folder = folder_of(system, identifier)
	folder.mkdir(parents=True, exist_ok=True, mode=0o700)
	paths = local_paths(system, unit, f"/var/www/{unit}")
	with tarfile.open(folder / "snapshot.tar.gz", "w:gz") as tar:
		for path in paths:
			tar.add(system.path(path), arcname=path.lstrip("/"))
	save(system, {"id": identifier, "site": unit, "at": int(time.time()), "state": "snapshot", "snapshot": {"local": paths, "remote": []},
	              "deleted": True, "kept_for": "restore"})


def restore_here(system, identifier):
	"""The snapshot put back on this server: files, services and Apache, started as they were"""
	folder = folder_of(system, identifier)
	archive = folder / "snapshot.tar.gz"
	if not archive.is_file():
		raise Stop("There is no snapshot of that export to restore.")
	with tarfile.open(archive, "r:gz") as tar:
		members = [member for member in tar.getmembers() if not member.name.startswith("/") and ".." not in member.name.split("/")]
		tar.extractall(system.path("/"), members=members, filter="tar")
	item = meta(system, identifier)
	system.run("systemctl", "daemon-reload", check=False)
	for path in item.get("snapshot", {}).get("local", []):
		if path.startswith("/etc/systemd/system/") and path.endswith(".service"):
			unit = os.path.basename(path)
			system.run("systemctl", "enable", "--now", unit, check=False)
			system.say(f"Started {unit}")
	if system.run("apache2ctl", "configtest", check=False, capture=True).ok:
		system.run("systemctl", "reload", "apache2", check=False)
	else:
		system.warn("Apache's config does not test well after the restore: look at it before it is reloaded")


def restore(system, identifier):
	"""harbor site export-restore: the site deleted with an export put back as it was, here and on the servers its
	environments run on"""
	from harbor.environments import settings
	from harbor.sites import convert
	from harbor.sites.actions import site_named
	item = meta(system, identifier)
	if not item.get("deleted"):
		raise Stop("That site was not deleted with its export: there is nothing to restore.")
	if system.path(f"{WEB_ROOT}/{item['site']}").exists():
		raise Stop(f"There is a site {item['site']} here again: it is not overwritten.")
	remote = item.get("snapshot", {}).get("remote", [])
	databases = read_json(folder_of(system, identifier) / "databases.json") or []
	system.plan([*(["Put its databases back"] if databases else []), f"Put {item['site']} back here", *[f"Put {entry['unit']} back on {entry['host']}" for entry in remote]])
	# The databases first: the site starts on them
	if databases:
		system.step("Put its databases back")
		restore_databases(system, identifier)
	system.step(f"Put {item['site']} back here")
	restore_here(system, identifier)
	site = site_named(system, item["site"])
	for entry in remote:
		system.step(f"Put {entry['unit']} back on {entry['host']}")
		environment = settings.load(system, site.site_root, entry["target"])
		result = convert._remote_run(system, environment, ("site", "export-restore-here", identifier), progress=False, capture=True)
		if not result.ok:
			system.warn(f"{entry['unit']} on {entry['host']} could not be put back: {result.stderr.strip()[-300:]}")
	item.update(deleted=False, restored=int(time.time()))
	save(system, item)
	system.say(f"{item['site']} is back as it was before the export.")


# ---- the zip's life: downloaded, then deleted 72 hours later (with the snapshot)
def downloaded(system, identifier):
	item = meta(system, identifier)
	if not item.get("downloaded"):
		item["downloaded"] = int(time.time())
		save(system, item)
	return item


def forget(system, identifier):
	"""An export deleted: its zip, and its snapshots here and on its environments' servers"""
	item = meta(system, identifier)
	for entry in item.get("snapshot", {}).get("remote", []) if item.get("deleted") else []:
		try:
			from harbor.environments import settings
			from harbor.sites import convert
			from harbor.sites.actions import site_named
			environment = settings.load(system, site_named(system, item["site"]).site_root, entry["target"])
			convert._remote_run(system, environment, ("site", "export-forget", identifier), progress=False, capture=True)
		except Exception:  # noqa: BLE001 - the site is deleted: its environments' settings may be gone too
			pass
	shutil.rmtree(folder_of(system, identifier), ignore_errors=True)


def cleanup(system):
	"""harbor site export-cleanup: the exports downloaded more than 72 hours ago deleted, their snapshots with them"""
	now = time.time()
	for item in exports(system):
		if item.get("state") == "snapshot":
			# Another server's snapshot is deleted by the export it belongs to
			continue
		keep = item.get("keep", KEEP_AFTER_DOWNLOAD)
		if keep and item.get("downloaded") and now - item["downloaded"] > keep:
			forget(system, item["id"])
			system.say(f"Deleted the export of {item.get('site')}")
		elif item.get("state") == "making" and now - (item.get("at") or now) > 24 * 3600:
			shutil.rmtree(folder_of(system, item["id"]), ignore_errors=True)
