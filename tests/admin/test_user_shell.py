"""A user's shell holds only the folders they have Terminal on: an environment alone, without the main site's (1.0.82)"""
import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest

from flask import Flask, abort

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

APP = ROOT_DIR / "admin" / "app.py"


class UserShellTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.web = os.path.realpath(folder.name)
        self.site = os.path.join(self.web, "shop.dk")
        for name in ("_production", "_staging", "scripts"):
            os.makedirs(os.path.join(self.site, name))
        environments = {"prod": os.path.join(self.site, "_production"), "staging": os.path.join(self.site, "_staging")}
        self.user = {"id": "u_1", "role": "user", "web": True, "sites": {"shop.dk": {"envs": ["prod"], "actions": ["terminal"]}}}
        self.ns = dict(os=os, abort=abort, access=access, WEB_ROOT=self.web, SHELL_ETC=("passwd",), SHELL_HIDDEN=("/srv",),
                       me=lambda: self.user, linux_names=lambda: {"u_1": "anna"}, linux_users_changed=lambda: None,
                       target_ids=lambda root: list(environments) if root == self.site else [],
                       parse_prod=lambda root, target: {"PROD_LOCAL_PATH": environments[target], "PROD_TARGET": ""})
        nodes = [node for node in ast.parse(APP.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("local_environment_folders", "shell_folders", "user_shell")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), self.ns)
        self.app = Flask(__name__)

    def shell(self, cwd):
        with self.app.test_request_context():
            return self.ns["user_shell"]("shop.dk", cwd)

    def test_an_environment_alone(self):
        argv = self.shell(os.path.join(self.site, "_production"))
        self.assertIn("BindPaths=" + os.path.join(self.site, "_production"), argv)
        self.assertNotIn("BindPaths=" + self.site, argv, "the main site's folder is not in the shell")
        self.assertFalse(any(item.endswith("_staging") for item in argv))
        # The main site's folder and the other environment's cannot be started in
        for folder in (self.site, os.path.join(self.site, "_staging")):
            with self.assertRaises(Exception) as caught:
                self.shell(folder)
            self.assertEqual(getattr(caught.exception, "code", None), 403, folder)

    def test_the_main_site_without_its_environments(self):
        self.user["sites"]["shop.dk"]["envs"] = ["main"]
        argv = self.shell(self.site)
        self.assertIn("BindPaths=" + self.site, argv)
        self.assertIn("InaccessiblePaths=-" + os.path.join(self.site, "_production"), argv)
        self.assertIn("InaccessiblePaths=-" + os.path.join(self.site, "_staging"), argv)
        with self.assertRaises(Exception):
            self.shell(os.path.join(self.site, "_staging"))
        with self.assertRaises(Exception):
            self.shell(os.path.join(self.site, "scripts"))

    def test_the_main_site_and_an_environment(self):
        self.user["sites"]["shop.dk"]["envs"] = ["main", "prod"]
        argv = self.shell(os.path.join(self.site, "_production"))
        self.assertIn("BindPaths=" + self.site, argv)
        self.assertNotIn("InaccessiblePaths=-" + os.path.join(self.site, "_production"), argv)
        self.assertIn("InaccessiblePaths=-" + os.path.join(self.site, "_staging"), argv)


if __name__ == "__main__":
    unittest.main()
