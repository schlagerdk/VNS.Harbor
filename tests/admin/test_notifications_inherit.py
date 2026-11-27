"""A master's notifications (its mail server, the site report, the usage, health and job alerts) are its members' too:
a member takes those it does not keep its own, and only from the master (phase 13)"""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

from flask import Flask, jsonify, request

ROOT_DIR = Path(__file__).resolve().parents[2]


def handler(name, **more):
	"""The admin's route by its name, alone, with what it uses given"""
	app = Flask(__name__)
	source = (ROOT_DIR / "admin/app.py").read_text()
	node = next(item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name == name)
	ns = {"app": app, "request": request, "jsonify": jsonify, "audit": lambda message: None, "NOTIFICATION_KINDS": ("mail", "report", "usage", "health", "jobs"),
		"error": lambda message, status=400: (jsonify(error=message), status), "subprocess": __import__("subprocess"), **more}
	exec(compile(ast.Module(body=[node], type_ignores=[]), name, "exec"), ns)
	return app.test_client()


class InheritTests(unittest.TestCase):
	def setUp(self):
		self.saved = {kind: Mock(return_value={}) for kind in ("mail", "usage", "health", "jobs")}
		self.keep_alive = Mock()
		self.keep_alive.load.return_value = {"enabled": True, "interval_minutes": 5}
		self.keep_alive.validate.side_effect = lambda data: data
		self.g = types.SimpleNamespace(from_master=True)

	def client(self, own):
		return handler("api_notifications_inherit", g=self.g, notifications_own=lambda: own, _keep_alive_module=self.keep_alive,
			save_mail_server=self.saved["mail"], save_usage_alerts=self.saved["usage"], save_health_alerts=self.saved["health"], save_job_alerts=self.saved["jobs"])

	def test_a_member_takes_the_masters_but_those_it_keeps_its_own(self):
		sent = {"mail": {"host": "mail.example.com"}, "report": {"enabled": False}, "usage": {"enabled": True, "emails": ["ops@example.com"]},
			"health": {"enabled": True}, "jobs": {"enabled": False, "emails": []}}
		answer = self.client(["health"]).put("/api/notifications/inherit", json=sent)
		self.assertEqual(answer.status_code, 200, answer.json)
		self.assertEqual(sorted(answer.json["taken"]), ["jobs", "mail", "report", "usage"])
		self.saved["health"].assert_not_called()
		self.saved["usage"].assert_called_once_with({"enabled": True, "emails": ["ops@example.com"]})
		# The report goes into keep-alive's settings, its interval kept
		self.keep_alive.save.assert_called_once_with({"enabled": True, "interval_minutes": 5, "report": {"enabled": False}})

	def test_only_the_master_sends_them(self):
		self.g.from_master = False
		self.assertEqual(self.client([]).put("/api/notifications/inherit", json={"mail": {}}).status_code, 403)
		self.saved["mail"].assert_not_called()


if __name__ == "__main__":
	unittest.main()
