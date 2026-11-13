"""A migrated site (phase 6): the admin runs the harbor command each system script called, and the site's own scripts
from Harbor's folder, as it ran the scripts in the site folder before"""
import ast
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from flask import abort
from werkzeug.exceptions import BadRequest

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin"
NAMES = ("HARBOR_SCRIPTS", "HARBOR_TARGET_SCRIPTS", "site_migrated", "migrated_site_json", "custom_scripts_dir", "migrated_scripts",
         "target_scripts", "harbor_script_command", "publish_entry")


class MigratedScriptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        tree = ast.parse((APP / "app.py").read_text())
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in NAMES)
                 or (isinstance(node, ast.Assign) and any(getattr(target, "id", "") in NAMES for target in node.targets))]
        self.ns = {"os": os, "json": json, "re": re, "abort": abort, "HARBOR_COMMAND": "/vns/bin/harbor",
                   "HARBOR_SITES": str(self.root / "state"), "WEB_ROOT": str(self.root / "www"),
                   "SCRIPT_RE": re.compile(r"^[A-Za-z0-9_.-]+\.sh$"), "TARGET_SCRIPTS": ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"),
                   "target_folder": lambda root, target: os.path.join(root, "scripts", "targets", target),
                   "target_scope": lambda target: "prod" if target == "prod" else "target-" + target}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "migrated scripts", "exec"), self.ns)
        state = self.root / "state/shop.dk"
        state.mkdir(parents=True)
        (state / "migrated.json").write_text("{}")
        (state / "site.json").write_text(json.dumps({"scripts": ["publish_local.sh", "change_env.sh", "edit_conf.sh"],
                                                     "targets": {"test": ["publish_prod.sh", "clear_logs_prod.sh"]}}))
        (self.root / "www/shop.dk/scripts").mkdir(parents=True)
        (self.root / "www/shop.dk/scripts/hello_world.sh").write_text("echo hello\n")

    def test_each_script_is_its_harbor_command(self):
        command = self.ns["harbor_script_command"]
        harbor = ["/usr/bin/python3", "/vns/bin/harbor"]
        self.assertEqual(command("shop.dk", "publish_local.sh"), harbor + ["site", "publish", "shop.dk"])
        self.assertEqual(command("shop.dk", "change_env.sh"), harbor + ["site", "action", "shop.dk", "change_env"])
        self.assertEqual(command("shop.dk", "restart_apache.sh"), harbor + ["server", "restart-apache"])
        self.assertEqual(command("shop.dk", "sync_prod.sh", "test"), ["/usr/bin/env", "VNS_TARGET=test", *harbor, "env", "sync", "shop.dk", "main", "test"])
        self.assertEqual(command("shop.dk", "publish_prod.sh", "test"), ["/usr/bin/env", "VNS_TARGET=test", *harbor, "env", "publish", "shop.dk"])
        self.assertEqual(command("shop.dk", "hello_world.sh"), ["/bin/bash", str((self.root / "www/shop.dk/scripts/hello_world.sh").resolve())])
        for name in ("nothing.sh", "../migrated.json"):
            with self.assertRaises(BadRequest):
                command("shop.dk", name)

    def test_what_it_shows(self):
        self.assertEqual(self.ns["migrated_scripts"]("shop.dk"), ["change_env.sh", "edit_conf.sh", "hello_world.sh", "publish_local.sh"])
        self.assertEqual(self.ns["target_scripts"]("shop.dk", "test"), ["publish_prod.sh", "clear_logs_prod.sh"])
        # One added after the migration: what an environment of its kind gets
        self.assertEqual(self.ns["target_scripts"]("shop.dk", "staging", "umbraco"), ["publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"])
        self.assertEqual(self.ns["target_scripts"]("shop.dk", "staging", "csharp"), ["publish_prod.sh"])
        self.assertEqual(self.ns["publish_entry"]("shop.dk"), "harbor:test")
        self.assertEqual(self.ns["publish_entry"]("shop.dk", "test"), "harbor:target-test")


if __name__ == "__main__":
    unittest.main()
