"""The scripts an export comes with (Settings, Export Site), for Windows (PowerShell) or Linux (bash), besides
publish: clear-logs, sync-settings, sync-data, check-packages, add-package and remove-package, each run from the site's
folder or from anywhere, each asking for the environment when there are several. And app_offline.htm, the page IIS
shows while publish.ps1 updates a site, which goes inside publish.ps1 (not a file of its own to change).

What sync-settings and sync-data copy is what Harbor's syncs copy (harbor/environments/sync.py): settings are, in each
uSync version folder, its settings folders and usync.config, and Views, wwwroot/css and wwwroot/scripts, each mirrored;
data is uSync's Content, Dictionary and Media, and wwwroot/media, merged (nothing deleted)."""
import base64
import re
import shlex

from harbor.core.paths import INSTALL
from harbor.environments.sync import CODE_FOLDERS, SETTINGS_FOLDERS

DATA_FOLDERS = ("Content", "Dictionary", "Media")

# ---- Linux (bash)
SH_HEAD = r'''#!/usr/bin/env bash
# @WHAT@
# Run it from anywhere: bash scripts/@NAME@.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# The site's environments: each folder and the environment it runs as
environments=(@FOLDERS@)
names=(@NAMES@)
# pick "question": index of the environment chosen (no question with only one)
pick() {
	if [ "${#environments[@]}" -eq 1 ]; then index=0; return; fi
	echo "$1"
	local choice
	select choice in "${environments[@]}"; do
		[ -n "${choice:-}" ] && { index=$((REPLY - 1)); return; }
	done
}
'''

SH_BODIES = {
	"clear-logs": ("Deletes an environment's logs (umbraco/Logs and logs).", r'''pick "Clear the logs of which environment?"
folder="${environments[$index]}"
rm -rf "${folder:?}"/umbraco/Logs/* "${folder:?}"/logs/*
echo "The logs of $folder (${names[$index]}) are cleared."
'''),
	"sync-settings": ("Copies the settings of one environment to another: uSync's settings (@SETTINGS@ and usync.config) and "
	                  "Views, wwwroot/css and wwwroot/scripts, the destination made the same. Its content and media stay.", r'''if [ "${#environments[@]}" -lt 2 ]; then echo "There is only one environment: nothing to sync to."; exit 1; fi
pick "Copy the settings from which environment?"; from="${environments[$index]}"
pick "To which environment?"; to="${environments[$index]}"
[ "$from" != "$to" ] || { echo "Choose two different environments."; exit 1; }
mirror() { [ -e "$from/$1" ] || return 0; rm -rf "${to:?}/$1"; mkdir -p "$(dirname "$to/$1")"; cp -a "$from/$1" "$to/$1"; }
for version in "$from"/uSync/*/; do
	[ -d "$version" ] || continue
	name="$(basename "$version")"
	for part in @SETTINGS@ usync.config; do mirror "uSync/$name/$part"; done
done
for part in @CODE@; do mirror "$part"; done
echo "The settings of $from are in $to. Import them in its Umbraco: Settings, uSync, Import."
'''),
	"sync-data": ("Copies the data of one environment to another: uSync's Content, Dictionary and Media, and wwwroot/media, "
	              "merged: what the destination has more is kept.", r'''if [ "${#environments[@]}" -lt 2 ]; then echo "There is only one environment: nothing to sync to."; exit 1; fi
pick "Copy the data from which environment?"; from="${environments[$index]}"
pick "To which environment?"; to="${environments[$index]}"
[ "$from" != "$to" ] || { echo "Choose two different environments."; exit 1; }
merge() { [ -d "$from/$1" ] || return 0; mkdir -p "$to/$1"; cp -a "$from/$1/." "$to/$1/"; }
for version in "$from"/uSync/*/; do
	[ -d "$version" ] || continue
	name="$(basename "$version")"
	for part in @DATA@; do merge "uSync/$name/$part"; done
done
merge "wwwroot/media"
echo "The data of $from is in $to. Import it in its Umbraco: Settings, uSync, Import."
'''),
	"check-packages": ("Shows the project's NuGet packages that have newer versions.", r'''dotnet list "@PROJECT@.csproj" package --outdated
'''),
	"add-package": ("Adds a NuGet package to the project (the newest version, or the one given). Publish takes it into an environment.", r'''read -r -p "Package to add: " package
[ -n "$package" ] || { echo "No package given."; exit 1; }
read -r -p "Version (empty: the newest): " version
if [ -n "$version" ]; then dotnet add "@PROJECT@.csproj" package "$package" --version "$version"; else dotnet add "@PROJECT@.csproj" package "$package"; fi
echo "$package is added. Publish to take it into an environment: bash scripts/publish.sh"
'''),
	"remove-package": ("Removes one of the project's NuGet packages. Publish takes it out of an environment.", r'''mapfile -t packages < <(grep -o '<PackageReference Include="[^"]*"' "@PROJECT@.csproj" | sed 's/.*Include="\([^"]*\)"/\1/')
[ "${#packages[@]}" -gt 0 ] || { echo "The project has no packages."; exit 1; }
echo "Remove which package?"
select package in "${packages[@]}"; do [ -n "${package:-}" ] && break; done
dotnet remove "@PROJECT@.csproj" package "$package"
echo "$package is removed. Publish to take it out of an environment: bash scripts/publish.sh"
'''),
}

# ---- Windows (PowerShell)
# Each script's start: started from Explorer (Run with PowerShell) its window closes as it ends, so then it waits for
# Enter first, also after an error, for its output to be read
PS1_PAUSE = r'''$ErrorActionPreference = "Stop"
$fromExplorer = $false
try {
	$parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
	$fromExplorer = (Get-Process -Id $parent).ProcessName -eq "explorer"
} catch {}
function Done { if ($fromExplorer) { Write-Host ""; [void](Read-Host "Press Enter to close") } }
trap { Write-Host "Error: $_" -ForegroundColor Red; Done; exit 1 }
'''

PS1_HEAD = r'''# @WHAT@
# Run it from anywhere: powershell -File scripts\@NAME@.ps1
@PAUSE@Set-Location (Join-Path $PSScriptRoot "..")
# The site's environments: each folder and the environment it runs as
$environments = @(@ENTRIES@)
# The environment chosen (no question with only one)
function Pick($question) {
	if ($environments.Count -eq 1) { return $environments[0] }
	for ($index = 0; $index -lt $environments.Count; $index++) { Write-Host "$($index + 1)) $($environments[$index].Folder) ($($environments[$index].Name))" }
	$chosen = $environments[[int](Read-Host $question) - 1]
	if (-not $chosen) { throw "Choose one of the numbers shown" }
	return $chosen
}
'''

PS1_BODIES = {
	"clear-logs": r'''$chosen = Pick "Clear the logs of which environment"
foreach ($logs in @("umbraco\Logs", "logs")) {
	$path = Join-Path $chosen.Folder $logs
	if (Test-Path $path) { Get-ChildItem -Force $path | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Host "The logs of $($chosen.Folder) ($($chosen.Name)) are cleared."
''',
	"sync-settings": r'''if ($environments.Count -lt 2) { throw "There is only one environment: nothing to sync to" }
$from = (Pick "Copy the settings from which environment").Folder
$to = (Pick "To which environment").Folder
if ($from -eq $to) { throw "Choose two different environments" }
function Mirror($path) {
	$source = Join-Path $from $path
	if (Test-Path $source -PathType Container) {
		robocopy $source (Join-Path $to $path) /MIR /R:5 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Host
		if ($LASTEXITCODE -ge 8) { throw "Copying $path failed (robocopy $LASTEXITCODE)" }
	} elseif (Test-Path $source) {
		New-Item -ItemType Directory -Force -Path (Split-Path (Join-Path $to $path)) | Out-Null
		Copy-Item -Force $source (Join-Path $to $path)
	}
}
foreach ($version in Get-ChildItem -Directory (Join-Path $from "uSync") -ErrorAction SilentlyContinue) {
	foreach ($part in @(@SETTINGS@, "usync.config")) { Mirror "uSync\$($version.Name)\$part" }
}
foreach ($part in @(@CODE@)) { Mirror $part }
$global:LASTEXITCODE = 0
Write-Host "The settings of $from are in $to. Import them in its Umbraco: Settings, uSync, Import."
''',
	"sync-data": r'''if ($environments.Count -lt 2) { throw "There is only one environment: nothing to sync to" }
$from = (Pick "Copy the data from which environment").Folder
$to = (Pick "To which environment").Folder
if ($from -eq $to) { throw "Choose two different environments" }
function Merge($path) {
	$source = Join-Path $from $path
	if (Test-Path $source -PathType Container) {
		robocopy $source (Join-Path $to $path) /E /R:5 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Host
		if ($LASTEXITCODE -ge 8) { throw "Copying $path failed (robocopy $LASTEXITCODE)" }
	}
}
foreach ($version in Get-ChildItem -Directory (Join-Path $from "uSync") -ErrorAction SilentlyContinue) {
	foreach ($part in @(@DATA@)) { Merge "uSync\$($version.Name)\$part" }
}
Merge "wwwroot\media"
$global:LASTEXITCODE = 0
Write-Host "The data of $from is in $to. Import it in its Umbraco: Settings, uSync, Import."
''',
	"check-packages": r'''dotnet list "@PROJECT@.csproj" package --outdated
''',
	"add-package": r'''$package = Read-Host "Package to add"
if (-not $package) { throw "No package given" }
$version = Read-Host "Version (empty: the newest)"
if ($version) { dotnet add "@PROJECT@.csproj" package $package --version $version } else { dotnet add "@PROJECT@.csproj" package $package }
if ($LASTEXITCODE -ne 0) { throw "$package could not be added" }
Write-Host "$package is added. Publish to take it into an environment: powershell -File scripts\publish.ps1"
''',
	"remove-package": r'''$packages = @(Select-String -Path "@PROJECT@.csproj" -Pattern '<PackageReference Include="([^"]+)"' -AllMatches | ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value })
if (-not $packages.Count) { throw "The project has no packages" }
for ($index = 0; $index -lt $packages.Count; $index++) { Write-Host "$($index + 1)) $($packages[$index])" }
$package = $packages[[int](Read-Host "Remove which package") - 1]
if (-not $package) { throw "Choose one of the numbers shown" }
dotnet remove "@PROJECT@.csproj" package $package
if ($LASTEXITCODE -ne 0) { throw "$package could not be removed" }
Write-Host "$package is removed. Publish to take it out of an environment: powershell -File scripts\publish.ps1"
''',
}


# Scripts that copy from one environment to another: only in an export with more than one
SYNC_SCRIPTS = ("sync-settings", "sync-data")


def wanted(name, environments):
	"""Whether a script belongs in an export of these environments"""
	return name not in SYNC_SCRIPTS or len(environments) > 1


def scripts(os_name, project, environments):
	"""Every script but publish, for the OS chosen: [(file name, bytes)]. environments: [(folder, environment name)]"""
	found = []
	for name, (what, body) in SH_BODIES.items():
		if not wanted(name, environments):
			continue
		if os_name == "windows":
			entries = ", ".join(f'@{{ Folder = "{folder}"; Name = "{named}" }}' for folder, named in environments)
			text = (PS1_HEAD.replace("@WHAT@", what).replace("@NAME@", name).replace("@ENTRIES@", entries) + PS1_BODIES[name] + "Done\n")
			text = text.replace("@PAUSE@", PS1_PAUSE)
			text = (text.replace("@PROJECT@", project).replace("@SETTINGS@", ", ".join(f'"{part}"' for part in SETTINGS_FOLDERS))
			        .replace("@CODE@", ", ".join(f'"{part.replace("/", chr(92))}"' for part in CODE_FOLDERS))
			        .replace("@DATA@", ", ".join(f'"{part}"' for part in DATA_FOLDERS)))
			found.append((f"{name}.ps1", text.replace("\n", "\r\n").encode("utf-8")))
		else:
			text = (SH_HEAD.replace("@WHAT@", what).replace("@NAME@", name)
			        .replace("@FOLDERS@", " ".join(shlex.quote(folder) for folder, _named in environments))
			        .replace("@NAMES@", " ".join(shlex.quote(named) for _folder, named in environments)) + body)
			text = (text.replace("@PROJECT@", project).replace("@SETTINGS@", " ".join(SETTINGS_FOLDERS))
			        .replace("@CODE@", " ".join(CODE_FOLDERS)).replace("@DATA@", " ".join(DATA_FOLDERS)))
			found.append((f"{name}.sh", text.encode("utf-8")))
	return found


# ---- the page IIS shows while publish.ps1 updates a site: on its own (IIS sends only it), the logo in it
APP_OFFLINE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Back in a moment</title>
<style>
	*, *::before, *::after { box-sizing: border-box; }
	html, body { height: 100%; margin: 0; }
	body { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 28px; padding: 24px;
		background: radial-gradient(1200px 600px at 50% -10%, rgba(124, 92, 255, .28), transparent 60%), linear-gradient(160deg, #0d1117, #161b2e 60%, #0d1117);
		color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; text-align: center; }
	.card { width: min(460px, 100%); padding: 40px 36px 36px; border: 1px solid rgba(255, 255, 255, .08); border-radius: 20px;
		background: rgba(22, 27, 34, .72); box-shadow: 0 24px 60px rgba(0, 0, 0, .45), inset 0 1px 0 rgba(255, 255, 255, .04); backdrop-filter: blur(8px); }
	.ring { width: 54px; height: 54px; margin: 0 auto 24px; border: 4px solid rgba(124, 92, 255, .2); border-top-color: #8b6cff; border-radius: 50%;
		animation: turn 1s linear infinite; }
	@keyframes turn { to { transform: rotate(360deg); } }
	h1 { margin: 0 0 10px; font-size: 24px; font-weight: 700; letter-spacing: -.01em; }
	p { margin: 0; color: #9aa4b2; font-size: 15px; line-height: 1.55; }
	.bar { position: relative; height: 4px; margin: 26px 0 0; overflow: hidden; border-radius: 4px; background: rgba(255, 255, 255, .06); }
	.bar::after { content: ""; position: absolute; inset: 0; width: 40%; border-radius: 4px; background: linear-gradient(90deg, #7c5cff, #39c5cf);
		animation: run 1.6s ease-in-out infinite; }
	@keyframes run { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
	.powered { display: inline-flex; align-items: center; gap: 10px; color: #8b95b8; font-size: 13px; font-weight: 600; text-decoration: none; opacity: .75;
		transition: opacity .2s; }
	.powered:hover { opacity: 1; }
	.powered img { width: 34px; height: auto; }
	@media (prefers-reduced-motion: reduce) { .ring, .bar::after { animation: none; } }
</style>
</head>
<body>
	<main class="card" role="status">
		<div class="ring" aria-hidden="true"></div>
		<h1>We are updating the site</h1>
		<p>It is back in a moment. This page looks again by itself.</p>
		<div class="bar" aria-hidden="true"></div>
	</main>
	<a class="powered" href="https://vns.dk" target="_blank" rel="noopener"><img src="data:image/png;base64,@LOGO@" alt="VNS - vital new software"><span>Powered by VNS</span></a>
</body>
</html>
"""


def app_offline():
	logo = (INSTALL / "admin" / "static" / "vns-logo-footer.png").read_bytes()
	return APP_OFFLINE.replace("@LOGO@", base64.b64encode(logo).decode("ascii"))


# ---- README.txt: what the customer got, in Danish and English
SCRIPT_TEXTS = {
	"publish": ("bygger projektet og lægger det i det miljø, du vælger (Debug eller Release efter miljøet). Miljøets egne indstillinger og data røres ikke",
	            "builds the project and puts it in the environment you choose (Debug or Release as the environment is). Its own settings and data are not touched"),
	"clear-logs": ("sletter et miljøs logfiler", "deletes an environment's logs"),
	"sync-settings": ("kopierer indstillinger (uSync, Views, css, scripts) fra ét miljø til et andet. Importér dem bagefter i Umbraco: Settings, uSync, Import",
	                  "copies settings (uSync, Views, css, scripts) from one environment to another. Import them afterwards in Umbraco: Settings, uSync, Import"),
	"sync-data": ("kopierer indhold og medier fra ét miljø til et andet (intet slettes). Importér dem bagefter i Umbraco",
	              "copies content and media from one environment to another (nothing is deleted). Import them afterwards in Umbraco"),
	"check-packages": ("viser NuGet-pakker, der findes i nyere versioner", "shows the NuGet packages that have newer versions"),
	"add-package": ("tilføjer en NuGet-pakke til projektet", "adds a NuGet package to the project"),
	"remove-package": ("fjerner en NuGet-pakke fra projektet", "removes a NuGet package from the project"),
}


def readme(site, os_name, environments, databases, framework="", built=True):
	"""README.txt for the export: environments [(folder, environment name)], databases [file names] in databases/"""
	windows = os_name == "windows"
	run = (lambda name: f"powershell -File scripts\\{name}.ps1") if windows else (lambda name: f"bash scripts/{name}.sh")
	# net10.0 is .NET 10
	dotnet = re.sub(r"^net(\d+)(?:\.0)?$", r".NET \1", framework) if framework.startswith("net") else ".NET"
	width = max([len(folder) for folder, _name in environments] + [8])
	lines_da = [f"{site}", "=" * len(site), "", "DANSK", "-----", ""]
	lines_en = ["ENGLISH", "-------", ""]
	if built:
		lines_da += [f"Denne pakke indeholder {site} og dets miljøer, bygget til {'Windows' if windows else 'Linux'}, med projektets kildekode,",
		             "så det kan lægges på en server og bygges videre der.", "",
		             "Mapperne:", f"  {'(roden)':<{width}}  projektet (.csproj, kode og Views): det, der bygges fra"]
		lines_en += [f"This package holds {site} and its environments, built for {'Windows' if windows else 'Linux'}, with the project's source,",
		             "so it can be put on a server and built further there.", "",
		             "The folders:", f"  {'(root)':<{width}}  the project (.csproj, code and Views): what is built from"]
		for folder, name in environments:
			lines_da.append(f"  {folder:<{width}}  miljøet {name}: det færdige site. Det er den mappe, sitet peges til")
			lines_en.append(f"  {folder:<{width}}  the {name} environment: the site as it runs. This is the folder the site points to")
		if databases:
			lines_da.append(f"  {'databases':<{width}}  backup af miljøernes databaser")
			lines_en.append(f"  {'databases':<{width}}  backups of the environments' databases")
		lines_da.append(f"  {'scripts':<{width}}  scripts til at bygge og vedligeholde sitet (se nedenfor)")
		lines_en.append(f"  {'scripts':<{width}}  scripts to build and look after the site (see below)")
		if windows:
			lines_da += ["", "Sådan sættes et miljø op (Windows/IIS):",
			             f"  1. Installér {dotnet} Hosting Bundle på serveren (og {dotnet} SDK for at bygge).",
			             "  2. Opret et site i IIS med Physical path sat til miljøets mappe, fx _publish.",
			             "     Miljøet (ASPNETCORE_ENVIRONMENT) står i mappens web.config."]
			lines_en += ["", "Setting up an environment (Windows/IIS):",
			             f"  1. Install the {dotnet} Hosting Bundle on the server (and the {dotnet} SDK to build).",
			             "  2. Make a site in IIS with its physical path at the environment's folder, such as _publish.",
			             "     Its environment (ASPNETCORE_ENVIRONMENT) is in the folder's web.config."]
		else:
			lines_da += ["", "Sådan sættes et miljø op (Linux):",
			             f"  1. Installér {dotnet} runtime på serveren (og {dotnet} SDK for at bygge).",
			             "  2. Kør sitet fra miljøets mappe, fx som en systemd-service: WorkingDirectory=<mappen>,",
			             "     ExecStart=dotnet <projekt>.dll og Environment=ASPNETCORE_ENVIRONMENT=<miljøet>, bag en webserver."]
			lines_en += ["", "Setting up an environment (Linux):",
			             f"  1. Install the {dotnet} runtime on the server (and the {dotnet} SDK to build).",
			             "  2. Run the site from the environment's folder, such as a systemd service: WorkingDirectory=<folder>,",
			             "     ExecStart=dotnet <project>.dll and Environment=ASPNETCORE_ENVIRONMENT=<environment>, behind a web server."]
		lines_da += ["  3. Udfyld appsettings.json i mappen: databasens server og adgangskode i connection string står tomme, og",
		             "     mailserveren (Smtp og Notifications) har eksempler (example.com): skift dem til jeres egne. Sitet kører",
		             "     på http (UseHttps false, UmbracoApplicationUrl http://): sæt dem til https, når det har et certifikat."]
		lines_en += ["  3. Fill in appsettings.json in the folder: the database's server and password in its connection string are empty,",
		             "     and the mail server (Smtp and Notifications) has examples (example.com): replace them with your own. The site",
		             "     runs on http (UseHttps false, UmbracoApplicationUrl http://): set them to https once it has a certificate."]
		if databases:
			how = "SQL Server: Restore Database" if any(name.endswith(".bak") for name in databases) else "pg_restore"
			lines_da += [f"  4. Gendan miljøets database på jeres databaseserver ({how}): den ligger i databases/ under mappens navn,",
			             "     fx databases/" + databases[0] + "."]
			lines_en += [f"  4. Restore the environment's database on your database server ({how}): it is in databases/ by the folder's name,",
			             "     such as databases/" + databases[0] + "."]
		lines_da += ["", "Scripts (kør dem fra en terminal i pakkens mappe):"]
		lines_en += ["", "Scripts (run them from a terminal in the package's folder):"]
		for name, (danish, english) in SCRIPT_TEXTS.items():
			if not wanted(name, environments):
				continue
			lines_da.append(f"  {run(name)}")
			lines_da.append(f"      {danish}")
			lines_en.append(f"  {run(name)}")
			lines_en.append(f"      {english}")
		if windows:
			lines_da += ["", "Mens publish opdaterer et miljø, viser IIS en side (app_offline.htm) til de besøgende."]
			lines_en += ["", "While publish updates an environment, IIS shows visitors a page (app_offline.htm)."]
	else:
		lines_da += [f"Denne pakke indeholder {site} og dets miljøer, som de kører. Det kører på Windows og Linux.", "",
		             "Mapperne: roden er sitet; en mappe der starter med _ er et miljø."]
		lines_en += [f"This package holds {site} and its environments, as they run. It runs on Windows and Linux.", "",
		             "The folders: the root is the site; a folder starting with _ is an environment."]
		if databases:
			lines_da.append("databases/ indeholder backup af miljøernes databaser.")
			lines_en.append("databases/ holds backups of the environments' databases.")
	lines_da += ["", "Lavet af VNS · vns.dk", "", ""]
	lines_en += ["", "Made by VNS · vns.dk", ""]
	text = "\n".join(lines_da + lines_en)
	return text.replace("\n", "\r\n").encode("utf-8") if windows else text.encode("utf-8")
