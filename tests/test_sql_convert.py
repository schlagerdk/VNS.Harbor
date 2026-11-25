"""A site's database converted between SQLite and Microsoft SQL Server or PostgreSQL (phase 11): the kinds guessed
from SQLite as VNS.SQLConvert did, the scripts for each server, and SQLite made from a server's tables"""
from decimal import Decimal
import importlib.util
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sql_convert_for_tests", ROOT / "lib/sql_convert.py")
convert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(convert)

UMBRACO = """
CREATE TABLE umbracoNode (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, uniqueId TEXT NOT NULL, parentId INTEGER NOT NULL,
 trashed INTEGER NOT NULL DEFAULT 0, createDate TEXT NOT NULL DEFAULT (DATE()), text TEXT NULL, nodeObjectType TEXT NULL);
CREATE UNIQUE INDEX IX_umbracoNode_UniqueId ON umbracoNode (uniqueId);
CREATE INDEX IX_umbracoNode_parentId ON umbracoNode (parentId);
CREATE TABLE umbracoUser (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, userName TEXT NOT NULL,
 userDisabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE cmsDictionary (pk INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, key TEXT NOT NULL);
CREATE TABLE umbracoKeyValue (key TEXT NOT NULL PRIMARY KEY, value TEXT NULL, updated TEXT NOT NULL);
CREATE TABLE umbracoDocumentVersion ("id" INTEGER NOT NULL, "templateId" INTEGER NULL, "published" INTEGER NOT NULL,
 CONSTRAINT PK_umbracoDocumentVersion PRIMARY KEY ("id"));
"""


def umbraco_database(folder):
	path = Path(folder) / "Umbraco.sqlite.db"
	with sqlite3.connect(path) as connection:
		connection.executescript(UMBRACO)
		connection.executemany("INSERT INTO umbracoNode (uniqueId, parentId, trashed, createDate, text, nodeObjectType) VALUES (?, ?, ?, ?, ?, ?)", [
			("a8b5c3d2-1e4f-4a6b-9c8d-7e6f5a4b3c2d", -1, 0, "2024-03-01 10:15:30.1234567", "Home\nGO\n:r /etc/passwd", "b796f64c-1f99-4ffb-b886-4bf4bc011a9c"),
			("b8b5c3d2-1e4f-4a6b-9c8d-7e6f5a4b3c2e", 1, 1, "2024-03-02T08:00:00Z", "It's", None),
		])
		connection.execute("INSERT INTO umbracoUser (key, userName, userDisabled) VALUES ('1e70f841-c261-413b-abb2-2d68cdb96094', 'admin', 0)")
		connection.execute("INSERT INTO cmsDictionary (id, key) VALUES ('c8b5c3d2-1e4f-4a6b-9c8d-7e6f5a4b3c2f', 'Header')")
		connection.execute("INSERT INTO umbracoKeyValue VALUES ('Umbraco.Core.Upgrader.State+Umbraco.Core', '{1A2B}', '2024-01-01 00:00:00')")
	connection.close()
	return path


def by_name(tables):
	return {table["name"]: {item["name"]: item for item in table["columns"]} | {"_table": table} for table in tables}


class ReadSqliteTests(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.TemporaryDirectory()
		self.path = umbraco_database(self.folder.name)
		self.connection = sqlite3.connect(self.path)
		self.tables = by_name(convert.read_sqlite(self.connection))

	def tearDown(self):
		self.connection.close()
		self.folder.cleanup()

	def test_kinds_are_guessed_as_vns_sqlconvert_did(self):
		node = self.tables["umbracoNode"]
		self.assertEqual(node["_table"]["identity"], "id")
		self.assertEqual(node["id"]["kind"], "int")
		self.assertEqual(node["uniqueId"]["kind"], "guid")
		self.assertEqual(node["nodeObjectType"]["kind"], "guid")
		self.assertEqual(node["trashed"]["kind"], "bool")
		self.assertEqual(node["createDate"]["kind"], "datetime")
		self.assertEqual(node["createDate"]["default"], {"now": True})
		self.assertEqual(node["text"]["kind"], "text")
		self.assertEqual(node["parentId"]["kind"], "int")
		self.assertEqual(self.tables["umbracoUser"]["key"]["kind"], "guid")
		self.assertEqual(self.tables["umbracoUser"]["userDisabled"]["kind"], "bool")
		self.assertTrue(self.tables["umbracoUser"]["userName"]["nocase"])

	def test_an_integer_key_without_autoincrement_is_the_sites_own(self):
		"""Umbraco gives umbracoDocumentVersion its content version's id: an IDENTITY on SQL Server refused it (Save and
		publish failed: Cannot insert explicit value for identity column)"""
		self.assertIsNone(self.tables["umbracoDocumentVersion"]["_table"]["identity"])
		self.assertEqual(self.tables["umbracoNode"]["_table"]["identity"], "id")
		create, _index, _skipped = convert.mssql_create(self.tables["umbracoDocumentVersion"]["_table"])
		self.assertNotIn("IDENTITY", create)
		self.assertIn("[id] INT NOT NULL", create)

	def test_umbracos_text_keys_stay_text(self):
		self.assertEqual(self.tables["umbracoKeyValue"]["key"]["kind"], "text")
		self.assertEqual(self.tables["cmsDictionary"]["key"]["kind"], "text")
		self.assertEqual(self.tables["cmsDictionary"]["id"]["kind"], "guid")
		self.assertIsNone(self.tables["umbracoKeyValue"]["_table"]["identity"])

	def test_indexes_are_read(self):
		indexes = {index["name"]: index for index in self.tables["umbracoNode"]["_table"]["indexes"]}
		self.assertEqual(indexes["IX_umbracoNode_UniqueId"], {"name": "IX_umbracoNode_UniqueId", "unique": True, "columns": ["uniqueId"]})
		self.assertFalse(indexes["IX_umbracoNode_parentId"]["unique"])

	def test_a_number_beyond_int_makes_a_bigint(self):
		self.connection.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, n INTEGER)")
		self.connection.execute("INSERT INTO big (n) VALUES (?)", (2 ** 40,))
		tables = by_name(convert.read_sqlite(self.connection))
		self.assertEqual(tables["big"]["n"]["kind"], "bigint")
		self.assertEqual(tables["big"]["id"]["kind"], "int")


class ScriptTests(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.TemporaryDirectory()
		self.path = umbraco_database(self.folder.name)
		self.connection = sqlite3.connect(self.path)
		self.tables = convert.read_sqlite(self.connection)

	def tearDown(self):
		self.connection.close()
		self.folder.cleanup()

	def mssql(self):
		stream = io.StringIO()
		convert.mssql_script(stream, self.tables, lambda table: convert.sqlite_rows(self.connection, table),
		                     lambda table: convert.text_lengths(self.connection, table, [
			                     item["name"] for item in table["columns"] if item["kind"] == "text"]))
		return stream.getvalue()

	def test_sql_server_gets_identity_not_autoincrement(self):
		script = self.mssql()
		self.assertIn("[id] INT IDENTITY(1,1) NOT NULL", script)
		self.assertNotIn("AUTOINCREMENT", script)
		self.assertIn("SET IDENTITY_INSERT [umbracoNode] ON;", script)
		self.assertIn("SET IDENTITY_INSERT [umbracoNode] OFF;", script)
		self.assertIn("[uniqueId] UNIQUEIDENTIFIER NOT NULL", script)
		self.assertIn("[trashed] BIT DEFAULT 0 NOT NULL", script)
		self.assertIn("[createDate] DATETIME2 DEFAULT GETUTCDATE() NOT NULL", script)
		self.assertIn("CREATE UNIQUE INDEX [IX_umbracoNode_UniqueId] ON [umbracoNode] ([uniqueId]);", script)
		# A text key a key may hold, not NVARCHAR(MAX)
		self.assertIn("[key] NVARCHAR(450) NOT NULL", script)
		self.assertIn("'2024-03-01T10:15:30.1234567'", script)
		self.assertIn("'2024-03-02T08:00:00.0000000'", script)
		self.assertIn("N'It''s'", script)

	def test_a_text_never_starts_a_line_of_the_script(self):
		"""sqlcmd reads GO and :r at the start of a line, wherever it is"""
		script = self.mssql()
		self.assertIn("N'Home' + NCHAR(10) + N'GO' + NCHAR(10) + N':r /etc/passwd'", script)
		self.assertFalse([line for line in script.splitlines() if line.startswith(":r") or line.strip() in ("GO 2",)])
		self.assertNotIn("\nGO\n:r", script)

	def test_postgresql(self):
		stream = io.StringIO()
		convert.pg_script(stream, self.tables, lambda table: convert.sqlite_rows(self.connection, table))
		script = stream.getvalue()
		self.assertIn('"id" integer GENERATED BY DEFAULT AS IDENTITY NOT NULL', script)
		self.assertIn('"uniqueId" uuid NOT NULL', script)
		self.assertIn('"trashed" boolean DEFAULT FALSE NOT NULL', script)
		self.assertIn('OVERRIDING SYSTEM VALUE VALUES', script)
		self.assertIn("SELECT setval(pg_get_serial_sequence('\"umbracoNode\"', 'id'), GREATEST(COALESCE((SELECT MAX(\"id\") FROM \"umbracoNode\"), 0) + 1, 1), false);", script)
		self.assertIn("'Home\nGO\n:r /etc/passwd'", script)
		self.assertIn("FALSE", script)
		self.assertIn('CREATE UNIQUE INDEX "IX_umbracoNode_UniqueId" ON "umbracoNode" ("uniqueId");', script)

	def test_a_key_too_long_for_sql_server_leaves_its_index_out(self):
		self.connection.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, path TEXT)")
		self.connection.execute("CREATE INDEX IX_pages_path ON pages (path)")
		self.connection.execute("INSERT INTO pages (path) VALUES (?)", ("x" * 600,))
		tables = [table for table in convert.read_sqlite(self.connection) if table["name"] == "pages"]
		create, index_sql, skipped = convert.mssql_create(tables[0], {"path": 600})
		self.assertEqual(skipped, ["IX_pages_path"])
		self.assertEqual(index_sql, "")
		self.assertIn("[path] NVARCHAR(MAX) NULL", create)


class ValueTests(unittest.TestCase):
	def test_defaults(self):
		self.assertEqual(convert.neutral_default("((0))"), {"number": "0"})
		self.assertEqual(convert.neutral_default("(getdate())"), {"now": True})
		self.assertEqual(convert.neutral_default("(N'It''s')"), {"text": "It's"})
		self.assertEqual(convert.neutral_default("'draft'::character varying"), {"text": "draft"})
		self.assertEqual(convert.neutral_default("(newid())"), {"newid": True})
		self.assertEqual(convert.neutral_default("false"), {"number": "0"})
		self.assertEqual(convert.neutral_default("datetime('now')"), {"now": True})
		self.assertIsNone(convert.neutral_default("(1) + (2)"))
		self.assertIsNone(convert.neutral_default("NULL"))

	def test_dates(self):
		self.assertEqual(convert.datetime_parts("2024-03-02T08:00:00+02:00"), ("2024-03-02", "06:00:00.0000000"))
		self.assertEqual(convert.datetime_parts("1700000000"), ("2023-11-14", "22:13:20.0000000"))
		self.assertEqual(convert.datetime_parts("1700000000123")[1], "22:13:20.1230000")
		self.assertIsNone(convert.datetime_parts("tomorrow"))
		self.assertIsNone(convert.datetime_parts("2024-02-30"))

	def test_a_value_that_is_not_its_kind_is_left_empty_and_counted(self):
		table = {"name": "t", "columns": [convert.column("g", "guid"), convert.column("d", "datetime", nullable=False)]}
		problems = {}
		self.assertEqual(convert.coerce_row(["not a guid", "never"], table, problems), [None, "1900-01-01 00:00:00.0000000"])
		self.assertEqual(problems, {("t", "g"): 1, ("t", "d"): 1})
		with self.assertRaises(ValueError):
			convert.coerce_row(["", "x"], {"name": "t", "columns": [convert.column("n", "int")]}, {})

	def test_sqlcmd_output_is_joined_from_its_parts(self):
		output = "vns_chunk\n---------\n<[{\"c0\":\"a  >\n<  b\"}]>    \n"
		self.assertEqual(convert.mssql_chunks(output), '[{"c0":"a    b"}]')
		# FOR JSON leaves U+2028 as it is: a line does not end there
		self.assertEqual(convert.mssql_chunks('<["a\u2028b>\n<c"]>\n'), '["a\u2028bc"]')


class ToSqliteTests(unittest.TestCase):
	def test_a_servers_tables_make_a_sqlite_database_as_umbraco_writes_one(self):
		tables = [{
			"name": "umbracoNode", "schema": "dbo", "primary_key": ["id"], "identity": "id", "types": {},
			"indexes": [{"name": "IX_umbracoNode_UniqueId", "unique": True, "columns": ["uniqueId"]}],
			"columns": [convert.column("id", "int", nullable=False), convert.column("uniqueId", "guid", nullable=False, nocase=True),
			            convert.column("trashed", "bool", nullable=False, default={"number": "0"}),
			            convert.column("createDate", "datetime", nullable=False, default={"now": True}),
			            convert.column("price", "decimal", precision=38, scale=6), convert.column("data", "blob")],
		}]
		rows = [[1, "a8b5c3d2-1e4f-4a6b-9c8d-7e6f5a4b3c2d", False, "2024-03-01T10:15:30.1230000", Decimal("12.500000"), b"\x01\x02"],
		        [2, "b8b5c3d2-1e4f-4a6b-9c8d-7e6f5a4b3c2e", True, "2024-03-01T10:15:30", None, None]]
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "Umbraco.sqlite.db"
			convert.write_sqlite(path, tables, lambda table: iter(rows))
			with sqlite3.connect(path) as connection:
				sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'umbracoNode'").fetchone()[0]
				found = connection.execute("SELECT * FROM umbracoNode ORDER BY id").fetchall()
				mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
				connection.execute("INSERT INTO umbracoNode (uniqueId, trashed, createDate) VALUES ('C8B5C3D2-1E4F-4A6B-9C8D-7E6F5A4B3C2F', 0, '2024-01-01')")
				next_id = connection.execute("SELECT MAX(id) FROM umbracoNode").fetchone()[0]
			connection.close()
			self.assertFalse((Path(folder) / "Umbraco.sqlite.db.converting").exists())
		self.assertIn('"id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT', sql)
		self.assertIn('"uniqueId" TEXT COLLATE NOCASE NOT NULL', sql)
		self.assertIn("DEFAULT CURRENT_TIMESTAMP", sql)
		self.assertEqual(found[0], (1, "A8B5C3D2-1E4F-4A6B-9C8D-7E6F5A4B3C2D", 0, "2024-03-01 10:15:30.123", "12.500000", b"\x01\x02"))
		self.assertEqual(found[1][3], "2024-03-01 10:15:30")
		self.assertEqual(found[1][2], 1)
		self.assertEqual(mode, "wal")
		self.assertEqual(next_id, 3)

	def test_an_existing_file_is_never_overwritten(self):
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "site.db"
			path.write_bytes(b"mine")
			with self.assertRaises(ValueError):
				convert.write_sqlite(path, [], lambda table: iter(()))
			self.assertEqual(path.read_bytes(), b"mine")

	def test_a_failed_conversion_leaves_no_file(self):
		tables = [{"name": "t", "schema": "dbo", "primary_key": [], "identity": None, "indexes": [], "types": {},
		           "columns": [convert.column("n", "int")]}]
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "site.db"
			with self.assertRaises(ValueError):
				convert.write_sqlite(path, tables, lambda table: iter([["many"]]))
			self.assertEqual(list(Path(folder).iterdir()), [])


class FromSqliteTests(unittest.TestCase):
	def test_rows_are_counted_on_both_sides(self):
		with tempfile.TemporaryDirectory() as folder:
			path = umbraco_database(folder)
			ran = []
			with mock.patch.object(convert, "mssql_empty", return_value=True), \
					mock.patch.object(convert, "run_mssql_script", side_effect=lambda settings, database, script: ran.append(Path(script).read_text())), \
					mock.patch.object(convert, "server_counts", return_value={"umbracoNode": 2, "umbracoUser": 1, "cmsDictionary": 1, "umbracoKeyValue": 1, "umbracoDocumentVersion": 0}):
				result = convert.from_sqlite(path, "mssql", {}, "site", umbraco=True)
			self.assertEqual(result, {"tables": 5, "rows": 5})
			self.assertIn("CREATE TABLE [umbracoNode]", ran[0])
			with mock.patch.object(convert, "mssql_empty", return_value=True), \
					mock.patch.object(convert, "run_mssql_script"), \
					mock.patch.object(convert, "server_counts", return_value={}), self.assertRaisesRegex(RuntimeError, "rows in SQLite"):
				convert.from_sqlite(path, "mssql", {}, "site")

	def test_umbraco_is_never_converted_to_postgresql(self):
		with self.assertRaisesRegex(ValueError, "Umbraco"):
			convert.from_sqlite("x.db", "postgres", {}, "site", umbraco=True)

	def test_only_into_an_empty_database(self):
		with mock.patch.object(convert, "postgres_empty", return_value=False), self.assertRaisesRegex(ValueError, "empty"):
			convert.from_sqlite("x.db", "postgres", {}, "site")


if __name__ == "__main__":
	unittest.main()
