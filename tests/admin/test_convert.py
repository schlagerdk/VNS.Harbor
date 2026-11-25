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


def lifted():
	tree = ast.parse(APP.read_text())
	nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "convert_command"
	         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CONVERT_NAME"]
	connection = {"id": "c1", "name": "Local", "host": ".", "port": 1433, "username": "sa", "password": "server secret", "engine": "mssql"}
	sql = mock.Mock(connection=mock.Mock(side_effect=lambda store, identifier: connection if identifier == "c1" else (_ for _ in ()).throw(ValueError("Unknown"))),
	                engine=lambda item: item.get("engine", "mssql"))
	ns = {"re": re, "json": json, "os": __import__("os"), "abort": flask.abort, "HARBOR_COMMAND": "/opt/harbor/current/bin/harbor",
	      "SQL_LOCK": mock.MagicMock(), "SQL_CONNECTIONS": "/x/sql-connections.json", "_sql_module": sql,
	      "convert_units": lambda: [("example.com", "example.com", "Production", "example.com"), ("example.com", "test-example.com", "Test", "test.example.com")],
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


if __name__ == "__main__":
	unittest.main()
