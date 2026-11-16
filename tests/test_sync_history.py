"""harbor/environments/history.py: when a site was synced, and whether it has changed since"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.environments import history  # noqa: E402


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.site = Path(folder.name)
        for name, text in (("uSync/v18/ContentTypes/home.config", "<home/>"), ("uSync/v18/usync.config", "<config/>"),
                           ("uSync/v18/Content/frontpage.config", "<frontpage/>"), ("uSync/v18/Media/logo.config", "<logo/>"),
                           ("Views/Home.cshtml", "<h1>Home</h1>"), ("wwwroot/css/site.css", "body {}"), ("wwwroot/media/ab/logo.png", "png")):
            (self.site / name).parent.mkdir(parents=True, exist_ok=True)
            (self.site / name).write_text(text)

    def test_each_kind_changes_with_its_own_files_only(self):
        before = history.fingerprints(self.site)
        (self.site / "Views/Home.cshtml").write_text("<h1>Welcome</h1>")
        after = history.fingerprints(self.site)
        self.assertNotEqual(before["settings"], after["settings"])
        self.assertEqual((before["content"], before["media"]), (after["content"], after["media"]))
        (self.site / "uSync/v18/Content/frontpage.config").write_text("<frontpage changed/>")
        self.assertNotEqual(after["content"], history.fingerprint(self.site, "content"))

    def test_usync_writing_the_same_again_is_no_change(self):
        """uSync exports everything again when a site that may send starts: same contents, new times"""
        before = history.fingerprints(self.site)
        for item in (self.site / "uSync").rglob("*.config"):
            item.write_text(item.read_text())
            os.utime(item, (1, 1))
        self.assertEqual(before, history.fingerprints(self.site))

    def test_media_files_by_size_and_time(self):
        before = history.fingerprint(self.site, "media")
        (self.site / "wwwroot/media/ab/photo.jpg").write_text("jpg")
        self.assertNotEqual(before, history.fingerprint(self.site, "media"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.system = RecordingSystem(root=Path(folder.name))

    def test_last_sync_and_changed_since(self):
        self.assertEqual(history.between(self.system, "shop.dk", "main", "production", ["settings"], {"settings": "a"}),
                         {"settings": {"last": None, "changed": None}}, "never synced: not known")
        history.record(self.system, "shop.dk", "main", "production", ["settings"], True, {"settings": "a"}, who="anna")
        found = history.between(self.system, "shop.dk", "main", "production", ["settings", "content"], {"settings": "a", "content": "c"})
        self.assertEqual(found["settings"]["last"]["who"], "anna")
        self.assertIs(found["settings"]["changed"], False)
        self.assertEqual(found["content"], {"last": None, "changed": None})
        self.assertIs(history.between(self.system, "shop.dk", "main", "production", ["settings"], {"settings": "b"})["settings"]["changed"], True)
        # Not on this server: not known
        self.assertIsNone(history.between(self.system, "shop.dk", "main", "production", ["settings"], {})["settings"]["changed"])

    def test_a_failed_sync_is_the_last_but_not_what_was_sent(self):
        history.record(self.system, "shop.dk", "main", "test", ["content", "media"], True, {"content": "c", "media": "m"})
        history.record(self.system, "shop.dk", "main", "test", ["content", "media"], False, {"content": "c2", "media": "m"})
        found = history.between(self.system, "shop.dk", "main", "test", ["content"], {"content": "c2"})
        self.assertFalse(found["content"]["last"]["ok"])
        self.assertIs(found["content"]["changed"], True, "c2 never arrived")

    def test_only_the_newest_are_kept(self):
        for index in range(history.KEEP + 5):
            history.record(self.system, "shop.dk", "main", "test", ["settings"], True, {"settings": str(index)})
        kept = history.entries(self.system, "shop.dk")
        self.assertEqual(len(kept), history.KEEP)
        self.assertEqual(kept[0]["fingerprints"]["settings"], str(history.KEEP + 4))


if __name__ == "__main__":
    unittest.main()
