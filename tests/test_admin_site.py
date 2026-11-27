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
        self.assertIn("WorkingDirectory=/opt/harbor/current/admin\n", text)
        self.assertIn("Environment=HARBOR_ADMIN_DATA=/var/www/admin.example.test/app\n", text)
        self.assertIn("ExecStart=/opt/harbor/venv/bin/gunicorn ", text)
        self.assertIn("--bind 127.0.0.1:4007 app:app", text, "its port is kept")
        self.assertNotIn("@", text)
        config = json.loads((self.app / "config.json").read_text())
        self.assertEqual(config["user"], "admin")
        self.assertNotIn("apache_dir", config, "the admin finds Harbor beside itself")
        self.assertEqual((self.app / "audit.log").read_text(), "kept\n")
        self.assertEqual(oct((self.app / "config.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(system.commands[-3:], ["systemctl daemon-reload", "systemctl enable admin.example.test",
                                                "systemctl restart admin.example.test"])

    def test_not_an_admin_site(self):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["admin", "update", "shop.dk"], system), 1)
        self.assertIn("not an admin site", system.said[-1])


class AdminAccessTests(AdminUpdateTests):
    CONF = ("<VirtualHost *:443>\n  ServerName admin.example.test\n\n  <Location />\n    Require all granted\n  </Location>\n\n"
            "  ProxyPass / http://127.0.0.1:4007/\n</VirtualHost>\n")

    def write_conf(self, text=None):
        conf = self.root / "etc/apache2/sites-available/admin.example.test.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(text or self.CONF)
        return conf

    def test_who_may_reach_it_is_read_and_set(self):
        conf = self.write_conf()
        system = RecordingSystem(root=self.root)
        self.assertEqual(admin.allowed_addresses(system), [])
        admin.set_allowed_addresses(system, "", ["192.0.2.10", "10.0.0.0/8,2001:db8::/32"])
        self.assertIn("  <Location />\n    Require ip 192.0.2.10 10.0.0.0/8 2001:db8::/32\n  </Location>\n", conf.read_text())
        self.assertEqual(admin.allowed_addresses(system), ["192.0.2.10", "10.0.0.0/8", "2001:db8::/32"])
        self.assertIn("systemctl reload apache2", system.commands)
        admin.set_allowed_addresses(system, "", [])
        self.assertEqual(conf.read_text(), self.CONF)

    def test_others_get_a_404_or_a_redirect_and_it_is_read_back(self):
        conf = self.write_conf()
        system = RecordingSystem(root=self.root)
        admin.set_allowed_addresses(system, "", ["192.0.2.10"], "redirect", "https://www.google.dk/")
        self.assertIn("    Require ip 192.0.2.10\n    ErrorDocument 403 https://www.google.dk/\n  </Location>", conf.read_text())
        self.assertEqual(admin.access_settings(system), {"addresses": ["192.0.2.10"], "response": "redirect", "target": "https://www.google.dk/"})
        admin.set_allowed_addresses(system, "", ["192.0.2.10", "10.0.0.0/8"], "notfound")
        self.assertIn("    Require all granted\n    <If \"!(-R '192.0.2.10' || -R '10.0.0.0/8')\">\n      Redirect 404\n    </If>\n  </Location>",
                      conf.read_text())
        self.assertEqual(admin.access_settings(system), {"addresses": ["192.0.2.10", "10.0.0.0/8"], "response": "notfound", "target": ""})
        admin.set_allowed_addresses(system, "", ["192.0.2.10"])
        self.assertEqual(admin.access_settings(system), {"addresses": ["192.0.2.10"], "response": "forbidden", "target": ""})
        admin.set_allowed_addresses(system, "", [])
        self.assertEqual(conf.read_text(), self.CONF)
        with self.assertRaises(Exception):
            admin.set_allowed_addresses(system, "", ["192.0.2.10"], "redirect", "/login")

    def test_behind_cloudflare_the_visitors_own_address_is_seen(self):
        """Behind Cloudflare's proxy the admin saw Cloudflare's address (Your address now, Require ip): the visitor's
        own is taken from CF-Connecting-IP, from Cloudflare's addresses only, once however often it is set"""
        from harbor.core.system import Result
        conf = self.write_conf()
        system = RecordingSystem(root=self.root)
        admin.real_address(system, "admin.example.test")
        text = conf.read_text()
        self.assertIn("  ServerName admin.example.test\n  # Harbor: the visitor's own address", text)
        self.assertIn("  RemoteIPHeader CF-Connecting-IP\n  RemoteIPTrustedProxy 173.245.48.0/20 ", text)
        self.assertIn("a2enmod -q remoteip", system.commands)
        # Again: nothing changes, and who may reach it is set as before beside it
        admin.real_address(system, "admin.example.test")
        self.assertEqual(conf.read_text(), text)
        admin.set_allowed_addresses(system, "", ["192.0.2.10"])
        self.assertEqual(conf.read_text().count("RemoteIPHeader"), 1)
        # One Apache refuses is taken back
        conf.write_text(self.CONF)
        admin.real_address(RecordingSystem(root=self.root, answers={"apache2ctl configtest": Result(1, "", "Syntax error")}), "admin.example.test")
        self.assertEqual(conf.read_text(), self.CONF)

    def test_a_change_apache_refuses_and_a_bad_address_are_refused(self):
        from harbor.core.system import Result
        conf = self.write_conf()
        system = RecordingSystem(root=self.root, answers={"apache2ctl configtest": Result(1, "", "Syntax error")})
        with self.assertRaises(Exception):
            admin.set_allowed_addresses(system, "", ["192.0.2.10"])
        self.assertEqual(conf.read_text(), self.CONF)
        with self.assertRaises(Exception):
            admin.set_allowed_addresses(RecordingSystem(root=self.root), "", ["192.0.2.10; rm -rf /"])


class AdminAppTests(unittest.TestCase):
    """The admin's code finds its data and the release's apache folder"""

    def test_from_a_release_the_apache_folder_is_its_own(self):
        text = (ROOT / "admin/app.py").read_text()
        self.assertIn('DATA = os.environ.get("HARBOR_ADMIN_DATA") or BASE', text)
        for name in ("sql-connections.json", "audit.log", "system-update.json"):
            self.assertNotIn(f'os.path.join(BASE, "{name}")', text)
            self.assertNotIn(f'Path(BASE) / "{name}"', text)


if __name__ == "__main__":
    unittest.main()
