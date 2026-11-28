"""The order of a master's members, chosen by an administrator: shown so everywhere (phase 13)"""
import ast
import io
import json
from pathlib import Path
import threading
import unittest

from flask import Flask, jsonify, request

ROOT_DIR = Path(__file__).resolve().parents[2]


def functions(*names, **given):
	"""Functions of the admin by their names, alone, with what they use given"""
	source = (ROOT_DIR / "admin/app.py").read_text()
	nodes = [item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name in names]
	ns = {"app": Flask(__name__), "request": request, "jsonify": jsonify, "json": json, "audit": lambda message: None,
		"CONFIG_LOCK": threading.Lock(), "save_config": lambda: None, "require_admin": lambda: None,
		"error": lambda message, status=400: (jsonify(error=message), status), **given}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), "admin", "exec"), ns)
	return ns


class ServerOrderTests(unittest.TestCase):
	def setUp(self):
		servers = {"zeta": {"member": True}, "alpha": {"member": True}, "beta": {"member": True}, "other": {}}
		self.cfg = {}
		self.ns = functions("member_names", "in_server_order", "api_master_server_order", cfg=self.cfg,
			open=lambda path, encoding=None: io.StringIO(json.dumps(servers)))

	def test_by_name_until_an_order_is_chosen_and_new_ones_after_it(self):
		self.assertEqual(self.ns["member_names"](), ["alpha", "beta", "zeta"])
		self.cfg["server_order"] = ["zeta", "gone", "alpha"]
		self.assertEqual(self.ns["member_names"](), ["zeta", "alpha", "beta"])
		self.assertEqual([item["server"] for item in self.ns["in_server_order"]([{"server": "beta"}, {"server": "alpha"}, {"server": "zeta"}])],
			["zeta", "alpha", "beta"])

	def test_only_members_are_ordered(self):
		client = self.ns["app"].test_client()
		answer = client.put("/api/master/server-order", json={"order": ["beta", "zeta", "alpha"]})
		self.assertEqual(answer.json["members"], ["beta", "zeta", "alpha"])
		self.assertEqual(client.put("/api/master/server-order", json={"order": ["beta", "other"]}).status_code, 400)
		self.assertEqual(self.cfg["server_order"], ["beta", "zeta", "alpha"])


class DatabaseUsageTests(unittest.TestCase):
	def test_the_sites_of_every_member_on_a_members_databases(self):
		"""sql-01's databases on the master's SQL Server: the sites on prod-01 and test-01 that run on them, each once
		(an environment on prod-01 managed from test-01 is told by both), found by sql-01's address"""
		import sys
		from unittest import mock
		sys.path.insert(0, str(ROOT_DIR))
		from harbor.server import master
		from concurrent.futures import ThreadPoolExecutor
		members = {"sql-01": {"host": "sql-01"}, "prod-01": {"host": "prod-01"}, "test-01": {"host": "test-01"}}
		asked = []

		def ask(system, name, entry, method, path, **more):
			asked.append((name, path))
			if path == "/api/sql/connections":
				return {"status": 200, "body": {"connections": [{"id": "c1", "host": "localhost", "port": 1433}]}}
			if name == "sql-01":
				return {"status": 200, "body": {"usage": {}}}
			if name == "prod-01":
				return {"status": 200, "body": {"usage": {"shop": [{"label": "shop.dk (Production)", "site": "shop.dk", "unit": "shop.dk", "where": ""}]}}}
			return {"status": 200, "body": {"usage": {"shop": [{"label": "shop.dk (Production, on prod-01)", "site": "shop.dk", "unit": "shop.dk", "where": "prod-01"}],
				"blog": [{"label": "blog.dk (Test)", "site": "blog.dk", "unit": "blog.dk", "where": ""}]}}}
		ns = functions("api_master_sql_usage", re=__import__("re"), ThreadPoolExecutor=ThreadPoolExecutor, member_names=lambda: list(members))
		with mock.patch.object(master, "ask", side_effect=ask), mock.patch.object(master, "members", return_value=members), \
				mock.patch.object(master, "reached_at", return_value="172.23.3.21"):
			usage = ns["app"].test_client().get("/api/master/sql-usage?server=sql-01&connection=c1").json["usage"]
		self.assertEqual([(item["unit"], item["server"]) for item in usage["shop"]], [("shop.dk", "test-01")])
		self.assertEqual([item["server"] for item in usage["blog"]], ["test-01"])
		self.assertIn(("prod-01", "/api/sql/usage?host=172.23.3.21&port=1433&engine=mssql"), asked)


if __name__ == "__main__":
	unittest.main()
