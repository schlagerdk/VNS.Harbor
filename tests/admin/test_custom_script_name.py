"""New script takes only what a script's name may hold, as it is typed: a space could be typed and was refused only
when Create was clicked (1.0.77)"""
import json
from pathlib import Path
import subprocess
import unittest

APP_JS = Path(__file__).resolve().parents[2] / "admin/static/app.js"


class CustomScriptNameTests(unittest.TestCase):
	def test_what_is_typed_becomes_a_name_the_server_takes(self):
		text = APP_JS.read_text()
		start = text.index("const scriptNameFilter = ")
		harness = """
%s
console.log(JSON.stringify(["clean up temp", "Ryd op æøå", "a/b;rm -rf", ".hidden", "ok-name_1.2", "café"].map(scriptNameFilter)));
""" % text[start:text.index(";\n", start) + 1]
		result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(json.loads(result.stdout), ["clean_up_temp", "Ryd_op_aeoeaa", "abrm_-rf", "hidden", "ok-name_1.2", "cafe"])
		# New script and Rename… (1.0.77) are given the filter, and wait for a valid name
		body = text[text.index("async function newCustomScript("):]
		self.assertIn("filter: scriptNameFilter, valid: validScriptName", body[:body.index("\n}\n")])
		rename = text[text.index('$("#editor-rename").onclick'):]
		self.assertIn("filter: scriptNameFilter, valid: (text) => validScriptName(text)", rename[:rename.index("\n});\n")])



class ScriptsSafeTests(unittest.TestCase):
	def test_scripts_someone_else_can_change_are_told(self):
		"""A scripts folder that is not root's alone is shown in the menu, to be made root's there (1.0.77)"""
		import ast
		import os
		import tempfile
		tree = ast.parse((APP_JS.parents[1] / "app.py").read_text())
		ns = {"os": os}
		exec(compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("owned_by_root", "scripts_safe")],
								type_ignores=[]), "app.py", "exec"), ns)
		with tempfile.TemporaryDirectory() as root:
			self.assertTrue(ns["scripts_safe"](os.path.join(root, "scripts")), "no scripts folder: nothing to make safe")
			folder = os.path.join(root, "scripts")
			os.mkdir(folder)
			open(os.path.join(folder, "delete_empty_folders.sh"), "w").close()
			# Made by the tests' own user, not root
			self.assertEqual(ns["scripts_safe"](folder), os.geteuid() == 0)


if __name__ == "__main__":
	unittest.main()
