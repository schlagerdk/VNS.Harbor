"""harbor site create against create_site.sh (frozen in tests/legacy): the same files and the same programs run"""
import re
import tempfile
import unittest

from tests.harness import Server, harbor_commands, without_temporary
from tests.test_core_bindings import NEEDS_BASH_4
from harbor.cli import main

PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n    <TargetFramework>{framework}</TargetFramework>\n  </PropertyGroup>\n</Project>\n'
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAMP = re.compile(rb'(var cacheDate = ")[0-9]{12}(")')


def comparable(files):
    """The files without what differs each time: a new UUID, the minute the site was made, and the virtual environment"""
    found = {}
    for name, content in files.items():
        if "/venv/" in f"/{name}/":
            continue
        if isinstance(content, bytes):
            content = STAMP.sub(rb"\1STAMP\2", UUID.sub(b"UUID", content))
        found[name] = content
    return found


@NEEDS_BASH_4
class CreateSiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")

    def compare(self, *args, stdin="", replies=None, make=None):
        if make:
            for server in (self.old, self.new):
                make(server)
        self.old.log.write_text("")
        result = self.old.run_legacy("create_site.sh", *args, stdin=stdin)
        system = self.new.system(replies=replies or [])
        code = main(["site", "create", *args], system)
        self.assertEqual(code, result.returncode, (result.stdout[-2000:], result.stderr, system.said))
        self.assertEqual(comparable(self.new.files()), comparable(self.old.files()))
        self.maxDiff = None
        # The script ran the .NET installer with bash, which the fake server does not record
        # VNS.Harbor is added to an Umbraco 17+ site by harbor, which the script did not (test_umbraco_gets_vns_harbor)
        commands = [line for line in without_temporary(harbor_commands(system))
                    if not line.startswith("bash TEMP ") and line != "dotnet add package VNS.Harbor"]
        self.assertEqual(commands, without_temporary(self.old.commands()))
        return result, system

    def test_html(self):
        for certificate in ("n", "y"):
            with self.subTest(certificate=certificate):
                self.setUp()
                result, system = self.compare("blog.dk", "html", "", "", "", "blog.dk", "", "", certificate)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(system.said[-1], f"Site created: {'https' if certificate == 'y' else 'http'}://blog.dk/ (folder: blog.dk)")

    def test_python(self):
        result, system = self.compare("app.dk", "python", "", "", "", "app.dk", "", "", "y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/var/www/app.dk/venv/bin/pip install -r /var/www/app.dk/app/requirements.txt", system.commands)

    def test_umbraco(self):
        for version, certificate, publish in (("18.3.1", "n", "_publish"), ("18.3.1", "y", ""), ("17.2.0", "y", "_publish")):
            with self.subTest(version=version, certificate=certificate, publish=publish):
                self.setUp()
                result, system = self.compare("shop.dk", "csharp", "umbraco", version, "Shop", "shop.dk", publish, "Release", certificate)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(any("dotnet publish" in line for line in system.commands))

    def test_umbraco_gets_vns_harbor(self):
        for version, expected in (("18.3.1", True), ("17.2.0", True)):
            with self.subTest(version=version):
                self.setUp()
                result, system = self.compare("shop.dk", "csharp", "umbraco", version, "Shop", "shop.dk", "_publish", "Release", "y")
                self.assertEqual("dotnet add package VNS.Harbor" in system.commands, expected)
        # Not found on the package source: the site is made all the same, and it says so
        system = Server(tempfile.mkdtemp(), "x").system(answers={"dotnet add package VNS.Harbor": __import__("harbor.core.system", fromlist=["Result"]).Result(1)})
        self.assertEqual(main(["site", "create", "x.dk", "csharp", "umbraco", "18.3.1", "X", "x.dk", "_publish", "Release", "n"], system), 0, system.said)
        self.assertIn("VNS.Harbor could not be added. Add it to the project later: dotnet add package VNS.Harbor", system.said)

    def test_umbraco_13_gets_dotnet_8(self):
        result, system = self.compare("old.dk", "csharp", "umbraco", "13", "Old", "old.dk", "_publish", "Debug", "y", "", "Test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.new.root / "var/www/old.dk/global.json").is_file())
        self.assertIn("dotnet add package VNS.Umbraco -v 13.0.0", system.commands)
        result = self.old.run_legacy("create_site.sh", "older.dk", "csharp", "umbraco", "13.3.1", "Older", "older.dk", "", "Debug", "n")
        system = self.new.system()
        self.assertEqual(main(["site", "create", "older.dk", "csharp", "umbraco", "13.3.1", "Older", "older.dk", "", "Debug", "n"], system), result.returncode)
        self.assertEqual(system.said[-1], result.stderr.strip())

    def test_a_dotnet_template(self):
        def template_output(server):
            # The fake dotnet makes nothing: the project the template makes is there already
            (server.root / "var/www/api.dk").mkdir()
            (server.root / "var/www/api.dk/Api.csproj").write_text(PROJECT.format(framework="net10.0"))
        result, system = self.compare("api.dk", "csharp", "empty", "", "Api", "api.dk", "_publish", "Debug", "n", "webapi", "Staging",
                                      make=template_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Environment=ASPNETCORE_ENVIRONMENT=Staging", (self.new.root / "etc/systemd/system/api.dk.service").read_text())
        # A template that makes no project stops it
        result, system = self.compare("none.dk", "csharp", "empty", "", "None", "none.dk", "_publish", "Debug", "n", "webapi")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(system.said[-1], "Template 'webapi' did not create None.csproj; a runnable C# site project is required.")

    def test_questions(self):
        # Nothing given: every question, in the script's order
        answers = ["n", "blog.dk", "1", ""]
        result, system = self.compare(stdin="\n".join(answers) + "\n", replies=answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(system.asked, ["Create an HTTPS certificate? [Y/n]: ", "Enter folder name: ", "Choose [1]: ",
                                        "Enter site URL binding (* = answer any name) [blog.dk]: "])
        # The certificate, the project, the kind, the publish folder, the build and the Umbraco version
        # A new site's defaults are harbor's own (test_new_site_defaults): here the script's are given
        answers = ["", "", "", "_publish", "Release", ""]
        result, system = self.compare("shop.dk", "csharp", "", "", "", "shop.dk", stdin="\n".join(answers) + "\n", replies=answers)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_answering_any_name_and_refusals(self):
        result, system = self.compare("blog.dk", "html", "", "", "", "*", "", "", "n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.new.root / "etc/apache2/sites-enabled/00-vns-default.conf").is_symlink())
        for args in (("other.dk", "html", "", "", "", "*", "", "", "n"), ("x.dk", "ruby", "", "", "", "x.dk", "", "", "n"),
                     ("x.dk", "html", "", "", "", "x.dk", "", "", "maybe"), ("x.dk", "csharp", "umbraco", "", "Bad name", "x.dk", "", "", "n"),
                     ("x.dk", "csharp", "umbraco", "", "X", "x.dk", "bin", "", "n"), ("x.dk", "csharp", "umbraco", "", "X", "x.dk", "", "Fast", "n"),
                     ("x.dk", "csharp", "empty", "", "X", "x.dk", "", "Debug", "n", "bad/name"), ("bad_name", "html", "", "", "", "x.dk", "", "", "n")):
            with self.subTest(args=args):
                result = self.old.run_legacy("create_site.sh", *args)
                system = self.new.system()
                self.assertEqual(main(["site", "create", *args], system), result.returncode, result.stderr)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(system.said[-1], result.stderr)
                self.assertEqual(comparable(self.new.files()), comparable(self.old.files()))


@NEEDS_BASH_4
class NewSiteDefaultTests(unittest.TestCase):
    def test_new_site_defaults(self):
        """A new site is where development happens: Development, published to _development (asked 2026-11-13)"""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        server = Server(folder.name, "new")
        system = server.system(replies=[""] * 6)
        self.assertEqual(main(["site", "create", "shop.dk", "csharp", "", "", "", "shop.dk"], system), 0, system.said)
        self.assertIn("Enter local publish folder [_development]: ", system.asked)
        service = (server.root / "etc/systemd/system/shop.dk.service").read_text()
        self.assertIn("Environment=ASPNETCORE_ENVIRONMENT=Development", service)
        self.assertIn("/var/www/shop.dk/_development", service)


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        from tests.harness import APACHE
        self.assertIn('/../bin/harbor" site create "$@"', (APACHE / "create_site.sh").read_text())


if __name__ == "__main__":
    unittest.main()
