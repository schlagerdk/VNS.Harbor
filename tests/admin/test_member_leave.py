"""A member that leaves its master, or is removed: the master's users go from it, and its access from the master's users
and templates (phase 13)"""
import ast
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from flask import Flask, jsonify, request

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402


def functions(*names, **given):
	"""Functions of the admin by their names, alone, with what they use given"""
	source = (ROOT_DIR / "admin/app.py").read_text()
	nodes = [item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name in names]
	ns = {"app": Flask(__name__), "request": request, "jsonify": jsonify, "access": access, "audit": lambda message: None, "re": __import__("re"),
		"os": __import__("os"), "CONFIG_LOCK": threading.Lock(), "save_config": lambda: None, "linux_users_changed": lambda: None,
		"require_admin": lambda: None, "error": lambda message, status=400: (jsonify(error=message), status), **given}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), "admin", "exec"), ns)
	return ns


class LeaveTests(unittest.TestCase):
	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.own = Path(folder.name) / "notifications-own.json"
		self.own.write_text('["health"]')

	def test_the_masters_users_go_and_its_own_stay(self):
		cfg = {"users": [{"id": "u_aaaaaa", "username": "anna", "role": "user", "from_master": True},
			{"id": "u_bbbbbb", "username": "boss", "role": "admin", "from_master": True, "password_hash": "h"},
			{"id": "root", "username": "admin", "role": "admin", "password_hash": "x"}]}
		ns = functions("left_master", cfg=cfg, NOTIFICATIONS_OWN=str(self.own))
		self.assertEqual(ns["left_master"](), ["anna", "boss"])
		self.assertEqual([user["username"] for user in cfg["users"]], ["admin"])
		self.assertFalse(self.own.exists())

	def test_with_no_administrator_of_its_own_the_masters_stay_as_its_own(self):
		cfg = {"users": [{"id": "u_aaaaaa", "username": "anna", "role": "user", "from_master": True},
			{"id": "u_bbbbbb", "username": "boss", "role": "admin", "from_master": True, "password_hash": "h"}]}
		ns = functions("left_master", cfg=cfg, NOTIFICATIONS_OWN=str(self.own))
		self.assertEqual(ns["left_master"](), ["anna"])
		self.assertEqual(cfg["users"], [{"id": "u_bbbbbb", "username": "boss", "role": "admin", "password_hash": "h"}])

	def test_the_master_takes_the_members_access_off_its_users_and_templates(self):
		cfg = {"users": [{"id": "u_aaaaaa", "servers": {"two": {"web": True}, "three": {"sql": True}}}, {"id": "u_bbbbbb"}],
			"user_templates": [{"id": "t_1", "name": "Dev", "access": {"servers": {"two": {"web": True}}}}]}
		ns = functions("api_master_forget_member", cfg=cfg)
		client = ns["app"].test_client()
		self.assertEqual(client.post("/api/master/forget-member", json={"member": "two"}).json, {"changed": 2})
		self.assertEqual(cfg["users"][0]["servers"], {"three": {"sql": True}})
		self.assertEqual(cfg["user_templates"][0]["access"]["servers"], {})
		self.assertEqual(client.post("/api/master/forget-member", json={"member": "../x"}).status_code, 400)


if __name__ == "__main__":
	unittest.main()
