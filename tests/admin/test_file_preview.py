"""Files shows pictures and PDFs (1.0.79): sent as they are with ?inline=1, framed by the admin only, nothing of them run;
SVG and HTML, which could run in the admin, are only downloaded"""
import ast
import io
import os
from pathlib import Path
import unittest

from flask import Flask, abort, send_file

APP = Path(__file__).resolve().parents[2] / "admin/app.py"


class FilePreviewTests(unittest.TestCase):
	def setUp(self):
		self.app = Flask(__name__)
		self.app.testing = True
		ns = {"app": self.app, "os": os, "send_file": send_file, "abort": abort, "request": __import__("flask").request}
		tree = ast.parse(APP.read_text())
		nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("file_response", "headers")
				 or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PREVIEW_TYPES"]
		exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), ns)

		@self.app.route("/file/<name>")
		def file(name):
			return ns["file_response"](io.BytesIO(b"content"), name)

		self.client = self.app.test_client()

	def test_a_picture_or_pdf_is_shown_and_nothing_of_it_runs(self):
		for name, kind in (("Billede.jpg", "image/jpeg"), ("Faktura.PDF", "application/pdf"), ("logo.png", "image/png")):
			response = self.client.get(f"/file/{name}?inline=1")
			self.assertEqual(response.status_code, 200, name)
			self.assertEqual(response.mimetype, kind)
			self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))
			self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
			self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
			self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
			self.assertNotIn("X-Harbor-Preview", response.headers)

	def test_what_could_run_is_only_downloaded(self):
		for name in ("drawing.svg", "page.html", "script.js"):
			self.assertEqual(self.client.get(f"/file/{name}?inline=1").status_code, 400, name)
			download = self.client.get(f"/file/{name}")
			self.assertTrue(download.headers["Content-Disposition"].startswith("attachment"))
			self.assertEqual(download.headers["X-Frame-Options"], "DENY")
			self.assertIn("frame-ancestors 'none'", download.headers["Content-Security-Policy"])


if __name__ == "__main__":
	unittest.main()
