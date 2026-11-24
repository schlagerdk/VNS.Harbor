"""Temporary addresses (1.0.77): domains whose every name points here, set under Settings, General; a new site can have
a name under one until its own domain is ready"""
import ast
import json
from pathlib import Path
import subprocess
import threading
import unittest

from flask import Flask, jsonify

ADMIN = Path(__file__).resolve().parents[2] / "admin"
NAMES = {"temp_domains", "temp_domains_shown", "wildcard_points_here", "api_temp_domains"}


class Abort(Exception):
	pass


class TempDomainTests(unittest.TestCase):
	def setUp(self):
		self.app = Flask(__name__)
		self.app.testing = True
		self.cfg = {}
		self.pointing = set()
		self.saved = 0
		self.user = {"id": "root", "role": "admin"}

		def abort(code, message=""):
			raise Abort(code, message)

		def save_config():
			self.saved += 1

		ns = {"app": self.app, "cfg": self.cfg, "jsonify": jsonify, "request": __import__("flask").request, "secrets": __import__("secrets"),
			  "CONFIG_LOCK": threading.Lock(), "me": lambda: self.user, "require_admin": lambda: None if self.user["role"] == "admin" else abort(403),
			  "error": lambda message: (jsonify(error=message), 400), "abort": abort, "save_config": save_config, "audit": lambda text: None,
			  "access": type("access", (), {"is_admin": staticmethod(lambda user: user["role"] == "admin")}),
			  "dns_name": lambda value, label, fully_qualified=False: value if "." in value else abort(400, "Invalid " + label),
			  "display_dns_name": lambda name: name.replace("xn--mjk-ula", "mjøk"), "detected_public_address": lambda: "192.0.2.10",
			  # *.domain points here when a name under it resolves here
			  "points_here": lambda name: name.split(".", 1)[1] in self.pointing}
		tree = ast.parse((ADMIN / "app.py").read_text())
		nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
		self.assertEqual({node.name for node in nodes}, NAMES)
		exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), ns)
		self.client = self.app.test_client()

	def test_a_domain_is_added_only_when_every_name_under_it_points_here(self):
		response = self.client.post("/api/temp-domains", json={"name": "example.net"})
		self.assertEqual(response.status_code, 400)
		self.assertIn("*.example.net does not point to this server", response.json["error"])
		self.assertIn("* A 192.0.2.10", response.json["error"])
		self.assertEqual(self.cfg.get("temp_domains", []), [])
		self.pointing |= {"example.net", "xn--mjk-ula.dk"}
		for name in ("example.net", "*.xn--mjk-ula.dk"):
			self.assertEqual(self.client.post("/api/temp-domains", json={"name": name}).status_code, 200)
		self.assertEqual(self.cfg["temp_domains"], ["example.net", "xn--mjk-ula.dk"], "kept without *., sorted")
		self.assertEqual(self.client.post("/api/temp-domains", json={"name": "example.net"}).status_code, 400, "not twice")
		self.assertEqual(self.client.get("/api/temp-domains").json["domains"][1], {"name": "xn--mjk-ula.dk", "display": "mjøk.dk"})
		self.assertEqual(self.client.delete("/api/temp-domains?name=example.net").json["domains"], [{"name": "xn--mjk-ula.dk", "display": "mjøk.dk"}])

	def test_a_user_who_may_create_sites_reads_them_and_changes_nothing(self):
		self.cfg["temp_domains"] = ["example.net"]
		self.user = {"id": "u_1", "role": "user", "create_sites": True}
		self.assertEqual(self.client.get("/api/temp-domains").json["domains"], [{"name": "example.net", "display": "example.net"}])
		with self.assertRaises(Abort):
			self.client.post("/api/temp-domains", json={"name": "example.org"})
		self.user = {"id": "u_2", "role": "user"}
		with self.assertRaises(Abort):
			self.client.get("/api/temp-domains")

	def test_the_binding_is_made_from_the_folder_name(self):
		text = (ADMIN / "static/app.js").read_text()
		start = text.index("const tempLabel = ")
		harness = """
%s
console.log(JSON.stringify(["www.example.com", "Shop.DK", "mjøk.dk", "", "x".repeat(80) + ".dk", "café.dk"].map(tempLabel)));
""" % text[start:text.index(";\n", start) + 1]
		result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		labels = json.loads(result.stdout)
		self.assertEqual(labels[:4], ["www-example-com", "shop-dk", "mjoek-dk", "new-site"], "æ, ø and å as ae, oe and aa")
		self.assertEqual(labels[5], "cafe-dk")
		self.assertEqual(len(labels[4]), 63, "a DNS label is at most 63")


if __name__ == "__main__":
	unittest.main()
