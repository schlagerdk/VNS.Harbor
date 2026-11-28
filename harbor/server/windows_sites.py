"""A Windows server's sites listed in Harbor, where they stay: the folders in its share (each an IIS site), for their
files and a note of where each is reached. Nothing is moved or changed on Windows, only the files edited in Files.

  /var/lib/harbor/servers.json   the Windows server's entry: list_sites (true while they are listed) and exclude
                                 (the folders not shown)

  harbor windows sites --server NAME [--all]        its sites as JSON (with --all, the folders left out too)
  harbor windows site-files --server NAME FOLDER    one file operation on a site's folder, JSON on stdin and stdout
  harbor servers windows-sites NAME on|off [--exclude FOLDER ...]
"""
import json
import re

from harbor.core.system import Stop
from harbor.state.store import write_json

# A site's folder in the share: one name, never a path (Windows does not allow these in a name either)
FORBIDDEN = set('/\\:*?"<>|')


def folder_name(folder):
	"""A folder in the share by its name, checked"""
	if not isinstance(folder, str) or not folder.strip() or folder in (".", "..") or any(char in FORBIDDEN or ord(char) < 32 for char in folder) or len(folder) > 255:
		raise Stop("Choose a site's folder.")
	return folder


def entry_of(system, name):
	from harbor.server import servers
	entry = servers.servers(system, "windows").get(name)
	if not entry:
		raise Stop(f"No Windows server {name}.")
	return entry


def configure(system, name, on, exclude=()):
	"""Its sites listed (on, with the folders left out) or not: only the entry in Servers changes"""
	from harbor.server import servers
	from harbor.state.store import read_json
	entry_of(system, name)
	excluded = sorted({folder_name(folder) for folder in exclude}, key=str.lower)
	found = read_json(system.path(servers.SERVERS)) or {}
	entry = {key: value for key, value in found[name].items() if key not in ("list_sites", "exclude")}
	found[name] = {**entry, "list_sites": True, **({"exclude": excluded} if excluded else {})} if on else entry
	write_json(system.path(servers.SERVERS), found)
	if on:
		system.say(f"{name}'s sites are listed" + (f", {len(excluded)} folder{'s' if len(excluded) != 1 else ''} left out." if excluded else "."))
	else:
		system.say(f"{name}'s sites are no longer listed.")


def open_share(system, name, folder=""):
	"""The share, or a site's folder in it, logged in"""
	from harbor.server import servers
	from harbor.sites.windows_import import share
	unc, user, password, domain = servers.windows_login(system, name)
	return share(system, unc, user, password, domain, folder)


# What a site is, read from its files: classic Umbraco (7 and older) says its version in web.config, a newer one in the
# build's deps.json; the .NET it runs in its runtimeconfig.json, or .NET Framework's in web.config
CLASSIC_UMBRACO = re.compile(r'<add\s+key="umbracoConfigurationStatus"\s+value="([0-9][^"]*)"', re.I)
FRAMEWORK = re.compile(r'targetFramework="([0-9][0-9.]*)"', re.I)
BUILT_UMBRACO = re.compile(r'"Umbraco\.Cms/([0-9][^"]*)"')
START_PAGES = {"index.html", "index.htm", "default.htm", "default.html"}


def what_it_is(root, folder, files):
	"""{kind, umbraco_version, dotnet, framework} of a site, from the files in its folder (the names given)"""
	lower = {name.lower(): name for name in files}
	found = {"kind": "html" if START_PAGES & set(lower) else "", "umbraco_version": "", "dotnet": "", "framework": ""}

	def read(name):
		return root.rclone("cat", f"{root.target}/{folder}/{name}", check=False, capture=True).stdout
	runtime = next((name for name in files if name.lower().endswith(".runtimeconfig.json")), None)
	if runtime:
		found["kind"] = "csharp"
		try:
			options = json.loads(read(runtime) or "{}").get("runtimeOptions") or {}
		except ValueError:
			options = {}
		frameworks = options.get("frameworks") or ([options["framework"]] if isinstance(options.get("framework"), dict) else [])
		version = next((item.get("version", "") for item in frameworks if isinstance(item, dict)
		                and item.get("name") in ("Microsoft.AspNetCore.App", "Microsoft.NETCore.App") and item.get("version")), "")
		found["dotnet"] = ".".join(version.split(".")[:2])
		deps = lower.get(runtime[:-len(".runtimeconfig.json")].lower() + ".deps.json")
		match = BUILT_UMBRACO.search(read(deps)) if deps else None
		if match:
			found.update(kind="umbraco", umbraco_version=match[1])
	if "web.config" in lower and not found["umbraco_version"]:
		text = read(lower["web.config"])
		match = CLASSIC_UMBRACO.search(text)
		if match:
			found.update(kind="umbraco", umbraco_version=match[1])
		framework = FRAMEWORK.search(text)
		if framework and not runtime:
			found["framework"] = framework[1]
			found["kind"] = found["kind"] if found["kind"] == "umbraco" else "csharp"
	return found


def folders(system, name, everything=False):
	"""Each folder in the share, by name: [{folder, excluded, umbraco, kind, umbraco_version, dotnet, framework}],
	without the excluded ones unless everything (then without what each is: only to choose which are shown)"""
	from concurrent.futures import ThreadPoolExecutor
	entry = entry_of(system, name)
	excluded = {folder.lower() for folder in entry.get("exclude") or []}
	root = open_share(system, name)
	# Two levels in one listing: the folders, the files in each, and which has an umbraco folder (its backoffice)
	paths = [line.strip() for line in root.rclone("lsf", root.target, "--max-depth", "2", capture=True).stdout.splitlines() if line.strip()]
	names = [path.rstrip("/") for path in paths if path.endswith("/") and path.count("/") == 1]
	umbraco = {path.split("/", 1)[0].lower() for path in paths if path.count("/") == 2 and path.endswith("/") and path.split("/", 1)[1].rstrip("/").lower() == "umbraco"}
	files = {}
	for path in paths:
		if not path.endswith("/") and path.count("/") == 1:
			files.setdefault(path.split("/", 1)[0].lower(), []).append(path.split("/", 1)[1])
	found = [{"folder": folder, "excluded": folder.lower() in excluded, "umbraco": folder.lower() in umbraco} for folder in sorted(names, key=str.lower)]
	if everything:
		return found
	shown = [item for item in found if not item["excluded"]]
	with ThreadPoolExecutor(max_workers=6) as executor:
		for item, what in zip(shown, executor.map(lambda item: what_it_is(root, item["folder"], files.get(item["folder"].lower(), [])), shown)):
			item.update(what)
	return shown


def site_files(system, name, folder):
	"""One file operation on a listed site's folder (lib/windows_files.py, JSON on stdin), as for a Windows environment"""
	from harbor.environments.actions import WINDOWS_FILES
	try:
		entry = entry_of(system, name)
		folder = folder_name(folder)
		if not entry.get("list_sites") or folder.lower() in {item.lower() for item in entry.get("exclude") or []}:
			raise Stop(f"{folder} is not a site listed on {name}.")
		share = open_share(system, name, folder)
	except Stop as error:
		system.say(json.dumps({"error": str(error) or "Could not open the Windows share"}))
		raise Stop("", 1) from None
	result = system.run("python3", str(WINDOWS_FILES), share.target, env=share.env, check=False)
	if not result.ok:
		raise Stop("", result.returncode)
