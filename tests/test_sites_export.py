"""A site exported for a customer (Settings, Export): VNS Harbor taken out of its project and folders, and an
environment's own files kept over a fresh build"""
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import zipfile

from harbor.core.system import Stop
from harbor.sites import export

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Umbraco.Cms" Version="18.3.1" />
    <PackageReference Include="VNS.Harbor" Version="18.3.2.3" />
  </ItemGroup>
  <!-- Exclude publish folder from build -->
  <ItemGroup>
    <Content Remove="_publish/**" />
    <Compile Remove="_publish/**" />
    <None Remove="_publish/**" />
  </ItemGroup>
  <!-- Exclude local prod publish folder from build -->
  <ItemGroup>
    <Content Remove="_test/**" />
    <Compile Remove="_test/**" />
    <None Remove="_test/**" />
  </ItemGroup>
</Project>
"""

NUGET = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <add key="nuget.org" value="https://api.nuget.org/v3/index.json" />
    <!-- VNS.Harbor while it is tested, before it is on nuget.org -->
    <add key="vns-local" value="/srv/vns-nuget" />
  </packageSources>
</configuration>
"""


class StripTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.root = Path(self.temp.name)

	def tearDown(self):
		self.temp.cleanup()

	def test_the_project_without_harbor(self):
		(self.root / "site.csproj").write_text(CSPROJ)
		(self.root / "nuget.config").write_text(NUGET)
		(self.root / "vns-harbor.json").write_text("{}")
		export.strip_project(self.root, ["_publish", "_test_external", "databases"])
		csproj = (self.root / "site.csproj").read_text()
		self.assertNotIn("VNS.Harbor", csproj)
		self.assertIn('<PackageReference Include="Umbraco.Cms" Version="18.3.1" />', csproj)
		# Harbor's blocks give way to one a folder, as Harbor writes them, and none is left empty
		self.assertNotIn("_test/**", csproj)
		self.assertNotIn("Exclude local prod publish folder", csproj)
		self.assertNotRegex(csproj, r"<ItemGroup>\s*</ItemGroup>")
		for folder in ("_publish", "_test_external", "databases"):
			self.assertIn(f'  <!-- Exclude {folder} from build -->\n  <ItemGroup>\n    <Content Remove="{folder}/**" />\n'
			              f'    <Compile Remove="{folder}/**" />\n    <None Remove="{folder}/**" />\n  </ItemGroup>\n', csproj)
		self.assertTrue(csproj.endswith("</ItemGroup>\n</Project>\n"))
		# English and Danish only, as Harbor builds it
		self.assertIn("<SatelliteResourceLanguages>en;da</SatelliteResourceLanguages>", csproj)
		# Left with nuget.org alone it says what dotnet does anyway: it goes
		self.assertFalse((self.root / "nuget.config").exists())
		self.assertFalse((self.root / "vns-harbor.json").exists())

	def test_a_nuget_config_with_the_customers_own_sources_stays(self):
		(self.root / "nuget.config").write_text(NUGET.replace('<add key="vns-local"', '<add key="ours" value="https://nuget.example.com/v3/index.json" />\n    <add key="vns-local"'))
		export.strip_project(self.root)
		nuget = (self.root / "nuget.config").read_text()
		self.assertNotIn("vns-local", nuget)
		self.assertIn("nuget.example.com", nuget)

	def test_a_folder_without_harbors_files_logs_and_caches(self):
		for name in ("vns-harbor.json", ".harbor-published", "scripts/run.sh", ".vns-publish/state.json", "umbraco/Logs/today.json",
		             "umbraco/Data/TEMP/x", "wwwroot/App_Plugins/VNS.Harbor/harbor.js", "umbraco/Data/Umbraco.sqlite.db", "wwwroot/media/a.jpg"):
			path = self.root / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text("x")
		export.strip_folder(self.root)
		left = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
		self.assertEqual(left, ["umbraco/Data/Umbraco.sqlite.db", "wwwroot/media/a.jpg"])

	def test_an_environments_own_over_its_build(self):
		live, build = self.root / "live", self.root / "build"
		for name, text in (("appsettings.json", "live"), ("appsettings-schema.json", "schema"), ("umbraco/Data/Umbraco.sqlite.db", "data"),
		                   ("Views/Home.cshtml", "edited"), ("wwwroot/media/a.jpg", "image"), ("wwwroot/App_Plugins/Old/old.js", "old"), ("Site.dll", "old build")):
			path = live / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(text)
		for name, text in (("appsettings.json", "built"), ("Site.dll", "new build"), ("wwwroot/App_Plugins/New/new.js", "new")):
			path = build / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(text)
		export.own_files(live, build)
		self.assertEqual((build / "appsettings.json").read_text(), "live")
		self.assertEqual((build / "Site.dll").read_text(), "new build")
		self.assertEqual((build / "umbraco/Data/Umbraco.sqlite.db").read_text(), "data")
		self.assertEqual((build / "Views/Home.cshtml").read_text(), "edited")
		self.assertEqual((build / "wwwroot/media/a.jpg").read_text(), "image")
		# The packages' code is the build's: an old one is not brought along, nor a schema file
		self.assertFalse((build / "wwwroot/App_Plugins/Old").exists())
		self.assertTrue((build / "wwwroot/App_Plugins/New/new.js").exists())
		self.assertFalse((build / "appsettings-schema.json").exists())


class SettingsTests(unittest.TestCase):
	def test_no_database_server_password_or_mail_server_of_ours(self):
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "appsettings.json"
			path.write_text("""{
  "ConnectionStrings": {
    "umbracoDbDSN": "Server=10.0.0.2,1433;Database=site;User Id=site;Password=secret;TrustServerCertificate=True;",
    "umbracoDbDSN_ProviderName": "Microsoft.Data.SqlClient",
    "local": "Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared"
  },
  // Ours
  "Umbraco": { "CMS": { "WebRouting": { "UmbracoApplicationUrl": "https://www.example.com" },
                        "Global": { "Id": "x", "UseHttps": true, "Smtp": { "SecureSocketOptions": "None", "Name": "Example", "From": "no-reply@example.com", "Host": "mail.example.com", "Port": 25 } },
                        "Content": { "Notifications": { "Email": "no-reply@example.com" } } } }
}
""")
			export.clean_settings(folder)
			text = path.read_text()
			# In place: its comment is still there
			self.assertIn("// Ours", text)
			data = json.loads(text.replace("  // Ours\n", ""))
			self.assertEqual(data["ConnectionStrings"]["umbracoDbDSN"], "Server=;Database=site;User Id=site;Password=;TrustServerCertificate=True;")
			self.assertEqual(data["ConnectionStrings"]["local"], "Data Source=|DataDirectory|/Umbraco.sqlite.db;Cache=Shared")
			# The fields stay with placeholders Umbraco starts with (it checks From and Email are addresses); the rest as it was
			self.assertEqual(data["Umbraco"]["CMS"]["Global"]["Smtp"], {"SecureSocketOptions": "None", "Name": "Your company", "From": "noreply@example.com",
			                                                           "Host": "smtp.example.com", "Port": 25})
			self.assertEqual(data["Umbraco"]["CMS"]["Content"]["Notifications"], {"Email": "noreply@example.com"})
			# Plain http: the customer's server has no certificate for it yet
			self.assertIs(data["Umbraco"]["CMS"]["Global"]["UseHttps"], False)
			self.assertEqual(data["Umbraco"]["CMS"]["WebRouting"]["UmbracoApplicationUrl"], "http://www.example.com")


class LayoutTests(unittest.TestCase):
	def test_another_servers_environment_beside_one_of_its_name_here(self):
		system = mock.Mock()
		system.path = lambda path: Path("/nonexistent" + path)
		site = mock.Mock(site_root="/var/www/example.com")
		test = mock.Mock(other={"ENVIRONMENT": "Test"})
		found = [("example.com", "local", "example.com", "/var/www/example.com/_publish", None, "main"),
		         ("test-example.com", "local", "test-example.com", "/var/www/example.com/_test", test, "test"),
		         ("test2-example.com", "remote", "test2-example.com", "/var/www/test2-example.com", test, "test-2")]
		with mock.patch("os.path.realpath", side_effect=lambda path: str(path).replace("/nonexistent", "")):
			named = export.folder_names(system, site, found)
		self.assertEqual([place for _part, place in named], ["_publish", "_test", "_test_external"])

	def test_the_publish_script_for_each_os(self):
		# Each environment its build (Development and Test Debug, Staging and Production Release) and its name
		name, content = export.publish_script("windows", "example", [("_publish", "Production"), ("_test", "Test")])
		self.assertEqual(name, "publish.ps1")
		self.assertIn(b'@{ Folder = "_publish"; Configuration = "Release"; Name = "Production" }, @{ Folder = "_test"; Configuration = "Debug"; Name = "Test" }', content)
		self.assertIn(b"ASPNETCORE_ENVIRONMENT", content)
		# Merged file by file by robocopy: never Copy-Item of a folder, which puts it inside one of its name (refs\\refs)
		self.assertIn(b"robocopy $build $folder @options", content)
		self.assertNotIn(b"Copy-Item -Recurse", content)
		# The page visitors see meanwhile is in the script itself: its logo in it, nothing it would fetch
		page = base64.b64decode(re.search(rb'FromBase64String\("([^"]+)"\)', content)[1]).decode()
		self.assertIn("Powered by VNS", page)
		self.assertIn("data:image/png;base64,", page)
		self.assertNotIn("@LOGO@", page)
		# The project tidied after the build, as Harbor does: bin, obj and the schema files
		self.assertIn(b'@("bin", "umbraco-package-schema.json")', content)
		self.assertNotIn(b'"obj"', content)
		self.assertNotIn(b"\r\r", content)
		# Started from Explorer it waits before its window closes
		self.assertIn(b'Read-Host "Press Enter to close"', content)
		self.assertTrue(content.endswith(b"Done\r\n"))
		name, content = export.publish_script("linux", "example", [("_publish", "Development"), ("_staging", "Staging")])
		self.assertEqual(name, "publish.sh")
		self.assertIn(b"environments=(_publish _staging)", content)
		self.assertIn(b"configurations=(Debug Release)", content)
		self.assertIn(b"names=(Development Staging)", content)
		self.assertIn(b'dotnet publish "example.csproj" -c "${configurations[$index]}"', content)
		self.assertIn(b'rm -rf "$build" bin appsettings-schema*.json umbraco-package-schema.json', content)


class DeleteTests(unittest.TestCase):
	def test_restore_has_every_environment_whatever_went_in_the_zip(self):
		"""Deleted with its export, the whole site goes: the snapshot Restore puts back has all of it, not only the one
		environment chosen for the zip"""
		with tempfile.TemporaryDirectory() as root:
			system = mock.Mock()
			system.path = lambda path: Path(root + str(path))
			site = mock.Mock(site_root="/var/www/example.com", name="example.com")
			everything = [("example.com", "local", "example.com", "/var/www/example.com/_publish", None, "main"),
			              ("test-example.com", "remote", "test-example.com", "/var/www/test-example.com", mock.Mock(other={}), "test")]
			def units(system, name, targets):
				return site, [part for part in everything if part[5] in targets]
			def build_zip(*args, **kwargs):
				raise AssertionError("not reached")
			with mock.patch.object(export, "units", side_effect=units), \
					mock.patch("harbor.environments.settings.target_ids", return_value=["test"]), \
					mock.patch.object(export, "snapshot", return_value={"local": [], "remote": []}) as snapshot, \
					mock.patch("harbor.sites.delete.delete_site") as delete, \
					mock.patch.object(export, "folder_names", return_value=[(everything[0], "_publish")]), \
					mock.patch.object(export, "environment_name", return_value="Production"), \
					mock.patch("harbor.sites.convert.forget_gone"), mock.patch.object(export, "EXPORTS", "/exports"):
				Path(root, "var/www/example.com/_publish").mkdir(parents=True)
				Path(root, "var/www/example.com/_publish/index.html").write_text("x")
				export.export_site(system, "example.com", "any", ["main"], delete=True)
			self.assertEqual([part[5] for part in snapshot.call_args.args[3]], ["main", "test"])
			delete.assert_called_once()


class WebConfigTests(unittest.TestCase):
	"""A Windows export has its web.config, to be put on IIS as it is, though the project runs on Linux and turns it off
	(found exporting a site, 1.0.88)"""
	def test_the_project_makes_it_again(self):
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "example.csproj"
			path.write_bytes(b"\xef\xbb\xbf<Project>\r\n  <PropertyGroup>\r\n    <TargetFramework>net10.0</TargetFramework>\r\n"
			                 b"    <IsTransformWebConfigDisabled>true</IsTransformWebConfigDisabled>\r\n  </PropertyGroup>\r\n</Project>\r\n")
			export.web_config_on(folder)
			self.assertEqual(path.read_bytes(), b"\xef\xbb\xbf<Project>\r\n  <PropertyGroup>\r\n    <TargetFramework>net10.0</TargetFramework>\r\n"
			                                    b"  </PropertyGroup>\r\n</Project>\r\n")

	def test_the_build_has_it(self):
		with tempfile.TemporaryDirectory() as folder:
			source, work = Path(folder, "source"), Path(folder, "work")
			source.mkdir()
			work.mkdir()
			(source / "example.csproj").write_text("<Project />")
			calls = []
			def run(*argv, **kwargs):
				calls.append(argv)
				output = Path(next(arg for arg in argv if arg.startswith("-p:PublishDir="))[len("-p:PublishDir="):])
				(output / "example.dll").write_text("")
			system = mock.Mock(run=run)
			output = export.build(system, source, work, "windows", "Release")
			self.assertIn("-p:IsTransformWebConfigDisabled=false", calls[0])
			# dotnet made none: the one it makes for IIS is written
			config = (output / "web.config").read_text()
			self.assertIn('arguments=".\\example.dll"', config)
			self.assertIn('modules="AspNetCoreModuleV2"', config)
			linux = export.build(mock.Mock(run=run), source, Path(folder, "work2"), "linux", "Release") if Path(folder, "work2").mkdir() is None else None
			self.assertFalse((linux / "web.config").exists())


class ZipTests(unittest.TestCase):
	def test_fast_deflate_and_media_stored(self):
		with tempfile.TemporaryDirectory() as folder:
			work = Path(folder, "work")
			tree = work / "example.com"
			(tree / "wwwroot/media").mkdir(parents=True)
			(tree / "appsettings.json").write_text("{}" * 1000)
			(tree / "wwwroot/media/photo.JPG").write_bytes(b"\xff\xd8" + b"x" * 1000)
			(tree / "scripts").mkdir()
			(tree / "scripts/publish.sh").write_text("#!/bin/bash\n")
			os.chmod(tree / "scripts/publish.sh", 0o755)
			archive = Path(folder, "example.zip")
			export.make_zip(tree, work, archive)
			with zipfile.ZipFile(archive) as zipped:
				found = {info.filename: info for info in zipped.infolist()}
			self.assertEqual(found["example.com/appsettings.json"].compress_type, zipfile.ZIP_DEFLATED)
			self.assertEqual(found["example.com/wwwroot/media/photo.JPG"].compress_type, zipfile.ZIP_STORED)
			# The publish script can be run as it is
			self.assertEqual(found["example.com/scripts/publish.sh"].external_attr >> 16 & 0o777, 0o755)


class ManagedTests(unittest.TestCase):
	"""A site that is an environment of a site on another server: the Harbor there exports it, as it has the project"""
	def system(self, root, servers):
		from harbor.core.system import RecordingSystem
		system = RecordingSystem(root=root)
		Path(root, "var/lib/vns-managed").mkdir(parents=True)
		Path(root, "var/lib/vns-managed/example.com").write_text("test.example.com\nWEB-01.example.net\n")
		Path(root, "var/lib/harbor").mkdir(parents=True, exist_ok=True)
		Path(root, "var/lib/harbor/servers.json").write_text(json.dumps(servers))
		return system

	def test_built_on_the_server_it_is_managed_from(self):
		with tempfile.TemporaryDirectory() as root:
			system = self.system(root, {"web-01": {"host": "10.0.0.5", "fqdn": "web-01.example.net", "user": "root", "port": 22}})
			with mock.patch.dict("os.environ", {"HARBOR_PROGRESS": "1"}):
				export.export_site(system, "example.com", "windows", ["main"], by="Anna", keep_hours=24)
			sent = next(line for line in system.commands if line.startswith("ssh "))
			# Its steps there are shown here as it takes them
			self.assertIn("export HARBOR_PROGRESS=1", sent)
			# There it is its site's environment, by its folder: built for Windows, kept there and listed here
			self.assertIn("root@10.0.0.5", sent)
			self.assertIn("site export test.example.com --os windows --targets unit:example.com --by Anna --keep 24", sent)
			self.assertFalse(Path(root, "var/lib/harbor/exports").exists(), "nothing made here")

	def test_not_without_that_server_in_servers(self):
		with tempfile.TemporaryDirectory() as root:
			system = self.system(root, {"web-02": {"host": "10.0.0.6", "fqdn": "web-02.example.net", "user": "root", "port": 22}})
			with self.assertRaises(Stop) as stopped:
				export.export_site(system, "example.com", "windows", ["main"])
			self.assertIn("add that server under Servers", str(stopped.exception))
			self.assertFalse([line for line in system.commands if line.startswith("ssh ")])
			# Nor deleted with its export from here: it is deleted from there
			with self.assertRaises(Stop):
				export.export_site(system, "example.com", "windows", ["main"], delete=True)

	def test_the_server_is_found_by_its_full_name_address_or_short_name(self):
		with tempfile.TemporaryDirectory() as root:
			system = self.system(root, {"web-01": {"host": "10.0.0.5", "user": "root", "port": 22}})
			for host in ("WEB-01.example.net", "web-01", "10.0.0.5"):
				self.assertEqual(export.manager_server(system, host)[0], "web-01", host)
			self.assertIsNone(export.manager_server(system, "web-02.example.net"))

	def test_its_exports_are_listed_for_its_server_with_their_link(self):
		with tempfile.TemporaryDirectory() as root:
			from harbor.core.system import RecordingSystem
			system = RecordingSystem(root=root)
			Path(root, "etc/systemd/system").mkdir(parents=True)
			Path(root, "etc/systemd/system/admin.service").write_text("[Service]\nEnvironment=HARBOR_ADMIN_DATA=/var/www/admin.web-01.example.net/app\n")
			Path(root, "var/www/admin.web-01.example.net/app").mkdir(parents=True)
			Path(root, "var/www/admin.web-01.example.net/app/config.json").write_text("{}")
			for identifier, environments in (("a" * 16, ["test.example.com", "example.com"]), ("b" * 16, ["test.example.com"])):
				Path(root, "var/lib/harbor/exports", identifier).mkdir(parents=True)
				Path(root, "var/lib/harbor/exports", identifier, "meta.json").write_text(json.dumps(
					{"id": identifier, "site": "test.example.com", "environments": environments, "state": "ready", "token": "t" * 30, "file": "x.zip", "at": 1}))
			with mock.patch("builtins.print") as printed:
				export.export_list(system, ["example.com"])
			found = json.loads(printed.call_args.args[0])["exports"]
			self.assertEqual([item["id"] for item in found], ["a" * 16])
			self.assertEqual(found[0]["link"], "https://admin.web-01.example.net/download/" + "t" * 30)
			self.assertNotIn("token", found[0])


class LifetimeTests(unittest.TestCase):
	def test_deleted_the_time_chosen_after_its_first_download_or_never(self):
		with tempfile.TemporaryDirectory() as root:
			system = mock.Mock()
			system.path = lambda path: Path(root + str(path))
			now = 1_000_000
			made = {"a" * 16: {"keep": 24 * 3600, "downloaded": now - 25 * 3600}, "b" * 16: {"keep": 72 * 3600, "downloaded": now - 25 * 3600},
			        "c" * 16: {"keep": None, "downloaded": now - 400 * 86400}, "d" * 16: {"downloaded": now - 73 * 3600}}
			for identifier, item in made.items():
				folder = Path(root, "exports", identifier)
				folder.mkdir(parents=True)
				(folder / "meta.json").write_text(json.dumps({"id": identifier, "site": "example.com", "state": "ready", "at": 1, **item}))
			with mock.patch.object(export, "EXPORTS", "/exports"), mock.patch("time.time", return_value=now):
				export.cleanup(system)
			# 24 hours gone; 72 hours not yet; never kept; one from before the choice keeps 72 hours
			self.assertEqual(sorted(path.name for path in Path(root, "exports").iterdir()), ["b" * 16, "c" * 16])


class ScriptsTests(unittest.TestCase):
	def test_every_script_for_each_os(self):
		from harbor.sites import export_scripts
		environments = [("_publish", "Production"), ("_test", "Test")]
		windows = dict(export_scripts.scripts("windows", "example", environments))
		self.assertEqual(sorted(windows), ["add-package.ps1", "check-packages.ps1", "clear-logs.ps1", "remove-package.ps1",
		                                   "sync-data.ps1", "sync-settings.ps1"])
		self.assertIn(b'@{ Folder = "_test"; Name = "Test" }', windows["clear-logs.ps1"])
		self.assertIn(b'"wwwroot\\css"', windows["sync-settings.ps1"])
		self.assertIn(b'"Content", "Dictionary", "Media"', windows["sync-data.ps1"])
		self.assertNotIn(b"\r\r", windows["sync-data.ps1"])
		# Started from Explorer it waits before its window closes, also after an error
		for name in ("clear-logs.ps1", "check-packages.ps1"):
			self.assertIn(b'Read-Host "Press Enter to close"', windows[name])
			self.assertIn(b"trap {", windows[name])
			self.assertTrue(windows[name].endswith(b"Done\r\n"), name)
			self.assertNotIn(b"@PAUSE@", windows[name])
		self.assertNotIn(b"@", windows["add-package.ps1"].replace(b"@(", b"").replace(b"@{", b""))
		linux = dict(export_scripts.scripts("linux", "example", environments))
		self.assertEqual(sorted(linux), ["add-package.sh", "check-packages.sh", "clear-logs.sh", "remove-package.sh", "sync-data.sh", "sync-settings.sh"])
		self.assertIn(b"environments=(_publish _test)", linux["sync-settings.sh"])
		self.assertIn(b"ContentTypes DataTypes", linux["sync-settings.sh"])
		self.assertIn(b'dotnet list "example.csproj" package --outdated', linux["check-packages.sh"])
		for name, content in linux.items():
			self.assertNotIn(b"@PROJECT@", content, name)
		# With one environment there is nothing to sync to
		alone = dict(export_scripts.scripts("linux", "example", environments[:1]))
		self.assertEqual(sorted(alone), ["add-package.sh", "check-packages.sh", "clear-logs.sh", "remove-package.sh"])


class ReadmeTests(unittest.TestCase):
	def test_what_the_customer_got_in_danish_and_english(self):
		from harbor.sites import export_scripts
		text = export_scripts.readme("example.com", "windows", [("_publish", "Production"), ("_test_external", "Test")], ["test_external.bak"], "net10.0")
		self.assertIn(b"\r\n", text)
		text = text.decode()
		for line in ("DANSK", "ENGLISH", "_publish", "_test_external", "databases/test_external.bak", "Physical path", ".NET 10 Hosting Bundle",
		             "powershell -File scripts\\publish.ps1", "powershell -File scripts\\sync-data.ps1", "app_offline.htm", "Smtp"):
			self.assertIn(line, text)
		self.assertNotIn(".NET 10.0", text)
		linux = export_scripts.readme("example.com", "linux", [("_publish", "Production")], [], "net10.0").decode()
		self.assertIn("bash scripts/publish.sh", linux)
		self.assertNotIn("databases", linux)
		self.assertNotIn("sync-data", linux)
		self.assertNotIn("\r", linux)


if __name__ == "__main__":
	unittest.main()
