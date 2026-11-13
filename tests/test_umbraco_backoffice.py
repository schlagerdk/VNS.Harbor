"""The VNS.Harbor package's connection and the backoffice login link (phase 9a)"""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.umbraco import backoffice  # noqa: E402

PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup><PackageReference Include="VNS.Harbor" Version="17.0.0" /></ItemGroup></Project>\n'


def decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class BackofficeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        site = self.root / "var/www/shop.dk"
        (site / "_publish").mkdir(parents=True)
        (site / "Shop.csproj").write_text(PROJECT)
        (site / "_publish/VNS.Harbor.dll").write_text("")
        for path in ("etc/systemd/system", "etc/apache2/sites-available"):
            (self.root / path).mkdir(parents=True)
        (self.root / "etc/systemd/system/shop.dk.service").write_text("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n")
        (self.root / "etc/apache2/sites-available/shop.dk.conf").write_text("<VirtualHost *:443>\n  ServerName shop.dk\n  SSLEngine On\n</VirtualHost>\n")
        self.system = RecordingSystem(root=self.root)

    def run_harbor(self, *argv):
        self.system = RecordingSystem(root=self.root)
        self.assertEqual(main(list(argv), self.system), 0, self.system.said)
        return self.system.said

    def test_the_site_is_connected_where_it_runs(self):
        self.run_harbor("site", "connect", "shop.dk", "--harbor-url", "https://admin.example.test")
        connection = self.root / "var/www/shop.dk/_publish/vns-harbor.json"
        data = json.loads(connection.read_text())
        self.assertEqual((data["harbor"], data["site"], data["environment"]), ("https://admin.example.test", "shop.dk", "main"))
        self.assertEqual(len(data["token"]), 64)
        self.assertEqual(stat.S_IMODE(connection.stat().st_mode), 0o640)
        self.assertIn(f"chown root:www-data {connection}", self.system.commands)
        secret = self.root / "etc/harbor/secrets/sites/shop.dk/connection/main.json"
        self.assertEqual(json.loads(secret.read_text())["login_key"], data["loginKey"])
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        # Connected again: the same keys, and the address stays when it is not given
        self.run_harbor("site", "connect", "shop.dk")
        again = json.loads(connection.read_text())
        self.assertEqual((again["token"], again["harbor"]), (data["token"], "https://admin.example.test"))
        # The state sync never takes the connection's secrets away
        self.run_harbor("state", "sync", "shop.dk")
        self.assertTrue(secret.is_file())

    def test_a_link_is_signed_once_for_the_chosen_user(self):
        with self.assertRaises(Exception):
            backoffice.login_link(self.system, "shop.dk")
        self.run_harbor("site", "backoffice-user", "shop.dk", "editor@shop.dk")
        link = backoffice.login_link(self.system, "shop.dk", who="martin", now=1_000_000)
        self.assertTrue(link.startswith("https://shop.dk/umbraco/vns-harbor/login?token="))
        payload, signature = link.split("token=", 1)[1].split(".")
        key = json.loads((self.root / "etc/harbor/secrets/sites/shop.dk/connection/main.json").read_text())["login_key"]
        self.assertEqual(decode(signature), hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest())
        token = json.loads(decode(payload))
        self.assertEqual((token["site"], token["environment"], token["user"], token["exp"]), ("shop.dk", "main", "editor@shop.dk", 1_000_060))
        self.assertGreaterEqual(len(token["nonce"]), 16)
        self.assertNotEqual(token["nonce"], json.loads(decode(backoffice.login_link(self.system, "shop.dk").split("token=")[1].split(".")[0]))["nonce"])
        log = (self.root / "var/lib/harbor/sites/shop.dk/backoffice-logins.log").read_text().splitlines()
        self.assertEqual(json.loads(log[0])["who"], "martin")

    def test_an_environment_uses_the_sites_user_and_is_connected_when_a_link_is_made(self):
        self.run_harbor("site", "backoffice-user", "shop.dk", "editor@shop.dk")
        target = self.root / "var/www/shop.dk/scripts/targets/test"
        target.mkdir(parents=True)
        (target / "publish_prod.sh").write_text("PROD_LOCAL_PATH='/var/www/shop.dk/_test'\nPROD_FOLDER='test.shop.dk'\nPROD_BINDING='test.shop.dk'\nPROD_HTTPS=n\n")
        # Published before the package was added: no link, the Backend button opens the login
        with self.assertRaises(Exception):
            backoffice.login_link(self.system, "shop.dk", "test")
        (self.root / "var/www/shop.dk/_test").mkdir()
        (self.root / "var/www/shop.dk/_test/VNS.Harbor.dll").write_text("")
        self.assertEqual(backoffice.backoffice_user(self.system, "shop.dk", "test"), "editor@shop.dk")
        link = backoffice.login_link(self.system, "shop.dk", "test")
        self.assertTrue(link.startswith("http://test.shop.dk/umbraco/vns-harbor/login?token="))
        written = json.loads((self.root / "var/www/shop.dk/_test/vns-harbor.json").read_text())
        self.assertEqual(written["environment"], "test")

    def test_harbors_address_is_the_admin_site_here(self):
        (self.root / "etc/systemd/system/admin.example.test.service").write_text("[Service]\nWorkingDirectory=/var/www/admin.example.test/app\n")
        (self.root / "etc/apache2/sites-available/admin.example.test.conf").write_text(
            "<VirtualHost *:443>\n  ServerName admin.example.test\n  SSLEngine On\n</VirtualHost>\n")
        self.run_harbor("site", "connect", "shop.dk")
        data = json.loads((self.root / "var/www/shop.dk/_publish/vns-harbor.json").read_text())
        self.assertEqual(data["harbor"], "https://admin.example.test")

    def test_an_environment_on_another_server_is_connected_over_ssh(self):
        target = self.root / "var/www/shop.dk/scripts/targets/production"
        target.mkdir(parents=True)
        (target / "publish_prod.sh").write_text("PROD_IP='10.0.0.9'\nPROD_USER='deploy'\nPROD_SSH_PORT='22'\nPROD_FOLDER='shop.dk'\n"
                                                "PROD_BINDING='shop.dk'\nPROD_HTTPS=y\n")
        self.run_harbor("site", "backoffice-user", "shop.dk", "editor@shop.dk")
        self.system = RecordingSystem(root=self.root, answers={"ssh": ""})
        backoffice.connect(self.system, "shop.dk", "production", harbor_url="https://admin.example.test")
        sent = next(index for index, command in enumerate(self.system.commands) if "vns-harbor.json.new" in command)
        self.assertTrue(self.system.commands[sent].startswith("ssh -p 22"))
        self.assertIn("deploy@10.0.0.9 sudo -n sh -c", self.system.commands[sent])
        self.assertIn("chown root:www-data /var/www/shop.dk/vns-harbor.json.new", self.system.commands[sent])
        data = json.loads(self.system.inputs[sent])
        self.assertEqual((data["environment"], data["harbor"]), ("production", "https://admin.example.test"))
        self.assertNotIn(data["token"], " ".join(self.system.commands))
        # Not published with the package there: no link, the Backend button opens the login
        self.system = RecordingSystem(root=self.root, answers={"ssh": Result(1)})
        with self.assertRaises(Exception):
            backoffice.login_link(self.system, "shop.dk", "production")
        self.assertTrue(any("test -f /var/www/shop.dk/VNS.Harbor.dll" in command for command in self.system.commands))

    def test_the_user_can_be_taken_away(self):
        self.run_harbor("site", "backoffice-user", "shop.dk", "editor@shop.dk")
        self.run_harbor("site", "backoffice-user", "shop.dk")
        self.assertEqual(backoffice.backoffice_user(self.system, "shop.dk"), "")
        self.assertTrue(backoffice.uses_package(self.system, backoffice.site_named(self.system, "shop.dk")))


class AdminScriptTests(unittest.TestCase):
    def test_the_admin_script_is_valid(self):
        node = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()
        if not node:
            self.skipTest("node is not installed")
        result = subprocess.run([node, "--check", str(ROOT / "apache/solutionfiles/admin/app/static/app.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
