"""harbor site receive-build: a C# site published from a developer's machine, set up the first time, then updated"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402


def real_rsync(argv, stdin=None):
    done = subprocess.run(argv, capture_output=True, text=True)
    return Result(done.returncode, done.stdout, done.stderr)


@unittest.skipUnless(shutil.which("rsync") and subprocess.run(["rsync", "--version"], capture_output=True, text=True).stdout.startswith("rsync  version"),
                     "needs rsync with filter rules, as on the servers")
class ReceiveBuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.incoming = self.root / "var/lib/harbor/incoming/shop.dk"
        patcher = mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, files):
        if self.incoming.exists():
            shutil.rmtree(self.incoming)
        for name, text in files.items():
            (self.incoming / name).parent.mkdir(parents=True, exist_ok=True)
            (self.incoming / name).write_text(text)

    def harbor(self, *argv):
        system = RecordingSystem(root=self.root, answers={"rsync": real_rsync})
        return main(["site", "receive-build", "shop.dk", *argv], system), system

    def test_nothing_sent_is_said(self):
        code, system = self.harbor()
        self.assertEqual(code, 1)
        self.assertIn("publish script", system.said[-1])

    def test_the_first_build_sets_the_site_up_and_later_ones_keep_what_it_has(self):
        self.build({"Shop.dll": "v1", "Shop.runtimeconfig.json": "{}", "appsettings.json": '{"local": true}',
                    "Views/Home.cshtml": "<h1>1</h1>", "App_Plugins/Shop/a.js": "a"})
        with mock.patch("harbor.sites.setup.setup_site") as setup:
            code, system = self.harbor("--binding", "shop.dk", "--https", "y", "--environment", "Production")
        self.assertEqual(code, 0, system.said)
        setup.assert_called_once_with(system, "shop.dk", "shop.dk", "y", "Production")
        site = self.root / "var/www/shop.dk"
        self.assertEqual((site / "appsettings.json").read_text(), '{"local": true}', "the first time, its settings come along")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").read_text())["binding"], "shop.dk")
        # Running there: its own settings, data and packages
        (site / "appsettings.json").write_text('{"server": true}')
        for name, text in (("umbraco/Data/Umbraco.sqlite.db", "live"), ("wwwroot/media/1/a.png", "png"),
                           ("App_Plugins/Extra/x.js", "added there"), ("Old.dll", "gone"), ("vns-harbor.json", "{}")):
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_text(text)
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}", "appsettings.json": '{"local": true}',
                    "Views/Home.cshtml": "<h1>2</h1>", "App_Plugins/Shop/a.js": "a2"})
        code, system = self.harbor()
        self.assertEqual(code, 0, system.said)
        self.assertEqual((site / "Shop.dll").read_text(), "v2")
        self.assertEqual((site / "Views/Home.cshtml").read_text(), "<h1>2</h1>")
        self.assertEqual((site / "App_Plugins/Shop/a.js").read_text(), "a2")
        self.assertFalse((site / "Old.dll").exists(), "the build is the build: what it no longer has goes")
        for name, text in (("appsettings.json", '{"server": true}'), ("umbraco/Data/Umbraco.sqlite.db", "live"),
                           ("wwwroot/media/1/a.png", "png"), ("App_Plugins/Extra/x.js", "added there"), ("vns-harbor.json", "{}")):
            self.assertEqual((site / name).read_text(), text, name)
        self.assertIn("systemctl restart shop.dk", system.commands)

    def test_a_site_with_a_project_here_is_not_replaced(self):
        site = self.root / "var/www/shop.dk"
        site.mkdir(parents=True)
        (site / "Shop.csproj").write_text("<Project/>")
        self.build({"Shop.dll": "v1", "Shop.runtimeconfig.json": "{}"})
        code, system = self.harbor()
        self.assertEqual(code, 1)
        self.assertIn("built here", system.said[-1])


if __name__ == "__main__":
    unittest.main()
