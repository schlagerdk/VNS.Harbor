"""harbor site action / env action / env windows-file(s) against test_action.sh, prod_action.sh, windows_file.sh and
windows_files.sh (frozen in tests/legacy)"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.harness import sent_today  # noqa: E402

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_core_site_config import Server  # noqa: E402
from tests.test_environments import Fake, split_call  # noqa: E402

LEGACY = ROOT / "tests/legacy"
# The Harbor installation (what was the apache folder: lib/ and the templates)
APACHE = ROOT
SERVICE = ("[Unit]\nDescription=Running .NET Core [shop.dk]\n[Service]\nWorkingDirectory={root}/var/www/shop.dk/_publish\n"
           "ExecStart=/usr/lib/dotnet/dotnet /var/www/shop.dk/_publish/Shop.dll --urls \"http://127.0.0.1:4010\"\n"
           "Environment=ASPNETCORE_ENVIRONMENT=Test\nEnvironment=DOTNET_ENVIRONMENT=Test\n")


def legacy_script(name, root, web_root):
    """A frozen script that runs from the real apache folder, with /etc under root and the sites in web_root"""
    text = (LEGACY / name).read_text().replace('cd "$(dirname "$0")"', f"cd {shlex.quote(str(APACHE))}")
    common = Path(root) / "common.sh"
    common.write_text((LEGACY / "lib/common.sh").read_text().replace("/etc/", f"{root}/etc/")
                      .replace("_rootPath=${VNS_WEB_ROOT:-/var/www}", f"_rootPath={web_root}")
                      .replace('_apacheDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', f'_apacheDir="{APACHE}"')
                      # The site templates as they were (Harbor 1.0 has none)
                      .replace('_solutionFolder="${_apacheDir}/solutionfiles"', f'_solutionFolder="{LEGACY}/solutionfiles"'))
    text = text.replace("source lib/common.sh", f"source {common}").replace("source lib/prod.sh", f"source {LEGACY}/lib/prod.sh")
    text = text.replace("source lib/windows_prod.sh", f"source {LEGACY}/lib/windows_prod.sh")
    return text


@NEEDS_BASH_4
class SiteActionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.old = Server(self.folder, "old")
        self.new = Server(self.folder, "new")
        for server in (self.old, self.new):
            (server.root / "etc/systemd/system/shop.dk.service").write_text(SERVICE.format(root=""))

    def run_legacy(self, *args, stdin=""):
        text = legacy_script("test_action.sh", self.old.root, self.old.root / "var/www")
        return subprocess.run(["bash", "-c", text, "test_action.sh", *args], input=stdin, capture_output=True, text=True,
                              env=os.environ | {"PATH": f"{self.old.root}/bin:{os.environ['PATH']}"})

    def test_change_environment(self):
        for given in ("staging", "4", "p", ""):
            for server in (self.old, self.new):
                (server.root / "etc/systemd/system/shop.dk.service").write_text(SERVICE.format(root=""))
                server.log.write_text("")
            result = self.run_legacy("shop.dk", "change_env", given, stdin="2\n")
            system = RecordingSystem(root=self.new.root, replies=["2"])
            code = main(["site", "action", "shop.dk", "change_env", given], system)
            self.assertEqual(code, result.returncode, (given, result.stderr))
            self.assertEqual(self.new.files(), self.old.files(), given)
            self.assertEqual(system.commands, self.old.commands(), given)
            self.assertEqual(bool(system.asked), not given)

    def test_unknown_environment_and_action(self):
        result = self.run_legacy("shop.dk", "change_env", "nowhere")
        system = RecordingSystem(root=self.new.root)
        self.assertEqual(main(["site", "action", "shop.dk", "change_env", "nowhere"], system), result.returncode)
        self.assertEqual(system.said[-1], result.stderr.strip())
        result = self.run_legacy("shop.dk", "dance")
        system = RecordingSystem(root=self.new.root)
        self.assertEqual(main(["site", "action", "shop.dk", "dance"], system), result.returncode)
        self.assertEqual(system.said[-1], result.stderr.strip())

    def test_certificate_for_the_site_here(self):
        conf = ("<VirtualHost *:80>\n  ServerName Shop.dk\n  ServerAlias www.shop.dk\n  ProxyRequests Off\n"
                "  RequestHeader set X-Forwarded-Proto \"http\"\n  ProxyPass / http://127.0.0.1:4010/\n</VirtualHost>\n")
        for server in (self.old, self.new):
            (server.root / "etc/apache2/sites-available/shop.dk.conf").write_text(conf)
            (server.root / "etc/acmesh/acme.sh").chmod(0o755)
            server.log.write_text("")
        result = self.run_legacy("shop.dk", "issue_certificate", "--yes")
        system = RecordingSystem(root=self.new.root)
        code = main(["site", "action", "shop.dk", "issue_certificate", "--yes"], system)
        self.assertEqual(code, result.returncode, result.stderr)
        self.assertEqual(self.new.files(), self.old.files())
        self.assertEqual(system.commands, self.old.commands())
        # Not confirmed: nothing happens
        system = RecordingSystem(root=self.new.root, replies=["n"])
        self.assertEqual(main(["site", "action", "shop.dk", "issue_certificate"], system), 1)
        self.assertEqual(system.commands, [])


@NEEDS_BASH_4
class EnvironmentActionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.web = self.folder / "var/www"
        self.target = self.web / "shop.dk/scripts/targets/production"
        self.target.mkdir(parents=True)
        self.fake = Fake(self.folder, variables=("SSHPASS", "RCLONE_SMB_HOST", "RCLONE_SMB_PASS"))
        self.stdin = self.folder / "stdin"
        self.fake.program("ssh", f'cat >"{self.stdin}"\n')
        self.fake.program("sshpass", 'shift; exec "$@"\n')
        self.fake.program("sudo", 'exec "$@"\n')

    def settings(self, extra="", windows=False):
        if windows:
            text = ("PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//10.0.0.9/D$/WEB/shop'\nPROD_SMB_USER='Admin'\nPROD_SMB_PASSWORD='p w'\n"
                    "PROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\nPROD_ENVIRONMENT=\"Production\"\n")
        else:
            text = ("PROD_LOCAL_PATH=''\nPROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='s3 cret'\nPROD_SSH_PORT='2222'\n"
                    "PROD_FOLDER='shop.dk'\nPROD_BINDING='Shop.DK'\nPROD_HTTPS=n\nPROD_ENVIRONMENT=\"Production\"\nPROD_ADMIN_MAIL=\"it@shop.dk\"\n")
        (self.target / "publish_prod.sh").write_text(text + extra)

    def run_legacy(self, *args):
        text = legacy_script("prod_action.sh", self.folder, self.web)
        result = subprocess.run(["bash", "-c", text, "prod_action.sh", *args], capture_output=True, text=True,
                                env=self.fake.env() | {"VNS_TARGET": "production"})
        sent = self.stdin.read_text() if self.stdin.exists() else ""
        return result, sent, (self.target / "publish_prod.sh").read_text()

    def run_harbor(self, *args, answers=None):
        os.environ["VNS_TARGET"] = "production"
        try:
            system = RecordingSystem(root=self.folder, answers=answers or {})
            code = main(["env", "action", *args], system)
        finally:
            del os.environ["VNS_TARGET"]
        return code, system

    def test_remote_actions_send_the_same_script(self):
        for args in (("shop.dk", "change_env", "staging"), ("shop.dk", "issue_certificate", "--yes")):
            for umbraco in (False, True):
                self.settings()
                (self.target / "sync_prod.sh").unlink(missing_ok=True)
                if umbraco:
                    (self.target / "sync_prod.sh").write_text("")
                self.fake.log.write_text("")
                self.stdin.unlink(missing_ok=True)
                result, sent, settings_after = self.run_legacy(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.settings()
                code, system = self.run_harbor(*args)
                self.assertEqual(code, 0, system.said)
                # The script's sshpass hands over to ssh, which recorded how it was called and what it was sent
                name, argv, variables = split_call(next(line for line in self.fake.calls() if line.startswith("ssh ")))
                self.assertEqual(system.commands[0], " ".join(["sshpass", "-e", name, *argv]), args)
                self.assertEqual(system.environments[0], {"SSHPASS": "s3 cret"})
                self.assertEqual(variables, {"SSHPASS": "s3 cret"})
                self.assertEqual(system.inputs[0], sent_today(sent), args)
                self.assertEqual((self.target / "publish_prod.sh").read_text(), settings_after, args)

    def test_windows_refuses_a_certificate_and_changes_web_config(self):
        self.settings(windows=True)
        result, _, _ = self.run_legacy("shop.dk", "issue_certificate", "--yes")
        code, system = self.run_harbor("shop.dk", "issue_certificate", "--yes")
        self.assertEqual((code, system.said[-1]), (result.returncode, result.stderr.strip()))
        self.assertEqual(system.commands, [])
        code, system = self.run_harbor("shop.dk", "change_env", "test", answers={"rclone obscure": "obscured"})
        self.assertEqual(code, 0, system.said)
        self.assertTrue(any(command.startswith("python3 ") and command.endswith("set-environment " + command.split(" ")[-2] + " Test")
                            for command in system.commands), system.commands)
        self.assertIn('PROD_ENVIRONMENT="Test"', (self.target / "publish_prod.sh").read_text())


class WindowsFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        target = Path(folder.name) / "var/www/shop.dk/scripts/targets/iis"
        target.mkdir(parents=True)
        target.joinpath("publish_prod.sh").write_text(
            "PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//10.0.0.9/D$/WEB/shop'\nPROD_SMB_USER='Admin'\nPROD_SMB_PASSWORD='p'\nPROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\n")
        (Path(folder.name) / "var/www/shop.dk/scripts/targets/linux").mkdir()
        (Path(folder.name) / "var/www/shop.dk/scripts/targets/linux/publish_prod.sh").write_text("PROD_IP='x'\nPROD_FOLDER='a'\nPROD_BINDING='a.dk'\n")
        self.root = Path(folder.name)

    def harbor(self, argv, target="iis", answers=None):
        os.environ["VNS_TARGET"] = target
        try:
            system = RecordingSystem(root=self.root, answers={"rclone obscure": "obscured", **(answers or {})})
            return main(argv, system), system
        finally:
            del os.environ["VNS_TARGET"]

    def test_files_are_read_and_saved_as_they_are(self):
        code, system = self.harbor(["env", "windows-file", "shop.dk", "read", "web.config"])
        self.assertEqual(code, 0)
        self.assertEqual(system.commands[-1], "rclone cat :smb:D$/WEB/shop/web.config")
        self.assertIsNone(system.inputs[-1])  # stdin and stdout are the command's own, byte for byte
        code, system = self.harbor(["env", "windows-file", "shop.dk", "save", "appsettings.Production.json"])
        self.assertEqual(system.commands[-1], "rclone rcat :smb:D$/WEB/shop/appsettings.Production.json")
        for argv, target, message in ((["env", "windows-file", "shop.dk", "read", "../secret.txt"], "iis", "Only web.config and appsettings*.json"),
                                      (["env", "windows-file", "shop.dk", "read", "web.config"], "linux", "not an environment on a Windows server")):
            code, system = self.harbor(argv, target)
            self.assertEqual(code, 1)
            self.assertIn(message, system.said[-1])
            self.assertFalse(any(command.startswith("rclone cat") for command in system.commands))

    def test_file_manager_answers_in_json(self):
        code, system = self.harbor(["env", "windows-files", "shop.dk"])
        self.assertEqual(code, 0)
        self.assertTrue(system.commands[-1].endswith("windows_files.py :smb:D$/WEB/shop"))
        self.assertEqual(system.environments[-1]["RCLONE_SMB_PASS"], "obscured")
        code, system = self.harbor(["env", "windows-files", "shop.dk"], answers={"rclone lsf": Result(1)})
        self.assertEqual(code, 1)
        self.assertTrue(system.said[-1].startswith('{"error": "Could not open //10.0.0.9/D$/WEB/shop.'))
        code, system = self.harbor(["env", "windows-files", "shop.dk"], target="linux")
        self.assertEqual(system.said[-1], '{"error": "This is not an environment on a Windows server."}')


if __name__ == "__main__":
    unittest.main()
