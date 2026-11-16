"""harbor env windows-move: environments on a live Windows server moved to a Linux server Harbor knows"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from tests.test_windows_import import FakeShare  # noqa: E402

WINDOWS = """PROD_TARGET=windows
PROD_WINDOWS_SHARE='//windows.example/d$/sites/shop.dk'
PROD_SMB_USER='admin'
PROD_SMB_PASSWORD='pw'
PROD_SMB_DOMAIN=''
PROD_FOLDER='shop.dk'
PROD_BINDING='www.shop.dk'
PROD_ENVIRONMENT=Production
"""
LINUX = """PROD_LOCAL_PATH=''
PROD_IP='10.0.0.9'
PROD_USER='root'
PROD_PASSWORD=''
PROD_SSH_PORT='22'
PROD_FOLDER='shop.dk'
PROD_BINDING='shop.dk'
PROD_HTTPS='n'
PROD_ENVIRONMENT=Production
"""


class FakeWindowsShare(FakeShare):
    def path(self, target):
        assert target.startswith(":smb:d$/sites"), target
        return self.folder / target[len(":smb:d$/sites"):].lstrip("/")


class WindowsMoveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "server"
        self.windows = Path(folder.name) / "windows"
        site = self.root / "var/www/test.shop.dk"
        (site / "scripts/targets/production").mkdir(parents=True)
        (site / "scripts/targets/production/publish_prod.sh").write_text(WINDOWS)
        (site / "Shop.csproj").write_text("<Project/>")
        for name, text in (("shop.dk/umbraco/Data/Umbraco.sqlite.db", "live"), ("shop.dk/wwwroot/media/a.png", "png"),
                           ("shop.dk/uSync/v18/Content/home.config", "<home/>")):
            (self.windows / name).parent.mkdir(parents=True, exist_ok=True)
            (self.windows / name).write_text(text)
        (self.root / "var/lib/harbor").mkdir(parents=True)
        (self.root / "var/lib/harbor/servers.json").write_text(json.dumps({"prod-01": {"host": "10.0.0.9", "user": "root", "port": 22}}))
        self.share = FakeWindowsShare(self.windows)
        patcher = mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def harbor(self, *argv):
        system = RecordingSystem(root=self.root, answers={"rclone": self.share})
        return main(["env", "windows-move", *argv], system), system

    def moved(self):
        """As harbor env move does for a copy: a new environment on the Linux server"""
        def copy(system, name, target):
            self.request = {key: value for key, value in os.environ.items() if key.startswith("VNS_")}
            folder = self.root / "var/www/test.shop.dk/scripts/targets/prod2"
            folder.mkdir(parents=True)
            (folder / "publish_prod.sh").write_text(LINUX)
        return mock.patch("harbor.environments.move.move_environment", side_effect=copy)

    def test_the_environments_on_windows_are_listed(self):
        code, system = self.harbor("list")
        self.assertEqual(code, 0, system.said)

    def test_move_copies_it_to_the_server_on_http_with_an_alias(self):
        with self.moved(), mock.patch("harbor.environments.bindings.change_binding") as binding:
            code, system = self.harbor("move", "test.shop.dk", "production", "--server", "prod-01", "--alias", "shop.dk.new.example")
        self.assertEqual(code, 0, system.said)
        self.assertEqual({key: self.request[key] for key in ("VNS_ENV_COPY", "VNS_PROD_TARGET", "VNS_PROD_IP", "VNS_PROD_PASSWORD", "VNS_PROD_HTTPS", "VNS_PROD_BINDING", "VNS_PROD_FOLDER")},
                         {"VNS_ENV_COPY": "1", "VNS_PROD_TARGET": "remote", "VNS_PROD_IP": "10.0.0.9", "VNS_PROD_PASSWORD": "",
                          "VNS_PROD_HTTPS": "n", "VNS_PROD_BINDING": "www.shop.dk", "VNS_PROD_FOLDER": "shop.dk"},
                         "a copy over Harbor's key, on HTTP, named as its folder on Windows")
        self.assertEqual(binding.call_args.kwargs["target"], "prod2")
        self.assertEqual(binding.call_args[0][3], "shop.dk.new.example", "as text, as change_binding reads it")
        links = json.loads((self.root / "var/lib/harbor/sites/test.shop.dk/windows-move.json").read_text())
        self.assertEqual(links, {"prod2": {"windows": "production", "server": "prod-01", "alias": "shop.dk.new.example"}})
        from harbor.environments import windows_move
        listed = windows_move.candidates(RecordingSystem(root=self.root))
        self.assertEqual([(item["target"], item["moved_to"]) for item in listed], [("production", "prod2")])
        # Not twice
        with self.moved():
            code, system = self.harbor("move", "test.shop.dk", "production", "--server", "prod-01")
        self.assertEqual(code, 1)
        # The new environment deleted: the one on Windows can be moved again
        import shutil
        shutil.rmtree(self.root / "var/www/test.shop.dk/scripts/targets/prod2")
        listed = windows_move.candidates(RecordingSystem(root=self.root))
        self.assertEqual([(item["target"], item["moved_to"]) for item in listed], [("production", "")])
        with self.moved(), mock.patch("harbor.environments.bindings.change_binding"):
            code, system = self.harbor("move", "test.shop.dk", "production", "--server", "prod-01")
        self.assertEqual(code, 0, system.said)
        self.assertEqual(list(json.loads((self.root / "var/lib/harbor/sites/test.shop.dk/windows-move.json").read_text())), ["prod2"])

    def test_refresh_brings_the_live_data_with_a_pause_on_windows(self):
        with self.moved(), mock.patch("harbor.environments.bindings.change_binding"):
            self.harbor("move", "test.shop.dk", "production", "--server", "prod-01")
        self.share.calls.clear()
        code, system = self.harbor("refresh", "test.shop.dk", "prod2", "--pause")
        self.assertEqual(code, 0, system.said)
        calls = self.share.calls
        offline = next(i for i, call in enumerate(calls) if call[0] == "copyto")
        database = next(i for i, call in enumerate(calls) if call[0] == "copy" and "umbraco/Data" in call[1])
        online = next(i for i, call in enumerate(calls) if call[0] == "deletefile")
        self.assertLess(offline, database)
        self.assertLess(database, online)
        # The site on Linux is stopped while its database is replaced, then started; the data goes there with rsync
        remote = [command for command in system.commands if command.startswith("ssh ")]
        self.assertTrue(any("systemctl stop shop.dk" in command for command in remote), remote)
        self.assertTrue(any("systemctl start shop.dk" in command for command in remote), remote)
        rsync = next(command for command in system.commands if command.startswith("rsync "))
        self.assertIn("root@10.0.0.9:/var/www/shop.dk/", rsync)

    def test_take_over_issues_the_certificate_and_forgets_windows(self):
        with self.moved(), mock.patch("harbor.environments.bindings.change_binding"):
            self.harbor("move", "test.shop.dk", "production", "--server", "prod-01", "--alias", "shop.dk.new.example")
        with mock.patch("harbor.environments.bindings.change_binding") as binding, \
                mock.patch("harbor.environments.actions.issue_certificate") as certificate, \
                mock.patch("harbor.sites.delete.delete_site") as delete:
            code, system = self.harbor("take-over", "test.shop.dk", "prod2")
        self.assertEqual(code, 0, system.said)
        self.assertEqual(binding.call_args[0][3], "", "the alias goes")
        certificate.assert_called_once()
        self.assertEqual(delete.call_args[0][1:], ("test.shop.dk", "n", "prod", "production"), "the Windows environment, from Harbor only")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/sites/test.shop.dk/windows-move.json").read_text()), {})

    def test_a_setting_changes_in_harbors_copy_when_the_site_folder_has_no_script(self):
        """A moved environment's settings are kept by Harbor: a new binding (or HTTPS after take over) goes there"""
        from harbor.environments import actions, settings
        system = RecordingSystem(root=self.root)
        site_root = "/var/www/test.shop.dk"
        settings.write_copy(system, site_root, "prod3", {"IP": "10.0.0.9", "USER": "root", "FOLDER": "shop.dk", "BINDING": "shop.dk", "HTTPS": "n"})
        environment = settings.load(system, site_root, "prod3")
        actions.set_setting(system, environment, "BINDING", "www.shop.dk", quoted=True)
        actions.set_setting(system, settings.load(system, site_root, "prod3"), "HTTPS", "y", quoted=False)
        changed = settings.load(system, site_root, "prod3")
        self.assertEqual((changed.BINDING, changed.HTTPS), ("www.shop.dk", "y"))


if __name__ == "__main__":
    unittest.main()
