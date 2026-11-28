"""A Windows server's sites on Web Server, in the admin: listed or not from Servers (its folders left out by name), and
where each is reached noted here only, checked"""
import ast
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

import flask
from flask import abort

APP = Path(__file__).resolve().parents[2] / "admin/app.py"


def lifted(**names):
	nodes = [node for node in ast.parse(APP.read_text()).body if isinstance(node, ast.FunctionDef)
	         and node.name in ("servers_command", "windows_folder_ok", "windows_sites_notes", "api_windows_site_note")]
	ns = {"abort": abort, "re": re, "os": os, "json": json, "HARBOR_COMMAND": "/vns/bin/harbor", "has_members": lambda: True,
	      "WINDOWS_SITES_SEEN": {}, "WINDOWS_SITES_LOCK": threading.Lock(), "require_admin": lambda: None, "audit": lambda message: None,
	      "jsonify": flask.jsonify, "request": flask.request, "error": lambda message, status=400: (flask.jsonify(error=message), status), **names}
	for node in nodes:
		node.decorator_list = []
	exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
	return ns


class WindowsSitesAdminTests(unittest.TestCase):
	def test_listed_from_servers_on_a_master_too(self):
		command = lifted()["servers_command"]
		title, argv, _env = command({"action": "windows_sites", "name": "iis", "on": True, "exclude": ["old backup", "Temp"]})
		self.assertEqual(argv[2:], ["servers", "windows-sites", "iis", "on", "--exclude", "old backup", "--exclude", "Temp"])
		self.assertIn("List the sites of iis", title)
		self.assertEqual(command({"action": "windows_sites", "name": "iis", "on": False})[1][2:], ["servers", "windows-sites", "iis", "off"])
		# A folder is a name, never a path
		for exclude in (["../etc"], ["a/b"], "Temp", [""]):
			with self.subTest(exclude=exclude), self.assertRaises(Exception):
				command({"action": "windows_sites", "name": "iis", "on": True, "exclude": exclude})

	def test_binding_and_backend_noted_here(self):
		with tempfile.TemporaryDirectory() as folder:
			notes = os.path.join(folder, "windows-sites.json")
			ns = lifted(WINDOWS_SITES_NOTES=notes)
			app = flask.Flask(__name__)

			def put(site, body):
				with app.test_request_context(json=body, method="PUT"):
					answer = ns["api_windows_site_note"]("iis", site)
					return answer if isinstance(answer, tuple) else (answer, 200)
			self.assertEqual(put("Alpha", {"binding": "WWW.Example.com", "backend": "https://www.example.com/umbraco"})[1], 200)
			self.assertEqual(json.loads(Path(notes).read_text()), {"iis": {"Alpha": {"binding": "www.example.com", "backend": "https://www.example.com/umbraco"}}})
			self.assertEqual(put("Alpha", {"binding": "not a name"})[1], 400)
			self.assertEqual(put("Alpha", {"backend": "javascript:alert(1)"})[1], 400)
			# Both empty: forgotten
			put("Alpha", {})
			self.assertEqual(json.loads(Path(notes).read_text()), {"iis": {}})


if __name__ == "__main__":
	unittest.main()
