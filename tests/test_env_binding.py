"""harbor env binding against change_binding_prod.sh (frozen in tests/legacy)"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_actions import legacy_script  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake, split_call  # noqa: E402

ALIASES = "www.shop.dk old.shop.dk"


@NEEDS_BASH_4
class EnvironmentBindingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.web = self.folder / "var/www"
        self.target = self.web / "shop.dk/scripts/targets/production"
        self.target.mkdir(parents=True)
        self.fake = Fake(self.folder, variables=("SSHPASS",))
        self.sent = self.folder / "sent"
        # The server answers the aliases it has; the script for the change is kept as it was sent
        self.fake.program("ssh", f'case "${{@: -1}}" in *server_aliases_from_config*) printf "%s" "{ALIASES}";; '
                                 f'*) cat >"{self.sent}";; esac\n')
        self.fake.program("sshpass", 'shift; exec "$@"\n')
        self.fake.program("sudo", 'exec "$@"\n')

    def settings(self, https="y", local=False, windows=False):
        if windows:
            text = ("PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//10.0.0.9/D$/WEB/shop'\nPROD_SMB_USER='Admin'\n"
                    "PROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\n")
        else:
            where = f"PROD_LOCAL_PATH='/var/www/shop.dk/_production/'\n" if local else "PROD_LOCAL_PATH=''\nPROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='s3 cret'\nPROD_SSH_PORT='2222'\n"
            text = (where + "PROD_FOLDER='shop.dk'\nPROD_BINDING='Shop.DK' # the public name\n"
                    f"PROD_HTTPS={https}\nPROD_ENVIRONMENT=\"Production\"\nPROD_ADMIN_MAIL=\"it@shop.dk\"\n")
        (self.target / "publish_prod.sh").write_text(text)

    def run_legacy(self, *args, stdin=""):
        self.fake.log.write_text("")
        self.sent.unlink(missing_ok=True)
        text = legacy_script("change_binding_prod.sh", self.folder, self.web)
        result = subprocess.run(["bash", "-c", text, "change_binding_prod.sh", *args], input=stdin, capture_output=True, text=True,
                                env=self.fake.env() | {"VNS_TARGET": "production"})
        sent = self.sent.read_text() if self.sent.exists() else None
        calls = [split_call(line) for line in self.fake.calls() if line.startswith(("ssh ", "bash "))]
        return result, sent, calls, (self.target / "publish_prod.sh").read_text()

    def run_harbor(self, *args, replies=None):
        os.environ["VNS_TARGET"] = "production"
        try:
            system = RecordingSystem(root=self.folder, answers={"sshpass": lambda argv: Result(0, ALIASES)}, replies=replies or [])
            code = main(["env", "binding", *args], system)
        finally:
            del os.environ["VNS_TARGET"]
        return code, system

    def compare(self, args, stdin="", replies=None, https="y"):
        self.settings(https)
        result, sent, calls, after = self.run_legacy(*args, stdin=stdin)
        self.settings(https)
        code, system = self.run_harbor(*args, replies=replies)
        self.assertEqual(code, result.returncode, (args, result.stderr, system.said))
        self.assertEqual((self.target / "publish_prod.sh").read_text(), after, args)
        self.assertEqual(len(system.commands), len(calls), (system.commands, calls))
        for command, (name, argv, variables) in zip(system.commands, calls):
            self.assertEqual(command, " ".join(["sshpass", "-e", name, *argv]), args)
        self.assertEqual(next((sent for sent in system.inputs if sent), None), sent, args)
        return result, system

    def test_new_binding_and_aliases_are_sent_and_saved(self):
        result, system = self.compare(("shop.dk", "new.shop.dk", "www.new.shop.dk, Shop.dk", "--yes"))
        self.assertIn("PROD_BINDING='new.shop.dk' # the public name", (self.target / "publish_prod.sh").read_text())
        self.assertIn("ALIASES='www.new.shop.dk shop.dk'", system.commands[-1])

    def test_aliases_kept_removed_and_unchanged(self):
        # Enter keeps the aliases the server has; - removes them; the same names change nothing
        self.compare(("shop.dk", "new.shop.dk"), stdin="\ny\n", replies=["", "y"])
        self.compare(("shop.dk", "new.shop.dk"), stdin="-\ny\n", replies=["-", "y"])
        result, system = self.compare(("shop.dk", "shop.dk", ALIASES, "--yes"))
        self.assertEqual(system.said[-1], "The binding and aliases are unchanged.")
        result, system = self.compare(("shop.dk", "", ALIASES), stdin="\n", replies=[""])

    def test_not_confirmed_and_invalid_names(self):
        self.compare(("shop.dk", "new.shop.dk", ""), stdin="n\n", replies=["n"])
        self.compare(("shop.dk", "intranet", "", "--yes"))
        self.compare(("shop.dk", "intranet", "", "--yes"), https="n")
        self.compare(("shop.dk", "new.shop.dk", "bad_alias", "--yes"))

    def test_windows_is_managed_in_iis(self):
        self.settings(windows=True)
        result, _, _, _ = self.run_legacy("shop.dk", "new.shop.dk", "", "--yes")
        code, system = self.run_harbor("shop.dk", "new.shop.dk", "", "--yes")
        # The script ran with the sites under the test's folder
        self.assertEqual((code, system.said[-1]), (result.returncode, result.stderr.strip().replace(str(self.folder), "")))
        self.assertEqual(system.commands, [])

    def test_failure_where_the_environment_is_keeps_the_settings(self):
        self.settings()
        os.environ["VNS_TARGET"] = "production"
        try:
            system = RecordingSystem(root=self.folder, answers={"sshpass": lambda argv: Result(0, ALIASES) if "bash -c" in argv[-1] else Result(3)})
            code = main(["env", "binding", "shop.dk", "new.shop.dk", "", "--yes"], system)
        finally:
            del os.environ["VNS_TARGET"]
        self.assertEqual(code, 3)
        self.assertIn("PROD_BINDING='Shop.DK'", (self.target / "publish_prod.sh").read_text())


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        text = (ROOT / "apache/change_binding_prod.sh").read_text()
        self.assertIn('/../bin/harbor" env binding "$@"', text)


if __name__ == "__main__":
    unittest.main()
