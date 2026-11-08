import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("copy_site", LIB / "copy_site.py")
copy_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy_site)
sys.path.pop(0)


class CopySiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.web = self.root / "www"
        self.configs = self.root / "configs"
        self.services = self.root / "services"
        for path in (self.web, self.configs, self.services):
            path.mkdir()
        self.source = self.web / "source.test"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "source.test.dll").write_bytes(b"\x00compiled")
        (self.source / "scripts/publish.sh").write_text(
            f'_site=source.test\n_projectName=source.test\ncd "{self.source}"\n'
            f'dotnet publish source.test.csproj\nsystemctl restart source.test\n')
        (self.source / "scripts/publish.sh").chmod(0o750)
        (self.source / "appsettings.json").write_text(json.dumps({
            "ConnectionStrings": {"db": "Database=source.test;Password=test"},
            "Umbraco": {"CMS": {"WebRouting": {"UmbracoApplicationUrl": "https://source.test"}}}}))
        (self.configs / "source.test.conf").write_text(
            f'<VirtualHost *:80>\nServerName source.test\nServerAlias alias.test\n'
            f'DocumentRoot {self.source}/wwwroot\nProxyPass / http://localhost:4000/\n</VirtualHost>\n')
        (self.services / "source.test.service").write_text(
            f'WorkingDirectory={self.source}\nExecStart=dotnet {self.source}/source.test.dll --urls http://localhost:4000\n'
            'RootDirectory=/var/lib/vns-roots/source.test\n')

    def prepare(self, target="target.test"):
        return copy_site.prepare("source.test", target, "new.example.test", "4002",
                                 self.web, self.configs, self.services)

    def test_copy_preserves_project_identity_and_changes_deployment_identity(self):
        before = (self.configs / "source.test.conf").read_text()
        self.assertFalse(self.prepare())
        target = self.web / "target.test"
        self.assertEqual((target / "source.test.dll").read_bytes(), b"\x00compiled")
        service = (self.services / "target.test.service").read_text()
        self.assertIn(str(target) + "/source.test.dll", service)
        self.assertIn("localhost:4002", service)
        script = (target / "scripts/publish.sh").read_text()
        self.assertIn("_projectName=source.test", script)
        self.assertIn("systemctl restart target.test", script)
        self.assertEqual((target / "scripts/publish.sh").stat().st_mode & 0o777, 0o750)
        settings = json.loads((target / "appsettings.json").read_text())
        self.assertIn("Database=source.test", settings["ConnectionStrings"]["db"])
        self.assertEqual(settings["Umbraco"]["CMS"]["WebRouting"]["UmbracoApplicationUrl"], "https://new.example.test")
        conf = (self.configs / "target.test.conf").read_text()
        self.assertIn("ServerName new.example.test", conf)
        self.assertNotIn("ServerAlias", conf)
        self.assertEqual((self.configs / "source.test.conf").read_text(), before)

    def test_production_and_collisions_are_rejected_before_copy(self):
        prod = self.source / "scripts/publish_prod.sh"
        prod.touch()
        with self.assertRaisesRegex(ValueError, "production"):
            self.prepare()
        self.assertFalse((self.web / "target.test").exists())
        prod.unlink()
        (self.web / "target.test").mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()

    def test_https_gets_new_certificate_paths_and_unicode_folder(self):
        conf = self.configs / "source.test.conf"
        conf.write_text(conf.read_text().replace("*:80", "*:443").replace("</VirtualHost>",
            "SSLEngine on\nSSLCertificateFile /old/fullchain.cer\nSSLCertificateKeyFile /old/key\n</VirtualHost>"))
        self.assertTrue(self.prepare("æøå.test"))
        text = (self.configs / "æøå.test.conf").read_text()
        self.assertIn("/etc/ssl/certs/æøå.test_ecc/fullchain.cer", text)
        self.assertTrue((self.services / "xn--5cab8c.test.service").exists())

    def test_static_site_needs_no_service_or_port(self):
        (self.services / "source.test.service").unlink()
        self.assertFalse(copy_site.prepare("source.test", "target.test", "new.example.test",
                                          web_root=self.web, configs=self.configs, services=self.services))
        self.assertFalse((self.services / "target.test.service").exists())

    def test_optional_database_change_preserves_server_login_and_source(self):
        original = (self.source / "appsettings.json").read_text()
        copy_site.prepare("source.test", "target.test", "new.example.test", "4002",
                          self.web, self.configs, self.services, database="target.test")
        settings = json.loads((self.web / "target.test/appsettings.json").read_text())
        self.assertEqual(settings["ConnectionStrings"]["db"],
                         'Database="target.test";Password=test')
        self.assertEqual((self.source / "appsettings.json").read_text(), original)

    def test_database_replacement_handles_quoted_and_duplicate_catalogs(self):
        value = 'Server=.;Initial Catalog="old;name";UID=user;PWD="secret;value";Database=old'
        self.assertEqual(copy_site.change_database(value, "æøå.test"),
                         'Server=.;Initial Catalog="æøå.test";UID=user;PWD="secret;value";Database="æøå.test"')

    def test_invalid_database_rejected_before_destination_created(self):
        with self.assertRaises(ValueError):
            copy_site.prepare("source.test", "target.test", "new.example.test", "4002",
                              self.web, self.configs, self.services, database="bad;name")
        self.assertFalse((self.web / "target.test").exists())

    def test_full_connection_change_quotes_values_and_preserves_options(self):
        original = 'Data Source=old;Initial Catalog=old;UID=old;PWD=old;Integrated Security=True;Encrypt=True'
        settings = {"server": "tcp:sql.test,1433", "database": "new.test",
                    "username": "newuser", "password": 'semi;quote"value'}
        updated = copy_site.change_connection(original, settings)
        self.assertIn('PWD="semi;quote""value"', updated)
        self.assertIn('Data Source="tcp:sql.test,1433"', updated)
        self.assertIn("Integrated Security=False", updated)
        self.assertIn("Encrypt=True", updated)
        self.assertNotIn("UID=old", updated)

    def test_full_connection_changes_copy_not_source(self):
        original = (self.source / "appsettings.json").read_bytes()
        settings = {"server": "sql.test", "database": "new.test",
                    "username": "newuser", "password": "newpassword"}
        copy_site.prepare("source.test", "target.test", "new.example.test", "4002",
                          self.web, self.configs, self.services, connection=settings)
        data = json.loads((self.web / "target.test/appsettings.json").read_text())
        connection = data["ConnectionStrings"]["db"]
        self.assertIn('Server="sql.test"', connection)
        self.assertIn('User ID="newuser"', connection)
        self.assertIn('Password="newpassword"', connection)
        self.assertEqual((self.source / "appsettings.json").read_bytes(), original)

    def test_missing_connection_field_rejected_before_copy(self):
        with self.assertRaises(ValueError):
            copy_site.prepare("source.test", "target.test", "new.example.test", "4002",
                              self.web, self.configs, self.services,
                              connection={"server": ".", "database": "new", "username": "user"})
        self.assertFalse((self.web / "target.test").exists())
