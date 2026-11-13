"""harbor self uninstall: Harbor off the server, the sites left running"""
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
from harbor.install import admin  # noqa: E402
from harbor.server import tools, users  # noqa: E402
from harbor.sites import delete  # noqa: E402


class UninstallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for path in ("opt/harbor/releases/1.0.0", "var/lib/harbor/sites/shop.dk", "etc/harbor/secrets", "etc/cron.d", "usr/local/bin",
                     "var/www/shop.dk/_publish", "var/www/admin.example.test/app", "etc/apache2/conf-available", "var/lib/vns-roots/shop.dk"):
            (self.root / path).mkdir(parents=True)
        (self.root / "opt/harbor/current").symlink_to("releases/1.0.0")
        (self.root / "usr/local/bin/harbor").symlink_to("/opt/harbor/current/bin/harbor")
        (self.root / "etc/cron.d/vns-keep-alive").write_text("* * * * * root x\n")
        (self.root / "etc/harbor/secrets/sql.json").write_text("{}")
        (self.root / "var/www/admin.example.test/app/config.json").write_text('{"secret_key": "k"}')
        (self.root / "etc/apache2/conf-available/vns-tls.conf").write_text("# the TLS policy\n")
        self.calls = []
        for target, name in ((delete, "delete_site"), (users, "sync"), (tools, "cron")):
            patcher = mock.patch.object(target, name, side_effect=lambda *args, name=name: self.calls.append((name, args[1:])))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_it(self, *extra, replies=()):
        system = RecordingSystem(root=self.root, answers={"hostname -f": "web.example.test"}, replies=list(replies))
        return main(["self", "uninstall", *extra], system), system

    def test_harbor_goes_and_the_sites_stay(self):
        code, system = self.run_it(replies=["web.example.test"])
        self.assertEqual(code, 0, system.said)
        archive = next(command for command in system.commands if command.startswith("tar -czf /root/harbor-"))
        self.assertTrue(archive.endswith(" -C / var/lib/harbor etc/harbor"), archive)
        self.assertEqual(self.calls, [("delete_site", ("admin.example.test", "n")), ("sync", ([],)), ("cron", (False,))])
        for path in ("opt/harbor", "var/lib/harbor", "etc/harbor", "etc/cron.d/vns-keep-alive", "usr/local/bin/harbor"):
            self.assertFalse(os.path.lexists(self.root / path), path)
        # The sites, their sandboxes and Apache's server-wide configs are the sites' own
        for path in ("var/www/shop.dk/_publish", "var/lib/vns-roots/shop.dk", "etc/apache2/conf-available/vns-tls.conf"):
            self.assertTrue((self.root / path).exists(), path)

    def test_nothing_changes_without_the_servers_name(self):
        code, system = self.run_it(replies=["yes"])
        self.assertEqual(code, 1)
        self.assertEqual((self.calls, system.commands[1:]), ([], []))
        self.assertTrue((self.root / "opt/harbor/releases/1.0.0").is_dir())
        self.assertIn("Nothing was changed.", system.said[-1])

    def test_yes_does_without_the_question(self):
        code, system = self.run_it("--yes")
        self.assertEqual(code, 0, system.said)
        self.assertEqual(system.asked, [])
        self.assertFalse((self.root / "opt/harbor").exists())


if __name__ == "__main__":
    unittest.main()
