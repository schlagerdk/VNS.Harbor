import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from flask import abort
from werkzeug.exceptions import BadRequest

from lib.domain_names import normalize_dns_name, normalize_site_name, service_name
from tests.admin.app_targets import add_target_helpers



ROOT_DIR = Path(__file__).resolve().parents[2]
class ProductionEnvironmentTests(unittest.TestCase):
    def test_modal_environment_for_local_and_remote_targets(self):
        source = (ROOT_DIR / "admin/app.py").read_text()
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("production_setup_env", "ssh_login", "kept_server", "dns_name")]
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "example.test/scripts"
            scripts.mkdir(parents=True)
            (scripts / "create_prod.sh").touch()
            ns = {"os": os, "re": re, "abort": abort, "WEB_ROOT": directory,
                  "NAME_RE": re.compile(r"^[A-Za-z0-9.-]+$"),
                  "BINDING_RE": re.compile(r"^[A-Za-z0-9.-]+$"),
                  "conf_binding": lambda site: "example.test", "_normalize_dns_name": normalize_dns_name,
                  "_normalize_site_name": normalize_site_name, "service_name": service_name}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "production env", "exec"), ns)
            add_target_helpers(ns)
            for target in ("local", "remote"):
                data = dict(target=target, folder="prod.test", binding="prod.test",
                            local_path=str(scripts.parent / "_prod"),
                            host="server.test", user="root", ssh_port="22")
                for environment in ("Development", "Test", "Staging", "Production"):
                    result = ns["production_setup_env"](data | {"environment": environment}, "example.test")
                    self.assertEqual(result["VNS_PROD_ENVIRONMENT"], environment)
                self.assertEqual(ns["production_setup_env"](data, "example.test")["VNS_PROD_ENVIRONMENT"],
                                 "Production")
                idn = ns["production_setup_env"](
                    data | {"folder": "æøå.dk", "binding": "ÆØÅ.dk"}, "example.test")
                self.assertEqual(idn["VNS_PROD_FOLDER"], "æøå.dk")
                self.assertEqual(idn["VNS_PROD_BINDING"], "xn--5cab8c.dk")
                with self.assertRaises(BadRequest):
                    ns["production_setup_env"](data | {"environment": "invalid"}, "example.test")

    def test_rendered_publish_script_preserves_selected_environment(self):
        create = (ROOT_DIR / "tests/legacy/solutionfiles/csharp/scripts/create_prod.sh").read_text()
        self.assertIn("R_PROD_ENVIRONMENT=\"${_prodEnvironment}\"", create)
        # harbor env create keeps the settings the script held (harbor/environments/create.py)
        sys.path.insert(0, str(ROOT_DIR))
        from harbor.environments.create import LINUX_SETTINGS, WINDOWS_SETTINGS, render
        from harbor.environments.settings import parse
        for template in (LINUX_SETTINGS, WINDOWS_SETTINGS):
            for environment in ("Development", "Test", "Staging", "Production"):
                self.assertEqual(parse(render(template, {"PROD_ENVIRONMENT": environment}))["ENVIRONMENT"], environment)


if __name__ == "__main__":
    unittest.main()
