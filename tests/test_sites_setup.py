"""harbor site setup against setup_site.sh (frozen in tests/legacy): the same files and the same programs run"""
import tempfile
import unittest

from tests.harness import APACHE, Server, as_migrated, harbor_commands
from tests.test_core_bindings import NEEDS_BASH_4
from harbor.cli import main

PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n    <TargetFramework>net10.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n'


def html(server):
    (server.root / "var/www/blog.dk").mkdir()
    (server.root / "var/www/blog.dk/index.html").write_text("<h1>Blog</h1>")


def project(server, built=True, umbraco=False):
    site = server.root / "var/www/shop.dk"
    site.mkdir()
    (site / "Shop.csproj").write_text(PROJECT.replace("</Project>", '  <ItemGroup><PackageReference Include="Umbraco.Cms" Version="17.0.0" /></ItemGroup>\n</Project>') if umbraco else PROJECT)
    if built:
        (site / "_publish").mkdir()
        (site / "_publish/Shop.dll").write_bytes(b"\x00")


def build(server):
    site = server.root / "var/www/app.dk"
    site.mkdir()
    (site / "App.dll").write_bytes(b"\x00")
    (site / "App.runtimeconfig.json").write_text("{}")
    (site / "App.deps.json").write_text('{"libraries": {"Umbraco.Cms/17.0.0": {}}}')


@NEEDS_BASH_4
class SetupSiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")

    def compare(self, make, *args):
        for server in (self.old, self.new):
            make(server)
        result = self.old.run_legacy("setup_site.sh", *args)
        system = self.new.system()
        code = main(["site", "setup", *args], system)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(*as_migrated(self, self.new.files(), self.old.files()))
        self.assertEqual(harbor_commands(system), self.old.commands())
        return result, system

    def test_static_files(self):
        for certificate in ("n", "y"):
            with self.subTest(certificate=certificate):
                self.setUp()
                result, system = self.compare(html, "blog.dk", "blog.dk", certificate)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_project_with_a_build_and_without(self):
        for built in (True, False):
            for umbraco in (False, True):
                with self.subTest(built=built, umbraco=umbraco):
                    self.setUp()
                    result, system = self.compare(lambda server: project(server, built, umbraco), "shop.dk", "shop.dk", "n", "Test")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(any("dotnet publish" in line for line in system.commands), not built)

    def test_a_build_with_https(self):
        result, system = self.compare(build, "app.dk", "app.dk", "y")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_answering_any_name(self):
        result, system = self.compare(html, "blog.dk", "*", "n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.new.root / "etc/apache2/sites-enabled/00-vns-default.conf").is_symlink())

    def test_what_cannot_be_set_up(self):
        def with_service(server):
            project(server)
            (server.root / "etc/systemd/system/shop.dk.service").write_text("[Service]\n")
        result, system = self.compare(with_service, "shop.dk", "shop.dk", "n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(system.said[-1], result.stderr.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
