import importlib.util
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
LIB = ROOT_DIR / "lib"
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

    def renamed(self, csproj="<Project>\n  <PropertyGroup>\n    <TargetFramework>net10.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n", project="kunde"):
        (self.source / "source.test.csproj").write_text(csproj)
        copy_site.prepare("source.test", "target.test", "new.example.test", "4002", self.web, self.configs, self.services, project=project)
        return self.web / "target.test"

    def test_new_project_name_renames_the_project_and_what_runs_it(self):
        (self.source / "bin/Debug").mkdir(parents=True)
        (self.source / "obj").mkdir()
        (self.source / "wwwroot/obj").mkdir(parents=True)
        (self.source / "source.test.deps.json").write_text("{}")
        (self.source / "App.sln").write_text('Project("{X}") = "source.test", "source.test.csproj", "{Y}"\n')
        (self.source / ".vns-publish").mkdir()
        (self.source / ".vns-publish/test.json").write_text("{}")
        target = self.renamed()
        self.assertFalse((target / "source.test.csproj").exists())
        self.assertIn("<RootNamespace>source.test</RootNamespace>", (target / "kunde.csproj").read_text())
        self.assertIn('"kunde", "kunde.csproj"', (target / "App.sln").read_text())
        service = (self.services / "target.test.service").read_text()
        self.assertIn(f"{target}/kunde.dll", service)
        self.assertNotIn("source.test.dll", service)
        script = (target / "scripts/publish.sh").read_text()
        self.assertIn("_projectName=kunde\n", script)
        self.assertIn("dotnet publish kunde.csproj", script)
        self.assertIn("systemctl restart target.test", script)
        for gone in ("source.test.dll", "source.test.deps.json", "bin", "obj", ".vns-publish"):
            self.assertFalse((target / gone).exists(), gone)
        self.assertTrue((target / "wwwroot/obj").is_dir(), "Only the project's own bin and obj are skipped")
        self.assertTrue((target / "appsettings.json").exists())
        self.assertTrue((self.source / "source.test.csproj").exists(), "The source is not changed")
        self.assertTrue((self.source / "bin").exists())

    def test_assembly_name_follows_and_an_existing_root_namespace_stays(self):
        target = self.renamed("<Project><PropertyGroup><AssemblyName>source.test</AssemblyName><RootNamespace>My.Code</RootNamespace></PropertyGroup></Project>")
        text = (target / "kunde.csproj").read_text()
        self.assertIn("<AssemblyName>kunde</AssemblyName>", text)
        self.assertIn("<RootNamespace>My.Code</RootNamespace>", text)
        self.assertEqual(text.count("RootNamespace>"), 2)

    def test_same_project_name_changes_nothing(self):
        target = self.renamed(project="source.test")
        self.assertTrue((target / "source.test.csproj").exists())
        self.assertTrue((target / "source.test.dll").exists())
        self.assertIn("source.test.dll", (self.services / "target.test.service").read_text())

    def test_invalid_project_or_no_project_is_refused_before_copying(self):
        (self.source / "source.test.csproj").write_text("<Project />")
        with self.assertRaises(ValueError):
            copy_site.prepare("source.test", "target.test", "new.example.test", "4002", self.web, self.configs, self.services, project="../evil")
        (self.source / "source.test.csproj").unlink()
        with self.assertRaises(ValueError):
            copy_site.prepare("source.test", "target.test", "new.example.test", "4002", self.web, self.configs, self.services, project="kunde")
        self.assertFalse((self.web / "target.test").exists())

    def test_local_sql_server_is_repointed_and_keeps_instance_and_port(self):
        for before, after in (
            ("Server=.;Database=a;User Id=u;Password=p", "Server=10.0.0.5;Database=a;User Id=u;Password=p"),
            ("Server=localhost;Database=a", "Server=10.0.0.5;Database=a"),
            ("Data Source=.\\SQLEXPRESS;Initial Catalog=a", "Data Source=10.0.0.5\\SQLEXPRESS;Initial Catalog=a"),
            ("Server=tcp:127.0.0.1,1433;Database=a", "Server=tcp:10.0.0.5,1433;Database=a"),
            ('Server="(local)";Database=a', 'Server="10.0.0.5";Database=a'),
            ("Server=db.example.test;Database=a", "Server=db.example.test;Database=a"),
            ("Server=10.0.0.9\\SQL;Database=localhost", "Server=10.0.0.9\\SQL;Database=localhost"),
        ):
            with self.subTest(before):
                self.assertEqual(copy_site.repoint_connection(before, "10.0.0.5"), after)

    def test_settings_only_change_for_sql_server(self):
        folder = self.root / "settings"
        folder.mkdir()
        (folder / "appsettings.json").write_text(json.dumps({"ConnectionStrings": {
            "db": "Server=.;Database=a", "db_ProviderName": "Microsoft.Data.SqlClient"}}))
        sqlite = json.dumps({"ConnectionStrings": {"db": "Data Source=|DataDirectory|/Umbraco.sqlite.db", "db_ProviderName": "Microsoft.Data.Sqlite"}})
        (folder / "appsettings.Production.json").write_text(sqlite)
        self.assertEqual(copy_site.repoint_local_sql(folder, "10.0.0.5"), 1)
        self.assertIn("Server=10.0.0.5", (folder / "appsettings.json").read_text())
        self.assertEqual((folder / "appsettings.Production.json").read_text(), sqlite)

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

    def test_collisions_are_rejected_before_copy(self):
        (self.web / "target.test").mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()

    def test_copy_of_a_site_with_environments_is_a_site_of_its_own(self):
        scripts = self.source / "scripts"
        (scripts / "publish.sh").rename(scripts / "publish_local.sh")
        (scripts / "clear_logs_local.sh").write_text("# clear\n")
        (scripts / "targets/production").mkdir(parents=True)
        (scripts / "targets/production/publish_prod.sh").write_text(f"PROD_LOCAL_PATH='{self.source}/_production'\nPROD_FOLDER='prod'\n")
        (scripts / "targets/live").mkdir()
        (scripts / "targets/live/publish_prod.sh").write_text("PROD_IP='10.0.0.5'\nPROD_FOLDER='live'\n")
        (scripts / "publish_prod.sh").write_text("PROD_IP='10.0.0.6'\n")
        (scripts / "data_sync.json").write_text(json.dumps({"main": {"enabled": True}, "production": {"enabled": True}}))
        (self.source / "_production").mkdir()
        (self.source / "_production/App.dll").write_text("the environment's build")
        (self.source / ".vns-publish").mkdir()
        for name in ("test.json", "prod.json", "target-production.json"):
            (self.source / ".vns-publish" / name).write_text("{}")
        self.prepare()
        target = self.web / "target.test"
        self.assertFalse((target / "scripts/targets").exists())
        self.assertFalse((target / "scripts/publish_prod.sh").exists())
        self.assertFalse((target / "_production").exists())
        self.assertTrue((target / "scripts/publish.sh").exists())
        self.assertIn(str(target), (target / "scripts/publish.sh").read_text())
        self.assertTrue((target / "scripts/clear_logs.sh").exists())
        self.assertEqual(sorted(path.name for path in (target / ".vns-publish").iterdir()), ["test.json"])
        self.assertEqual(json.loads((target / "scripts/data_sync.json").read_text()), {"main": {"enabled": True}})
        # The source keeps everything
        self.assertTrue((scripts / "targets/production/publish_prod.sh").exists())
        self.assertTrue((self.source / "_production/App.dll").exists())

    def test_copy_on_another_server_is_renamed_in_its_new_folder(self):
        new = self.web / "renamed.test"
        shutil.copytree(self.source, new)
        conf = self.root / "incoming.conf"
        service = self.root / "incoming.service"
        conf.write_text((self.configs / "source.test.conf").read_text())
        service.write_text((self.services / "source.test.service").read_text())
        old = str(self.source)
        result = subprocess.run([sys.executable, str(LIB / "copy_site.py"), "rename", old, str(new), "new.example.test",
                                 "--conf", str(conf), "--service", str(service)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"WorkingDirectory={new}", service.read_text())
        self.assertIn("/var/lib/vns-roots/renamed.test", service.read_text())
        self.assertIn("localhost:4000", service.read_text())
        self.assertIn("ServerName new.example.test", conf.read_text())
        self.assertNotIn("ServerAlias", conf.read_text())
        self.assertIn(str(new), (new / "scripts/publish.sh").read_text())
        self.assertIn("https://new.example.test", (new / "appsettings.json").read_text())

    def test_environments_keep_their_servers_and_one_in_the_folder_follows_it(self):
        new = self.web / "renamed.test"
        shutil.copytree(self.source, new)
        (new / "scripts/publish_prod.sh").write_text("PROD_IP='10.0.0.5'\nPROD_FOLDER='source.test'\nPROD_BINDING='source.test'\n")
        (new / "scripts/targets/staging").mkdir(parents=True)
        (new / "scripts/targets/staging/publish_prod.sh").write_text(f"PROD_LOCAL_PATH='{self.source}/_staging'\nPROD_FOLDER='staging-source.test'\n")
        copy_site.rewrite_copy(self.source, new, "new.example.test", (self.configs / "source.test.conf").read_text(), "")
        self.assertEqual((new / "scripts/publish_prod.sh").read_text(), "PROD_IP='10.0.0.5'\nPROD_FOLDER='source.test'\nPROD_BINDING='source.test'\n")
        self.assertEqual((new / "scripts/targets/staging/publish_prod.sh").read_text(), f"PROD_LOCAL_PATH='{new}/_staging'\nPROD_FOLDER='staging-source.test'\n")

    def test_same_binding_on_another_server_keeps_the_aliases(self):
        conf = self.root / "incoming.conf"
        conf.write_text((self.configs / "source.test.conf").read_text())
        text = copy_site.rewrite_copy(self.source, self.source, "source.test", conf.read_text(), "")[0]
        self.assertIn("ServerAlias alias.test", text)

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

    def test_a_postgresql_connection_keeps_its_own_words(self):
        """Npgsql's Host, Port and Username: never Server or User ID written into it (1.0.82)"""
        original = "Host=localhost;Port=5432;Database=shop;Username=old;Password=old;SSL Mode=Prefer"
        settings = {"server": "db.example.com,5433", "database": "shop_copy", "username": "shop", "password": 'semi;quote"value'}
        updated = copy_site.change_connection(original, settings)
        self.assertEqual(updated, 'Host="db.example.com";Port=5433;Database="shop_copy";Username="shop";Password="semi;quote""value";SSL Mode=Prefer')
        self.assertNotIn("Server", updated)
        # Without a port given, its own stays
        updated = copy_site.change_connection(original, dict(settings, server="db.example.com"))
        self.assertIn("Port=5432", updated)

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
