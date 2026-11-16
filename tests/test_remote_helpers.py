"""The helpers in lib/ that run on another server over SSH (python3 - or -c): on their own, with no file beside them"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.names import standalone  # noqa: E402


def run(code, stdin="", **env):
    """As python3 -c on a server without harbor: isolated, in an empty folder"""
    with tempfile.TemporaryDirectory() as folder:
        return subprocess.run([sys.executable, "-I", "-c", code], input=stdin, text=True, capture_output=True, cwd=folder,
                              env={"PATH": os.environ.get("PATH", ""), **env})


class RemoteHelperTests(unittest.TestCase):
    def test_edit_config_runs_without_domain_names_beside_it(self):
        """Edit config on an environment elsewhere failed: name '__file__' is not defined"""
        ran = run(standalone((ROOT / "lib/managed_config.py").read_text()), json.dumps({"kind": "conf", "action": "read"}), SITE="shop.dk")
        self.assertNotIn("Traceback", ran.stderr)
        self.assertIn("Configuration file not found", json.loads(ran.stdout)["error"], "it got as far as looking for the file")
        ran = run(standalone((ROOT / "lib/managed_config.py").read_text()), json.dumps({"kind": "conf", "action": "read"}), SITE="bad name")
        self.assertNotIn("Traceback", ran.stderr, "a bad name is refused by the names it carries")

    def test_every_helper_sent_on_its_own_finds_its_names(self):
        for name in ("managed_config.py", "remote_files.py", "site_permissions.py"):
            with self.subTest(name):
                ran = run(standalone((ROOT / "lib" / name).read_text()), "{}", SITE="shop.dk", ROOT="/nonexistent")
                for error in ("ModuleNotFoundError", "NameError", "__file__"):
                    self.assertNotIn(error, ran.stderr)


if __name__ == "__main__":
    unittest.main()
