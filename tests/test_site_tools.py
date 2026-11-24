"""The tools a site's scripts folder had (phase 2d): each template as it was (tests/legacy/solutionfiles), rendered for a
site on the fake server, against its harbor command. And the templates now: wrappers that call harbor."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from harbor.cli import main
from harbor.core.system import RecordingSystem, Result
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
        site = Site("shop.dk", project_name="Shop", publish_folder="_publish", build_config="Debug", harbor_dir=str(self.old.legacy_apache()))
        text = self.old.redirect(render((TEMPLATES / template).read_text(), site).replace("@APACHE_DIR@", site.harbor_dir))
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
        # Unlike some scripts, systemctl status never opens a pager: the admin's window waited for a key that never came.
        # Compared without it, on both sides
        pager = lambda line: line.replace(" --no-pager", "") if line.startswith("systemctl status ") else line
        mine = [pager(line) for line in harbor_commands(system) if not line.startswith(ignore)]
        theirs = [pager(line) for line in self.old.commands() if not line.startswith(ignore)]
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
        # The script reloaded Apache; harbor installs the certificate where the config reads it, which reloads it and
        # makes each renewal reload it too (1.0.67)
        result, system = self.compare("common/scripts/issue_certificat.sh", ["site", "reissue-certificate"], skip=("systemctl reload", "/etc/acmesh/acme.sh --install-cert"))
        self.assertIn("/etc/acmesh/acme.sh --home /etc/acmesh --config-home /etc/ssl/data --install-cert -d shop.dk "
                      "--key-file /etc/ssl/certs/shop.dk_ecc/shop.dk.key --fullchain-file /etc/ssl/certs/shop.dk_ecc/fullchain.cer "
                      "--reloadcmd systemctl reload apache2", system.commands)

    def test_publish(self):
        result, system = self.compare("csharp/scripts/publish.sh", ["site", "publish"], "Release", answers={"systemctl is-active --quiet": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.new.root / "var/www/shop.dk/App_Plugins").exists())
        self.assertTrue(any("dotnet publish Shop.csproj -c Release -p:PublishDir=_publish" in line for line in system.commands))


class ConfigurationTests(unittest.TestCase):
    """Staging and Production are Release builds, Development and Test Debug: the site's environment decides"""

    def publish(self, environment, saved="Debug", given=""):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "var/www/shop.dk").mkdir(parents=True)
        (root / "etc/systemd/system").mkdir(parents=True)
        lines = "[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n" + (f"Environment=ASPNETCORE_ENVIRONMENT={environment}\n" if environment else "")
        (root / "etc/systemd/system/shop.dk.service").write_text(lines)
        state = root / "var/lib/harbor/sites/shop.dk"
        state.mkdir(parents=True)
        (state / "migrated.json").write_text("{}")
        (state / "site.json").write_text(json.dumps({"configuration": saved}))
        system = RecordingSystem(root=root)
        with mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}):
            os.environ.pop("VNS_PUBLISH_TRACKED", None)
            self.assertEqual(main(["site", "publish", "shop.dk", *([given] if given else [])], system), 0, system.said)
        return system.commands[0].split(" site publish shop.dk ", 1)[1].split()[0]

    def test_the_environment_decides(self):
        for environment, expected in (("Production", "Release"), ("Staging", "Release"), ("Test", "Debug"), ("Development", "Debug")):
            with self.subTest(environment=environment):
                self.assertEqual(self.publish(environment), expected)
        self.assertEqual(self.publish("Production", saved="Debug"), "Release", "what the site was made with comes second")

    def test_without_an_environment_what_it_was_made_with(self):
        self.assertEqual(self.publish("", saved="Release"), "Release")
        self.assertEqual(self.publish("", saved="Debug"), "Debug")

    def test_one_given_is_used(self):
        self.assertEqual(self.publish("Production", given="Debug"), "Debug")



class NoPagerTests(unittest.TestCase):
    def test_systemctl_status_never_waits_for_a_key(self):
        """Clear logs hung in its window: systemctl status opened a pager (lines 1-12/12 (END)) on the terminal"""
        root = Path(__file__).resolve().parents[1] / "harbor"
        found = [f"{path.relative_to(root)}:{number}" for path in root.rglob("*.py")
                 for number, line in enumerate(path.read_text().splitlines(), 1)
                 if '"systemctl", "status"' in line and "--no-pager" not in line]
        self.assertEqual(found, [])

if __name__ == "__main__":
    unittest.main()


class PinnedRequirementsTests(unittest.TestCase):
    def test_pinned_versions_move_to_the_latest(self):
        from harbor.sites.tools import pinned_to_latest
        text = "# app\nFlask==2.3.3\nrequests[socks] == 2.31.0 ; python_version >= '3.8'\nJinja2>=3.1\ngunicorn==23.0.0\n"
        new, changed = pinned_to_latest(text, {"flask": "3.1.2", "Requests": "2.32.5", "Jinja2": "3.1.6", "gunicorn": "23.0.0"})
        self.assertEqual(new, "# app\nFlask==3.1.2\nrequests[socks]==2.32.5 ; python_version >= '3.8'\nJinja2>=3.1\ngunicorn==23.0.0\n")
        self.assertEqual(changed, ["Flask 2.3.3 → 3.1.2", "requests 2.31.0 → 2.32.5"])

