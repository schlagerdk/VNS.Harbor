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
	return to_server(system, unit, folder, service_text, entry, settings, to, database, login, password, umbraco, by)


def _plan(system, steps):
	system.plan(steps)
	return steps


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
	                "to": {"engine": engine, "connection": settings.get("id", ""), "host": settings["host"], "database": database, "login": login}})
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
	                "from": {"engine": entry["engine"], "connection": settings.get("id", ""), "host": settings["host"], "database": database},
	                "previous": entry["values"], "to": {"engine": "sqlite", "path": path}})
	system.say(f"{unit} runs on {os.path.relpath(path, folder)}. Its database {database} is kept: Settings, Convert deletes it.")


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


def delete_old(system, identifier):
	"""The database a site ran on before it was converted, deleted: a SQLite file (with its -wal and -shm), or a
	database on a server (with the logins only it had, as the SQL Server page deletes one), the connection to it in
	VNS_CONVERT_CONNECTION. Never one the site uses again"""
	items = records(system)
	item = next((item for item in items if item.get("id") == identifier), None)
	if not item:
		raise Stop("That conversion is not known here.")
	old = item["from"]
	if old["engine"] != "sqlite":
		try:
			settings = json.loads(os.environ.pop("VNS_CONVERT_CONNECTION", "") or "null")
		except ValueError:
			settings = None
		if not isinstance(settings, dict) or not {"host", "username", "password"} <= settings.keys():
			raise Stop("The connection to the old database's server is not known here any more.")
		database = old["database"]
		try:
			entry = site_connection(system, item["folder"], item["key"])
		except Stop:
			entry = None
		if entry and entry["engine"] == old["engine"] and server_database(entry["value"], entry["engine"]).lower() == database.lower():
			raise Stop(f"{item['unit']} uses {database} again: it is not deleted.")
		system.plan([f"Delete the database {database}"])
		system.step(f"Delete the database {database}")
		convert = converter()
		try:
			if old["engine"] == "mssql":
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
				databases.append({"engine": side["engine"], "connection": side.get("connection", ""), "database": side["database"],
				                  "login": side.get("login", "") if side is item["to"] else ""})
	return files, databases


def rollback_to(system, identifier):
	"""Back to the database the site ran on before this conversion, in one go, whatever was converted after it: nothing
	is converted. The site is stopped, gets the connection string it had then, its SQLite file (kept as .bak) takes its
	name again, and it is started and asked for its home page. Then what it ran on since is deleted: each database and
	file the conversions made, and those they kept. A file to delete is moved aside until the site runs, and a database
	on a server deleted only then: when the start fails, all is put back as it was. The servers' connections in
	VNS_CONVERT_CONNECTIONS ({id: connection})"""
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
		              server=fields.get("server") or fields.get("data source") or fields.get("host", ""))
	return result
