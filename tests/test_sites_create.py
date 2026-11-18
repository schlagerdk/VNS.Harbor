"""harbor site create against create_site.sh (frozen in tests/legacy): the same files and the same programs run"""
import re
import tempfile
import unittest

from tests.harness import Server, as_migrated, harbor_commands, without_temporary
from tests.test_core_bindings import NEEDS_BASH_4
from harbor.cli import main

PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup>\n    <TargetFramework>{framework}</TargetFramework>\n  </PropertyGroup>\n</Project>\n'
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STAMP = re.compile(rb'(var cacheDate = ")[0-9]{12}(")')


DOTNET_10 = {"/usr/lib/dotnet/dotnet --list-sdks": "10.0.100 [/usr/lib/dotnet/sdk]",
             "/usr/lib/dotnet/dotnet --list-runtimes": "Microsoft.AspNetCore.App 10.0.1 [/usr/lib/dotnet/shared/Microsoft.AspNetCore.App]"}


def comparable(files):
    """The files without what differs each time: a new UUID, the minute the site was made, and the virtual environment.
    Nor .NET in /usr/lib/dotnet and its link: harbor has .NET 10 there (DOTNET_10), which the script never looked for."""
    found = {}
    for name, content in files.items():
        if "/venv/" in f"/{name}/" or name.startswith(("usr/lib/dotnet", "usr/bin/dotnet")):
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
        # .NET 10 is here on both, as on a server: harbor checks for it before making a C# site (the script did not)
        for server in (self.old, self.new):
            program = server.root / "usr/lib/dotnet/dotnet"
            program.parent.mkdir(parents=True, exist_ok=True)
            program.write_text("#!/bin/bash\n" + "".join(f'[[ "$*" == "{command.split(" ", 1)[1]}" ]] && echo "{answer}"\n' for command, answer in DOTNET_10.items()) + "exit 0\n")
            program.chmod(0o755)
            # And set up for the sites (harbor.core.dotnet.setup), as a server with .NET is
            link = server.root / "usr/bin/dotnet"
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                link.symlink_to("/usr/lib/dotnet/dotnet")
        self.old.log.write_text("")
        result = self.old.run_legacy("create_site.sh", *args, stdin=stdin)
        system = self.new.system(replies=replies or [], answers=DOTNET_10)
        code = main(["site", "create", *args], system)
        self.assertEqual(code, result.returncode, (result.stdout[-2000:], result.stderr, system.said))
        self.assertEqual(*as_migrated(self, comparable(self.new.files()), comparable(self.old.files())))
        self.maxDiff = None
        # The script ran the .NET installer with bash, which the fake server does not record
        # VNS.Harbor is added to an Umbraco 17+ site by harbor, which the script did not (test_umbraco_gets_vns_harbor)
        commands = [line for line in without_temporary(harbor_commands(system))
                    if not line.startswith("bash TEMP ") and not line.startswith("dotnet add package VNS.Harbor")
                    and not line.startswith("/usr/lib/dotnet/dotnet --list-")]
        self.assertEqual(commands, without_temporary(self.old.commands()))
        if code == 0:
            self.assertPlanned(system)
        return result, system

    def assertPlanned(self, system):
        """The admin's window shows every step from the start: the plan is the steps the creation took"""
        plans = [steps for kind, steps in system.events if kind == "plan"]
        self.assertEqual(len(plans), 1, system.events)
        self.assertEqual(plans[0], [text for kind, text in system.events if kind == "step"])

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
        # Umbraco 18 and newer: the package's versions follow Umbraco's
        for version, expected in (("18.3.1", True), ("17.2.0", False)):
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

    def test_the_dotnet_a_site_needs_is_installed_first(self):
        # .NET 10 removed in Server tools, only 8.0 left: an Umbraco 18 site installs .NET 10 before its template
        server = Server(tempfile.mkdtemp(), "x")
        (server.root / "usr/lib/dotnet").mkdir(parents=True)
        (server.root / "usr/lib/dotnet/dotnet").write_text("")
        system = server.system(answers={"/usr/lib/dotnet/dotnet --list-sdks": "8.0.404 [x]",
                                        "/usr/lib/dotnet/dotnet --list-runtimes": "Microsoft.AspNetCore.App 8.0.11 [x]"})
        self.assertEqual(main(["site", "create", "x.dk", "csharp", "umbraco", "18.3.1", "X", "x.dk", "_publish", "Release", "n"], system), 0, system.said)
        installs = [index for index, line in enumerate(system.commands) if line.startswith("bash ") and "--channel 10.0" in line]
        template = system.commands.index("dotnet new install Umbraco.Templates@18.3.1 --force")
        self.assertTrue(installs and installs[-1] < template, system.commands)
        self.assertIn("Install .NET 10.0 SDK and ASP.NET Core runtime", system.said)

    def test_the_first_dotnet_sets_up_what_the_sites_need(self):
        # A new server: harbor server init installs no .NET, the first site does, with /usr/bin/dotnet and the rest
        server = Server(tempfile.mkdtemp(), "x")
        system = server.system()
        self.assertEqual(main(["site", "create", "x.dk", "csharp", "umbraco", "18.3.1", "X", "x.dk", "_publish", "Release", "n"], system), 0, system.said)
        self.assertEqual(__import__("os").readlink(server.root / "usr/bin/dotnet"), "/usr/lib/dotnet/dotnet")
        self.assertEqual((server.root / "etc/profile.d/dotnet.sh").read_text(), "export DOTNET_ROOT=/usr/lib/dotnet\n")
        self.assertIn("dotnet tool install --tool-path /usr/local/bin dotnet-outdated-tool", system.commands)

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
                self.assertEqual(*as_migrated(self, comparable(self.new.files()), comparable(self.old.files())))


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


if __name__ == "__main__":
    unittest.main()
