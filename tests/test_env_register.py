"""harbor env register against register_env.sh (frozen in tests/legacy)"""
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

SETTINGS = {"VNS_PROD_IP": "10.0.0.5", "VNS_PROD_USER": "deploy", "VNS_PROD_PASSWORD": "s3 cret", "VNS_PROD_SSH_PORT": "2222",
            "VNS_PROD_FOLDER": "shop-prod.dk"}
FOUND = ("folder=yes\nworking=/var/www/shop-prod.dk\ndll=/var/www/shop-prod.dk/Shop.dll\nenvironment=Staging\n"
         "binding=shop.dk\nhttps=y\n")
SHOWN = ("SSHPASS", "R_APACHE_DIR", "VNS_PROD_MODAL", "VNS_PROD_NO_PUBLISH", "VNS_PROD_TARGET", "VNS_PROD_IP", "VNS_PROD_USER",
         "VNS_PROD_PASSWORD", "VNS_PROD_SSH_PORT", "VNS_PROD_FOLDER", "VNS_PROD_BINDING", "VNS_PROD_ENVIRONMENT", "VNS_PROD_HTTPS")


@NEEDS_BASH_4
class RegisterEnvironmentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        site = self.folder / "var/www/shop.dk"
        site.mkdir(parents=True)
        (site / "Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup><PackageReference Include="Umbraco.Cms" /></ItemGroup></Project>\n')
        for path in ("etc/apache2/sites-available", "etc/systemd/system"):
            (self.folder / path).mkdir(parents=True)
        (self.folder / "etc/apache2/sites-available/shop.dk.conf").write_text("<VirtualHost *:80>\n  ServerName test.shop.dk\n  ServerAdmin it@shop.dk\n</VirtualHost>\n")
        (self.folder / "etc/systemd/system/shop.dk.service").write_text("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n")
        self.fake = Fake(self.folder, variables=SHOWN)
        self.answer = self.folder / "answer"
        self.sent = self.folder / "sent"
        self.created = self.folder / "created.sh"
        # The server answers what it was asked; the note is kept as it was sent
        self.fake.program("ssh", f'case "${{@: -1}}" in *"bash -s") cat >"{self.sent}.read"; cat "{self.answer}";; '
                                 f'*) cat >"{self.sent}.note";; esac\n')
        self.fake.program("sshpass", 'shift; exec "$@"\n')
        self.fake.program("sudo", 'exec "$@"\n')
        self.fake.program("hostname", 'echo admin.here.dk\n')
        self.fake.program("bash", f'cp "$1" "{self.created}"; exit "${{FAIL_CREATE:-0}}"\n')

    def run_legacy(self, found, env=None):
        self.answer.write_text(found)
        self.fake.log.write_text("")
        for path in (self.created, Path(f"{self.sent}.read"), Path(f"{self.sent}.note")):
            path.unlink(missing_ok=True)
        text = legacy_script("register_env.sh", self.folder, self.folder / "var/www").replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
        result = subprocess.run(["/bin/bash", "-c", text, "register_env.sh", "shop.dk"], capture_output=True, text=True,
                                env=self.fake.env() | SETTINGS | (env or {}))
        read = lambda path: path.read_text() if path.exists() else None  # noqa: E731
        calls = [split_call(line) for line in self.fake.calls() if line.startswith(("ssh ", "bash "))]
        created = read(self.created)
        return result, calls, read(Path(f"{self.sent}.read")), read(Path(f"{self.sent}.note")), created and created.replace(str(self.folder), "")

    def run_harbor(self, found, create=0):
        created = {}

        def bash(argv):
            created["text"] = Path(argv[1]).read_text()
            return Result(create)

        def ssh(argv):
            return Result(0, found) if argv[-1].endswith("bash -s") else Result(0)
        saved = dict(os.environ)
        os.environ.update(SETTINGS)
        try:
            system = RecordingSystem(root=self.folder, answers={"sshpass": ssh, "bash": bash, "hostname -f": "admin.here.dk"})
            code = main(["env", "register", "shop.dk"], system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, system, created.get("text")

    def compare(self, found, create=0):
        result, calls, read, note, created = self.run_legacy(found, {"FAIL_CREATE": str(create)})
        code, system, harbor_created = self.run_harbor(found, create)
        self.assertEqual(code, result.returncode, (result.stderr, system.said))
        remote = [(command, env, input) for command, env, input in zip(system.commands, system.environments, system.inputs)
                  if command.startswith(("sshpass", "bash"))]
        self.assertEqual(len(remote), len(calls), (system.commands, calls))
        for (command, env, input), (name, argv, variables) in zip(remote, calls):
            if name == "bash":
                self.assertEqual(env, {key: value for key, value in variables.items() if key != "SSHPASS"})
            else:
                self.assertEqual(command, " ".join(["sshpass", "-e", name, *argv]))
        self.assertEqual(next((input for command, _, input in remote if command.endswith("bash -s")), None), read)
        self.assertEqual(next((input for command, _, input in remote if "vns-managed" in command), None), note)
        self.assertEqual(harbor_created, created)
        return result, system

    def test_a_site_that_runs_the_same_dll(self):
        result, system = self.compare(FOUND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(system.said[-1], "shop-prod.dk on 10.0.0.5 is now an environment of shop.dk. Nothing was published.")

    def test_what_is_refused(self):
        for found in ("", FOUND + "project=yes\n", FOUND + "managed=other.dk admin.other.dk \n", FOUND.replace("dll=", "x="),
                      FOUND.replace("binding=", "x="), FOUND.replace("working=/var/www/shop-prod.dk", "working=/srv/shop"),
                      FOUND.replace("Shop.dll", "Other.dll")):
            with self.subTest(found=found):
                result, system = self.compare(found)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(system.said[-1], result.stderr.rstrip("\n"))

    def test_an_unknown_environment_is_production_and_a_failed_setup_stops(self):
        result, system = self.compare(FOUND.replace("Staging", "Prod"))
        self.assertEqual(result.returncode, 0, result.stderr)
        result, system = self.compare(FOUND, create=3)
        self.assertEqual(result.returncode, 3)


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        self.assertIn('/../bin/harbor" env register "$@"', (ROOT / "apache/register_env.sh").read_text())


if __name__ == "__main__":
    unittest.main()
