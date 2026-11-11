from pathlib import Path
import re
import unittest

APACHE = Path(__file__).resolve().parents[1]


class AdminUpdaterTests(unittest.TestCase):
    def test_server_state_survives_replacing_the_app_files(self):
        updater = (APACHE / "update_admin_site.sh").read_text()
        rsync = re.search(r"rsync -a --delete(?:[^\n]*\\\n)+[^\n]*", updater).group()
        for name in ("/config.json", "/sql-connections.json", "/audit.log", "/system-update.json", "/system-update.log"):
            with self.subTest(name=name):
                self.assertIn("--exclude " + name, rsync)

    def test_the_activity_log_is_not_part_of_the_repository(self):
        ignored = (APACHE.parent / ".gitignore").read_text().splitlines()
        self.assertIn("apache/solutionfiles/admin/app/audit.log", ignored)


if __name__ == "__main__":
    unittest.main()
