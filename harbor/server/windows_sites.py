"""A Windows server's sites listed in Harbor, where they stay: the folders in its share (each an IIS site), for their
files and a note of where each is reached. Nothing is moved or changed on Windows, only the files edited in Files.

  /var/lib/harbor/servers.json   the Windows server's entry: list_sites (true while they are listed) and exclude
                                 (the folders not shown)

  harbor windows sites --server NAME [--all]        its sites as JSON (with --all, the folders left out too)
  harbor windows site-files --server NAME FOLDER    one file operation on a site's folder, JSON on stdin and stdout
  harbor servers windows-sites NAME on|off [--exclude FOLDER ...]
"""
import json

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


def folders(system, name, everything=False):
	"""Each folder in the share, by name: [{folder, excluded}], without the excluded ones unless everything"""
	entry = entry_of(system, name)
	excluded = {folder.lower() for folder in entry.get("exclude") or []}
	root = open_share(system, name)
	# Two levels in one listing: the folders, and which of them has an umbraco folder (its backoffice is /umbraco)
	paths = [line.strip().rstrip("/") for line in root.rclone("lsf", root.target, "--dirs-only", "--max-depth", "2", capture=True).stdout.splitlines() if line.strip()]
	names = [path for path in paths if "/" not in path]
	umbraco = {path.split("/", 1)[0].lower() for path in paths if path.count("/") == 1 and path.split("/", 1)[1].lower() == "umbraco"}
	found = [{"folder": folder, "excluded": folder.lower() in excluded, "umbraco": folder.lower() in umbraco} for folder in sorted(names, key=str.lower)]
	return found if everything else [item for item in found if not item["excluded"]]


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
