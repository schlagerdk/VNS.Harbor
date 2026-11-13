"""harbor state sync: Harbor's own copy of the state in the site folders, equal to them, passwords kept apart"""
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402

REMOTE = ("PROD_IP='10.0.0.5'          # IP or hostname\nPROD_USER='deploy'\nPROD_PASSWORD='s3 cr'\\''et'\nPROD_SSH_PORT='22'\n"
          "PROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\nPROD_HTTPS=n\nPROD_ENVIRONMENT=\"Production\"\n\nrest_of_the_script() { :; }\n")
LOCAL = "PROD_LOCAL_PATH='/var/www/shop.dk/_test'\nPROD_FOLDER='test-shop.dk'\nPROD_BINDING='test.shop.dk'\n"


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.site = self.root / "var/www/shop.dk"
        (self.site / "scripts/targets/production").mkdir(parents=True)
        (self.site / "scripts/targets/production/publish_prod.sh").write_text(REMOTE)
        (self.site / "scripts/publish_prod.sh").write_text(LOCAL)
        (self.site / "scripts/data_sync.json").write_text('{"main": {"enabled": true}}')
        (self.site / ".vns-publish").mkdir()
        (self.site / ".vns-publish/test.json").write_text('{"fingerprint": "abc"}\n')
        (self.root / "var/www/blog.dk").mkdir()
        self.state = self.root / "var/lib/harbor/sites/shop.dk"
        self.secrets = self.root / "etc/harbor/secrets/sites/shop.dk"

    def sync(self, *site):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["state", "sync", *site], system), 0, system.said)
        return system

    def test_settings_and_passwords_are_kept_apart(self):
        self.sync()
        settings = json.loads((self.state / "environments/production.json").read_text())
        self.assertEqual(settings, {"IP": "10.0.0.5", "USER": "deploy", "SSH_PORT": "22", "FOLDER": "shop.dk", "BINDING": "shop.dk",
                                    "HTTPS": "n", "ENVIRONMENT": "Production"})
        self.assertEqual(json.loads((self.secrets / "production.json").read_text()), {"PASSWORD": "s3 cr'et"})
        self.assertFalse((self.secrets / "prod.json").exists(), "no password, no file")
        self.assertEqual(json.loads((self.state / "environments/prod.json").read_text())["LOCAL_PATH"], "/var/www/shop.dk/_test")
        self.assertEqual(json.loads((self.state / "data_sync.json").read_text()), {"main": {"enabled": True}})
        self.assertEqual(json.loads((self.state / "publish/test.json").read_text()), {"fingerprint": "abc"})
        for path in (self.root / "etc/harbor/secrets", self.root / "etc/harbor/secrets/sites", self.secrets):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
        self.assertEqual(stat.S_IMODE((self.secrets / "production.json").stat().st_mode), 0o600)
        # A site with no state has no folder here
        self.assertFalse((self.root / "var/lib/harbor/sites/blog.dk").exists())

    def test_what_changes_or_goes_in_the_site_folder_changes_or_goes_here(self):
        self.sync()
        (self.site / "scripts/targets/production/publish_prod.sh").write_text(REMOTE.replace("PROD_PASSWORD='s3 cr'\\''et'", "PROD_PASSWORD=''"))
        (self.site / "scripts/publish_prod.sh").unlink()
        (self.site / ".vns-publish/test.json").unlink()
        (self.site / "scripts/data_sync.json").unlink()
        self.sync("shop.dk")
        self.assertFalse((self.secrets / "production.json").exists())
        self.assertFalse((self.state / "environments/prod.json").exists())
        self.assertFalse((self.state / "publish").exists())
        self.assertFalse((self.state / "data_sync.json").exists())
        self.assertTrue((self.state / "environments/production.json").is_file())

    def test_a_deleted_site_takes_its_state_along(self):
        self.sync()
        import shutil
        shutil.rmtree(self.site)
        self.sync()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.secrets.exists())

    def test_twice_changes_nothing(self):
        self.sync()
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob("*.json") if "harbor" in str(path)}
        self.sync()
        self.assertEqual({path: path.stat().st_mtime_ns for path in self.root.rglob("*.json") if "harbor" in str(path)}, before)

    def test_an_unreadable_script_is_told_and_left_as_it_was(self):
        self.sync()
        (self.site / "scripts/targets/production/publish_prod.sh").write_text("PROD_IP='unclosed\n")
        system = self.sync()
        self.assertTrue(any(line.startswith("Not synced: shop.dk production") for line in system.said), system.said)
        self.assertTrue((self.state / "environments/production.json").is_file())



class KeptAfterCommandsTests(unittest.TestCase):
    """2c: a command that changes a site leaves Harbor's copy equal to its folder"""

    def test_a_deleted_environment_goes_from_the_copy_too(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        site = root / "var/www/shop.dk"
        (site / "scripts/targets/production").mkdir(parents=True)
        (site / "scripts/targets/production/publish_prod.sh").write_text(REMOTE)
        (root / "etc/apache2/sites-enabled").mkdir(parents=True)
        system = RecordingSystem(root=root)
        main(["state", "sync"], system)
        state = root / "var/lib/harbor/sites/shop.dk"
        self.assertTrue((state / "environments/production.json").is_file())
        os = __import__("os")
        os.environ["VNS_TARGET"] = "production"
        try:
            system = RecordingSystem(root=root, answers={"sshpass": ""})
            main(["site", "delete", "shop.dk", "n", "prod"], system, keep=True)
        finally:
            del os.environ["VNS_TARGET"]
        self.assertFalse((site / "scripts/targets/production").exists(), system.said)
        self.assertFalse((state / "environments/production.json").exists())
        self.assertFalse((root / "etc/harbor/secrets/sites/shop.dk/production.json").exists())

    def test_which_sites_a_command_touches(self):
        from harbor.cli import parser, touched_sites
        self.assertEqual(touched_sites(parser().parse_args(["site", "copy", "a.dk", "b.dk", "b.dk"])), ["a.dk", "b.dk"])
        self.assertEqual(touched_sites(parser().parse_args(["site", "create", "c.dk", "html"])), ["c.dk"])
        self.assertIsNone(touched_sites(parser().parse_args(["site", "receive", "/var/lib/vns-incoming/x"])))
        self.assertEqual(touched_sites(parser().parse_args(["sql", "list-databases"])), [])
        self.assertEqual(touched_sites(parser().parse_args(["env", "create", "shop.dk", "local"])), ["shop.dk"])
        self.assertEqual(touched_sites(parser().parse_args(["env", "publish", "shop.dk"])), ["shop.dk"])


if __name__ == "__main__":
    unittest.main()
