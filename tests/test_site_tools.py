"""The tools a site's scripts folder had (phase 2d): each template as it was (tests/legacy/solutionfiles), rendered for a
site on the fake server, against its harbor command. And the templates now: wrappers that call harbor."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from harbor.cli import main
from harbor.core.system import Result
from harbor.core.templates import Site, render
from tests.harness import APACHE, LEGACY, Server, harbor_commands
from tests.test_core_bindings import NEEDS_BASH_4

TEMPLATES = LEGACY / "solutionfiles"
SERVICE = ("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\nExecStart=/usr/lib/dotnet/dotnet /var/www/shop.dk/_publish/Shop.dll\n"
           "Environment=ASPNETCORE_ENVIRONMENT=Test\nEnvironment=DOTNET_ENVIRONMENT=Test\n")
CONF = "<VirtualHost *:80>\n  ServerName shop.dk\n  ServerAlias www.shop.dk\n</VirtualHost>\n"


def make(server):
    site = server.root / "var/www/shop.dk"
    (site / "_publish/umbraco/Logs").mkdir(parents=True)
    (site / "_publish/umbraco/Logs/log.txt").write_text("log")
    (site / "_publish/appsettings-schema.json").write_text("{}")
    (site / "appsettings-schema.json").write_text("{}")
    (site / "App_Plugins").mkdir()
    (site / "bin").mkdir()
    (site / "app").mkdir()
    (site / "app/app.py").write_text("")
    (site / "Shop.csproj").write_text('<Project><ItemGroup><PackageReference Include="Umbraco.Cms" /></ItemGroup></Project>\n')
    (server.root / "etc/systemd/system/shop.dk.service").write_text(SERVICE)
    (server.root / "etc/apache2/sites-available/shop.dk.conf").write_text(CONF)


@NEEDS_BASH_4
class SiteToolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")
        for server in (self.old, self.new):
            make(server)
        bin_folder = self.old.programs()
        # The terminal's editor and log viewer, and a wait that does not wait
        for name in ("nano", "journalctl", "sleep", "clear"):
            (bin_folder / name).write_text(f'#!/bin/bash\necho "{name} $*" >>"{self.old.log}"\n')
            (bin_folder / name).chmod(0o755)

    def legacy(self, template, *args, stdin=""):
        self.old.log.write_text("")
        site = Site("shop.dk", project_name="Shop", publish_folder="_publish", build_config="Debug", apache_dir=str(self.old.legacy_apache()))
        text = self.old.redirect(render((TEMPLATES / template).read_text(), site))
        text = text.replace('[[ $EUID -ne 0 ]] && exec sudo "$0" "$@"', "").replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
        script = self.old.root / "var/www/shop.dk/scripts" / Path(template).name
        script.parent.mkdir(exist_ok=True)
        script.write_text(text)
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, input=stdin,
                              env=os.environ | {"PATH": f"{self.old.programs()}:{os.environ['PATH']}", "VNS_PUBLISH_TRACKED": "test"}, timeout=60)

    def compare(self, template, argv, *args, replies=(), answers=None, skip=()):
        result = self.legacy(template, *args, stdin="".join(f"{reply}\n" for reply in replies))
        system = self.new.system(replies=list(replies), answers=answers or {})
        os.environ["VNS_PUBLISH_TRACKED"] = "test"
        try:
            code = main([*argv, "shop.dk", *args] if argv[0] == "site" else argv, system)
        finally:
            del os.environ["VNS_PUBLISH_TRACKED"]
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual({name: content for name, content in self.new.files().items() if "/scripts/" not in name},
                         {name: content for name, content in self.old.files().items() if "/scripts/" not in name})
        ignore = ("clear", "sleep", "uname", *skip)
        mine = [line for line in harbor_commands(system) if not line.startswith(ignore)]
        theirs = [line for line in self.old.commands() if not line.startswith(ignore)]
        self.assertEqual(mine, theirs)
        return result, system

    def test_service_tools(self):
        self.compare("common/service-scripts/check_service.sh", ["site", "check-service"])
        self.compare("common/service-scripts/restart_service.sh", ["site", "restart-service"])
        self.compare("common/scripts/restart_apache.sh", ["server", "restart-apache"])

    def test_editing_with_and_without_reload(self):
        for answer in ("", "n"):
            with self.subTest(answer=answer):
                self.compare("common/scripts/edit_conf.sh", ["site", "edit-conf"], replies=[answer])
                self.compare("common/service-scripts/edit_service.sh", ["site", "edit-service"], replies=[answer])

    def test_logs_and_permissions(self):
        self.compare("csharp/umbraco/scripts/clear_logs.sh", ["site", "clear-logs"])
        self.assertFalse((self.new.root / "var/www/shop.dk/_publish/umbraco/Logs").exists())
        self.compare("python/scripts/fix_permissions.sh", ["site", "fix-app-permissions"], skip=("find",))

    def test_a_certificate_again(self):
        for server in (self.old, self.new):
            acme = server.root / "etc/acmesh/acme.sh"
            acme.exists() or acme.write_text("#!/bin/bash\n")
            acme.chmod(0o755)
        self.compare("common/scripts/issue_certificat.sh", ["site", "reissue-certificate"])

    def test_publish(self):
        result, system = self.compare("csharp/scripts/publish.sh", ["site", "publish"], "Release", answers={"systemctl is-active --quiet": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.new.root / "var/www/shop.dk/App_Plugins").exists())
        self.assertTrue(any("dotnet publish Shop.csproj -c Release -p:PublishDir=_publish" in line for line in system.commands))


class TemplateTests(unittest.TestCase):
    def test_the_templates_call_harbor(self):
        for path in sorted(TEMPLATES.rglob("*.sh")):
            template = (APACHE / "solutionfiles" / path.relative_to(TEMPLATES)).read_text()
            with self.subTest(template=path.name):
                self.assertIn('"@APACHE_DIR@/../bin/harbor"', template)
                self.assertNotIn("PROD_PASSWORD", template)

    def test_an_environment_finds_its_site_and_id_from_where_it_is(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "var/www/shop.dk/scripts/targets/staging"
        target.mkdir(parents=True)
        (folder / "bin").mkdir()
        (folder / "bin/harbor").write_text('#!/bin/bash\necho "$VNS_TARGET $*"\n')
        (folder / "bin/harbor").chmod(0o755)
        script = target / "clear_logs_prod.sh"
        script.write_text((APACHE / "solutionfiles/csharp/umbraco/prod-scripts/clear_logs_prod.sh").read_text()
                          .replace("@APACHE_DIR@", str(folder / "apache")).replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', ""))
        (folder / "apache").mkdir()
        output = subprocess.run(["bash", str(script)], capture_output=True, text=True).stdout
        self.assertEqual(output.strip(), "staging env clear-logs shop.dk")


if __name__ == "__main__":
    unittest.main()
