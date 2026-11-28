"""The badges on Settings' menu: what waits on an administrator, by its part (1.0.119)"""
import ast
import json
import logging
from pathlib import Path
import tempfile
import types
import unittest

APP = Path(__file__).resolve().parents[2] / "admin/app.py"


class FakeApp:
	logger = logging.getLogger("attention-test")

	def route(self, *args, **kwargs):
		return lambda function: function


def read_json(path, default):
	try:
		return json.loads(Path(path).read_text())
	except (OSError, ValueError):
		return default


def lifted(state, update=None, auto=None):
	tree = ast.parse(APP.read_text())
	nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "api_attention"]
	health = types.SimpleNamespace(read_json=read_json, STATE=state / "health")
	jobs = types.SimpleNamespace(read_json=read_json, STATE=state / "cron-watch")
	keep_alive = types.SimpleNamespace(report=types.SimpleNamespace(read_json=read_json), STATE=state / "keep-alive")
	namespace = {"app": FakeApp(), "require_admin": lambda: None, "jsonify": lambda data: data, "Path": Path, "DATA": str(state / "data"),
				 "_health": health, "_cron_watch": jobs, "_keep_alive_module": keep_alive,
				 "system_update_status": lambda: update or {"status": "current"}}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), "attention", "exec"), namespace)
	return namespace["api_attention"]


class AttentionTests(unittest.TestCase):
	def setUp(self):
		self.folder = tempfile.TemporaryDirectory()
		self.state = Path(self.folder.name)
		for name in ("health", "cron-watch", "keep-alive", "data"):
			(self.state / name).mkdir()

	def tearDown(self):
		self.folder.cleanup()

	def write(self, path, data):
		(self.state / path).write_text(json.dumps(data))

	def test_nothing_waiting_shows_no_badge(self):
		self.assertEqual(lifted(self.state)(), {})

	def test_each_part_counts_what_waits_there(self):
		self.write("health/alerts.json", {"open": {"disk:/": "/ is 95 % full", "memory": "92 % used"}})
		self.write("health/updates.json", {"security": 3, "reboot": True})
		self.write("cron-watch/alerts.json", {"open": {"a": {"name": "Backup"}}, "mail_error": {"error": "refused"}})
		self.write("data/usage-state.json", {"open": {"x": {}}})
		found = lifted(self.state, update={"status": "available", "mode": "release", "latest_version": "9.9.9"})()
		self.assertEqual(found["health"]["count"], 4)
		self.assertEqual(found["health"]["tone"], "red")
		self.assertIn("3 security updates", found["health"]["title"])
		self.assertEqual(found["jobs"], {"count": 1, "tone": "red", "title": "1 job failing: Backup"})
		self.assertEqual(found["usage"]["tone"], "amber")
		self.assertEqual(found["notifications"]["title"], "Could not be mailed: Job alerts")
		self.assertEqual(found["updates"], {"count": 1, "tone": "amber", "title": "Harbor v9.9.9 is out"})

	def test_only_updates_waiting_is_amber(self):
		self.write("health/updates.json", {"security": 0, "reboot": True})
		self.assertEqual(lifted(self.state)()["health"], {"count": 1, "tone": "amber", "title": "The server needs a restart"})


if __name__ == "__main__":
	unittest.main()
