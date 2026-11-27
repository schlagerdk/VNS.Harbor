"""Fix permissions is shown only when it has something to fix (lib/site_permissions.py needs_fixing, 1.0.92)"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("site_permissions", ROOT / "lib/site_permissions.py")
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


class NeedsFixingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.web = Path(folder.name) / "www"
        self.site = self.web / "shop.dk"
        (self.site / "wwwroot").mkdir(parents=True)
        (self.site / "wwwroot/a.txt").write_text("x")
        self.service = Path(folder.name) / "none.service"
        self.addCleanup(os.chmod, self.site / "wwwroot", 0o755)

    def check(self, uid=None, gid=None):
        return permissions.needs_fixing(self.site, self.service, os.getuid() if uid is None else uid, os.getgid() if gid is None else gid, self.web)

    def test_nothing_to_fix_when_every_file_is_its_owners_and_readable(self):
        os.chmod(self.site / "wwwroot/a.txt", 0o644)
        self.assertIsNone(self.check(), "a mode publish leaves (644) is not counted")

    def test_a_file_of_another_owner_or_one_it_cannot_read_needs_fixing(self):
        self.assertEqual(self.check(uid=os.getuid() + 1), str(self.site.resolve()))
        os.chmod(self.site / "wwwroot", 0o600)
        self.assertEqual(self.check(), str(self.site.resolve() / "wwwroot"))

    def test_its_group_and_vns_harbor_json_are_left_out(self):
        """A site's folder may be its builds' group (vns-build); vns-harbor.json stays root:www-data, as Harbor writes it"""
        self.assertIsNone(self.check(gid=os.getgid() + 1))
        (self.site / "vns-harbor.json").write_text("{}")
        os.chmod(self.site / "vns-harbor.json", 0o000)
        self.addCleanup(os.chmod, self.site / "vns-harbor.json", 0o644)
        self.assertIsNone(self.check())

    def test_scripts_are_left_out_as_fixing_leaves_them(self):
        (self.site / "scripts").mkdir()
        (self.site / "scripts/run.sh").write_text("")
        os.chmod(self.site / "scripts/run.sh", 0o000)
        self.addCleanup(os.chmod, self.site / "scripts/run.sh", 0o644)
        self.assertIsNone(self.check())


if __name__ == "__main__":
    unittest.main()
