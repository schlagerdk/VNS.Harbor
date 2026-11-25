"""A site's database converted (phase 11): the site's connection string found and changed in place, the rest of its
settings as they were, and put back when a step fails"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from harbor.core.system import Stop
from harbor.sites import convert

APPSETTINGS = """{
  // Umbraco's own
  "$schema": "appsettings-schema.json",
  "ConnectionStrings": {
    "umbracoDbDSN": "Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared;Foreign Keys=True;Pooling=True",
    "umbracoDbDSN_ProviderName": "Microsoft.Data.Sqlite"
  },
  "Umbraco": { "CMS": { "Global": { "Id": "x" } } },
}
"""


class Fake:
	def __init__(self, root):
		self.root = Path(root)
		self.runs, self.steps, self.said = [], [], []

	def path(self, absolute):
		return self.root / str(absolute).lstrip("/")

	def run(self, *argv, check=True, capture=False, **kwargs):
		self.runs.append(argv)
		return mock.Mock(ok=True, stdout="", stderr="")

	def plan(self, steps):
		self.planned = steps

	def step(self, text):
		self.steps.append(text)

	def say(self, text):
		self.said.append(text)

	warn = say

	def sleep(self, seconds):
		pass


class Site(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.system = Fake(self.temp.name)
		self.folder = "/var/www/example.com/_test"
		folder = self.system.path(self.folder)
		(folder / "umbraco/Data").mkdir(parents=True)
		with sqlite3.connect(folder / "umbraco/Data/Umbraco.sqlite.db") as connection:
			connection.execute("PRAGMA journal_mode = WAL")
			connection.execute("CREATE TABLE umbracoNode (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
			connection.execute("INSERT INTO umbracoNode (text) VALUES ('Home')")
		connection.close()
		(folder / "appsettings.json").write_text(APPSETTINGS)
		(folder / "appsettings.Production.json").write_text('{\n\t"Logging": {}\n}\n')
		service = self.system.path("/etc/systemd/system/example.com-test.service")
		service.parent.mkdir(parents=True)
		service.write_text(f"[Service]\nWorkingDirectory={self.folder}\nExecStart=/usr/bin/dotnet Site.dll --urls http://localhost:5123\n")

	def tearDown(self):
		self.temp.cleanup()


class FindTests(Site):
	def test_the_sites_sqlite_connection_and_file(self):
		folder, text = convert.working_folder(self.system, "example.com-test")
		self.assertEqual(folder, self.folder)
		self.assertEqual(convert.port_of(text), 5123)
		self.assertTrue(convert.is_umbraco(self.system, folder))
		entry = convert.site_connection(self.system, folder)
		self.assertEqual((entry["key"], entry["engine"], entry["files"]), ("umbracoDbDSN", "sqlite", ["appsettings.json"]))
		self.assertEqual(convert.sqlite_file(self.system, folder, entry["value"], True), self.folder + "/umbraco/Data/Umbraco.sqlite.db")

	def test_a_service_that_sets_its_own_connection_is_left_alone(self):
		service = self.system.path("/etc/systemd/system/example.com-test.service")
		service.write_text(service.read_text() + "Environment=ConnectionStrings__umbracoDbDSN=Server=x\n")
		with self.assertRaisesRegex(Stop, "sets a connection string itself"):
			convert.working_folder(self.system, "example.com-test")

	def test_a_file_outside_the_site_is_refused(self):
		with self.assertRaisesRegex(Stop, "outside"):
			convert.sqlite_file(self.system, self.folder, "Data Source=../../other/site.db", False)

	def test_describe(self):
		found = convert.describe(self.system, "example.com-test")
		self.assertEqual({key: found[key] for key in ("engine", "file", "umbraco", "convertible")},
		                 {"engine": "sqlite", "file": "umbraco/Data/Umbraco.sqlite.db", "umbraco": True, "convertible": True})


class ChangeTests(Site):
	def test_changed_in_place_with_the_provider(self):
		text = self.system.path(self.folder + "/appsettings.json").read_text()
		changed = convert.change_text(text, "umbracoDbDSN", 'Server=localhost,1433;Database=site;User Id=site;Password="a;b";', "Microsoft.Data.SqlClient")
		self.assertIn("// Umbraco's own", changed)
		self.assertIn('"umbracoDbDSN": "Server=localhost,1433;Database=site;User Id=site;Password=\\"a;b\\";"', changed)
		self.assertIn('"umbracoDbDSN_ProviderName": "Microsoft.Data.SqlClient"', changed)
		self.assertTrue(changed.endswith('"Umbraco": { "CMS": { "Global": { "Id": "x" } } },\n}\n'))

	def test_a_provider_is_added_after_its_connection(self):
		text = '{\n\t"ConnectionStrings": {\n\t\t"Default": "Data Source=app.db",\n\t\t"Other": "x"\n\t}\n}\n'
		changed = convert.change_text(text, "Default", "Host=db;Database=app", "Npgsql")
		self.assertEqual(changed, '{\n\t"ConnectionStrings": {\n\t\t"Default": "Host=db;Database=app",\n\t\t"Default_ProviderName": "Npgsql",\n'
		                          '\t\t"Other": "x"\n\t}\n}\n')

	def test_change_and_put_back(self):
		path = self.system.path(self.folder + "/appsettings.json")
		os.chmod(path, 0o640)
		entry = convert.site_connection(self.system, self.folder)
		before = convert.change_settings(self.system, self.folder, entry, "Server=x;Database=y", lambda old: "Microsoft.Data.SqlClient")
		self.assertIn('"umbracoDbDSN": "Server=x;Database=y"', path.read_text())
		self.assertEqual(path.stat().st_mode & 0o777, 0o640)
		convert.put_back(self.system, self.folder, before)
		self.assertEqual(path.read_text(), APPSETTINGS)

	def test_connection_strings(self):
		settings = {"host": ".", "port": 1433, "trust_certificate": True}
		self.assertEqual(convert.mssql_string(settings, "site", "site_user", "p;w"),
		                 'Server=localhost,1433;Database=site;User Id=site_user;Password="p;w";TrustServerCertificate=True;')
		self.assertEqual(convert.postgres_string({"host": "db.example.com", "port": 5432}, "app", "app", "pw"),
		                 "Host=db.example.com;Port=5432;Database=app;Username=app;Password=pw;SSL Mode=Prefer")
		self.assertEqual(convert.sqlite_string(self.folder, self.folder + "/umbraco/Data/Umbraco-2.sqlite.db", True),
		                 "Data Source=|DataDirectory|/Umbraco-2.sqlite.db;Cache=Shared;Foreign Keys=True;Pooling=True")
		self.assertEqual(convert.sqlite_string(self.folder, self.folder + "/App_Data/app.sqlite.db", False), "Data Source=App_Data/app.sqlite.db")


class ConvertTests(Site):
	def run_convert(self, failing=None, **kwargs):
		environment = {"VNS_CONVERT_CONNECTION": json.dumps({"id": "c1", "host": ".", "port": 1433, "username": "sa", "password": "x"}),
		               "VNS_CONVERT_LOGIN_PASSWORD": "a long password"}
		fake_sql = mock.Mock(query_json=mock.Mock(return_value=[]), literal=lambda text: "'" + text + "'", identifier=lambda text: "[" + text + "]")
		self.fake_sql = fake_sql
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=fake_sql))
		fake_convert.from_sqlite.side_effect = failing or (lambda *args, **kw: {"tables": 3, "rows": 9})
		with mock.patch.dict(os.environ, environment), mock.patch.object(convert, "converter", return_value=fake_convert), \
				mock.patch.object(convert, "responds", return_value=(True, "")):
			convert.convert_site(self.system, "example.com-test", "mssql", database="site", login="site_user", **kwargs)
		return fake_sql

	def test_to_sql_server(self):
		fake_sql = self.run_convert()
		self.assertEqual(self.system.steps, ["Create the database site and its login site_user on Microsoft SQL Server", "Stop example.com-test",
		                                     "Convert Umbraco.sqlite.db to site", "Change the connection string",
		                                     "Start example.com-test on Microsoft SQL Server"])
		fake_sql.create_restore_login.assert_called_once_with(mock.ANY, "site", "site_user", "a long password")
		text = self.system.path(self.folder + "/appsettings.json").read_text()
		self.assertIn('"umbracoDbDSN": "Server=localhost,1433;Database=site;User Id=site_user;Password=\\"a long password\\";TrustServerCertificate=True;"', text)
		self.assertIn('"umbracoDbDSN_ProviderName": "Microsoft.Data.SqlClient"', text)
		# Kept whole as .bak, so a conversion back has its name free and Roll back takes it again
		data = self.system.path(self.folder + "/umbraco/Data")
		names = sorted(path.name for path in data.iterdir())
		self.assertEqual(len(names), 1)
		self.assertRegex(names[0], r"^Umbraco\.sqlite\.db\.\d{8}-\d{6}\.bak$")
		with sqlite3.connect(data / names[0]) as connection:
			self.assertEqual(connection.execute("SELECT text FROM umbracoNode").fetchall(), [("Home",)])
		connection.close()
		kept = convert.records(self.system)
		self.assertEqual(kept[0]["from"], {"engine": "sqlite", "path": self.folder + "/umbraco/Data/Umbraco.sqlite.db",
		                                   "backup": self.folder + "/umbraco/Data/" + names[0]})
		self.assertEqual(kept[0]["to"]["database"], "site")
		self.assertNotIn("password", json.dumps(kept))

	def test_a_failed_conversion_puts_the_site_back(self):
		def fail(*args, **kwargs):
			raise RuntimeError("umbracoNode has 2 rows in SQLite and 0 in site")
		with self.assertRaisesRegex(Stop, "umbracoNode"):
			self.run_convert(failing=fail)
		self.assertEqual(self.system.path(self.folder + "/appsettings.json").read_text(), APPSETTINGS)
		self.assertTrue(self.system.path(self.folder + "/umbraco/Data/Umbraco.sqlite.db").is_file())
		self.assertIn(("systemctl", "start", "example.com-test.service"), self.system.runs)
		self.assertIn("DROP DATABASE [site]", self.fake_sql.execute.call_args_list[-1][0][1])
		self.assertIn("DROP LOGIN [site_user]", self.fake_sql.execute.call_args_list[-1][0][1])
		self.assertEqual(convert.records(self.system), [])

	def test_umbraco_never_to_postgresql(self):
		environment = {"VNS_CONVERT_CONNECTION": json.dumps({"engine": "postgres", "host": ".", "username": "postgres", "password": "x"}),
		               "VNS_CONVERT_LOGIN_PASSWORD": "a long password"}
		with mock.patch.dict(os.environ, environment), self.assertRaisesRegex(Stop, "Umbraco does not run on PostgreSQL"):
			convert.convert_site(self.system, "example.com-test", "postgres", database="site", login="site_user")

	def rollback(self):
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=self.fake_sql))
		with mock.patch.dict(os.environ, {"VNS_CONVERT_CONNECTION": json.dumps({"host": ".", "port": 1433, "username": "sa", "password": "x"})}), \
				mock.patch.object(convert, "converter", return_value=fake_convert), mock.patch.object(convert, "responds", return_value=(True, "")):
			convert.rollback_to(self.system, convert.records(self.system)[0]["id"])

	def test_roll_back(self):
		"""The site back on its SQLite file with the settings it had, the new database and login deleted"""
		self.run_convert()
		self.rollback()
		self.assertEqual(self.system.path(self.folder + "/appsettings.json").read_text(), APPSETTINGS)
		self.assertEqual(sorted(path.name for path in self.system.path(self.folder + "/umbraco/Data").iterdir()), ["Umbraco.sqlite.db"])
		dropped = self.fake_sql.execute.call_args_list[-1][0][1]
		self.assertIn("DROP DATABASE [site]", dropped)
		self.assertIn("DROP LOGIN [site_user]", dropped)
		self.assertEqual(convert.records(self.system), [])
		self.assertEqual(self.system.steps[-4], "Put the connection string back")
		self.assertRegex(self.system.steps[-3], r"^Umbraco\.sqlite\.db\.\d{8}-\d{6}\.bak is Umbraco\.sqlite\.db again$")
		self.assertEqual(self.system.steps[-2:], ["Start example.com-test on Umbraco.sqlite.db", "Delete site"])

	def test_roll_back_a_conversion_from_before_the_settings_were_kept(self):
		self.run_convert()
		items = convert.records(self.system)
		del items[0]["previous"]
		# Nor was the file set aside then
		del items[0]["from"]["backup"]
		os.rename(self.system.path(items[0]["from"]["path"] + ".bak").parent / next(
			path.name for path in self.system.path(self.folder + "/umbraco/Data").iterdir()), self.system.path(items[0]["from"]["path"]))
		convert.write_json(self.system.path(convert.RECORD), items)
		self.rollback()
		text = self.system.path(self.folder + "/appsettings.json").read_text()
		self.assertIn('"umbracoDbDSN": "Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared;Foreign Keys=True;Pooling=True"', text)
		self.assertIn('"umbracoDbDSN_ProviderName": "Microsoft.Data.Sqlite"', text)

	def test_nothing_to_roll_back_when_the_site_moved_on(self):
		self.run_convert()
		convert.put_back(self.system, self.folder, {"appsettings.json": APPSETTINGS.encode()})
		with self.assertRaisesRegex(Stop, "nothing to roll back"):
			self.rollback()

	def test_each_conversion_keeps_its_own_file(self):
		"""SQLite to SQL Server and back, twice: two files kept, each by when, and rolled back one after the other to the first"""
		data = self.system.path(self.folder + "/umbraco/Data")
		with mock.patch.object(convert.time, "strftime", return_value="20261125-120000"):
			self.run_convert()
			self.assertTrue((data / "Umbraco.sqlite.db.20261125-120000.bak").is_file())
			# As a conversion back makes it: a new file of the same name, the site on it again
			(data / "Umbraco.sqlite.db").write_bytes((data / "Umbraco.sqlite.db.20261125-120000.bak").read_bytes())
			convert.put_back(self.system, self.folder, {"appsettings.json": APPSETTINGS.encode()})
			items = convert.records(self.system)
			convert.write_json(self.system.path(convert.RECORD), items)
			self.run_convert()
		self.assertEqual(sorted(path.name for path in data.iterdir()), ["Umbraco.sqlite.db.20261125-120000-2.bak", "Umbraco.sqlite.db.20261125-120000.bak"])
		self.assertEqual(len(convert.records(self.system)), 2)

	def three_conversions(self):
		"""SQLite to SQL Server (site), back to SQLite, to SQL Server again (site1): the site on site1, the original
		kept as A.bak and the SQLite of the second as C.bak"""
		data = self.system.path(self.folder + "/umbraco/Data")
		original = data / "Umbraco.sqlite.db"
		os.rename(original, data / "Umbraco.sqlite.db.A.bak")
		(data / "Umbraco.sqlite.db.C.bak").write_bytes(b"second")
		sqlite_path, server = self.folder + "/umbraco/Data/Umbraco.sqlite.db", 'Server=localhost,1433;Database=site;User Id=site;Password=pw;TrustServerCertificate=True;'
		self.system.path(self.folder + "/appsettings.json").write_text(APPSETTINGS.replace(
			"Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared;Foreign Keys=True;Pooling=True", server.replace("site;", "site1;", 1)).replace(
			"Microsoft.Data.Sqlite", "Microsoft.Data.SqlClient"))
		sqlite_value = {"appsettings.json": {"value": "Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared;Foreign Keys=True;Pooling=True", "provider": "Microsoft.Data.Sqlite"}}
		items = [
			{"id": "one", "at": 1, "from": {"engine": "sqlite", "path": sqlite_path, "backup": sqlite_path + ".A.bak"}, "previous": sqlite_value,
			 "to": {"engine": "mssql", "database": "site", "login": "site", "connection": "c"}},
			{"id": "two", "at": 2, "from": {"engine": "mssql", "database": "site", "connection": "c"},
			 "previous": {"appsettings.json": {"value": server, "provider": "Microsoft.Data.SqlClient"}}, "to": {"engine": "sqlite", "path": sqlite_path}},
			{"id": "three", "at": 3, "from": {"engine": "sqlite", "path": sqlite_path, "backup": sqlite_path + ".C.bak"}, "previous": sqlite_value,
			 "to": {"engine": "mssql", "database": "site1", "login": "site1", "connection": "c"}}]
		convert.write_json(self.system.path(convert.RECORD), [{**item, "unit": "example.com-test", "folder": self.folder, "key": "umbracoDbDSN"} for item in items])
		self.fake_sql = mock.Mock(literal=lambda text: "'" + text + "'", identifier=lambda text: "[" + text + "]")
		return data

	def test_roll_back_to_an_older_conversion_in_one_go(self):
		"""Back to the first SQL Server: nothing converted, the second's SQLite and site1 deleted, the original kept"""
		data = self.three_conversions()
		self.assertEqual([item["id"] for item in convert.chain(self.system, "two")], ["three", "two"])
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=self.fake_sql))
		with mock.patch.dict(os.environ, {"VNS_CONVERT_CONNECTIONS": json.dumps({"c": {"host": ".", "port": 1433, "username": "sa", "password": "x"}})}), \
				mock.patch.object(convert, "converter", return_value=fake_convert), mock.patch.object(convert, "responds", return_value=(True, "")):
			convert.rollback_to(self.system, "two")
		fake_convert.from_sqlite.assert_not_called()
		fake_convert.to_sqlite.assert_not_called()
		self.assertIn('"umbracoDbDSN": "Server=localhost,1433;Database=site;', self.system.path(self.folder + "/appsettings.json").read_text())
		self.assertEqual(sorted(path.name for path in data.iterdir()), ["Umbraco.sqlite.db.A.bak"])
		dropped = [call[0][1] for call in self.fake_sql.execute.call_args_list]
		self.assertEqual(len(dropped), 1)
		self.assertIn("DROP DATABASE [site1]", dropped[0])
		self.assertEqual([item["id"] for item in convert.records(self.system)], ["one"])
		self.assertEqual(self.system.steps, ["Stop example.com-test", "Put the connection string back", "Start example.com-test on site",
		                                     "Delete Umbraco.sqlite.db.C.bak", "Delete site1"])

	def test_roll_back_to_the_original_in_one_go(self):
		data = self.three_conversions()
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=self.fake_sql))
		with mock.patch.dict(os.environ, {"VNS_CONVERT_CONNECTIONS": json.dumps({"c": {"host": ".", "port": 1433, "username": "sa", "password": "x"}})}), \
				mock.patch.object(convert, "converter", return_value=fake_convert), mock.patch.object(convert, "responds", return_value=(True, "")):
			convert.rollback_to(self.system, "one")
		self.assertIn("Umbraco.sqlite.db;Cache=Shared", self.system.path(self.folder + "/appsettings.json").read_text())
		self.assertEqual(sorted(path.name for path in data.iterdir()), ["Umbraco.sqlite.db"])
		with sqlite3.connect(data / "Umbraco.sqlite.db") as connection:
			self.assertEqual(connection.execute("SELECT text FROM umbracoNode").fetchall(), [("Home",)])
		connection.close()
		dropped = " ".join(call[0][1] for call in self.fake_sql.execute.call_args_list)
		self.assertIn("DROP DATABASE [site1]", dropped)
		self.assertIn("DROP DATABASE [site]", dropped)
		self.assertEqual(len(self.fake_sql.execute.call_args_list), 2)
		self.assertEqual(convert.records(self.system), [])

	def test_roll_back_two_from_sqlite_to_the_original(self):
		"""SQLite to SQL Server and back: the site on a new Umbraco.sqlite.db, which goes for the original to take its name"""
		data = self.three_conversions()
		items = [item for item in convert.records(self.system) if item["id"] != "three"]
		convert.write_json(self.system.path(convert.RECORD), items)
		(data / "Umbraco.sqlite.db.C.bak").rename(data / "Umbraco.sqlite.db")
		convert.put_back(self.system, self.folder, {"appsettings.json": APPSETTINGS.encode()})
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=self.fake_sql))
		with mock.patch.dict(os.environ, {"VNS_CONVERT_CONNECTIONS": json.dumps({"c": {"host": ".", "port": 1433, "username": "sa", "password": "x"}})}), \
				mock.patch.object(convert, "converter", return_value=fake_convert), mock.patch.object(convert, "responds", return_value=(True, "")):
			convert.rollback_to(self.system, "one")
		self.assertEqual(sorted(path.name for path in data.iterdir()), ["Umbraco.sqlite.db"])
		with sqlite3.connect(data / "Umbraco.sqlite.db") as connection:
			self.assertEqual(connection.execute("SELECT text FROM umbracoNode").fetchall(), [("Home",)])
		connection.close()
		self.assertEqual(convert.records(self.system), [])

	def test_a_failed_start_deletes_nothing(self):
		data = self.three_conversions()
		before = self.system.path(self.folder + "/appsettings.json").read_text()
		fake_convert = mock.Mock(sql_admin=mock.Mock(return_value=self.fake_sql))
		with mock.patch.dict(os.environ, {"VNS_CONVERT_CONNECTIONS": json.dumps({"c": {"host": ".", "port": 1433, "username": "sa", "password": "x"}})}), \
				mock.patch.object(convert, "converter", return_value=fake_convert), mock.patch.object(convert, "responds", return_value=(False, "HTTP 500")), \
				self.assertRaisesRegex(Stop, "HTTP 500"):
			convert.rollback_to(self.system, "one")
		self.assertEqual(self.system.path(self.folder + "/appsettings.json").read_text(), before)
		self.assertEqual(sorted(path.name for path in data.iterdir()), ["Umbraco.sqlite.db.A.bak", "Umbraco.sqlite.db.C.bak"])
		self.fake_sql.execute.assert_not_called()
		self.assertEqual(len(convert.records(self.system)), 3)

	def test_a_name_taken_again_stops_the_roll_back(self):
		self.run_convert()
		self.system.path(self.folder + "/umbraco/Data/Umbraco.sqlite.db").write_bytes(b"another")
		with self.assertRaisesRegex(Stop, "cannot take its name back"):
			self.rollback()
		self.assertEqual(self.system.path(self.folder + "/umbraco/Data/Umbraco.sqlite.db").read_bytes(), b"another")

	def test_the_old_sqlite_file_is_deleted_only_when_asked_and_not_in_use(self):
		self.run_convert()
		identifier = convert.records(self.system)[0]["id"]
		convert.delete_old(self.system, identifier)
		self.assertEqual(list(self.system.path(self.folder + "/umbraco/Data").iterdir()), [])
		self.assertEqual(convert.records(self.system), [])


if __name__ == "__main__":
	unittest.main()
