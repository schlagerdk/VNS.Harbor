"""A site's database converted between SQLite and Microsoft SQL Server or PostgreSQL (phase 11 of dev/HARBOR.md).

Built on VNS.SQLConvert's rules. A database is read into tables of neutral columns (int, bigint, bool, text, guid,
datetime, date, time, decimal, float, blob), each table's rows read one page at a time, and written to the other kind:
SQLite through Python's sqlite3, Microsoft SQL Server as a script run by sqlcmd, PostgreSQL as a script run by psql in
one transaction. SQLite's types are loose (Umbraco and EF Core write INTEGER and TEXT for most), so a column's kind is
guessed from its name, its declared type and its values, as VNS.SQLConvert did, with Umbraco's own columns named.

Foreign keys are not carried over (the sites keep their integrity themselves); tables, columns, primary keys, identity
columns, defaults that have a meaning on both sides, and indexes are. The rows of every table are counted on both sides
when it is done.

Data never reaches a line of its own in a script: sqlcmd reads GO and :r at the start of a line, so a line break in a
text goes as NCHAR(10)."""
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import uuid

KINDS = ("int", "bigint", "bool", "text", "guid", "datetime", "date", "time", "decimal", "float", "blob")
INT32 = (-2 ** 31, 2 ** 31 - 1)
# A page of rows read from a server: about this many bytes
PAGE_BYTES = 4 * 1024 * 1024
# Rows in one INSERT, and INSERTs in one batch of a script
INSERT_ROWS = 100
BATCH_INSERTS = 10

_UMBRACO_GUID_COLUMNS = {
	"cmsdictionary": {"id", "parent"},
	"cmslanguagetext": {"uniqueid"},
	"cmspropertytype": {"uniqueid"},
	"cmspropertytypegroup": {"uniqueid"},
	"umbracoaccess": {"id"},
	"umbracoaccessrule": {"id", "accessid"},
	"umbracoaudit": {"performinguserkey", "affecteduserkey"},
	"umbracocontentschedule": {"id"},
	"umbracocreatedpackageschema": {"packageid"},
	"umbracodocumenturl": {"uniqueid"},
	"umbracodocumenturlalias": {"uniqueid"},
	"umbracoexternallogin": {"userormemberkey"},
	"umbracoexternalmember": {"key"},
	"umbracolongrunningoperation": {"id"},
	"umbraconode": {"uniqueid", "nodeobjecttype"},
	"umbracoredirecturl": {"id", "contentkey"},
	"umbracorelationtype": {"typeuniqueid", "parentobjecttype", "childobjecttype"},
	"umbracotwofactorlogin": {"userormemberkey"},
	"umbracouser": {"key"},
	"umbracouserdata": {"key", "userkey"},
	"umbracousergroup": {"key"},
	"umbracousergroup2granularpermission": {"usergroupkey", "uniqueid"},
	"umbracousergroup2permission": {"usergroupkey"},
	"umbracouserlogin": {"sessionid"},
	"umbracowebhook": {"key"},
	"umbracowebhook2contenttypekeys": {"entitykey"},
	"umbracowebhooklog": {"webhookkey", "key"},
	"umbracowebhookrequest": {"webhookkey"},
	"umediaops_unusedmediaitems": {"scanid"},
	"umediaops_unusedmediascans": {"id"},
}
# Keys that are text, not GUIDs, though their values may look like them
_UMBRACO_TEXT_KEY_TABLES = {"cmsdictionary", "umbracokeyvalue"}
_UMBRACO_TEXT_COLUMNS = {
	"umbracoopeniddictapplications": {"id", "clientid"},
	"umbracoopeniddictauthorizations": {"id", "applicationid"},
	"umbracoopeniddictscopes": {"id"},
	"umbracoopeniddicttokens": {"id", "applicationid", "authorizationid", "referenceid"},
}
# Text columns SQLite compares without case, as SQL Server does: Umbraco joins and looks up by them
_NOCASE_COLUMNS = {
	"umbracouser": {"key", "username", "userlogin", "useremail"},
	"umbracousergroup": {"key", "usergroupalias"},
	"umbracouserdata": {"userkey", "group", "identifier"},
	"umbracousergroup2permission": {"usergroupkey", "permission"},
	"umbracouser2clientid": {"clientid"},
	"umbracoopeniddictapplications": {"id", "clientid"},
	"umbracoopeniddictauthorizations": {"id", "applicationid", "subject", "status", "type"},
	"umbracoopeniddictscopes": {"id", "name"},
	"umbracoopeniddicttokens": {"id", "applicationid", "authorizationid", "subject", "status", "type", "referenceid"},
}
_BOOL_NAMES = {
	"userdisabled", "usernoconsole", "published", "edited", "current", "isdraft", "isprimary", "iselement",
	"allowatroot", "isdefault", "membercanedit", "viewonprofile", "issensitive", "labelontop", "preventcleanup",
	"istrashed", "trashed", "isdependency", "isactive", "isschedulingpublisher", "enabled", "hasaccesstoalllanguages",
	"exceptionoccured",
}
_BOOL_MARKERS = ("is", "has", "can", "allow", "enabled", "disabled", "published", "edited", "current", "mandatory",
                 "prevent", "approved", "locked", "primary", "element", "trashed", "dual")
_DATETIME_NAMES = {"created", "updated", "createdat", "updatedat", "modified", "modifiedat", "deletedat"}
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STRICT_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
DATETIME_TEXT = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?")
TIME_TEXT = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?")


def _sibling(name, module):
	spec = importlib.util.spec_from_file_location(module, Path(__file__).with_name(name))
	found = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(found)
	return found


_modules = {}


def sql_admin():
	if "sql" not in _modules:
		_modules["sql"] = _sibling("sql_admin.py", "vns_convert_sql_admin")
	return _modules["sql"]


def pg_admin():
	if "pg" not in _modules:
		_modules["pg"] = sql_admin().pg()
	return _modules["pg"]


def say(log, text):
	if log:
		log(text)


# ---- neutral columns
def column(name, kind, *, nullable=True, length=None, precision=None, scale=None, default=None, nocase=False):
	return {"name": name, "kind": kind, "nullable": nullable, "length": length, "precision": precision, "scale": scale,
	        "default": default, "nocase": nocase}


def neutral_default(text):
	"""A column's default in words both sides understand: now, newid, a number, a text, or None for one that has no
	meaning on the other side (a sequence, a function of its own)"""
	if text is None:
		return None
	raw = str(text).strip()
	while raw.startswith("(") and raw.endswith(")") and raw[1:-1].strip():
		inner = raw[1:-1].strip()
		# (1) + (2) is not one parenthesis round all of it
		depth = 0
		for char in inner:
			depth += 1 if char == "(" else -1 if char == ")" else 0
			if depth < 0:
				break
		if depth < 0:
			break
		raw = inner
	raw = re.sub(r"::[a-z ]+(?:\(\d+(?:,\s*\d+)?\))?$", "", raw, flags=re.IGNORECASE)
	upper = raw.upper()
	if upper == "NULL":
		return None
	if re.fullmatch(r"(?:CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME|NOW\(\)|NOW|GETDATE\(\)|GETUTCDATE\(\)|SYSDATETIME\(\)|"
	                r"SYSUTCDATETIME\(\)|LOCALTIMESTAMP|TRANSACTION_TIMESTAMP\(\)|(?:DATE|DATETIME|TIME)\([^()]*\)|"
	                r"TIMEZONE\('UTC'::TEXT, NOW\(\)\)|\(NOW\(\) AT TIME ZONE 'UTC'\))", upper):
		return {"now": True}
	if upper in ("NEWID()", "NEWSEQUENTIALID()", "GEN_RANDOM_UUID()", "UUID_GENERATE_V4()"):
		return {"newid": True}
	if upper in ("TRUE", "FALSE"):
		return {"number": "1" if upper == "TRUE" else "0"}
	if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw):
		return {"number": raw.lstrip("+")}
	found = re.fullmatch(r"N?'((?:[^']|'')*)'", raw, flags=re.DOTALL) or re.fullmatch(r'"((?:[^"]|"")*)"', raw, flags=re.DOTALL)
	if found:
		quote = "'" if raw.lstrip("N")[0] == "'" else '"'
		return {"text": found[1].replace(quote * 2, quote)}
	return None


# ---- reading SQLite
def sqlite_identifier(name):
	return '"' + name.replace('"', '""') + '"'


def _sqlite_tables(connection):
	return [row[0] for row in connection.execute(
		"SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]


def _text_affinity(declared):
	return not declared or any(token in declared for token in ("CHAR", "CLOB", "TEXT", "JSON"))


def _guess_guid(declared, table, name, samples):
	if any(token in declared for token in ("UNIQUEIDENTIFIER", "UUID", "GUID")):
		return True
	if not _text_affinity(declared) or name in _UMBRACO_TEXT_COLUMNS.get(table, ()):
		return False
	if name in _UMBRACO_GUID_COLUMNS.get(table, ()):
		return True
	if name == "key" and table in _UMBRACO_TEXT_KEY_TABLES:
		return False
	if name == "uniqueid":
		return True
	text = [sample for sample in samples if isinstance(sample, str)]
	ratio = sum(1 for sample in text if STRICT_GUID.fullmatch(sample.strip())) / len(text) if text else 0.0
	if name == "key" and table.startswith("umbraco"):
		return ratio >= 0.95
	if name.endswith("key") and table.startswith("umbraco") and text:
		return ratio >= 0.9
	if ("guid" in name or "uuid" in name) and text:
		return ratio >= 0.98
	# EF Core writes a Guid as TEXT: a column whose every value is one, named as an id or a key
	if (name.endswith("id") or name.endswith("key")) and len(text) >= 3 and len(text) == len(samples):
		return ratio == 1.0
	return False


def _guess_datetime(declared, name, samples):
	if "DATE" in declared or "TIME" in declared:
		return "date" if "DATE" in declared and "TIME" not in declared else "time" if declared == "TIME" else "datetime"
	if not _text_affinity(declared):
		return None
	if not ("date" in name or name.endswith("time") or "timestamp" in name or name in _DATETIME_NAMES
	        or name.endswith("created") or name.endswith("updated")):
		return None
	text = [sample for sample in samples if isinstance(sample, str) and sample.strip()]
	if not samples:
		return "datetime"
	if not text:
		return None
	ratio = sum(1 for sample in text if DATETIME_TEXT.fullmatch(sample.strip())) / len(text)
	return "datetime" if ratio >= 0.7 else None


def _guess_bool(declared, name, default, samples):
	if "BOOL" in declared or "BIT" == declared:
		return True
	if declared and not any(token in declared for token in ("INT", "NUM", "DEC", "REAL", "FLOA", "DOUB")):
		return False
	if not (name.startswith("is") or name.startswith("has") or name in _BOOL_NAMES or any(marker in name for marker in _BOOL_MARKERS)):
		return False
	found = neutral_default(default)
	if found and found.get("number") in ("0", "1"):
		return True
	values = [str(sample).strip().lower() for sample in samples if sample is not None]
	return bool(values) and sum(1 for value in values if value in ("0", "1", "true", "false")) / len(values) >= 0.95


def read_sqlite(connection, log=None):
	"""The tables of a SQLite database, with each column's kind"""
	tables = []
	for name in _sqlite_tables(connection):
		quoted = sqlite_identifier(name)
		info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
		if not info:
			continue
		lower = name.lower()
		primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
		declared_types = {row[1]: str(row[2] or "").strip().upper() for row in info}
		# Numbered by the database only with AUTOINCREMENT, as Umbraco and EF Core write an identity column. A single
		# INTEGER PRIMARY KEY without it is given its value by the site (Umbraco's umbracoDocumentVersion takes its
		# umbracoContentVersion's id, umbracoLock its lock's): on SQL Server an IDENTITY there refuses the site's inserts
		create = (connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() or [""])[0] or ""
		identity = primary[0] if (len(primary) == 1 and declared_types[primary[0]] == "INTEGER"
		                          and re.search(r"\bAUTOINCREMENT\b", create, re.IGNORECASE)) else None
		columns = []
		for _cid, column_name, declared, notnull, default, _pk in info:
			declared = str(declared or "").strip().upper()
			key = column_name.lower()
			quoted_column = sqlite_identifier(column_name)
			samples = [row[0] for row in connection.execute(
				f"SELECT {quoted_column} FROM {quoted} WHERE {quoted_column} IS NOT NULL LIMIT 200")]
			length = re.search(r"\(\s*(\d+)\s*\)", declared)
			nullable = not notnull and column_name not in primary
			if column_name == identity:
				kind = "bigint" if "BIG" in declared else "int"
			elif _guess_guid(declared, lower, key, samples):
				kind = "guid"
			elif _guess_bool(declared, key, default, samples):
				kind = "bool"
			elif _guess_datetime(declared, key, samples):
				kind = _guess_datetime(declared, key, samples)
			elif "INT" in declared:
				kind = "bigint" if "BIG" in declared else "int"
			elif "BLOB" in declared or (not declared and samples and all(isinstance(sample, bytes) for sample in samples)):
				kind = "blob"
			elif any(token in declared for token in ("REAL", "FLOA", "DOUB")):
				kind = "float"
			elif any(token in declared for token in ("DEC", "NUM", "MONEY")):
				kind = "decimal"
			else:
				kind = "text"
			if kind == "int":
				low, high = connection.execute(f"SELECT MIN({quoted_column}), MAX({quoted_column}) FROM {quoted}").fetchone()
				if any(isinstance(value, int) and not INT32[0] <= value <= INT32[1] for value in (low, high)):
					kind = "bigint"
			precision = scale = None
			if kind == "decimal":
				found = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", declared)
				precision, scale = (int(found[1]), int(found[2] or 0)) if found else (38, 10)
			nocase = kind == "guid" or kind == "text" and (
				key in _NOCASE_COLUMNS.get(lower, ()) or key in ("key", "id", "uniqueid", "nodeobjecttype")
				or key.endswith("key") or key.endswith("id"))
			columns.append(column(column_name, kind, nullable=nullable, length=int(length[1]) if length and kind == "text" else None,
			                      precision=precision, scale=scale, default=None if column_name == identity else neutral_default(default),
			                      nocase=nocase))
		indexes = []
		for _seq, index_name, unique, origin, partial in connection.execute(f"PRAGMA index_list({quoted})").fetchall():
			if origin == "pk":
				continue
			parts = connection.execute(f"PRAGMA index_info({sqlite_identifier(index_name)})").fetchall()
			names = [part[2] for part in parts]
			if partial or not names or any(part is None for part in names):
				say(log, f"Index {index_name} on {name} is left out: it is partial or on an expression")
				continue
			# A UNIQUE in the table's own words is an index SQLite names itself, by a name no one else may give
			if index_name.lower().startswith("sqlite_"):
				index_name = "UQ_" + name + "_" + "_".join(names)
			indexes.append({"name": index_name, "unique": bool(unique), "columns": names})
		tables.append({"name": name, "columns": columns, "primary_key": primary, "identity": identity, "indexes": indexes})
	return tables


def sqlite_rows(connection, table):
	names = ", ".join(sqlite_identifier(item["name"]) for item in table["columns"])
	order = " ORDER BY " + ", ".join(sqlite_identifier(name) for name in table["primary_key"]) if table["primary_key"] else ""
	cursor = connection.execute(f"SELECT {names} FROM {sqlite_identifier(table['name'])}{order}")
	while True:
		rows = cursor.fetchmany(1000)
		if not rows:
			return
		yield from rows


def sqlite_count(connection, name):
	return connection.execute(f"SELECT COUNT(*) FROM {sqlite_identifier(name)}").fetchone()[0]


def text_lengths(connection, table, names):
	"""The longest value of each named text column, in characters"""
	if not names:
		return {}
	row = connection.execute("SELECT " + ", ".join(f"MAX(LENGTH({sqlite_identifier(name)}))" for name in names)
	                         + f" FROM {sqlite_identifier(table['name'])}").fetchone()
	return {name: int(value or 0) for name, value in zip(names, row)}


def snapshot(path, folder):
	"""A copy of a SQLite database as it is, with what its WAL holds, to read from while the site's own stays as it was"""
	copy = Path(folder) / "source.sqlite"
	source = sqlite3.connect("file:" + urllib.parse.quote(str(Path(path).resolve())) + "?mode=ro", uri=True)
	target = sqlite3.connect(copy)
	try:
		source.backup(target)
	finally:
		source.close()
		target.close()
	return copy


# ---- values
def _fraction(text):
	return (text or "").ljust(7, "0")[:7]


def datetime_parts(value):
	"""(date, time with 7 decimals) of a value that holds a date and time: ISO text, a datetime, or seconds or
	milliseconds since 1970. None when it is not one"""
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d"), value.strftime("%H:%M:%S.") + f"{value.microsecond:06d}0"
	if isinstance(value, (int, float)) and not isinstance(value, bool) or isinstance(value, str) and re.fullmatch(r"\d{10}|\d{13}", value.strip()):
		number = float(value)
		seconds = number / 1000 if abs(number) >= 1e11 else number
		try:
			moment = datetime(1970, 1, 1) + timedelta(seconds=seconds)
		except OverflowError:
			return None
		return datetime_parts(moment)
	if not isinstance(value, str):
		return None
	found = DATETIME_TEXT.fullmatch(value.strip())
	if not found:
		return None
	year, month, day, hour, minute, second, fraction, zone = found.groups()
	try:
		moment = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
	except ValueError:
		return None
	if zone and zone not in ("Z", "+00:00", "+0000", "-00:00"):
		sign = 1 if zone[0] == "+" else -1
		digits = zone[1:].replace(":", "")
		try:
			moment = moment - sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
		except OverflowError:
			return None
	return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M:%S.") + _fraction(fraction)


def coerce(value, item):
	"""A value as its column's kind holds it: int, bool, str, Decimal, float, bytes, or None. ValueError when it cannot
	be one"""
	if value is None:
		return None
	kind = item["kind"]
	if kind in ("int", "bigint"):
		if isinstance(value, bool):
			return int(value)
		if isinstance(value, int):
			return value
		if isinstance(value, (float, Decimal)) and value == int(value):
			return int(value)
		if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
			return int(value)
		raise ValueError(f"{value!r} is not a whole number")
	if kind == "bool":
		if isinstance(value, str):
			text = value.strip().lower()
			if text in ("1", "true", "t", "yes", "y"):
				return True
			if text in ("0", "false", "f", "no", "n", ""):
				return False
			raise ValueError(f"{value!r} is not true or false")
		return bool(value)
	if kind == "guid":
		if isinstance(value, bytes) and len(value) == 16:
			return str(uuid.UUID(bytes_le=value)).upper()
		text = str(value).strip().strip("{}")
		if not GUID.fullmatch(text):
			return None
		return text.upper()
	if kind in ("datetime", "date"):
		found = datetime_parts(value.decode() if isinstance(value, bytes) else value)
		if not found:
			return None
		return found[0] if kind == "date" else found[0] + " " + found[1]
	if kind == "time":
		found = TIME_TEXT.fullmatch(str(value).strip())
		if not found:
			parts = datetime_parts(value)
			return parts[1] if parts else None
		return f"{int(found[1]):02d}:{found[2]}:{found[3] or '00'}.{_fraction(found[4])}"
	if kind == "decimal":
		try:
			number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
		except InvalidOperation:
			raise ValueError(f"{value!r} is not a number") from None
		return None if not number.is_finite() else number
	if kind == "float":
		try:
			number = float(value)
		except (TypeError, ValueError):
			raise ValueError(f"{value!r} is not a number") from None
		return None if math.isnan(number) or math.isinf(number) else number
	if kind == "blob":
		return bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode("utf-8")
	if isinstance(value, bytes):
		return value.decode("utf-8", errors="replace")
	if isinstance(value, bool):
		return "1" if value else "0"
	if isinstance(value, float):
		return repr(value)
	return str(value)


def coerce_row(row, table, problems):
	values = []
	for value, item in zip(row, table["columns"]):
		try:
			found = coerce(value, item)
		except ValueError as error:
			raise ValueError(f"{table['name']}.{item['name']}: {error}") from None
		if found is None and value is not None:
			problems[(table["name"], item["name"])] = problems.get((table["name"], item["name"]), 0) + 1
		if found is None and not item["nullable"]:
			found = _filler(item)
		values.append(found)
	return values


def _filler(item):
	"""What a value that could not be read becomes in a column that must have one (as VNS.SQLConvert did: 1900-01-01)"""
	return {"datetime": "1900-01-01 00:00:00.0000000", "date": "1900-01-01", "time": "00:00:00.0000000",
	        "guid": "00000000-0000-0000-0000-000000000000", "bool": False, "int": 0, "bigint": 0, "float": 0.0,
	        "decimal": Decimal(0), "blob": b"", "text": ""}[item["kind"]]


# ---- SQL Server
def mssql_identifier(name):
	return "[" + name.replace("]", "]]") + "]"


def key_length(count):
	"""The longest text a key of this many text columns may hold: SQL Server's 900 bytes, NVARCHAR's two each"""
	return max(64, min(450, 900 // (2 * max(count, 1))))


def mssql_type(item):
	kind = item["kind"]
	if kind == "text":
		length = item["length"]
		return f"NVARCHAR({min(max(length, 1), 4000)})" if length and length <= 4000 else "NVARCHAR(MAX)"
	if kind == "decimal":
		precision = min(item["precision"] or 38, 38)
		return f"DECIMAL({precision}, {min(item['scale'] if item['scale'] is not None else 10, precision)})"
	return {"int": "INT", "bigint": "BIGINT", "bool": "BIT", "guid": "UNIQUEIDENTIFIER", "datetime": "DATETIME2",
	        "date": "DATE", "time": "TIME", "float": "FLOAT", "blob": "VARBINARY(MAX)"}[kind]


def mssql_text(value):
	"""A text as T-SQL, never with a line break of its own (sqlcmd reads GO and :r at the start of a line). One that
	has them is joined from parts round NCHAR(10) and NCHAR(13), as nvarchar(max) so nothing is cut at 4000"""
	parts = re.split(r"(\r|\n)", value)
	if len(parts) == 1:
		return "N'" + value.replace("'", "''") + "'"
	pieces = ["CAST(N'' AS NVARCHAR(MAX))"]
	for part in parts:
		if part in ("\r", "\n"):
			pieces.append("NCHAR(13)" if part == "\r" else "NCHAR(10)")
		elif part:
			pieces.append("N'" + part.replace("'", "''") + "'")
	return " + ".join(pieces)


def mssql_literal(value, item):
	if value is None:
		return "NULL"
	kind = item["kind"]
	if kind in ("int", "bigint"):
		return str(int(value))
	if kind == "bool":
		return "1" if value else "0"
	if kind == "float":
		return repr(float(value))
	if kind == "decimal":
		return format(value, "f")
	if kind == "blob":
		return "0x" + value.hex().upper()
	if kind == "guid":
		return "'" + value + "'"
	if kind in ("datetime", "date", "time"):
		return "'" + value.replace(" ", "T") + "'" if kind == "datetime" else "'" + value + "'"
	return mssql_text(value)


def mssql_default(item):
	found = item["default"]
	if not found:
		return ""
	if found.get("now"):
		return " DEFAULT " + ("CAST(GETUTCDATE() AS DATE)" if item["kind"] == "date" else "CAST(GETUTCDATE() AS TIME)"
		                      if item["kind"] == "time" else "GETUTCDATE()")
	if found.get("newid"):
		return " DEFAULT NEWID()" if item["kind"] == "guid" else ""
	if "number" in found:
		if item["kind"] in ("text", "guid", "blob", "datetime", "date", "time"):
			return "" if item["kind"] != "text" else " DEFAULT N'" + found["number"] + "'"
		return " DEFAULT " + found["number"]
	if item["kind"] in ("int", "bigint", "bool", "float", "decimal"):
		return " DEFAULT " + found["text"] if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", found["text"]) else ""
	if item["kind"] == "blob":
		return ""
	return " DEFAULT " + mssql_text(found["text"])


def _constraint(prefix, table, extra=""):
	name = re.sub(r"\W", "_", f"{prefix}_{table}{extra}")
	return name[:128]


def mssql_create(table, lengths=None):
	"""CREATE TABLE and its indexes for SQL Server. Text columns in a key get a length a key may have; one with a value
	longer than that is left out of an index (said in the result), and makes a primary key fail"""
	lengths = lengths or {}
	keys = [table["primary_key"]] + [index["columns"] for index in table["indexes"]]
	text_names = {item["name"] for item in table["columns"] if item["kind"] == "text"}
	limit = {}
	for key in keys:
		count = sum(1 for name in key if name in text_names)
		for name in key:
			if name in text_names:
				limit[name] = min(limit.get(name, 450), key_length(count))
	skipped, indexes = [], []
	for index in table["indexes"]:
		too_long = [name for name in index["columns"] if name in limit and lengths.get(name, 0) > limit[name]]
		if too_long:
			skipped.append(index["name"])
		else:
			indexes.append(index)
	for name in table["primary_key"]:
		if name in limit and lengths.get(name, 0) > limit[name]:
			raise ValueError(f"{table['name']}.{name} holds a value of {lengths[name]} characters, longer than a SQL Server key may have")
	used = {name for index in indexes for name in index["columns"]} | set(table["primary_key"])
	lines = []
	for item in table["columns"]:
		kind = mssql_type(item)
		if item["name"] in used and item["name"] in limit:
			kind = f"NVARCHAR({min(item['length'] or limit[item['name']], limit[item['name']])})"
		identity = " IDENTITY(1,1)" if item["name"] == table["identity"] else ""
		lines.append(f"    {mssql_identifier(item['name'])} {kind}{identity}{mssql_default(item) if not identity else ''}"
		             f" {'NULL' if item['nullable'] else 'NOT NULL'}")
	if table["primary_key"]:
		lines.append(f"    CONSTRAINT {mssql_identifier(_constraint('PK', table['name']))} PRIMARY KEY ("
		             + ", ".join(mssql_identifier(name) for name in table["primary_key"]) + ")")
	sql = f"CREATE TABLE {mssql_identifier(table['name'])} (\n" + ",\n".join(lines) + "\n);\n"
	index_sql = ""
	for index in indexes:
		index_sql += (f"CREATE {'UNIQUE ' if index['unique'] else ''}INDEX {mssql_identifier(index['name'][:128])} ON "
		              f"{mssql_identifier(table['name'])} (" + ", ".join(mssql_identifier(name) for name in index["columns"]) + ");\n")
	return sql, index_sql, skipped


def mssql_script(stream, tables, rows_of, lengths_of, log=None):
	"""The script that makes the tables and fills them, written to stream"""
	problems = {}
	stream.write("SET NOCOUNT ON;\nSET XACT_ABORT ON;\nGO\n")
	pending_indexes = []
	for table in tables:
		create, index_sql, skipped = mssql_create(table, lengths_of(table))
		for name in skipped:
			say(log, f"Index {name} on {table['name']} is left out: a value is longer than a SQL Server key may have")
		stream.write(create + "GO\n")
		pending_indexes.append(index_sql)
		names = ", ".join(mssql_identifier(item["name"]) for item in table["columns"])
		identity = bool(table["identity"])
		head = f"INSERT INTO {mssql_identifier(table['name'])} ({names}) VALUES\n"
		rows, inserts = [], 0

		def flush():
			nonlocal inserts
			if not rows:
				return
			if identity and inserts % BATCH_INSERTS == 0:
				stream.write(f"SET IDENTITY_INSERT {mssql_identifier(table['name'])} ON;\n")
			stream.write(head + ",\n".join(rows) + ";\n")
			rows.clear()
			inserts += 1
			if inserts % BATCH_INSERTS == 0:
				stream.write((f"SET IDENTITY_INSERT {mssql_identifier(table['name'])} OFF;\n" if identity else "") + "GO\n")

		for row in rows_of(table):
			values = coerce_row(row, table, problems)
			rows.append("(" + ", ".join(mssql_literal(value, item) for value, item in zip(values, table["columns"])) + ")")
			if len(rows) >= INSERT_ROWS:
				flush()
		flush()
		if inserts % BATCH_INSERTS:
			stream.write((f"SET IDENTITY_INSERT {mssql_identifier(table['name'])} OFF;\n" if identity else "") + "GO\n")
	for index_sql in pending_indexes:
		if index_sql:
			stream.write(index_sql + "GO\n")
	return problems


# ---- PostgreSQL
def pg_identifier(name):
	if "\0" in name:
		raise ValueError("A name holds a NUL character")
	return '"' + name.replace('"', '""') + '"'


def pg_name(name):
	"""A name PostgreSQL keeps whole: at most 63 bytes, else cut and told apart by a short hash"""
	if len(name.encode("utf-8")) <= 63:
		return name
	import hashlib
	tail = "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
	cut = name.encode("utf-8")[:63 - len(tail)].decode("utf-8", errors="ignore")
	return cut + tail


def pg_type(item):
	kind = item["kind"]
	if kind == "text":
		return f"varchar({item['length']})" if item["length"] and item["length"] <= 10485760 else "text"
	if kind == "decimal":
		precision = min(item["precision"] or 38, 1000)
		return f"numeric({precision}, {min(item['scale'] if item['scale'] is not None else 10, precision)})"
	return {"int": "integer", "bigint": "bigint", "bool": "boolean", "guid": "uuid", "datetime": "timestamp(6)",
	        "date": "date", "time": "time(6)", "float": "double precision", "blob": "bytea"}[kind]


def pg_text(value):
	return "'" + value.replace("\0", "").replace("'", "''") + "'"


def pg_literal(value, item):
	if value is None:
		return "NULL"
	kind = item["kind"]
	if kind in ("int", "bigint"):
		return str(int(value))
	if kind == "bool":
		return "TRUE" if value else "FALSE"
	if kind == "float":
		return repr(float(value))
	if kind == "decimal":
		return format(value, "f")
	if kind == "blob":
		return "'\\x" + value.hex() + "'"
	if kind == "datetime":
		return "'" + value[:26] + "'"
	if kind == "time":
		return "'" + value[:15] + "'"
	if kind in ("guid", "date"):
		return "'" + value + "'"
	return pg_text(value)


def pg_default(item):
	found = item["default"]
	if not found:
		return ""
	kind = item["kind"]
	if found.get("now"):
		return " DEFAULT " + ("CURRENT_DATE" if kind == "date" else "LOCALTIME" if kind == "time" else "(now() AT TIME ZONE 'UTC')")
	if found.get("newid"):
		return " DEFAULT gen_random_uuid()" if kind == "guid" else ""
	number = found.get("number")
	if number is not None:
		if kind == "bool":
			return " DEFAULT " + ("FALSE" if number in ("0", "0.0") else "TRUE")
		if kind in ("int", "bigint", "float", "decimal"):
			return " DEFAULT " + number
		return " DEFAULT " + pg_text(number) if kind == "text" else ""
	if kind in ("int", "bigint", "float", "decimal"):
		return " DEFAULT " + found["text"] if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", found["text"]) else ""
	if kind == "bool":
		return " DEFAULT " + ("TRUE" if found["text"].strip().lower() in ("1", "true") else "FALSE")
	if kind == "text":
		return " DEFAULT " + pg_text(found["text"])
	return ""


def pg_create(table):
	lines = []
	for item in table["columns"]:
		identity = " GENERATED BY DEFAULT AS IDENTITY" if item["name"] == table["identity"] else ""
		lines.append(f"    {pg_identifier(item['name'])} {pg_type(item)}{identity}{pg_default(item) if not identity else ''}"
		             f"{'' if item['nullable'] else ' NOT NULL'}")
	if table["primary_key"]:
		lines.append(f"    CONSTRAINT {pg_identifier(pg_name('PK_' + table['name']))} PRIMARY KEY ("
		             + ", ".join(pg_identifier(name) for name in table["primary_key"]) + ")")
	sql = f"CREATE TABLE {pg_identifier(table['name'])} (\n" + ",\n".join(lines) + "\n);\n"
	return sql


def pg_indexes(tables):
	"""The indexes, each named once in the schema (PostgreSQL's index names are the schema's, not the table's)"""
	used, sql = {pg_name("PK_" + table["name"]).lower() for table in tables}, ""
	for table in tables:
		for index in table["indexes"]:
			name = pg_name(index["name"])
			number = 2
			while name.lower() in used:
				name = pg_name(f"{index['name']}_{number}")
				number += 1
			used.add(name.lower())
			sql += (f"CREATE {'UNIQUE ' if index['unique'] else ''}INDEX {pg_identifier(name)} ON {pg_identifier(table['name'])} ("
			        + ", ".join(pg_identifier(column_name) for column_name in index["columns"]) + ");\n")
	return sql


def pg_script(stream, tables, rows_of, log=None):
	problems = {}
	stream.write("SET client_encoding = 'UTF8';\nSET standard_conforming_strings = on;\n")
	for table in tables:
		stream.write(pg_create(table))
		names = ", ".join(pg_identifier(item["name"]) for item in table["columns"])
		head = f"INSERT INTO {pg_identifier(table['name'])} ({names}) " + ("OVERRIDING SYSTEM VALUE " if table["identity"] else "") + "VALUES\n"
		rows = []
		for row in rows_of(table):
			values = coerce_row(row, table, problems)
			rows.append("(" + ", ".join(pg_literal(value, item) for value, item in zip(values, table["columns"])) + ")")
			if len(rows) >= INSERT_ROWS:
				stream.write(head + ",\n".join(rows) + ";\n")
				rows.clear()
		if rows:
			stream.write(head + ",\n".join(rows) + ";\n")
		if table["identity"]:
			quoted = pg_identifier(table["name"])
			column_name = pg_identifier(table["identity"])
			stream.write(f"SELECT setval(pg_get_serial_sequence({pg_text(quoted)}, {pg_text(table['identity'])}), "
			             f"GREATEST(COALESCE((SELECT MAX({column_name}) FROM {quoted}), 0) + 1, 1), false);\n")
	stream.write(pg_indexes(tables))
	return problems


# ---- SQLite as the target
def sqlite_type(item):
	kind = item["kind"]
	base = {"int": "INTEGER", "bigint": "INTEGER", "bool": "INTEGER", "text": "TEXT", "guid": "TEXT", "datetime": "TEXT",
	        "date": "TEXT", "time": "TEXT", "decimal": "TEXT", "float": "REAL", "blob": "BLOB"}[kind]
	return base + (" COLLATE NOCASE" if item["nocase"] and base == "TEXT" else "")


def sqlite_default(item):
	found = item["default"]
	if not found or found.get("newid"):
		return ""
	if found.get("now"):
		return " DEFAULT " + ("CURRENT_DATE" if item["kind"] == "date" else "CURRENT_TIME" if item["kind"] == "time" else "CURRENT_TIMESTAMP")
	if "number" in found:
		return " DEFAULT " + (found["number"] if item["kind"] not in ("text", "guid") else "'" + found["number"] + "'")
	return " DEFAULT '" + found["text"].replace("'", "''") + "'"


def sqlite_create(table):
	lines = []
	single = table["identity"] and table["primary_key"] == [table["identity"]]
	for item in table["columns"]:
		if single and item["name"] == table["identity"]:
			lines.append(f"    {sqlite_identifier(item['name'])} INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT")
			continue
		lines.append(f"    {sqlite_identifier(item['name'])} {sqlite_type(item)}{'' if item['nullable'] else ' NOT NULL'}{sqlite_default(item)}")
	if table["primary_key"] and not single:
		lines.append(f"    CONSTRAINT {sqlite_identifier('PK_' + table['name'])} PRIMARY KEY ("
		             + ", ".join(sqlite_identifier(name) for name in table["primary_key"]) + ")")
	return f"CREATE TABLE {sqlite_identifier(table['name'])} (\n" + ",\n".join(lines) + "\n)"


def sqlite_value(value, item):
	if value is None:
		return None
	if item["kind"] == "bool":
		return 1 if value else 0
	if item["kind"] == "decimal":
		return format(value, "f")
	if item["kind"] == "datetime":
		# As Microsoft.Data.Sqlite writes one: yyyy-MM-dd HH:mm:ss.FFFFFFF
		return value.rstrip("0").rstrip(".")
	return value


def write_sqlite(path, tables, rows_of, log=None):
	"""A new SQLite database at path, made and filled in a file beside it and moved into place when it is whole"""
	path = Path(path)
	if path.exists():
		raise ValueError(f"{path.name} is there already")
	working = path.with_name(path.name + ".converting")
	if working.exists():
		working.unlink()
	problems = {}
	try:
		with sqlite3.connect(working) as connection:
			connection.execute("PRAGMA journal_mode = OFF")
			connection.execute("PRAGMA synchronous = OFF")
			used = set()
			for table in tables:
				say(log, f"Table {table['name']}")
				connection.execute(sqlite_create(table))
				marks = ", ".join("?" for _ in table["columns"])
				names = ", ".join(sqlite_identifier(item["name"]) for item in table["columns"])
				insert = f"INSERT INTO {sqlite_identifier(table['name'])} ({names}) VALUES ({marks})"
				batch = []
				for row in rows_of(table):
					values = coerce_row(row, table, problems)
					batch.append([sqlite_value(value, item) for value, item in zip(values, table["columns"])])
					if len(batch) >= 1000:
						connection.executemany(insert, batch)
						batch.clear()
				if batch:
					connection.executemany(insert, batch)
				for index in table["indexes"]:
					name = index["name"] if not index["name"].lower().startswith("sqlite_") else "IX_" + index["name"]
					number = 2
					while name.lower() in used:
						name = f"{index['name']}_{number}"
						number += 1
					used.add(name.lower())
					connection.execute(f"CREATE {'UNIQUE ' if index['unique'] else ''}INDEX {sqlite_identifier(name)} ON "
					                   f"{sqlite_identifier(table['name'])} (" + ", ".join(sqlite_identifier(column_name) for column_name in index["columns"]) + ")")
			connection.commit()
		connection.close()
		with sqlite3.connect(working) as connection:
			connection.execute("PRAGMA journal_mode = WAL")
		connection.close()
		os.replace(working, path)
	except BaseException:
		if working.exists():
			working.unlink()
		raise
	return problems


# ---- reading SQL Server
_MSSQL_KINDS = {"int": "int", "smallint": "int", "tinyint": "int", "bigint": "bigint", "bit": "bool",
                "uniqueidentifier": "guid", "datetime": "datetime", "datetime2": "datetime", "smalldatetime": "datetime",
                "datetimeoffset": "datetime", "date": "date", "time": "time", "decimal": "decimal", "numeric": "decimal",
                "money": "decimal", "smallmoney": "decimal", "float": "float", "real": "float", "binary": "blob",
                "varbinary": "blob", "image": "blob", "timestamp": "blob", "rowversion": "blob"}


def read_mssql(settings, database):
	sql = sql_admin()
	rows = sql.query_json(settings, """
SELECT SCHEMA_NAME(t.schema_id) AS sch, t.name AS tbl, c.name AS col, ty.name AS type, c.max_length, c.precision, c.scale, CAST(c.is_nullable AS int) AS nullable,
 CAST(c.is_identity AS int) AS is_identity, CAST(c.is_computed AS int) AS computed, dc.definition AS dflt, c.collation_name AS coll
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
WHERE t.is_ms_shipped = 0 ORDER BY t.name, c.column_id""", database=database)
	keys = sql.query_json(settings, """
SELECT SCHEMA_NAME(t.schema_id) AS sch, t.name AS tbl, i.name AS idx, CAST(i.is_primary_key AS int) AS pk, CAST(i.is_unique AS int) AS uniq,
 CAST(i.has_filter AS int) AS filtered, c.name AS col, ic.key_ordinal AS ord
FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.type > 0 AND ic.key_ordinal > 0 ORDER BY t.name, i.name, ic.key_ordinal""", database=database)
	sizes = sql.query_json(settings, """
SELECT OBJECT_SCHEMA_NAME(object_id) AS sch, OBJECT_NAME(object_id) AS tbl, SUM(row_count) AS rows, SUM(used_page_count) * 8192 AS bytes
FROM sys.dm_db_partition_stats WHERE index_id IN (0, 1) GROUP BY object_id""", database=database)
	return _sized(_server_tables(rows, keys, _mssql_column), sizes)


def _sized(tables, sizes):
	"""Each table with its rows and bytes as the server counts them, to read it in pages of about PAGE_BYTES"""
	found = {(row.get("sch") or "public", row["tbl"]): (int(row["rows"] or 0), int(row["bytes"] or 0)) for row in sizes}
	for table in tables:
		table["size"] = found.get((table["schema"], table["name"]), (0, 0))
	return tables


def _page(table):
	rows, bytes_used = table.get("size") or (0, 0)
	return max(50, min(20000, int(PAGE_BYTES * max(rows, 1) / max(bytes_used, 1))))


def _mssql_column(row):
	kind = _MSSQL_KINDS.get(row["type"].lower(), "text")
	length = None
	if kind == "text" and row["type"].lower() in ("char", "varchar", "nchar", "nvarchar") and row["max_length"] > 0:
		length = row["max_length"] // 2 if row["type"].lower().startswith("n") else row["max_length"]
	precision = row["precision"] if kind == "decimal" else None
	scale = row["scale"] if kind == "decimal" else None
	if row["type"].lower() in ("money", "smallmoney"):
		precision, scale = 19, 4
	nocase = kind in ("text", "guid") and "_CI" in (row.get("coll") or "_CI")
	return column(row["col"], kind, nullable=bool(row["nullable"]), length=length, precision=precision, scale=scale,
	              default=neutral_default(row.get("dflt")), nocase=nocase), row["type"].lower()


def _server_tables(rows, keys, describe):
	tables = {}
	for row in rows:
		if row.get("computed"):
			continue
		schema = row.get("sch") or "dbo"
		same = next((table for name, table in tables.items() if name.lower() == row["tbl"].lower()), None)
		if same is not None and (same["name"] != row["tbl"] or same["schema"] != schema):
			raise ValueError(f"Two tables are named {row['tbl']} (in different schemas, or but for case): SQLite would hold one")
		table = tables.setdefault(row["tbl"], {"name": row["tbl"], "schema": schema, "columns": [], "primary_key": [],
		                                      "identity": None, "indexes": [], "types": {}})
		item, source_type = describe(row)
		table["types"][item["name"]] = source_type
		table["columns"].append(item)
		if row.get("is_identity"):
			table["identity"] = item["name"]
	found = {}
	for row in keys:
		if row["tbl"] not in tables or tables[row["tbl"]]["schema"] != (row.get("sch") or "dbo"):
			continue
		index = found.setdefault((row["tbl"], row["idx"]), {"name": row["idx"], "unique": bool(row["uniq"]), "pk": bool(row["pk"]),
		                                                    "filtered": bool(row.get("filtered")), "columns": []})
		index["columns"].append(row["col"])
	for (name, _index_name), index in found.items():
		table = tables[name]
		known = {item["name"] for item in table["columns"]}
		if not set(index["columns"]) <= known:
			continue
		if index["pk"]:
			table["primary_key"] = index["columns"]
		elif not index["filtered"]:
			table["indexes"].append({"name": index["name"], "unique": index["unique"], "columns": index["columns"]})
	result = list(tables.values())
	for table in result:
		# SQLite numbers only a single INTEGER PRIMARY KEY: another identity column is a plain one there
		if table["identity"] and table["primary_key"] != [table["identity"]]:
			table["identity"] = None
		for item in table["columns"]:
			if item["name"] in table["primary_key"]:
				item["nullable"] = False
	return result


def _mssql_select(item, source_type, number):
	quoted = mssql_identifier(item["name"])
	if source_type in ("datetimeoffset",):
		expression = f"CAST(SWITCHOFFSET({quoted}, '+00:00') AS DATETIME2)"
	elif source_type in ("xml", "sql_variant", "hierarchyid", "geography", "geometry") or item["kind"] == "text" and source_type not in (
			"char", "varchar", "nchar", "nvarchar", "text", "ntext"):
		expression = f"CAST({quoted} AS NVARCHAR(MAX))"
	elif source_type in ("text", "ntext"):
		expression = f"CAST({quoted} AS NVARCHAR(MAX))"
	elif source_type == "image":
		expression = f"CAST({quoted} AS VARBINARY(MAX))"
	elif item["kind"] == "decimal":
		# A number as text, so JSON keeps all its digits
		expression = f"CAST({quoted} AS NVARCHAR(64))"
	else:
		expression = quoted
	return f"{expression} AS [c{number}]"


def mssql_chunks(output):
	"""The text between < and > on each line sqlcmd printed, joined. Lines end at \n alone: a text may hold U+2028 and
	the like, which FOR JSON leaves as they are and str.splitlines() would split at"""
	parts = []
	for line in output.split("\n"):
		start, end = line.find("<"), line.rfind(">")
		if start == 0 and end > start:
			parts.append(line[1:end])
	return "".join(parts)


def mssql_rows(settings, database, table):
	"""A table's rows, a page at a time, as FOR JSON gives them, in parts of 3998 characters (never one cut in a
	surrogate pair) between < and >: nvarchar(4000) with them, as a text made of a non-max one is cut there"""
	quoted = mssql_identifier(table["schema"]) + "." + mssql_identifier(table["name"])
	selects = ", ".join(_mssql_select(item, table["types"][item["name"]], number) for number, item in enumerate(table["columns"]))
	page = _page(table)
	order = ", ".join(mssql_identifier(name) for name in table["primary_key"]) if table["primary_key"] else None
	offset = 0
	while True:
		window = f" ORDER BY {order} OFFSET {offset} ROWS FETCH NEXT {page} ROWS ONLY" if order else ""
		query = f"""
DECLARE @j NVARCHAR(MAX) = (SELECT {selects} FROM {quoted}{window} FOR JSON PATH, INCLUDE_NULL_VALUES);
DECLARE @p BIGINT = 1, @n INT, @len BIGINT = COALESCE(DATALENGTH(@j) / 2, 0);
DECLARE @out TABLE (i INT IDENTITY PRIMARY KEY, s NVARCHAR(4000));
WHILE @p <= @len
BEGIN
 SET @n = 3998;
 IF UNICODE(SUBSTRING(@j, @p + @n - 1, 1)) BETWEEN 55296 AND 56319 SET @n = 3997;
 INSERT @out (s) VALUES (SUBSTRING(@j, @p, @n));
 SET @p = @p + @n;
END
SELECT N'<' + s + N'>' AS vns_chunk FROM @out ORDER BY i;"""
		text = mssql_chunks(sqlcmd(settings, database, query, ["-y", "0", "-w", "65535"]))
		found = json.loads(text) if text.strip() else []
		for item in found:
			yield [_mssql_value(item.get(f"c{number}"), column_item) for number, column_item in enumerate(table["columns"])]
		if not order or len(found) < page:
			return
		offset += page


def _mssql_value(value, item):
	if value is None:
		return None
	if item["kind"] == "blob" and isinstance(value, str):
		return base64.b64decode(value)
	if item["kind"] == "decimal":
		return Decimal(str(value))
	return value


def server_counts(engine, settings, database, tables):
	"""The rows of each table as the server counts them, 100 tables a query"""
	counts = {}
	for start in range(0, len(tables), 100):
		part = tables[start:start + 100]
		if engine == "mssql":
			query = " UNION ALL ".join(f"SELECT {sql_admin().literal(table['name'])} AS t, COUNT_BIG(*) AS n FROM "
			                           f"{mssql_identifier(table.get('schema') or 'dbo')}.{mssql_identifier(table['name'])}" for table in part)
			# FOR JSON takes no UNION of its own: one round it
			rows = sql_admin().query_json(settings, f"SELECT t, n FROM ({query}) AS counted", database=database)
		else:
			query = " UNION ALL ".join(f"SELECT {pg_text(table['name'])} AS t, COUNT(*) AS n FROM {pg_identifier(table['name'])}" for table in part)
			rows = pg_admin().query_json(settings, query, database=database)
		counts.update({row["t"]: int(row["n"]) for row in rows})
	return counts


def mssql_empty(settings, database):
	found = sql_admin().query_json(settings, "SELECT COUNT(*) AS n FROM sys.tables WHERE is_ms_shipped = 0", database=database)
	return not found[0]["n"]


def _sqlcmd_file(settings, database, path, extra=(), timeout=6 * 3600):
	tool = sql_admin().sqlcmd_path()
	if not tool:
		raise ValueError("sqlcmd is not installed on the admin server. Install the Microsoft SQL command-line tools.")
	args = [tool, "-S", f'tcp:{settings["host"]},{settings["port"]}', "-U", settings["username"], "-d", database,
	        "-b", "-x", "-r", "0", "-l", "15", "-t", "0", "-f", "65001", *extra, "-i", str(path)]
	if settings.get("trust_certificate"):
		args.append("-C")
	result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
	                        env=os.environ | {"SQLCMDPASSWORD": settings["password"]}, timeout=timeout)
	if result.returncode:
		raise RuntimeError((result.stderr or result.stdout or "sqlcmd failed").strip()[-4000:])
	return result.stdout


def sqlcmd(settings, database, query, extra=(), timeout=3600):
	"""Harbor's own query, its output as sqlcmd prints it with the options given (no line cut at 80 characters)"""
	descriptor, filename = tempfile.mkstemp(prefix="vns-convert-", suffix=".sql")
	try:
		with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
			stream.write("SET NOCOUNT ON;\n" + query + "\n")
		return _sqlcmd_file(settings, database, filename, extra, timeout)
	finally:
		os.unlink(filename)


def run_mssql_script(settings, database, path, timeout=6 * 3600):
	_sqlcmd_file(settings, database, path, timeout=timeout)


# ---- reading PostgreSQL
_PG_KINDS = {"smallint": "int", "integer": "int", "bigint": "bigint", "boolean": "bool", "uuid": "guid",
             "timestamp without time zone": "datetime", "timestamp with time zone": "datetime", "date": "date",
             "time without time zone": "time", "time with time zone": "time", "numeric": "decimal", "real": "float",
             "double precision": "float", "bytea": "blob", "money": "decimal"}


def read_postgres(settings, database):
	pg = pg_admin()
	rows = pg.query_json(settings, """
SELECT c.table_name AS tbl, c.column_name AS col, c.data_type AS type, c.character_maximum_length AS length,
 c.numeric_precision AS precision, c.numeric_scale AS scale, (c.is_nullable = 'YES') AS nullable,
 (c.is_identity = 'YES' OR c.column_default LIKE 'nextval(%') AS is_identity, (c.is_generated = 'ALWAYS') AS computed,
 CASE WHEN c.column_default LIKE 'nextval(%' THEN NULL ELSE c.column_default END AS dflt, c.ordinal_position AS position
FROM information_schema.columns c JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' ORDER BY c.table_name, c.ordinal_position""", database=database)
	keys = pg.query_json(settings, """
SELECT t.relname AS tbl, i.relname AS idx, x.indisprimary AS pk, x.indisunique AS uniq, (x.indpred IS NOT NULL OR x.indexprs IS NOT NULL) AS filtered,
 a.attname AS col, k.ord
FROM pg_index x JOIN pg_class t ON t.oid = x.indrelid JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE n.nspname = 'public' AND k.ord <= x.indnkeyatts ORDER BY t.relname, i.relname, k.ord""", database=database)
	sizes = pg.query_json(settings, """
SELECT 'public' AS sch, c.relname AS tbl, GREATEST(c.reltuples, 0)::bigint AS rows, pg_total_relation_size(c.oid) AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')""", database=database)
	return _sized(_server_tables(rows, keys, _pg_column), sizes)


def _pg_column(row):
	source_type = row["type"].lower()
	kind = _PG_KINDS.get(source_type, "text")
	precision = row.get("precision") if kind == "decimal" else None
	scale = row.get("scale") if kind == "decimal" else None
	if kind == "decimal" and precision is None:
		precision, scale = 38, 10
	if source_type == "money":
		precision, scale = 19, 4
	length = row.get("length") if kind == "text" else None
	return column(row["col"], kind, nullable=bool(row["nullable"]), length=length, precision=precision, scale=scale,
	              default=neutral_default(row.get("dflt"))), source_type


def _pg_select(item, source_type, number):
	quoted = pg_identifier(item["name"])
	if source_type == "timestamp with time zone":
		expression = f"({quoted} AT TIME ZONE 'UTC')"
	elif item["kind"] == "decimal":
		expression = f"{quoted}::text"
	elif item["kind"] == "blob":
		expression = f"encode({quoted}, 'base64')"
	elif item["kind"] == "text" and source_type not in ("text", "character varying", "character"):
		expression = f"{quoted}::text"
	else:
		expression = quoted
	return f"{expression} AS c{number}"


def postgres_rows(settings, database, table):
	pg = pg_admin()
	quoted = pg_identifier(table["name"])
	selects = ", ".join(_pg_select(item, table["types"][item["name"]], number) for number, item in enumerate(table["columns"]))
	page = _page(table)
	order = " ORDER BY " + ", ".join(pg_identifier(name) for name in table["primary_key"]) if table["primary_key"] else ""
	offset = 0
	while True:
		window = f"{order} LIMIT {page} OFFSET {offset}" if order else ""
		output = pg.run([pg.psql(), "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-d", database, "-c",
		                 f"SELECT row_to_json(q)::text FROM (SELECT {selects} FROM {quoted}{window}) q"], settings).stdout
		count = 0
		for line in output.split("\n"):
			if not line.strip():
				continue
			item = json.loads(line)
			count += 1
			yield [_pg_value(item.get(f"c{number}"), column_item) for number, column_item in enumerate(table["columns"])]
		if not order or count < page:
			return
		offset += page


def _pg_value(value, item):
	if value is None:
		return None
	if item["kind"] == "blob":
		return base64.b64decode(value)
	if item["kind"] == "decimal":
		return Decimal(str(value))
	return value


def postgres_empty(settings, database):
	found = pg_admin().query_json(settings, "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema NOT IN "
	                                        "('pg_catalog', 'information_schema')", database=database)
	return not found[0]["n"]


def run_postgres_script(settings, database, path, timeout=6 * 3600):
	pg = pg_admin()
	pg.run([pg.psql(), "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-d", database, "-f", str(path)],
	       settings, timeout=timeout)


# ---- the conversions
def _problems(log, problems):
	for (table, name), count in sorted(problems.items()):
		say(log, f"{table}.{name}: {count} value(s) could not be read as the column's type and were left empty")


def from_sqlite(path, engine, settings, database, log=None, umbraco=False):
	"""A site's SQLite database into an empty database on a server (mssql or postgres). Returns the tables and rows"""
	if engine not in ("mssql", "postgres"):
		raise ValueError("Convert to Microsoft SQL Server or PostgreSQL")
	if umbraco and engine == "postgres":
		raise ValueError("Umbraco does not run on PostgreSQL")
	empty = mssql_empty if engine == "mssql" else postgres_empty
	if not empty(settings, database):
		raise ValueError(f"The database {database} has tables already: convert into an empty one")
	with tempfile.TemporaryDirectory(prefix="vns-convert-") as folder:
		copy = snapshot(path, folder)
		with sqlite3.connect(copy) as connection:
			say(log, "Read the tables")
			tables = read_sqlite(connection, log)
			script = Path(folder) / "convert.sql"
			say(log, f"Write {len(tables)} tables as {'Microsoft SQL Server' if engine == 'mssql' else 'PostgreSQL'}")
			with script.open("w", encoding="utf-8", newline="\n") as stream:
				if engine == "mssql":
					problems = mssql_script(stream, tables, lambda table: sqlite_rows(connection, table),
					                        lambda table: text_lengths(connection, table, [
						                        item["name"] for item in table["columns"] if item["kind"] == "text" and (
							                        item["name"] in table["primary_key"] or any(item["name"] in index["columns"] for index in table["indexes"]))]),
					                        log)
				else:
					problems = pg_script(stream, tables, lambda table: sqlite_rows(connection, table), log)
			_problems(log, problems)
			say(log, "Load them into " + database)
			(run_mssql_script if engine == "mssql" else run_postgres_script)(settings, database, script)
			say(log, "Count the rows on both sides")
			counts = server_counts(engine, settings, database, tables)
			total = 0
			for table in tables:
				here, there = sqlite_count(connection, table["name"]), counts.get(table["name"], 0)
				if here != there:
					raise RuntimeError(f"{table['name']} has {here} rows in SQLite and {there} in {database}")
				total += here
		connection.close()
	return {"tables": len(tables), "rows": total}


def to_sqlite(engine, settings, database, path, log=None):
	"""A database on a server (mssql or postgres) into a new SQLite file at path. Returns the tables and rows"""
	if engine not in ("mssql", "postgres"):
		raise ValueError("Convert from Microsoft SQL Server or PostgreSQL")
	say(log, "Read the tables")
	tables = (read_mssql if engine == "mssql" else read_postgres)(settings, database)
	rows_of = (lambda table: mssql_rows(settings, database, table)) if engine == "mssql" else (
		lambda table: postgres_rows(settings, database, table))
	problems = write_sqlite(path, tables, rows_of, log)
	_problems(log, problems)
	say(log, "Count the rows on both sides")
	total = 0
	try:
		counts = server_counts(engine, settings, database, tables)
		connection = sqlite3.connect(path)
		try:
			for table in tables:
				here, there = sqlite_count(connection, table["name"]), counts.get(table["name"], 0)
				if here != there:
					raise RuntimeError(f"{table['name']} has {there} rows in {database} and {here} in SQLite")
				total += here
		finally:
			connection.close()
	except BaseException:
		Path(path).unlink(missing_ok=True)
		raise
	return {"tables": len(tables), "rows": total}
