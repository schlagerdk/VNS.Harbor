"""Reading the state (phase 2b): the site folder while its file is there, else Harbor's copy"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.environments import settings  # noqa: E402


def helper(name):
    spec = importlib.util.spec_from_file_location(f"vns_{name}", ROOT / "apache/lib" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = "PROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='pw'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n"


class ReadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.site = self.root / "var/www/shop.dk"
        (self.site / "scripts/targets/production").mkdir(parents=True)
        (self.site / "scripts/targets/production/publish_prod.sh").write_text(SCRIPT)
        (self.site / "scripts/data_sync.json").write_text('{"main": {"enabled": true}}')
        (self.site / ".vns-publish").mkdir()
        (self.site / ".vns-publish/target-production.json").write_text('{"fingerprint": "abc"}\n')
        self.system = RecordingSystem(root=self.root)
        self.assertEqual(main(["state", "sync"], self.system), 0)

    def remove_site_state(self):
        (self.site / "scripts/targets/production/publish_prod.sh").unlink()
        (self.site / "scripts/data_sync.json").unlink()
        (self.site / ".vns-publish/target-production.json").unlink()

    def test_an_environment_without_its_script_is_read_from_harbor(self):
        self.remove_site_state()
        self.assertEqual(settings.target_ids(self.system, "/var/www/shop.dk"), ["production"])
        environment = settings.load(self.system, "/var/www/shop.dk", "production")
        self.assertEqual((environment.IP, environment.USER, environment.PASSWORD, environment.BINDING), ("10.0.0.5", "deploy", "pw", "shop.dk"))

    def test_the_script_wins_while_it_is_there(self):
        (self.site / "scripts/targets/production/publish_prod.sh").write_text(SCRIPT.replace("10.0.0.5", "10.0.0.6"))
        self.assertEqual(settings.load(self.system, "/var/www/shop.dk", "production").IP, "10.0.0.6")
        # Another environment only in Harbor's copy is not shown while the site folder has scripts of its own
        (self.root / "var/lib/harbor/sites/shop.dk/environments/ghost.json").write_text('{"FOLDER": "g", "BINDING": "g.dk"}')
        self.assertEqual(settings.target_ids(self.system, "/var/www/shop.dk"), ["production"])

    def test_sync_setup_and_publish_status(self):
        data_sync, publish_status = helper("data_sync"), helper("publish_status")
        data_sync.HARBOR_STATE = publish_status.HARBOR_STATE = self.root / "var/lib/harbor/sites"
        self.assertEqual(data_sync.load(str(self.site))["main"]["enabled"], True)
        self.assertEqual(publish_status.status(str(self.site), "target-production", "abc"), "current")
        self.remove_site_state()
        self.assertEqual(data_sync.load(str(self.site))["main"]["enabled"], True)
        self.assertEqual(publish_status.status(str(self.site), "target-production", "abc"), "current")
        self.assertEqual(publish_status.status(str(self.site), "target-production", "new"), "pending")
        # Neither here nor there
        (self.root / "var/lib/harbor/sites/shop.dk/data_sync.json").unlink()
        self.assertEqual(data_sync.load(str(self.site)), {})


if __name__ == "__main__":
    unittest.main()
