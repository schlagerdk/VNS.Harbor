"""A site's database converted (phase 11 of dev/HARBOR.md): its SQLite database into a new database of its own on
Microsoft SQL Server or PostgreSQL, or its database on one of them into a new SQLite file in the site, with its
connection string changed and the site stopped while it is done.

The site is a service here (the main site, or an environment in a folder here). Its settings are the
appsettings*.json in its working folder: the connection string is changed in each one that has it, in place (the rest
of the file as it was), and put back as it was when a step fails. The database it used is kept: Settings, Convert
shows it with Delete (harbor site convert-delete for a SQLite file; the SQL Server page's drop for one on a server).

The connection to the server comes in VNS_CONVERT_CONNECTION (as the admin keeps it, with its password), the new
login's password in VNS_CONVERT_LOGIN_PASSWORD: never on the command line."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import time
import uuid

from harbor.core.names import service_name
from harbor.core.paths import LIB
from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

RECORD = "/var/lib/harbor/conversions.json"
WEB_ROOT = "/var/www"
ENGINES = {"mssql": "Microsoft SQL Server", "postgres": "PostgreSQL", "sqlite": "SQLite"}
PROVIDERS = {"mssql": "Microsoft.Data.SqlClient", "postgres": "Npgsql", "sqlite": "Microsoft.Data.Sqlite"}
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:sqlite\.db|sqlite|sqlite3|db)")
# As a site's own name: letters, digits, . _ and - (PostgreSQL keeps 63 bytes of a name)
DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
LOGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


def _lib(name, module):
	import sys
	if str(LIB) not in sys.path:
		sys.path.insert(0, str(LIB))
	spec = importlib.util.spec_from_file_location(module, LIB / name)
	found = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(found)
	return found


_loaded = {}


def converter():
	if "convert" not in _loaded:
		_loaded["convert"] = _lib("sql_convert.py", "vns_site_convert")
	return _loaded["convert"]


def info():
	if "info" not in _loaded:
		_loaded["info"] = _lib("database_info.py", "vns_site_convert_info")
	return _loaded["info"]


# ---- the site
def service_file(system, unit):
	return system.path(f"/etc/systemd/system/{service_name(unit)}.service")


def working_folder(system, unit):
	"""The folder the site's service runs in, under /var/www"""
	path = service_file(system, unit)
	if not path.is_file():
		raise Stop(f"{unit} has no service here: only a site that runs on this server is converted here.")
	text = path.read_text()
	found = re.search(r"(?m)^WorkingDirectory=(.+)$", text)
	if not found:
		raise Stop(f"The service of {unit} names no working folder.")
	folder = os.path.normpath(found[1].strip().strip('"'))
	if not folder.startswith(WEB_ROOT + "/") or not system.path(folder).is_dir():
		raise Stop(f"The service of {unit} runs in {folder}, which is not a site's folder in {WEB_ROOT}.")
	if re.search(r"(?i)connectionstrings(?:__|:)", text):
		raise Stop(f"The service of {unit} sets a connection string itself: change it there by hand.")
	return folder, text


def port_of(service_text):
	found = re.search(r"(?:localhost|127\.0\.0\.1|\*|0\.0\.0\.0|\+):(\d{2,5})", service_text)
	return int(found[1]) if found else None


def settings_files(system, folder):
	return sorted(path for path in system.path(folder).glob("appsettings*.json") if path.is_file() and not path.is_symlink()
	              and "schema" not in path.name)


def is_umbraco(system, folder):
	if system.path(f"{folder}/umbraco").is_dir():
		return True
	for path in settings_files(system, folder):
		try:
			if "Umbraco" in info().configuration_json(info().read_config_text(path)):
				return True
		except (OSError, ValueError):
			continue
	return False


def is_python(system, folder):
	return any(system.path(f"{folder}/{name}").is_file() for name in ("requirements.txt", "pyproject.toml", "app.py", "wsgi.py", "manage.py")) and not list(
		system.path(folder).glob("*.dll"))


def site_connection(system, folder, key=""):
	"""The connection string the site uses for its database: its name, its value (in appsettings.json, else the first
	file that has it), its kind (SQLite, MS SQL, PostgreSQL), and the files that set it"""
	found = {}
	for path in settings_files(system, folder):
		try:
			data = info().configuration_json(info().read_config_text(path))
		except (OSError, ValueError) as error:
			raise Stop(f"{path.name} cannot be read: {error}") from None
		connections = data.get("ConnectionStrings")
		if not isinstance(connections, dict):
			continue
		for name, value in connections.items():
			if not isinstance(value, str) or not value.strip() or name.lower().endswith("_providername"):
				continue
			provider = str(connections.get(name + "_ProviderName", "")).lower()
			kind = info().connection_type(value, provider)
			if kind not in ("SQLite", "MS SQL", "SQL Express", "PostgreSQL"):
				continue
			entry = found.setdefault(name, {"key": name, "files": [], "values": {}})
			entry["files"].append(path.name)
			entry["values"][path.name] = {"value": value, "provider": connections.get(name + "_ProviderName"), "kind": kind}
	if key:
		if key not in found:
			raise Stop(f"{key} is not a database connection string of the site.")
		entry = found[key]
	elif len(found) == 1:
		entry = next(iter(found.values()))
	elif not found:
		raise Stop("The site has no database connection string in its appsettings*.json.")
	else:
		raise Stop("The site has several connection strings: " + ", ".join(sorted(found)) + ". Choose the one to convert.")
	main = entry["values"].get("appsettings.json") or next(iter(entry["values"].values()))
	kinds = {value["kind"] for value in entry["values"].values()}
	if len(kinds) > 1:
		raise Stop(f"{entry['key']} points at different kinds of database in {', '.join(entry['files'])}.")
	kind = main["kind"]
	entry.update(value=main["value"], provider=main["provider"],
	             engine="sqlite" if kind == "SQLite" else "postgres" if kind == "PostgreSQL" else "mssql")
	return entry


def sqlite_file(system, folder, value, umbraco):
	"""The SQLite file a connection string names: |DataDirectory| is Umbraco's umbraco/Data"""
	fields = info().connection_fields(value)
	name = fields.get("data source") or fields.get("datasource") or fields.get("filename", "")
	if not name or name == ":memory:" or name.startswith("file:"):
		raise Stop("The site's SQLite connection string names no file here.")
	if "|DataDirectory|" in name:
		if not umbraco:
			raise Stop("The site's SQLite file is in |DataDirectory|, which only Umbraco names here.")
		name = name.replace("|DataDirectory|", f"{folder}/umbraco/Data")
	path = os.path.normpath(name if name.startswith("/") else f"{folder}/{name}")
	if not path.startswith(folder + "/"):
		raise Stop("The site's SQLite file is outside its folder.")
	if not system.path(path).is_file():
		raise Stop(f"The site's SQLite file {os.path.relpath(path, folder)} is not there.")
	return path


def server_database(value, engine):
	fields = info().connection_fields(value)
	name = fields.get("initial catalog") or fields.get("database", "")
	if not name:
		raise Stop("The site's connection string names no database.")
	return name


# ---- connection strings
def mssql_string(settings, database, login, password):
	host = "localhost" if settings["host"] in (".", "127.0.0.1") else settings["host"]
	quote = lambda text: '"' + text.replace('"', '""') + '"' if re.search(r'[;"\'= ]', text) else text
	return (f"Server={host},{settings.get('port') or 1433};Database={quote(database)};User Id={quote(login)};Password={quote(password)};"
	        + ("TrustServerCertificate=True;" if settings.get("trust_certificate", True) else "Encrypt=True;"))


def postgres_string(settings, database, login, password):
	host = "localhost" if settings["host"] in (".", "127.0.0.1") else settings["host"]
	quote = lambda text: '"' + text.replace('"', '""') + '"' if re.search(r'[;"\'= ]', text) else text
	return (f"Host={host};Port={settings.get('port') or 5432};Database={quote(database)};Username={quote(login)};Password={quote(password)}"
	        + (";SSL Mode=Prefer" if settings.get("trust_certificate", True) else ";SSL Mode=VerifyFull"))


def sqlite_string(folder, path, umbraco):
	data = f"{folder}/umbraco/Data/"
	if umbraco and path.startswith(data):
		return f"Data Source=|DataDirectory|/{path[len(data):]};Cache=Shared;Foreign Keys=True;Pooling=True"
	return f"Data Source={os.path.relpath(path, folder)}"


def json_string(value):
	return json.dumps(value, ensure_ascii=False)


def change_text(text, key, value, provider):
	"""The file's text with ConnectionStrings' key set to value and its _ProviderName to provider (added after it when
	the site had none and provider is given), everything else as it was. None when it cannot be found as it is written"""
	block = re.search(r'"ConnectionStrings"\s*:\s*\{', text)
	if not block:
		return None
	start = block.end()
	# The end of the block: the brace that closes it, outside strings
	depth, index, inside = 1, start, False
	while index < len(text) and depth:
		char = text[index]
		if inside:
			if char == "\\":
				index += 1
			elif char == '"':
				inside = False
		elif char == '"':
			inside = True
		elif char in "{[":
			depth += 1
		elif char in "}]":
			depth -= 1
		index += 1
	if depth:
		return None
	end = index - 1
	body = text[start:end]
	pair = re.compile(r'("' + re.escape(key) + r'"\s*:\s*)"(?:[^"\\]|\\.)*"')
	found = pair.search(body)
	if not found:
		return None
	body = body[:found.start()] + found[1] + json_string(value) + body[found.end():]
	provider_pair = re.compile(r'("' + re.escape(key + "_ProviderName") + r'"\s*:\s*)"(?:[^"\\]|\\.)*"')
	existing = provider_pair.search(body)
	if existing and provider:
		body = body[:existing.start()] + existing[1] + json_string(provider) + body[existing.end():]
	elif not existing and provider:
		found = pair.search(body)
		line_start = body.rfind("\n", 0, found.start()) + 1
		indent = re.match(r"[ \t]*", body[line_start:])[0]
		after = body[found.end():]
		comma = re.match(r"\s*,", after)
		insertion = f',\n{indent}"{key}_ProviderName": {json_string(provider)}'
		body = body[:found.end()] + insertion + after if not comma else body[:found.end()] + insertion + "," + after[comma.end():]
	changed = text[:start] + body + text[end:]
	try:
		data = info().configuration_json(changed)
	except ValueError:
		return None
	connections = data.get("ConnectionStrings", {})
	if connections.get(key) != value or provider and connections.get(key + "_ProviderName") != provider:
		return None
	return changed


def change_settings(system, folder, entry, value, provider_for):
	"""Each file that sets the connection gets the new one. Returns the files' texts as they were, to put back"""
	before = {}
	for name in entry["files"]:
		path = system.path(f"{folder}/{name}")
		raw = path.read_bytes()
		before[name] = raw
		text = info().read_config_text(path)
		provider = provider_for(entry["values"][name]["provider"])
		changed = change_text(text, entry["key"], value, provider)
		if changed is None:
			data = info().configuration_json(text)
			data["ConnectionStrings"][entry["key"]] = value
			if provider:
				data["ConnectionStrings"][entry["key"] + "_ProviderName"] = provider
			changed = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
		write_like(path, changed, raw)
	return before


def write_like(path, text, raw):
	"""Written in place with the file's owner and rights kept"""
	info_before = path.stat()
	newline = "\r\n" if b"\r\n" in raw else "\n"
	data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
	if raw.startswith(b"\xef\xbb\xbf"):
		data = b"\xef\xbb\xbf" + data
	path.write_bytes(data)
	try:
		os.chown(path, info_before.st_uid, info_before.st_gid)
	except PermissionError:
		pass
	os.chmod(path, info_before.st_mode & 0o7777)


def set_aside(system, path):
	"""A SQLite file the site no longer runs on, renamed to <name>.<when>.bak, whole: what its WAL held is written into
	it first. Its name is then free for a conversion back, and Roll back renames it again. When it was set aside is in
	its name: one conversion after another each keep their own, in the order they were made"""
	stamp = time.strftime("%Y%m%d-%H%M%S")
	backup = f"{path}.{stamp}.bak"
	number = 2
	while system.path(backup).exists():
		backup = f"{path}.{stamp}-{number}.bak"
		number += 1
	import sqlite3
	connection = sqlite3.connect(system.path(path))
	try:
		connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
		connection.execute("PRAGMA journal_mode = DELETE")
	finally:
		connection.close()
	os.rename(system.path(path), system.path(backup))
	for name in (path + "-wal", path + "-shm"):
		system.path(name).unlink(missing_ok=True)
	return backup


def put_back(system, folder, before):
	for name, raw in before.items():
		path = system.path(f"{folder}/{name}")
		path.write_bytes(raw)


# ---- the service
def active(system, unit):
	return system.run("systemctl", "is-active", "--quiet", f"{service_name(unit)}.service", check=False).ok


def responds(system, port, seconds=150):
	"""The site answers on its port without a server error, and Umbraco does not send it to its installer"""
	if not port:
		return True, ""
	deadline = time.monotonic() + seconds
	last = "no answer"
	while time.monotonic() < deadline:
		try:
			connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
			connection.request("GET", "/", headers={"User-Agent": "VNS Harbor convert"})
			response = connection.getresponse()
			location = response.getheader("Location") or ""
			response.read(65536)
			connection.close()
			if "/install" in location.lower():
				return False, "Umbraco sends the site to its installer: it does not find its database"
			if response.status < 500:
				return True, ""
			last = f"HTTP {response.status}"
		except (OSError, http.client.HTTPException) as error:
			last = str(error) or type(error).__name__
		system.sleep(3)
	return False, last


def record(system, entry):
	path = system.path(RECORD)
	items = read_json(path) or []
	items.append(entry)
	write_json(path, items, private=True)


def records(system):
	return read_json(system.path(RECORD)) or []


def forget_gone(system):
	"""The conversions of sites that are gone (their folder and service deleted), and those whose SQLite file is gone,
	are forgotten: their files went with the folder. A database they kept on a server stays there, on the SQL Server page. Returns how many"""
	items = records(system)
	def there(item):
		old = item.get("from") or {}
		# A SQLite file it kept that is gone (its site deleted, and maybe made again in the same folder since): there is
		# nothing left to roll back to or delete
		if old.get("engine") == "sqlite" and not system.path(old.get("backup") or old.get("path") or "/nonexistent").exists():
			return False
		if item.get("folder") and system.path(item["folder"]).is_dir():
			return True
		try:
			return bool(item.get("unit")) and service_file(system, item["unit"]).is_file()
		except ValueError:
			return False
	left = [item for item in items if there(item)]
	if len(left) != len(items):
		write_json(system.path(RECORD), left, private=True)
	return len(items) - len(left)


# ---- the tools a conversion runs here
TOOLS = {"mssql": "the Microsoft SQL Server tools (sqlcmd)", "postgres": "the PostgreSQL tools (psql, pg_dump)"}


def tools(system):
	"""Which engines' tools this server has: a conversion runs them here, where the site is"""
	convert = converter()
	return {"mssql": bool(convert.sql_admin().sqlcmd_path()), "postgres": bool(convert.pg_admin().tool("psql") and convert.pg_admin().tool("pg_dump"))}


def need_tools(system, engine):
	if not tools(system).get(engine):
		raise Stop(f"{TOOLS[engine][0].upper()}{TOOLS[engine][1:]} are not installed on this server, where the site runs: "
		           "install them in Settings, SQL Server, or convert it from a Harbor that has them.")


# ---- the conversions
def convert_site(system, unit, to, database="", login="", filename="", key="", by=""):
	"""to: mssql or postgres (from the site's SQLite) or sqlite (from its database on a server)"""
	if to not in ENGINES:
		raise Stop("Convert to mssql, postgres or sqlite.")
	connection = os.environ.pop("VNS_CONVERT_CONNECTION", "")
	password = os.environ.pop("VNS_CONVERT_LOGIN_PASSWORD", "")
	try:
		settings = json.loads(connection) if connection else None
	except ValueError:
		settings = None
	if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
		raise Stop("Give the connection to the server in VNS_CONVERT_CONNECTION.")
	engine = "postgres" if settings.get("engine") == "postgres" else "mssql"
	folder, service_text = working_folder(system, unit)
	if is_python(system, folder):
		raise Stop("A Python site's database is not converted yet.")
	umbraco = is_umbraco(system, folder)
	entry = site_connection(system, folder, key)
	if to == "sqlite":
		if entry["engine"] == "sqlite":
			raise Stop("The site uses SQLite already.")
		if entry["engine"] != engine:
			raise Stop(f"The site's database is on {ENGINES[entry['engine']]}: choose a connection to it.")
		# Before anything is made: the tools run here, where the site is
		need_tools(system, engine)
		return to_sqlite(system, unit, folder, service_text, entry, settings, filename, umbraco, by)
	if entry["engine"] != "sqlite":
		raise Stop(f"The site uses {ENGINES[entry['engine']]}: only a SQLite database is converted to a server.")
	if to != engine:
		raise Stop(f"The connection chosen is {ENGINES[engine]}, not {ENGINES[to]}.")
	if umbraco and to == "postgres":
		raise Stop("Umbraco does not run on PostgreSQL: choose Microsoft SQL Server.")
	if not DATABASE_NAME.fullmatch(database or ""):
		raise Stop("A database's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit.")
	if not LOGIN_NAME.fullmatch(login or ""):
		raise Stop("A login's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit.")
	if not 8 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
		raise Stop("The login's password must be 8-128 characters without line breaks.")
	need_tools(system, engine)
	return to_server(system, unit, folder, service_text, entry, settings, to, database, login, password, umbraco, by)


def _plan(system, steps):
	system.plan(steps)
	return steps


def create_database(system, engine, settings, database, login, password):
	"""The new database and its own login on the server; the settings its tables are loaded with"""
	convert = converter()
	sql = convert.sql_admin()
	created = False
	try:
		if engine == "mssql":
			if sql.query_json(settings, "SELECT name FROM sys.databases WHERE name=" + sql.literal(database)):
				raise Stop(f"There is a database {database} already. Choose another name.")
			if sql.query_json(settings, "SELECT name FROM sys.server_principals WHERE name=" + sql.literal(login)):
				raise Stop(f"There is a SQL login {login} already. Choose another name.")
			sql.execute(settings, f"CREATE DATABASE {sql.identifier(database)};")
			created = True
			sql.create_restore_login(settings, database, login, password)
			load = settings
		else:
			pg = convert.pg_admin()
			if pg.query_json(settings, f"SELECT 1 AS found FROM pg_database WHERE datname = {pg.literal(database)}"):
				raise Stop(f"There is a database {database} already. Choose another name.")
			if pg.role_exists(settings, login):
				raise Stop(f"There is a login {login} already. Choose another name.")
			pg.create_login(settings, login, password)
			created = True
			pg.create_database(settings, database, owner=login)
			pg.own_schema(settings, database, login)
			# The tables are the login's own: it makes them
			load = {**settings, "username": login, "password": password}
	except (RuntimeError, ValueError) as error:
		if created:
			drop_server(system, engine, settings, database, login)
		raise Stop(str(error)) from None
	return load


def to_server(system, unit, folder, service_text, entry, settings, engine, database, login, password, umbraco, by):
	convert = converter()
	sql = convert.sql_admin()
	path = sqlite_file(system, folder, entry["value"], umbraco)
	running = active(system, unit)
	server = ENGINES[engine]
	steps = _plan(system, [f"Create the database {database} and its login {login} on {server}", *([f"Stop {unit}"] if running else []),
	                       f"Convert {os.path.basename(path)} to {database}", "Change the connection string",
	                       f"Start {unit} on {server}"])
	system.step(steps[0])
	load = create_database(system, engine, settings, database, login, password)
	before = {}
	backup = ""
	try:
		if running:
			system.step(steps[1])
			system.run("systemctl", "stop", f"{service_name(unit)}.service")
		system.step(steps[-3])
		result = convert.from_sqlite(system.path(path), engine, load, database, log=system.say, umbraco=umbraco)
		system.say(f"{result['tables']} tables, {result['rows']} rows")
		system.step(steps[-2])
		value = mssql_string(settings, database, login, password) if engine == "mssql" else postgres_string(settings, database, login, password)
		before = change_settings(system, folder, entry, value,
		                         lambda old: PROVIDERS[engine] if umbraco or old else None)
		backup = set_aside(system, path)
		system.say(f"{os.path.basename(path)} is kept as {os.path.basename(backup)}")
		system.step(steps[-1])
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if backup and system.path(backup).exists() and not system.path(path).exists():
			os.rename(system.path(backup), system.path(path))
		drop_server(system, engine, settings, database, login)
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {os.path.basename(path)} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	record(system, {"id": uuid.uuid4().hex, "unit": unit, "folder": folder, "key": entry["key"], "at": int(time.time()), "by": by,
	                "from": {"engine": "sqlite", "path": path, "backup": backup}, "previous": entry["values"],
	                "to": {"engine": engine, "connection": settings.get("id", ""), "host": settings["host"], "port": settings.get("port"),
		                       "database": database, "login": login}})
	system.say(f"{unit} runs on {database}. Its SQLite file is kept as {os.path.relpath(backup, folder)}: Roll back takes it again, Delete deletes it.")


def to_sqlite(system, unit, folder, service_text, entry, settings, filename, umbraco, by):
	convert = converter()
	database = server_database(entry["value"], entry["engine"])
	data = f"{folder}/umbraco/Data" if umbraco else f"{folder}/App_Data"
	if not filename:
		filename = "Umbraco.sqlite.db" if umbraco else re.sub(r"[^A-Za-z0-9._-]", "_", database) + ".sqlite.db"
		number = 2
		while system.path(f"{data}/{filename}").exists():
			filename = (f"Umbraco-{number}.sqlite.db" if umbraco else re.sub(r"[^A-Za-z0-9._-]", "_", database) + f"-{number}.sqlite.db")
			number += 1
	if not FILE_NAME.fullmatch(filename):
		raise Stop("A SQLite file's name has letters, digits, . _ and -, and ends in .sqlite.db, .sqlite, .sqlite3 or .db.")
	path = f"{data}/{filename}"
	if system.path(path).exists():
		raise Stop(f"{os.path.relpath(path, folder)} is there already. Choose another name.")
	running = active(system, unit)
	steps = _plan(system, [*([f"Stop {unit}"] if running else []), f"Convert {database} to {os.path.relpath(path, folder)}",
	                       "Change the connection string", f"Start {unit} on SQLite"])
	before = {}
	made = False
	try:
		if running:
			system.step(steps[0])
			system.run("systemctl", "stop", f"{service_name(unit)}.service")
		system.step(steps[-3])
		folder_info = system.path(folder).stat()
		target = system.path(data)
		if not target.is_dir():
			target.mkdir(parents=True)
			os.chown(target, folder_info.st_uid, folder_info.st_gid)
		made = True
		result = convert.to_sqlite(entry["engine"], settings, database, system.path(path), log=system.say)
		owner = target.stat()
		for name in (path, path + "-wal", path + "-shm"):
			if system.path(name).exists():
				os.chown(system.path(name), owner.st_uid, owner.st_gid)
				os.chmod(system.path(name), 0o660)
		system.say(f"{result['tables']} tables, {result['rows']} rows")
		system.step(steps[-2])
		before = change_settings(system, folder, entry, sqlite_string(folder, path, umbraco),
		                         lambda old: PROVIDERS["sqlite"] if umbraco or old else None)
		system.step(steps[-1])
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if made:
			for name in (path, path + "-wal", path + "-shm"):
				system.path(name).unlink(missing_ok=True)
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {database} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	record(system, {"id": uuid.uuid4().hex, "unit": unit, "folder": folder, "key": entry["key"], "at": int(time.time()), "by": by,
	                "from": {"engine": entry["engine"], "connection": settings.get("id", ""), "host": settings["host"], "port": settings.get("port"),
		                         "database": database},
	                "previous": entry["values"], "to": {"engine": "sqlite", "path": path}})
	system.say(f"{unit} runs on {os.path.relpath(path, folder)}. Its database {database} is kept: Settings, Convert DB deletes it.")


def start_and_check(system, unit, service_text):
	system.run("systemctl", "start", f"{service_name(unit)}.service")
	ok, why = responds(system, port_of(service_text))
	if not ok or not active(system, unit):
		journal = system.run("journalctl", "-u", f"{service_name(unit)}.service", "-n", "30", "--no-pager", capture=True, check=False).stdout
		for line in journal.splitlines()[-12:]:
			system.say(line)
		raise Stop(f"{unit} did not start on the new database ({why or 'its service stopped'}).")


def drop_server(system, engine, settings, database, login):
	"""The database and login made for a conversion that did not go through"""
	convert = converter()
	try:
		if engine == "mssql":
			sql = convert.sql_admin()
			sql.execute(settings, f"""
IF DB_ID({sql.literal(database)}) IS NOT NULL
BEGIN
 ALTER DATABASE {sql.identifier(database)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
 DROP DATABASE {sql.identifier(database)};
END;
""" + (f"IF SUSER_ID({sql.literal(login)}) IS NOT NULL DROP LOGIN {sql.identifier(login)};" if login else ""))
		else:
			pg = convert.pg_admin()
			if pg.query_json(settings, f"SELECT 1 AS found FROM pg_database WHERE datname = {pg.literal(database)}"):
				pg.drop_database(settings, database, [login] if login else [])
			elif login and pg.role_exists(settings, login):
				pg.execute(settings, f"DROP ROLE {pg.identifier(login)};")
		system.say(f"The new database {database}" + (f" and its login {login} are" if login else " is") + " deleted")
	except (RuntimeError, ValueError, OSError) as error:
		system.warn(f"The new database {database} could not be deleted again: {error}")


def delete_old(system, identifier, leave=False):
	"""The database a site ran on before it was converted, deleted: a SQLite file (with its -wal and -shm), or a
	database on a server (with the logins only it had, as the SQL Server page deletes one), the connection to it in
	VNS_CONVERT_CONNECTION. Never one the site uses again. leave: one on a server is only checked and forgotten here,
	and deleted by the Harbor that asked (it has the tools this server has not)"""
	items = records(system)
	item = next((item for item in items if item.get("id") == identifier), None)
	if not item:
		raise Stop("That conversion is not known here.")
	old = item["from"]
	if old["engine"] != "sqlite":
		database = old["database"]
		try:
			entry = site_connection(system, item["folder"], item["key"])
		except Stop:
			entry = None
		if entry and entry["engine"] == old["engine"] and server_database(entry["value"], entry["engine"]).lower() == database.lower():
			raise Stop(f"{item['unit']} uses {database} again: it is not deleted.")
		if not leave:
			try:
				settings = json.loads(os.environ.pop("VNS_CONVERT_CONNECTION", "") or "null")
			except ValueError:
				settings = None
			if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
				raise Stop("The connection to the old database's server is not known here any more.")
			system.plan([f"Delete the database {database}"])
			system.step(f"Delete the database {database}")
			drop_old(system, old["engine"], settings, database)
	else:
		path = old.get("backup") or old["path"]
		if not path.startswith(WEB_ROOT + "/"):
			raise Stop("The old SQLite file is not in a site's folder.")
		folder = item["folder"]
		try:
			entry = site_connection(system, folder, item["key"])
		except Stop:
			entry = None
		if entry and entry["engine"] == "sqlite" and sqlite_file(system, folder, entry["value"], is_umbraco(system, folder)) == path:
			raise Stop(f"{item['unit']} uses {os.path.basename(path)} again: it is not deleted.")
		system.plan([f"Delete {os.path.basename(path)}"])
		system.step(f"Delete {os.path.basename(path)}")
		for name in (path, path + "-wal", path + "-shm"):
			system.path(name).unlink(missing_ok=True)
	write_json(system.path(RECORD), [other for other in items if other.get("id") != identifier], private=True)


def _drop_backups(system, engine, settings, database):
	"""The backup files of a database on its server (its .bak files there, or its dumps), as Delete's "Also delete its
	backup files" deletes them; a file that holds another database's backups is left"""
	convert = converter()
	try:
		if engine == "mssql":
			sql = convert.sql_admin()
			if not sql.backup_file_access(settings):
				system.warn(f"The backups of {database} are left on {settings.get('host')}: its backup folder is not reached here")
				return
			found = sql.backup_files_for_deletion(settings, database)
			if found:
				sql.delete_backup_files(found, settings)
		else:
			pg = convert.pg_admin()
			for item in pg.backups(settings):
				if item.get("database_name") == database and not item.get("error"):
					Path(item["path"]).unlink(missing_ok=True)
					system.say(f"Deleted backup file: {item['path']}")
	except (RuntimeError, ValueError, OSError) as error:
		system.warn(f"The backups of {database} could not all be deleted: {error}. Delete them on the SQL Server page.")


def drop_old(system, engine, settings, database, backups=False):
	"""A database a site ran on before, deleted with the logins only it had, as the SQL Server page deletes one.
	backups: its backup files there go too (a move: nothing of it is left on the server it left)"""
	convert = converter()
	try:
		if backups:
			_drop_backups(system, engine, settings, database)
		if engine == "mssql":
			sql = convert.sql_admin()
			if sql.query_json(settings, "SELECT name FROM sys.databases WHERE name=" + sql.literal(database)):
				logins = sql.exclusive_database_logins(settings, database)
				sql.execute(settings, f"""
ALTER DATABASE {sql.identifier(database)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
DROP DATABASE {sql.identifier(database)};""")
				for login in logins:
					sql.execute(settings, "DROP LOGIN " + sql.identifier(login) + ";")
					system.say("Deleted the login " + login)
			else:
				system.say(f"{database} is gone already")
		else:
			pg = convert.pg_admin()
			if pg.query_json(settings, f"SELECT 1 AS found FROM pg_database WHERE datname = {pg.literal(database)}"):
				pg.drop_database(settings, database, pg.exclusive_logins(settings, database))
			else:
				system.say(f"{database} is gone already")
	except (RuntimeError, ValueError, OSError) as error:
		raise Stop(str(error)) from None


def uses(system, folder, key, side):
	"""Whether the site's connection string points at this side of a conversion (its from or its to)"""
	try:
		entry = site_connection(system, folder, key)
	except Stop:
		return False
	if entry["engine"] != side["engine"]:
		return False
	if side["engine"] == "sqlite":
		try:
			return sqlite_file(system, folder, entry["value"], is_umbraco(system, folder)) == side["path"]
		except Stop:
			return False
	return server_database(entry["value"], entry["engine"]).lower() == side["database"].lower()


def connection_for(identifier):
	"""A server's connection as the admin gave it: VNS_CONVERT_CONNECTIONS ({id: connection}) for a rollback of several,
	else VNS_CONVERT_CONNECTION"""
	try:
		several = json.loads(os.environ.get("VNS_CONVERT_CONNECTIONS", "") or "{}")
		if isinstance(several, dict) and identifier in several:
			return several[identifier]
		return json.loads(os.environ.get("VNS_CONVERT_CONNECTION", "") or "null")
	except ValueError:
		return None


def chain(system, identifier):
	"""The conversions a rollback to this one undoes, newest first: it and those made after it of the same site"""
	items = records(system)
	item = next((item for item in items if item.get("id") == identifier), None)
	if not item:
		raise Stop("That conversion is not known here.")
	return sorted((other for other in items if other.get("unit") == item["unit"] and (other.get("at") or 0) >= (item.get("at") or 0)),
	              key=lambda other: other.get("at") or 0, reverse=True)


def rollback_deletes(system, found):
	"""What a rollback through these conversions (newest first) deletes: what each made, and what each after the
	oldest kept. Once each: (SQLite files, databases on servers)"""
	target = found[-1]
	files, databases = [], []
	for item in found:
		sides = [item["to"]] + ([item["from"]] if item is not target else [])
		for side in sides:
			if side["engine"] == "sqlite":
				path = side.get("backup") or side["path"] if side is item["from"] else side["path"]
				# One a conversion back made has the old one's name: it is deleted too, before the old one takes its name
				if path not in files and system.path(path).exists():
					files.append(path)
			elif (side.get("connection"), side["database"].lower()) not in {(entry["connection"], entry["database"].lower()) for entry in databases}:
				databases.append({"engine": side["engine"], "connection": side.get("connection", ""), "host": side.get("host", ""),
				                  "port": side.get("port"), "database": side["database"],
				                  "login": side.get("login", "") if side is item["to"] else ""})
	return files, databases


def rollback_to(system, identifier, leave=False):
	"""Back to the database the site ran on before this conversion, in one go, whatever was converted after it: nothing
	is converted. The site is stopped, gets the connection string it had then, its SQLite file (kept as .bak) takes its
	name again, and it is started and asked for its home page. Then what it ran on since is deleted: each database and
	file the conversions made, and those they kept. A file to delete is moved aside until the site runs, and a database
	on a server deleted only then: when the start fails, all is put back as it was. The servers' connections in
	VNS_CONVERT_CONNECTIONS ({id: connection}). leave: the databases on servers are left for the Harbor that asked to
	delete (it has the tools this server has not)"""
	found = chain(system, identifier)
	target, newest = found[-1], found[0]
	unit, folder, key = target["unit"], target["folder"], target["key"]
	_, service_text = working_folder(system, unit)
	if not uses(system, newest["folder"], newest["key"], newest["to"]):
		raise Stop(f"{unit} does not run on the database of its newest conversion: there is nothing to roll back.")
	umbraco = is_umbraco(system, folder)
	old = target["from"]
	previous = target.get("previous")
	if not previous:
		if old["engine"] != "sqlite":
			raise Stop("The connection string the site had before was not kept (a conversion from before 1.0.83): change it back by hand.")
		# A conversion from before the connection string was kept: a SQLite one is made again from its file
		previous = {name: {"value": sqlite_string(folder, old["path"], umbraco), "provider": PROVIDERS["sqlite"] if umbraco else None}
		            for name in site_connection(system, folder, key)["files"]}
	backup = old.get("backup") if old["engine"] == "sqlite" else ""
	if old["engine"] == "sqlite" and not system.path(backup or old["path"]).is_file():
		raise Stop(f"{os.path.basename(backup or old['path'])} is not there any more: the site cannot go back to it.")
	files, databases = rollback_deletes(system, found)
	if leave:
		databases = []
	# The SQLite file the site runs on now has the old one's name (a conversion back made it): it goes aside first
	if backup and system.path(old["path"]).exists() and old["path"] not in files:
		raise Stop(f"{os.path.basename(old['path'])} is there again (another file of that name): {os.path.basename(backup)} cannot take its name back.")
	for entry in databases:
		settings = connection_for(entry["connection"])
		if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
			raise Stop(f"The connection to {entry['database']}'s server is not known here any more.")
		entry["settings"] = settings
	old_name = os.path.basename(old["path"]) if old["engine"] == "sqlite" else old["database"]
	gone = [os.path.basename(path) for path in files] + [entry["database"] for entry in databases]
	running = active(system, unit)
	steps = _plan(system, [*([f"Stop {unit}"] if running else []), "Put the connection string back",
	                       *([f"{os.path.basename(backup)} is {old_name} again"] if backup else []), f"Start {unit} on {old_name}",
	                       *[f"Delete {name}" for name in gone]])
	if running:
		system.step(f"Stop {unit}")
		system.run("systemctl", "stop", f"{service_name(unit)}.service")
	system.step("Put the connection string back")
	before, aside, renamed = {}, [], False
	try:
		for name, value in previous.items():
			path = system.path(f"{folder}/{name}")
			if not path.is_file():
				continue
			raw = path.read_bytes()
			before[name] = raw
			text = info().read_config_text(path)
			changed = change_text(text, key, value["value"], value.get("provider"))
			if changed is None:
				data = info().configuration_json(text)
				data.setdefault("ConnectionStrings", {})[key] = value["value"]
				if value.get("provider"):
					data["ConnectionStrings"][key + "_ProviderName"] = value["provider"]
				changed = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
			write_like(path, changed, raw)
		# The files to delete moved aside (with their -wal and -shm), so the old one can take its name
		for path in files:
			for name in (path, path + "-wal", path + "-shm"):
				if system.path(name).exists():
					os.rename(system.path(name), system.path(name + ".rollback"))
					aside.append(name)
		if backup:
			system.step(f"{os.path.basename(backup)} is {old_name} again")
			os.rename(system.path(backup), system.path(old["path"]))
			renamed = True
		system.step(f"Start {unit} on {old_name}")
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if renamed:
			os.rename(system.path(old["path"]), system.path(backup))
		for name in aside:
			os.rename(system.path(name + ".rollback"), system.path(name))
		system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
		system.say(f"{unit} runs on what it ran on before the rollback, and nothing is deleted")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	for path in files:
		system.step(f"Delete {os.path.basename(path)}")
		for name in (path, path + "-wal", path + "-shm"):
			system.path(name + ".rollback").unlink(missing_ok=True)
	for entry in databases:
		system.step(f"Delete {entry['database']}")
		drop_server(system, entry["engine"], entry["settings"], entry["database"], entry["login"])
	ids = {item["id"] for item in found}
	write_json(system.path(RECORD), [other for other in records(system) if other.get("id") not in ids], private=True)
	os.environ.pop("VNS_CONVERT_CONNECTIONS", None)
	os.environ.pop("VNS_CONVERT_CONNECTION", None)
	system.say(f"{unit} runs on {old_name} again.")


def describe(system, unit):
	"""What the admin shows of a site's database: its engine, its file or database, and whether it can be converted
	here"""
	try:
		folder, _ = working_folder(system, unit)
	except Stop as error:
		return {"unit": unit, "convertible": False, "reason": str(error)}
	if is_python(system, folder):
		return {"unit": unit, "folder": folder, "convertible": False, "reason": "A Python site's database is not converted yet."}
	umbraco = is_umbraco(system, folder)
	try:
		entry = site_connection(system, folder)
	except Stop as error:
		return {"unit": unit, "folder": folder, "umbraco": umbraco, "convertible": False, "reason": str(error)}
	result = {"unit": unit, "folder": folder, "umbraco": umbraco, "key": entry["key"], "engine": entry["engine"], "convertible": True}
	if entry["engine"] == "sqlite":
		try:
			path = sqlite_file(system, folder, entry["value"], umbraco)
			result.update(file=os.path.relpath(path, folder), size=system.path(path).stat().st_size)
		except Stop as error:
			result.update(convertible=False, reason=str(error))
	else:
		fields = info().connection_fields(entry["value"])
		result.update(database=fields.get("initial catalog") or fields.get("database", ""),
		              server=fields.get("server") or fields.get("data source") or fields.get("host", ""),
		              # Its login (never its password): Move offers to take it along when only this database has it
		              login=login_of(entry["value"])[0])
		if entry["engine"] == "postgres" and fields.get("port"):
			result["server"] = f"{result['server']},{fields['port']}"
	return result


# ---- what Settings, Convert shows, here or from another server's Harbor
def _side_server(side):
	"""The database server a side of a conversion is on, as the admin matches it with its own connections, and the
	database (and its login) on it"""
	return {"engine": side.get("engine"), "connection": side.get("connection", ""), "host": side.get("host", ""), "port": side.get("port"),
	        "database": side.get("database", ""), "login": side.get("login", "")}


def kept(system, units=None):
	"""What each site ran on before a conversion, newest first, as Settings, Convert shows it: whether it can be rolled
	back to (while the site runs on its newest conversion's database), what that deletes, and the database servers
	each needs (rollback: those it deletes from; delete: the old database's). Only these units' when given. Those of
	sites that are gone are forgotten first"""
	try:
		forget_gone(system)
	except OSError:
		pass
	found = sorted((item for item in records(system) if units is None or item.get("unit") in units),
	               key=lambda item: item.get("at") or 0, reverse=True)
	newest, oldest = {}, {}
	for item in found:
		newest.setdefault(item.get("unit"), item)
		# The oldest kept: what the site ran on before its first conversion, where it started
		oldest[item.get("unit")] = item.get("id")
	current = {unit: bool(item.get("folder") and item.get("key")) and uses(system, item["folder"], item["key"], item.get("to") or {})
	           for unit, item in newest.items()}
	name = lambda side: side.get("database") or os.path.basename(side.get("path") or "")
	result = []
	for item in found:
		old = item.get("from") or {}
		new = item.get("to") or {}
		rolling = current.get(item.get("unit"), False)
		deletes, servers = [], []
		if rolling:
			try:
				files, databases = rollback_deletes(system, chain(system, item["id"]))
				deletes = [os.path.basename(path) for path in files] + [entry["database"] for entry in databases]
				servers = [_side_server(entry) for entry in databases]
			except Exception:  # noqa: BLE001 - a record that cannot be read only offers nothing to delete
				deletes, servers = [], []
		result.append({"id": item.get("id"), "unit": item.get("unit"), "at": item.get("at"), "by": item.get("by", ""),
		               # A SQLite file is kept as its .bak (from 1.0.83), else as it was
		               "engine": old.get("engine"),
		               "file": os.path.relpath(old.get("backup") or old["path"], item.get("folder") or WEB_ROOT) if old.get("path") else "",
		               "database": old.get("database", ""), "connection": old.get("connection", ""),
		               "exists": system.path(old.get("backup") or old["path"]).is_file() if old.get("path") else True,
		               "to": new.get("engine"),
		               # What Roll back deletes: the new database, or the new SQLite file; and the name the old one has again
		               "to_name": name(new), "from_name": name(old),
		               "rollback": rolling,
		               "original": oldest.get(item.get("unit")) == item.get("id"),
		               "deletes": deletes,
		               # The servers a rollback deletes from, and the one Delete deletes the old database from
		               "servers": servers, "server": _side_server(old) if old.get("engine") not in (None, "sqlite") else None,
		               # What it runs on now
		               "current": name(newest[item.get("unit")].get("to") or {})})
	return result


def overview(system, units):
	"""harbor site convert-info: these sites' databases and what they kept, as another server's admin reads them"""
	sites = []
	for unit in units:
		item = describe(system, unit)
		item.pop("folder", None)
		sites.append(item)
	try:
		found = tools(system)
	except Exception:  # noqa: BLE001 - tools not known: the conversion says so itself
		found = {}
	return {"sites": sites, "kept": kept(system, set(units) if units else None), "tools": found}


# ---- a site on another server, converted there by its own Harbor over SSH
REMOTE_ACTIONS = ("convert", "convert-info", "convert-rollback", "convert-delete", "convert-move")
# What goes to the other server on its standard input, never on a command line: one line each
SECRETS = ("VNS_CONVERT_CONNECTION", "VNS_CONVERT_CONNECTIONS", "VNS_CONVERT_LOGIN_PASSWORD")
REMOTE_SCRIPT = r'''IFS= read -r a; IFS= read -r b; IFS= read -r c
export VNS_CONVERT_CONNECTION="$a" VNS_CONVERT_CONNECTIONS="$b" VNS_CONVERT_LOGIN_PASSWORD="$c"
h=/usr/local/bin/harbor; [ -x "$h" ] || h=/opt/harbor/current/bin/harbor
if [ ! -x "$h" ]; then echo "Harbor is not installed on this server: its sites are converted by a Harbor there." >&2; exit 3; fi
'''


def _remote_run(system, environment, parts, values=("", "", ""), progress=True, capture=False):
	"""harbor <parts> as root on the environment's server, the secrets on its stdin"""
	from harbor.environments import settings as env_settings
	if any("\n" in value or "\r" in value for value in values):
		raise Stop("A connection or password has a line break.")
	command = " ".join(shlex.quote(part) for part in parts)
	exported = os.environ.get("HARBOR_PROGRESS", "") if progress else ""
	script = REMOTE_SCRIPT + (f"export HARBOR_PROGRESS={shlex.quote(exported)}\n" if exported else "unset HARBOR_PROGRESS\n") + f'exec "$h" {command}\n'
	return env_settings.remote_exec(system, environment, "bash -c " + shlex.quote(script), input="\n".join(values) + "\n", check=False, capture=capture)


def _remote_json(system, environment, parts, values=("", "", "")):
	"""What harbor <parts> there prints as JSON, on its last line that is"""
	result = _remote_run(system, environment, parts, values, progress=False, capture=True)
	lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
	if not result.ok or not lines:
		said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip() and not line.startswith("Warning: Permanently added")]
		raise Stop(said[-1] if said else "The server could not be reached.")
	return json.loads(lines[-1])


def _fetch(system, environment, path, local):
	"""A file on the environment's server copied here, as it is (it may be large: never held in memory)"""
	import subprocess
	from harbor.environments import settings as env_settings
	argv, env = env_settings.ssh_command(environment)
	sudo = "" if environment.USER == "root" else "sudo -n "
	with open(local, "wb") as target:
		result = subprocess.run([*argv, f"{environment.USER}@{environment.IP}", f"{sudo}cat {shlex.quote(path)}"], stdout=target,
		                        stderr=subprocess.PIPE, env={**os.environ, **env})
	if result.returncode != 0:
		raise Stop(f"The SQLite file could not be copied here: {result.stderr.decode(errors='replace').strip()[-400:]}")


def remote(system, name, target, action, arguments):
	"""harbor site <action> run by the Harbor on the server where this site's environment runs, as root there. Its steps
	(and its JSON for convert-info) come back as it prints them; the connections and the password go on its stdin.
	When that server has not the tools for the database server and this one has, this Harbor does the database's part
	itself (relay): the conversion's data, a rollback's and a delete's databases"""
	from harbor.environments import remote as remote_env
	if action not in REMOTE_ACTIONS:
		raise Stop(f"Unknown convert action: {action}")
	_site, environment = remote_env.remote_environment(system, name, target)
	values = [os.environ.pop(key, "") for key in SECRETS]
	if action == "convert-move":
		# The database's part is always done here (server to server), the site's there
		return relay_move(system, environment, list(arguments), values)
	if action in ("convert", "convert-rollback", "convert-delete"):
		relayed = _relay(system, environment, action, list(arguments), values)
		if relayed:
			return
	result = _remote_run(system, environment, ("site", action, *arguments), values)
	if not result.ok:
		raise Stop("", result.returncode or 1)


def _here(engine):
	try:
		return bool(tools(None).get(engine))
	except Exception:  # noqa: BLE001
		return False


def _relay(system, environment, action, arguments, values):
	"""Whether this Harbor does the database's part (and so the whole of it): only when that server lacks the tools"""
	if action == "convert":
		unit, to = arguments[0], arguments[1]
		info = _remote_json(system, environment, ("site", "convert-info", unit))
		if to == "sqlite":
			# From a server there without its tools: read here into a file sent there
			site = next((item for item in info.get("sites", []) if item.get("unit") == unit), {})
			engine = site.get("engine")
			if engine not in ("mssql", "postgres") or (info.get("tools") or {}).get(engine) or not _here(engine):
				return False
			relay_to_sqlite(system, environment, unit, engine, arguments[2:], values)
			return True
		if (info.get("tools") or {}).get(to) or not _here(to):
			return False
		site = next((item for item in info.get("sites", []) if item.get("unit") == unit), {})
		relay_to_server(system, environment, unit, to, arguments[2:], values, site)
		return True
	identifier = arguments[0]
	info = _remote_json(system, environment, ("site", "convert-info"))
	item = next((item for item in info.get("kept", []) if item.get("id") == identifier), None)
	if not item:
		raise Stop("That conversion is not known on that server.")
	servers = item.get("servers") or [] if action == "convert-rollback" else [item["server"]] if item.get("server") else []
	engines = {server["engine"] for server in servers}
	if not engines or all((info.get("tools") or {}).get(engine) for engine in engines) or not all(_here(engine) for engine in engines):
		return False
	# There: the site back on its old database (or the old one forgotten); here: the databases deleted
	result = _remote_run(system, environment, ("site", action, identifier, "--leave-databases"), ("", "", ""))
	if not result.ok:
		raise Stop("", result.returncode or 1)
	connections = {}
	try:
		connections = json.loads(values[1] or "{}") if action == "convert-rollback" else {}
		single = json.loads(values[0] or "null")
	except ValueError:
		single = None
	for server in servers:
		settings = connections.get(server.get("connection")) if action == "convert-rollback" else single
		if not isinstance(settings, dict):
			system.warn(f"{server['database']} could not be deleted here: the connection to its server is not known")
			continue
		system.say(f"Delete {server['database']} from here")
		if action == "convert-rollback":
			drop_server(system, server["engine"], settings, server["database"], server.get("login", ""))
		else:
			drop_old(system, server["engine"], settings, server["database"])
	return True


def relay_to_server(system, environment, unit, engine, options, values, site):
	"""A site's SQLite database on another server converted here: there it is stopped and its file made whole, here the
	new database made and loaded from a copy of it, there the connection string changed and the site started. What
	fails puts it back: there as it was, here the new database deleted"""
	import tempfile
	opts = dict(zip(options[0::2], options[1::2]))
	database, login, by = opts.get("--database", ""), opts.get("--login", ""), opts.get("--by", "")
	try:
		settings = json.loads(values[0] or "null")
	except ValueError:
		settings = None
	password = values[2]
	if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
		raise Stop("Give the connection to the server in VNS_CONVERT_CONNECTION.")
	if not DATABASE_NAME.fullmatch(database) or not LOGIN_NAME.fullmatch(login) or not 8 <= len(password) <= 128:
		raise Stop("The database, its login or the password is not as a conversion needs it.")
	if site.get("engine") != "sqlite":
		raise Stop(site.get("reason") or "Only a SQLite database is converted to a server.")
	if site.get("umbraco") and engine == "postgres":
		raise Stop("Umbraco does not run on PostgreSQL: choose Microsoft SQL Server.")
	server = ENGINES[engine]
	name = os.path.basename(site.get("file") or "the SQLite file")
	steps = _plan(system, [f"Create the database {database} and its login {login} on {server}",
	                       f"Stop {unit} on {environment.IP} and copy {name} here", f"Convert {name} to {database} here",
	                       f"Change the connection string and start {unit} on {server}"])
	system.say(f"{environment.IP} has not the {server} tools: this Harbor converts the data")
	system.step(steps[0])
	load = create_database(system, engine, settings, database, login, password)
	prepared, finishing = None, False
	folder = tempfile.mkdtemp(prefix="harbor-convert-")
	copy = os.path.join(folder, "site.sqlite.db")
	try:
		system.step(steps[1])
		prepared = _remote_json(system, environment, ("site", "convert-relay", "prepare", unit, *(["--key", opts["--key"]] if opts.get("--key") else [])))
		_fetch(system, environment, prepared["path"], copy)
		system.step(steps[2])
		result = converter().from_sqlite(Path(copy), engine, load, database, log=system.say, umbraco=bool(prepared.get("umbraco")))
		system.say(f"{result['tables']} tables, {result['rows']} rows")
		system.step(steps[3])
		finish = ("site", "convert-relay", "finish", unit, "--engine", engine, "--database", database, "--login", login, "--key", prepared["key"],
		          "--by", by, *(["--running"] if prepared.get("running") else []))
		finishing = True
		done = _remote_run(system, environment, finish, (values[0], "", password))
		if not done.ok:
			raise Stop("", done.returncode or 1)
	except BaseException as error:
		drop_server(system, engine, settings, database, login)
		# Not yet changed there: the site started again on its SQLite file (finish puts itself back)
		if prepared and prepared.get("running") and not finishing:
			system.say(f"{unit} runs on {name} again, as before")
			_remote_run(system, environment, ("site", "convert-relay", "resume", unit), progress=False, capture=True)
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	finally:
		import shutil
		shutil.rmtree(folder, ignore_errors=True)


def relay_prepare(system, unit, key=""):
	"""(On the site's server, for a relay) The site stopped and its SQLite file whole, for the Harbor that converts it:
	where it is, whether it ran, as JSON"""
	folder, _ = working_folder(system, unit)
	umbraco = is_umbraco(system, folder)
	entry = site_connection(system, folder, key)
	if entry["engine"] != "sqlite":
		raise Stop(f"The site uses {ENGINES[entry['engine']]}: only a SQLite database is converted to a server.")
	path = sqlite_file(system, folder, entry["value"], umbraco)
	running = active(system, unit)
	if running:
		system.run("systemctl", "stop", f"{service_name(unit)}.service")
	import sqlite3
	connection = sqlite3.connect(system.path(path))
	try:
		connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
	finally:
		connection.close()
	print(json.dumps({"path": path, "running": running, "umbraco": umbraco, "key": entry["key"]}), flush=True)


def relay_resume(system, unit):
	system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)


def relay_finish(system, unit, engine, database, login, key, running, by):
	"""(On the site's server, for a relay) Its database loaded by the Harbor that asked: the connection string changed,
	the SQLite file set aside, the site started and asked for its home page, the conversion kept here. What fails is
	put back, and the site started on its SQLite file again"""
	try:
		settings = json.loads(os.environ.pop("VNS_CONVERT_CONNECTION", "") or "null")
	except ValueError:
		settings = None
	password = os.environ.pop("VNS_CONVERT_LOGIN_PASSWORD", "")
	if not isinstance(settings, dict) or "host" not in settings or engine not in ("mssql", "postgres"):
		raise Stop("Give the connection to the server in VNS_CONVERT_CONNECTION.")
	folder, service_text = working_folder(system, unit)
	umbraco = is_umbraco(system, folder)
	entry = site_connection(system, folder, key)
	path = sqlite_file(system, folder, entry["value"], umbraco)
	before, backup = {}, ""
	try:
		value = mssql_string(settings, database, login, password) if engine == "mssql" else postgres_string(settings, database, login, password)
		before = change_settings(system, folder, entry, value, lambda old: PROVIDERS[engine] if umbraco or old else None)
		backup = set_aside(system, path)
		system.say(f"{os.path.basename(path)} is kept as {os.path.basename(backup)}")
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if backup and system.path(backup).exists() and not system.path(path).exists():
			os.rename(system.path(backup), system.path(path))
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {os.path.basename(path)} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	record(system, {"id": uuid.uuid4().hex, "unit": unit, "folder": folder, "key": entry["key"], "at": int(time.time()), "by": by,
	                "from": {"engine": "sqlite", "path": path, "backup": backup}, "previous": entry["values"],
	                "to": {"engine": engine, "connection": settings.get("id", ""), "host": settings["host"], "port": settings.get("port"),
	                       "database": database, "login": login}})
	system.say(f"{unit} runs on {database}. Its SQLite file is kept as {os.path.relpath(backup, folder)}: Roll back takes it again, Delete deletes it.")


# ---- a database moved to another server of its kind (Settings, Convert, Move): the site on the same engine, its
# database copied by the server's own backup (Microsoft SQL Server: BACKUP and RESTORE, the file carried between the
# servers' backup folders; PostgreSQL: pg_dump and pg_restore), the connection string changed, the old one kept
def login_of(value):
	"""The login and password a connection string names"""
	fields = info().connection_fields(value)
	return (fields.get("user id") or fields.get("uid") or fields.get("username") or fields.get("user") or "",
	        fields.get("password") or fields.get("pwd") or "")


def _copy_backup(sql, source, source_path, target, target_path):
	"""A backup file from one SQL server's backup folder to another's, never held in memory"""
	import shutil
	import subprocess
	reader = sql.backup_file_pipe(source, source_path, write=False)
	writer = sql.backup_file_pipe(target, target_path, write=True)
	sql.file_operation(target, "prepare_write", {target_path: []})
	if reader is None and writer is None:
		shutil.copyfile(source_path, target_path)
		return
	if reader is None:
		with open(source_path, "rb") as stream:
			result = subprocess.run(writer[0], stdin=stream, stderr=subprocess.PIPE, env=writer[1])
		if result.returncode:
			raise RuntimeError("The backup could not be written on the new server: " + result.stderr.decode(errors="replace").strip()[-300:])
		return
	if writer is None:
		with open(target_path, "wb") as stream:
			result = subprocess.run(reader[0], stdout=stream, stderr=subprocess.PIPE, env=reader[1])
		if result.returncode:
			raise RuntimeError("The backup could not be read on the old server: " + result.stderr.decode(errors="replace").strip()[-300:])
		return
	read = subprocess.Popen(reader[0], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=reader[1])
	written = subprocess.run(writer[0], stdin=read.stdout, stderr=subprocess.PIPE, env=writer[1])
	read.stdout.close()
	failed = read.wait()
	if failed or written.returncode:
		raise RuntimeError("The backup could not be carried to the new server: "
		                   + (read.stderr.read().decode(errors="replace") + written.stderr.decode(errors="replace")).strip()[-300:])


def _forget_backup(sql, settings, path):
	try:
		found = sql.file_operation(settings, "inspect", {path: None}, skip_missing=True)
		if found:
			sql.file_operation(settings, "delete", found)
	except (RuntimeError, ValueError, OSError):
		pass


def move_database(system, engine, source, target, old, new, login, password):
	"""The database old on source copied as new on target, owned by login (made there with password). The new one is
	deleted again when a step fails; the old one is never touched"""
	convert = converter()
	if engine == "postgres":
		pg = convert.pg_admin()
		if pg.query_json(target, f"SELECT 1 AS found FROM pg_database WHERE datname = {pg.literal(new)}"):
			raise Stop(f"There is a database {new} on the new server already. Choose another name.")
		if pg.role_exists(target, login):
			raise Stop(f"There is a login {login} on the new server already. Choose another, or a new login.")
		system.say(f"Back up {old}")
		# A file of its own, never one of the backups (nor left among them)
		import tempfile
		handle, dump = tempfile.mkstemp(prefix="harbor-move-", suffix=".dump")
		os.close(handle)
		os.chmod(dump, 0o600)
		try:
			pg.run([pg.pg_dump(), "-Fc", "-d", old, "-f", dump], source)
		except (RuntimeError, ValueError, OSError) as error:
			Path(dump).unlink(missing_ok=True)
			raise Stop(str(error)) from None
		made = False
		try:
			pg.create_login(target, login, password)
			made = True
			pg.create_database(target, new, login)
			pg.own_schema(target, new, login)
			system.say(f"Restore it as {new}")
			pg.restore(target, new, dump, login)
		except BaseException as error:
			if made:
				drop_server(system, engine, target, new, login)
			if isinstance(error, (RuntimeError, ValueError, OSError)):
				raise Stop(str(error)) from None
			raise
		finally:
			Path(dump).unlink(missing_ok=True)
		return
	sql = convert.sql_admin()
	for settings, which in ((source, "old"), (target, "new")):
		if not sql.backup_file_access(settings) or "\\" in settings.get("backup_directory", ""):
			raise Stop(f"Moving needs the backup folder of the {which} server ({settings.get('host')}): it is reached here, "
			           "or over SSH when its connection has SSH turned on (SQL Server page, Edit connection).")
	if sql.query_json(target, "SELECT name FROM sys.databases WHERE name=" + sql.literal(new)):
		raise Stop(f"There is a database {new} on the new server already. Choose another name.")
	if sql.query_json(target, "SELECT name FROM sys.server_principals WHERE name=" + sql.literal(login)):
		raise Stop(f"There is a SQL login {login} on the new server already. Choose another, or a new login.")
	separator = "\\" if "\\" in target["backup_directory"] else "/"
	carried = target["backup_directory"].rstrip("/\\") + separator + f"{new}.move-{uuid.uuid4().hex[:8]}.bak"
	# A backup file of its own (never one of the backups there, nor left among them), and copy-only: the server's own
	# backups go on as before
	made = source["backup_directory"].rstrip("/\\") + ("\\" if "\\" in source["backup_directory"] else "/") + f"{old}.move-{uuid.uuid4().hex[:8]}.bak"
	restored = False
	try:
		system.say(f"Back up {old}")
		sql.prepare_backup_access(source, made, write=True)
		sql.execute(source, f"BACKUP DATABASE {sql.identifier(old)} TO DISK={sql.literal(made)} WITH INIT, COPY_ONLY;")
		system.say(f"Carry the backup to {target.get('host')}")
		_copy_backup(sql, source, made, target, carried)
		system.say(f"Restore it as {new}")
		sql.restore(target, new, carried, 1, False)
		restored = True
		sql.create_restore_login(target, new, login, password)
	except BaseException as error:
		if restored:
			drop_server(system, engine, target, new, login)
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	finally:
		_forget_backup(sql, target, carried)
		_forget_backup(sql, source, made)


def _server_settings(name):
	try:
		settings = json.loads(os.environ.pop(name, "") or "null")
	except ValueError:
		settings = None
	if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
		raise Stop(f"Give the connection in {name}.")
	return settings


def _move_names(database, login, password):
	if not DATABASE_NAME.fullmatch(database or ""):
		raise Stop("A database's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit.")
	if not login or len(login) > 128 or any(char in login for char in "\0\r\n[]"):
		raise Stop("The login's name cannot be used.")
	if not password or len(password) > 128 or any(char in password for char in "\0\r\n"):
		raise Stop("The login's password must be at most 128 characters without line breaks.")


def move_site(system, unit, database, login="", keep_login=False, key="", by=""):
	"""harbor site convert-move: the site's database on Microsoft SQL Server or PostgreSQL moved to another server of the
	same kind (VNS_CONVERT_CONNECTION), from its own (VNS_CONVERT_SOURCE). keep_login: its login goes with it, by its name
	and password; else a new one (VNS_CONVERT_LOGIN_PASSWORD)"""
	target, source = _server_settings("VNS_CONVERT_CONNECTION"), _server_settings("VNS_CONVERT_SOURCE")
	password = os.environ.pop("VNS_CONVERT_LOGIN_PASSWORD", "")
	folder, service_text = working_folder(system, unit)
	umbraco = is_umbraco(system, folder)
	entry = site_connection(system, folder, key)
	engine = entry["engine"]
	if engine == "sqlite":
		raise Stop("The site uses SQLite: Convert puts it on a server.")
	if ("postgres" if target.get("engine") == "postgres" else "mssql") != engine:
		raise Stop(f"Move it to another {ENGINES[engine]}: Convert changes its kind.")
	need_tools(system, engine)
	old = server_database(entry["value"], engine)
	if keep_login:
		login, password = login_of(entry["value"])
	_move_names(database, login, password)
	running = active(system, unit)
	steps = _plan(system, [*([f"Stop {unit}"] if running else []), f"Copy {old} to {database} on {target['host']}", "Change the connection string",
	                       f"Start {unit} on {database}", f"Delete {old} and its backups on {source['host']}"])
	if running:
		system.step(steps[0])
		system.run("systemctl", "stop", f"{service_name(unit)}.service")
	before, moved = {}, False
	try:
		system.step(steps[-4])
		move_database(system, engine, source, target, old, database, login, password)
		moved = True
		system.step(steps[-3])
		value = mssql_string(target, database, login, password) if engine == "mssql" else postgres_string(target, database, login, password)
		before = change_settings(system, folder, entry, value, lambda previous: None)
		system.step(steps[-2])
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if moved:
			drop_server(system, engine, target, database, login)
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {old} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	# Nothing is kept: a move back is a move. Its conversions find the database where it is now
	rehome(system, unit, {"engine": engine, "host": source["host"], "port": source.get("port"), "database": old},
	       {"engine": engine, "connection": target.get("id", ""), "host": target["host"], "port": target.get("port"), "database": database, "login": login})
	system.step(steps[-1])
	try:
		drop_old(system, engine, source, old, backups=True)
	except Stop as error:
		system.warn(f"{old} could not be deleted on {source['host']}: {error}. Delete it on the SQL Server page.")
	system.say(f"{unit} runs on {database} on {target['host']}.")


def relay_move(system, environment, arguments, values):
	"""A site on another server moved to another database server: there it is stopped, here its database copied, there
	its connection string changed and it started. What fails puts it back: there as it was, here the new one deleted"""
	unit = arguments[0]
	keep_login = "--keep-login" in arguments
	rest = [item for item in arguments[1:] if item != "--keep-login"]
	opts = dict(zip(rest[0::2], rest[1::2]))
	try:
		target = json.loads(values[0] or "null")
	except ValueError:
		target = None
	source = _server_settings("VNS_CONVERT_SOURCE")
	if not isinstance(target, dict) or not {"host", "username", "password"} <= target.keys():
		raise Stop("Give the connection to the new server in VNS_CONVERT_CONNECTION.")
	database, by = opts.get("--database", ""), opts.get("--by", "")
	engine = "postgres" if target.get("engine") == "postgres" else "mssql"
	if not _here(engine):
		need_tools(system, engine)
	stopped = _remote_json(system, environment, ("site", "convert-relay", "stop", unit, *(["--key", opts["--key"]] if opts.get("--key") else [])))
	if stopped.get("engine") != engine:
		if stopped.get("running"):
			_remote_run(system, environment, ("site", "convert-relay", "resume", unit), progress=False, capture=True)
		raise Stop(f"Move it to another {ENGINES.get(stopped.get('engine'), 'server')}: Convert changes its kind.")
	login, password = (stopped.get("login", ""), stopped.get("password", "")) if keep_login else (opts.get("--login", ""), values[2])
	old = stopped.get("database", "")
	steps = _plan(system, [f"Stop {unit} on {environment.IP}", f"Copy {old} to {database} on {target['host']}",
	                       f"Change the connection string and start {unit} on {database}", f"Delete {old} and its backups on {source['host']}"])
	system.step(steps[0])
	switching = False
	try:
		_move_names(database, login, password)
		system.step(steps[1])
		move_database(system, engine, source, target, old, database, login, password)
		system.step(steps[2])
		switching = True
		origin = json.dumps({"engine": engine, "connection": source.get("id", ""), "host": source["host"], "port": source.get("port"), "database": old})
		done = _remote_run(system, environment, ("site", "convert-relay", "switch", unit, "--engine", engine, "--database", database, "--login", login,
		                                         "--key", stopped["key"], "--by", by, "--origin", origin, *(["--running"] if stopped.get("running") else [])),
		                   (values[0], "", password))
		if not done.ok:
			raise Stop("", done.returncode or 1)
		switching = False
	except BaseException as error:
		if switching:
			# There it put itself back and started on the old database: here the new one goes
			drop_server(system, engine, target, database, login)
		elif stopped.get("running"):
			# The copy failed (move_database deleted what it made): the site started there again
			_remote_run(system, environment, ("site", "convert-relay", "resume", unit), progress=False, capture=True)
			system.say(f"{unit} runs on {old} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	# It runs on the new one: the old one goes from its server, with the logins only it had
	system.step(steps[3])
	try:
		drop_old(system, engine, source, old, backups=True)
	except Stop as error:
		system.warn(f"{old} could not be deleted on {source['host']}: {error}. Delete it on the SQL Server page.")


def rehome(system, unit, old, new):
	"""After a move: the conversions of the site whose database was the one moved name it where it is now, so a roll
	back deletes that one"""
	items = records(system)
	# This server's own by any of its names
	host = lambda side: "localhost" if (side.get("host") or "").strip("[]").casefold() in (".", "localhost", "127.0.0.1", "::1", "(local)") \
		else (side.get("host") or "").strip("[]").casefold()
	changed = False
	for item in items:
		side = item.get("to") or {}
		if item.get("unit") == unit and side.get("engine") == old.get("engine") and (side.get("database") or "").lower() == (old.get("database") or "").lower() \
				and host(side) == host(old) and str(side.get("port") or "") in ("", str(old.get("port") or "")):
			item["to"] = {**side, **{key: value for key, value in new.items() if value is not None}}
			changed = True
	if changed:
		write_json(system.path(RECORD), items, private=True)


def relay_stop(system, unit, key=""):
	"""(On the site's server, for a move) The site stopped; its database, login and password as JSON for the Harbor that
	moves it"""
	folder, _ = working_folder(system, unit)
	entry = site_connection(system, folder, key)
	running = active(system, unit)
	if running:
		system.run("systemctl", "stop", f"{service_name(unit)}.service")
	login, password = login_of(entry["value"]) if entry["engine"] != "sqlite" else ("", "")
	print(json.dumps({"running": running, "key": entry["key"], "engine": entry["engine"], "login": login, "password": password,
	                  "database": server_database(entry["value"], entry["engine"]) if entry["engine"] != "sqlite" else ""}), flush=True)


def relay_login(system, unit, key=""):
	"""(On the site's server, for an export that deletes it) Its database, login and password as JSON, the site left
	running: Restore makes the login again with them"""
	folder, _ = working_folder(system, unit)
	entry = site_connection(system, folder, key)
	login, password = login_of(entry["value"]) if entry["engine"] != "sqlite" else ("", "")
	print(json.dumps({"engine": entry["engine"], "login": login, "password": password,
	                  "database": server_database(entry["value"], entry["engine"]) if entry["engine"] != "sqlite" else ""}), flush=True)


def relay_switch(system, unit, engine, database, login, key, running, by, origin):
	"""(On the site's server, for a move) Its connection string to the database moved, the site started and asked for its
	home page, the move kept here. What fails is put back, and the site started on its old database again"""
	target = _server_settings("VNS_CONVERT_CONNECTION")
	password = os.environ.pop("VNS_CONVERT_LOGIN_PASSWORD", "")
	try:
		old = json.loads(origin)
	except ValueError:
		raise Stop("The old database is not given.") from None
	folder, service_text = working_folder(system, unit)
	entry = site_connection(system, folder, key)
	before = {}
	try:
		value = mssql_string(target, database, login, password) if engine == "mssql" else postgres_string(target, database, login, password)
		before = change_settings(system, folder, entry, value, lambda previous: None)
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {old.get('database')} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	# Nothing is kept: a move back is a move. Its conversions find the database where it is now
	rehome(system, unit, old, {"engine": engine, "connection": target.get("id", ""), "host": target["host"], "port": target.get("port"),
	                           "database": database, "login": login})
	system.say(f"{unit} runs on {database} on {target['host']}.")


def _send(system, environment, local, path):
	"""A file here sent to the environment's server, as root there, never held in memory"""
	import subprocess
	from harbor.environments import settings as env_settings
	argv, env = env_settings.ssh_command(environment)
	sudo = "" if environment.USER == "root" else "sudo -n "
	command = f"{sudo}sh -c " + shlex.quote(f"mkdir -p {shlex.quote(os.path.dirname(path))} && chmod 700 {shlex.quote(os.path.dirname(path))} && cat > {shlex.quote(path)}")
	with open(local, "rb") as stream:
		result = subprocess.run([*argv, f"{environment.USER}@{environment.IP}", command], stdin=stream, stderr=subprocess.PIPE, env={**os.environ, **env})
	if result.returncode != 0:
		raise Stop(f"The SQLite file could not be sent there: {result.stderr.decode(errors='replace').strip()[-400:]}")


INCOMING = "/var/lib/harbor/convert-incoming"


def relay_to_sqlite(system, environment, unit, engine, options, values):
	"""A site on another server, whose database server's tools are not there, to SQLite: there it is stopped, here its
	database read into a SQLite file sent there, there the file put in the site, its connection string changed and it
	started. What fails puts it back as it was"""
	import tempfile
	opts = dict(zip(options[0::2], options[1::2]))
	try:
		settings = json.loads(values[0] or "null")
	except ValueError:
		settings = None
	if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
		raise Stop("Give the connection to the server in VNS_CONVERT_CONNECTION.")
	filename = opts.get("--file", "")
	if filename and not FILE_NAME.fullmatch(filename):
		raise Stop("A SQLite file's name has letters, digits, . _ and -, and ends in .sqlite.db, .sqlite, .sqlite3 or .db.")
	steps = _plan(system, [f"Stop {unit} on {environment.IP}", "Convert its database to SQLite here", f"Send it to {environment.IP}",
	                       f"Change the connection string and start {unit} on SQLite"])
	system.say(f"{environment.IP} has not the {ENGINES[engine]} tools: this Harbor converts the data")
	system.step(steps[0])
	stopped = _remote_json(system, environment, ("site", "convert-relay", "stop", unit, *(["--key", opts["--key"]] if opts.get("--key") else [])))
	folder = tempfile.mkdtemp(prefix="harbor-convert-")
	copy = os.path.join(folder, "site.sqlite.db")
	incoming = f"{INCOMING}/{uuid.uuid4().hex}.sqlite.db"
	switching = False
	try:
		if stopped.get("engine") != engine:
			raise Stop(f"The site's database is on {ENGINES.get(stopped.get('engine'), 'another server')}: choose a connection to it.")
		system.step(steps[1])
		result = converter().to_sqlite(engine, settings, stopped["database"], Path(copy), log=system.say)
		system.say(f"{result['tables']} tables, {result['rows']} rows")
		system.step(steps[2])
		_send(system, environment, copy, incoming)
		system.step(steps[3])
		switching = True
		origin = json.dumps({"engine": engine, "connection": settings.get("id", ""), "host": settings["host"], "port": settings.get("port"),
		                     "database": stopped["database"]})
		done = _remote_run(system, environment, ("site", "convert-relay", "sqlite", unit, "--key", stopped["key"], "--by", opts.get("--by", ""),
		                                         "--incoming", incoming, *(["--file", filename] if filename else []), "--origin", origin,
		                                         *(["--running"] if stopped.get("running") else [])))
		if not done.ok:
			raise Stop("", done.returncode or 1)
	except BaseException as error:
		if not switching and stopped.get("running"):
			_remote_run(system, environment, ("site", "convert-relay", "resume", unit), progress=False, capture=True)
			system.say(f"{unit} runs on {stopped.get('database')} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	finally:
		import shutil
		shutil.rmtree(folder, ignore_errors=True)


def relay_sqlite(system, unit, key, by, incoming, filename, running, origin):
	"""(On the site's server, for a relay to SQLite) The SQLite file the Harbor that asked made, put in the site; its
	connection string changed, it started and asked for its home page, the conversion kept here. What fails is put back"""
	try:
		old = json.loads(origin)
	except ValueError:
		raise Stop("The old database is not given.") from None
	if not incoming.startswith(INCOMING + "/") or ".." in incoming or not system.path(incoming).is_file():
		raise Stop("The SQLite file sent is not there.")
	folder, service_text = working_folder(system, unit)
	umbraco = is_umbraco(system, folder)
	entry = site_connection(system, folder, key)
	data = f"{folder}/umbraco/Data" if umbraco else f"{folder}/App_Data"
	database = old.get("database") or "site"
	if not filename:
		filename = "Umbraco.sqlite.db" if umbraco else re.sub(r"[^A-Za-z0-9._-]", "_", database) + ".sqlite.db"
		number = 2
		while system.path(f"{data}/{filename}").exists():
			filename = (f"Umbraco-{number}.sqlite.db" if umbraco else re.sub(r"[^A-Za-z0-9._-]", "_", database) + f"-{number}.sqlite.db")
			number += 1
	path = f"{data}/{filename}"
	before, placed = {}, False
	try:
		if system.path(path).exists():
			raise Stop(f"{os.path.relpath(path, folder)} is there already. Choose another name.")
		target = system.path(data)
		if not target.is_dir():
			owner = system.path(folder).stat()
			target.mkdir(parents=True)
			os.chown(target, owner.st_uid, owner.st_gid)
		os.rename(system.path(incoming), system.path(path))
		placed = True
		owner = target.stat()
		os.chown(system.path(path), owner.st_uid, owner.st_gid)
		os.chmod(system.path(path), 0o660)
		before = change_settings(system, folder, entry, sqlite_string(folder, path, umbraco),
		                         lambda previous: PROVIDERS["sqlite"] if umbraco or previous else None)
		start_and_check(system, unit, service_text)
	except BaseException as error:
		put_back(system, folder, before)
		if placed:
			for name in (path, path + "-wal", path + "-shm"):
				system.path(name).unlink(missing_ok=True)
		system.path(incoming).unlink(missing_ok=True)
		if running:
			system.run("systemctl", "start", f"{service_name(unit)}.service", check=False)
			system.say(f"{unit} runs on {database} again, as before")
		if isinstance(error, (RuntimeError, ValueError, OSError)):
			raise Stop(str(error)) from None
		raise
	record(system, {"id": uuid.uuid4().hex, "unit": unit, "folder": folder, "key": entry["key"], "at": int(time.time()), "by": by,
	                "from": {key: old.get(key) for key in ("engine", "connection", "host", "port", "database")},
	                "previous": entry["values"], "to": {"engine": "sqlite", "path": path}})
	system.say(f"{unit} runs on {os.path.relpath(path, folder)}. Its database {database} is kept: Settings, Convert DB deletes it.")


def transfer_database(system, old, new, login, move=False):
	"""harbor site convert-transfer: a database copied (or moved) to another server of its kind with a login of its own,
	no site with it (SQL Server page's Copy and Move of one no site runs on). The old server in VNS_CONVERT_SOURCE, the
	new in VNS_CONVERT_CONNECTION, the login's password in VNS_CONVERT_LOGIN_PASSWORD. Moved: the old one deleted once
	the new one is there, with the logins only it had"""
	target, source = _server_settings("VNS_CONVERT_CONNECTION"), _server_settings("VNS_CONVERT_SOURCE")
	password = os.environ.pop("VNS_CONVERT_LOGIN_PASSWORD", "")
	engine = "postgres" if target.get("engine") == "postgres" else "mssql"
	if ("postgres" if source.get("engine") == "postgres" else "mssql") != engine:
		raise Stop("Copy it to a server of its own kind.")
	need_tools(system, engine)
	_move_names(new, login, password)
	steps = _plan(system, [f"Copy {old} to {new} on {target['host']}", *([f"Delete {old} and its backups on {source['host']}"] if move else [])])
	system.step(steps[0])
	move_database(system, engine, source, target, old, new, login, password)
	if move:
		system.step(steps[1])
		try:
			drop_old(system, engine, source, old, backups=True)
		except Stop as error:
			system.warn(f"{old} could not be deleted on {source['host']}: {error}. Delete it on the SQL Server page.")
	system.say(f"{new} is on {target['host']}" + (f"; {old} is deleted from {source['host']}." if move else "."))
