import ast
import os
from pathlib import Path
import tempfile
import unittest

from werkzeug.exceptions import BadRequest
from flask import abort


APP = Path(__file__).resolve().parents[2] / "admin/app.py"


def lifted(**names):
	"""web_state and takes_no_sites from app.py, with the markers where the test puts them"""
	nodes = [node for node in ast.parse(APP.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in ("web_state", "takes_no_sites")]
	ns = {"os": os, "abort": abort, **names}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), "web state", "exec"), ns)
	return ns


class WebStateTests(unittest.TestCase):
	def test_a_server_that_keeps_its_sites_takes_no_new_one(self):
		with tempfile.TemporaryDirectory() as folder:
			ns = lifted(NO_WEB=os.path.join(folder, "no-web"), CLOSED=os.path.join(folder, "no-new-sites"))
			self.assertEqual(ns["web_state"](), "on")
			ns["takes_no_sites"]("make the site")
			Path(ns["CLOSED"]).touch()
			self.assertEqual(ns["web_state"](), "closed")
			with self.assertRaises(BadRequest) as refused:
				ns["takes_no_sites"]("make the site")
			self.assertIn("keeps its sites", refused.exception.description)
			Path(ns["NO_WEB"]).touch()
			self.assertEqual(ns["web_state"](), "off")
			with self.assertRaises(BadRequest) as refused:
				ns["takes_no_sites"]("make the copy")
			self.assertIn("hosts no sites", refused.exception.description)


if __name__ == "__main__":
	unittest.main()
