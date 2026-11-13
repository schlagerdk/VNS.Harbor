"""harbor admin: the admin site run from the Harbor release in use (cutover step 4a)"""
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
from harbor.install import admin  # noqa: E402

# The service of an admin site made by VNS 1.x: its code copied into the site, its own venv
OLD_SERVICE = """[Service]
User=root
WorkingDirectory=/var/www/admin.example.test/app
ExecStart=/var/www/admin.example.test/venv/bin/gunicorn --worker-class gthread --workers 1 --threads 32 --timeout 120 --bind 127.0.0.1:4007 app:app
"""


class AdminUpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.app = self.root / "var/www/admin.example.test/app"
        self.app.mkdir(parents=True)
        (self.app / "config.json").write_text(json.dumps({"secret_key": "k", "user": "admin", "apache_dir": "/vns/apache"}))
        (self.app / "audit.log").write_text("kept\n")
        (self.root / "var/www/shop.dk/app").mkdir(parents=True)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.service = self.root / "etc/systemd/system/admin.example.test.service"
        self.service.write_text(OLD_SERVICE)
        for patch in (mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}),
                      mock.patch.object(admin, "download_vendor"), mock.patch.object(admin, "prepare_python")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_only_folders_with_the_admins_login_are_admin_sites(self):
        self.assertEqual(admin.admin_sites(RecordingSystem(root=self.root)), ["admin.example.test"])

    def test_the_service_runs_the_release_with_the_data_where_it_was(self):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["admin", "update"], system), 0, system.said)
        text = self.service.read_text()
        self.assertIn("WorkingDirectory=/opt/harbor/current/apache/solutionfiles/admin/app\n", text)
        self.assertIn("Environment=HARBOR_ADMIN_DATA=/var/www/admin.example.test/app\n", text)
        self.assertIn("ExecStart=/opt/harbor/venv/bin/gunicorn ", text)
        self.assertIn("--bind 127.0.0.1:4007 app:app", text, "its port is kept")
        self.assertNotIn("@", text)
        config = json.loads((self.app / "config.json").read_text())
        self.assertEqual((config["user"], config["apache_dir"]), ("admin", "/opt/harbor/current/apache"))
        self.assertEqual((self.app / "audit.log").read_text(), "kept\n")
        self.assertEqual(oct((self.app / "config.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(system.commands[-3:], ["systemctl daemon-reload", "systemctl enable admin.example.test",
                                                "systemctl restart admin.example.test"])

    def test_not_an_admin_site(self):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["admin", "update", "shop.dk"], system), 1)
        self.assertIn("not an admin site", system.said[-1])


class AdminAppTests(unittest.TestCase):
    """The admin's code finds its data and the release's apache folder"""

    def test_from_a_release_the_apache_folder_is_its_own(self):
        text = (ROOT / "apache/solutionfiles/admin/app/app.py").read_text()
        self.assertIn('DATA = os.environ.get("HARBOR_ADMIN_DATA") or BASE', text)
        for name in ("sql-connections.json", "audit.log", "system-update.json"):
            self.assertNotIn(f'os.path.join(BASE, "{name}")', text)
            self.assertNotIn(f'Path(BASE) / "{name}"', text)


if __name__ == "__main__":
    unittest.main()
