"""Settings, Convert (phase 11): the task the admin starts for a site's database. The connection's password and the
new login's go in the task's environment, never on its command line, and only a site here is converted"""
import ast
import json
from pathlib import Path
import re
import unittest
from unittest import mock

import flask
from werkzeug.exceptions import BadRequest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "admin/app.py"


FUNCTIONS = ("convert_command", "convert_move_command", "convert_remote_unit", "convert_kept_item", "convert_connection", "convert_host")
KEPT = {"id": "a" * 32, "unit": "example.com", "database": "", "file": "umbraco/Data/Umbraco.sqlite.db.bak",
        "servers": [{"engine": "mssql", "connection": "made-on-test", "host": "sql.example.com", "port": 1433}], "server": None}


def lifted(remote_kept=None):
	tree = ast.parse(APP.read_text())
	nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
	         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("CONVERT_NAME", "CONVERT_LOCAL_HOSTS")]
	connection = {"id": "c1", "name": "Local", "host": ".", "port": 1433, "username": "sa", "password": "server secret", "engine": "mssql"}
	remote = {"id": "c2", "name": "SQL", "host": "SQL.example.com", "port": 1433, "username": "sa", "password": "other secret", "engine": "mssql"}
	found = {"c1": connection, "c2": remote}
	sql = mock.Mock(connection=mock.Mock(side_effect=lambda store, identifier: found[identifier] if identifier in found else (_ for _ in ()).throw(ValueError("Unknown"))),
	                connections=lambda store: [connection, remote], engine=lambda item: item.get("engine", "mssql"))
	ns = {"re": re, "json": json, "os": __import__("os"), "abort": flask.abort, "HARBOR_COMMAND": "/opt/harbor/current/bin/harbor",
	      "SQL_LOCK": mock.MagicMock(), "SQL_CONNECTIONS": "/x/sql-connections.json", "_sql_module": sql,
	      "convert_units": lambda: [("example.com", "example.com", "Production", "example.com"), ("example.com", "test-example.com", "Test", "test.example.com")],
	      "convert_remote_units": lambda: [("example.com", "prod", "example.com", "Production", "example.com", "prod-host", ("prod.example.com", "root", "22"))],
	      "convert_remote_info": lambda site, target, units: ({"sites": [], "kept": remote_kept or []}, ""),
	      "convert_address_for": lambda host: "10.0.0.1", "convert_own_addresses": lambda: {"10.0.0.1"},
	      "convert_source": lambda data, remote: ({"engine": "mssql", "database": "site"}, {"id": "c1", "host": "."}, ".", ""),
	      "me": lambda: {"name": "Admin"}}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
	return ns["convert_command"]


class ConvertCommandTests(unittest.TestCase):
	def setUp(self):
		self.command = lifted()

	def test_to_a_server_with_the_passwords_in_its_environment(self):
		title, argv, env = self.command({"kind": "convert_site", "unit": "test-example.com", "to": "mssql", "connection": "c1",
		                                 "database": "example_test", "login": "example_test", "password": "login secret"})
		self.assertEqual(title, "Convert test-example.com to Microsoft SQL Server")
		self.assertEqual(argv, ["/usr/bin/python3", "/opt/harbor/current/bin/harbor", "site", "convert", "test-example.com", "mssql", "--by", "Admin",
		                        "--database", "example_test", "--login", "example_test"])
		self.assertNotIn("secret", " ".join(argv))
		self.assertEqual(env["VNS_CONVERT_LOGIN_PASSWORD"], "login secret")
		self.assertEqual(json.loads(env["VNS_CONVERT_CONNECTION"])["password"], "server secret")

	def test_to_sqlite(self):
		title, argv, env = self.command({"kind": "convert_site", "unit": "example.com", "to": "sqlite", "connection": "c1", "file": "Umbraco-2.sqlite.db"})
		self.assertEqual(argv[-2:], ["--file", "Umbraco-2.sqlite.db"])
		self.assertNotIn("VNS_CONVERT_LOGIN_PASSWORD", env)

	def test_a_name_with_dots_as_the_sites(self):
		_title, argv, _env = self.command({"kind": "convert_site", "unit": "test-example.com", "to": "mssql", "connection": "c1",
		                                   "database": "test.example.com", "login": "test.example.com", "password": "long enough"})
		self.assertEqual(argv[-4:], ["--database", "test.example.com", "--login", "test.example.com"])

	def test_refused(self):
		with flask.Flask(__name__).app_context():
			for payload in ({"unit": "other.example.com", "to": "mssql"},
			                {"unit": "example.com", "to": "oracle", "connection": "c1"},
			                {"unit": "example.com", "to": "postgres", "connection": "c1", "database": "a", "login": "a", "password": "long enough"},
			                {"unit": "example.com", "to": "mssql", "connection": "c1", "database": ".bad", "login": "a", "password": "long enough"},
			                {"unit": "example.com", "to": "mssql", "connection": "c1", "database": "a", "login": "a", "password": "short"},
			                {"unit": "example.com", "to": "sqlite", "connection": "c1", "file": "../../etc/passwd"},
			                {"unit": "example.com", "to": "mssql", "connection": "gone", "database": "a", "login": "a", "password": "long enough"}):
				with self.subTest(payload=payload), self.assertRaises(BadRequest):
					self.command({"kind": "convert_site", **payload})


class RemoteTests(unittest.TestCase):
	"""An environment on another server: converted, rolled back and deleted by the Harbor there"""
	def test_converted_by_the_harbor_where_it_runs(self):
		title, argv, env = lifted()({"kind": "convert_site", "where": "prod-host", "unit": "example.com", "to": "mssql", "connection": "c2",
		                             "database": "site", "login": "site", "password": "login secret"})
		self.assertEqual(title, "Convert example.com on prod-host to Microsoft SQL Server")
		self.assertEqual(argv[2:9], ["site", "convert-remote", "example.com", "prod", "convert", "example.com", "mssql"])
		self.assertNotIn("secret", " ".join(argv))
		self.assertEqual(env["VNS_CONVERT_LOGIN_PASSWORD"], "login secret")

	def test_this_servers_own_sql_server_by_its_address(self):
		# localhost there is the other server itself: it is given this server's address instead
		_title, _argv, env = lifted()({"kind": "convert_site", "where": "prod-host", "unit": "example.com", "to": "mssql", "connection": "c1",
		                               "database": "site", "login": "site", "password": "long enough"})
		self.assertEqual(json.loads(env["VNS_CONVERT_CONNECTION"])["host"], "10.0.0.1")

	def test_rolled_back_here_on_this_servers_own_by_its_address(self):
		own = {**KEPT, "servers": [{"engine": "mssql", "connection": "made-elsewhere", "host": "10.0.0.1", "port": 1433}]}
		_title, _argv, env = lifted([own])({"kind": "convert_rollback", "where": "prod-host", "unit": "example.com", "id": "a" * 32})
		sent = json.loads(env["VNS_CONVERT_CONNECTIONS"])["made-elsewhere"]
		self.assertEqual((sent["id"], sent["host"]), ("c1", "10.0.0.1"))

	def test_rolled_back_from_either_server_with_this_ones_connection(self):
		# Made from the other Harbor: its connection's id is unknown here, so the one to the same host and port is sent
		title, argv, env = lifted([KEPT])({"kind": "convert_rollback", "where": "prod-host", "unit": "example.com", "id": "a" * 32})
		self.assertEqual(argv[-5:], ["convert-remote", "example.com", "prod", "convert-rollback", "a" * 32])
		self.assertEqual(json.loads(env["VNS_CONVERT_CONNECTIONS"])["made-on-test"]["id"], "c2")

	def test_no_rollback_without_a_connection_to_its_server(self):
		gone = {**KEPT, "servers": [{"engine": "mssql", "connection": "x", "host": "localhost", "port": 1433}]}
		with flask.Flask(__name__).app_context(), self.assertRaises(BadRequest):
			lifted([gone])({"kind": "convert_rollback", "where": "prod-host", "unit": "example.com", "id": "a" * 32})


class MoveTests(unittest.TestCase):
	def test_its_login_along_and_both_servers_in_its_environment(self):
		title, argv, env = lifted()({"kind": "convert_move", "unit": "example.com", "connection": "c2", "database": "site", "keep_login": True})
		self.assertEqual(argv[3:], ["convert-move", "example.com", "--database", "site", "--by", "Admin", "--keep-login"])
		self.assertEqual((json.loads(env["VNS_CONVERT_CONNECTION"])["id"], json.loads(env["VNS_CONVERT_SOURCE"])["id"]), ("c2", "c1"))
		self.assertNotIn("VNS_CONVERT_LOGIN_PASSWORD", env)
		self.assertNotIn("secret", " ".join(argv))

	def test_never_to_the_server_it_is_on(self):
		with flask.Flask(__name__).app_context(), self.assertRaises(BadRequest):
			lifted()({"kind": "convert_move", "unit": "example.com", "connection": "c1", "database": "site", "keep_login": True})


class SqlUsageTests(unittest.TestCase):
	def test_a_server_by_any_of_its_names(self):
		tree = ast.parse(APP.read_text())
		nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("sql_server_key", "convert_host")
		         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("CONVERT_LOCAL_HOSTS", "SQL_DEFAULT_PORTS")]
		ns = {"_sql_module": mock.Mock(own_addresses=lambda: {"10.0.0.1"})}
		exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
		key = ns["sql_server_key"]
		# This server's own, as localhost, . or its address, with the engine's port when none is named
		self.assertEqual(key("localhost", 1433, "mssql"), key(".", "", "mssql"))
		self.assertEqual(key("localhost", 1433, "mssql"), key("10.0.0.1", "1433", "mssql"))
		# Another server's own (localhost there) is that server
		self.assertEqual(key("localhost", "", "mssql", "10.0.0.2"), key("10.0.0.2", 1433, "mssql"))
		self.assertNotEqual(key("10.0.0.2", 1433, "mssql"), key("10.0.0.2", 1434, "mssql"))


class SqlTransferTests(unittest.TestCase):
	def lifted(self, usage=None):
		tree = ast.parse(APP.read_text())
		nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "sql_transfer_command"
		         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CONVERT_NAME"]
		found = {"a": {"id": "a", "name": "Here", "host": "localhost", "username": "sa", "password": "one secret"},
		         "b": {"id": "b", "name": "Other", "host": "10.0.0.2", "username": "sa", "password": "other secret", "ssh_enabled": True}}
		sql = mock.Mock(connection=lambda store, identifier: found[identifier] if identifier in found else (_ for _ in ()).throw(ValueError("Unknown")),
		                engine=lambda item: item.get("engine", "mssql"), identifier=lambda name: name, backup_file_access=lambda item: True)
		ns = {"re": re, "json": json, "abort": flask.abort, "HARBOR_COMMAND": "/opt/harbor/current/bin/harbor", "SQL_LOCK": mock.MagicMock(),
		      "SQL_CONNECTIONS": "/x", "_sql_module": sql, "sql_database_usage": lambda identifier: usage or {}}
		exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
		return ns["sql_transfer_command"]

	def test_both_servers_and_the_password_in_its_environment(self):
		title, argv, env = self.lifted()({"connection": "a", "target": "b", "database": "shop", "name": "shop", "login": "shop", "password": "long enough", "move": True})
		self.assertEqual(title, "Move shop to Other")
		self.assertEqual(argv[2:], ["site", "convert-transfer", "shop", "--name", "shop", "--login", "shop", "--move"])
		self.assertNotIn("secret", " ".join(argv))
		self.assertEqual((json.loads(env["VNS_CONVERT_SOURCE"])["id"], json.loads(env["VNS_CONVERT_CONNECTION"])["id"]), ("a", "b"))

	def test_one_a_site_runs_on_is_moved_with_its_site(self):
		with flask.Flask(__name__).app_context(), self.assertRaises(BadRequest):
			self.lifted({"shop": [{"label": "shop.example.com (Production)"}]})({"connection": "a", "target": "b", "database": "shop", "name": "shop",
			                                                                     "login": "shop", "password": "long enough", "move": True})


class DeleteSiteDatabaseTests(unittest.TestCase):
	def test_the_server_a_connection_string_names(self):
		tree = ast.parse(APP.read_text())
		nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "database_server"]
		ns = {}
		exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
		self.assertEqual(ns["database_server"]({"server": "10.0.0.1,1433"}), ("10.0.0.1", "1433"))
		self.assertEqual(ns["database_server"]({"server": "tcp:sql.example.com"}), ("sql.example.com", ""))
		self.assertEqual(ns["database_server"]({"server": "sql\\EXPRESS"}), ("sql", ""))


if __name__ == "__main__":
	unittest.main()
