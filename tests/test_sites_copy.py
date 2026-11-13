"""harbor site copy against copy_site.sh (frozen in tests/legacy): the same files and the same programs run"""
import os
import tempfile
import unittest

from tests.harness import APACHE, Server, harbor_commands
from tests.test_core_bindings import NEEDS_BASH_4
from harbor.cli import main

CONF = ("<VirtualHost *:80>\n  ServerAdmin it@shop.dk\n  ServerName shop.dk\n  ServerAlias www.shop.dk\n  DocumentRoot /var/www/shop.dk/_publish/wwwroot\n"
        "  ProxyRequests Off\n  RequestHeader set X-Forwarded-Proto \"http\"\n  ProxyPass / http://127.0.0.1:4000/\n</VirtualHost>\n")
HTTPS = CONF.replace("*:80", "*:443").replace("</VirtualHost>", "  SSLEngine On\n  SSLCertificateFile /etc/ssl/certs/shop.dk_ecc/fullchain.cer\n  SSLCertificateKeyFile /etc/ssl/certs/shop.dk_ecc/shop.dk.key\n</VirtualHost>")
SERVICE = ("[Unit]\nDescription=Running .NET Core [shop.dk]\n[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n"
           "ExecStart=/usr/lib/dotnet/dotnet /var/www/shop.dk/_publish/Shop.dll --urls \"http://127.0.0.1:4000\"\n"
           "SyslogIdentifier=shop.dk\nRootDirectory=/var/lib/vns-roots/shop.dk\nBindPaths=/var/www/shop.dk\n")


def make_site(server, conf=CONF):
    site = server.root / "var/www/shop.dk"
    (site / "scripts").mkdir(parents=True)
    (site / "_publish").mkdir()
    (site / "Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>\n')
    (site / "_publish/Shop.dll").write_bytes(b"\x00build")
    (site / "appsettings.json").write_text('{"Umbraco": {"CMS": {"WebRouting": {"UmbracoApplicationUrl": "https://shop.dk"}}}}')
    (site / "scripts/publish.sh").write_text(f"#!/bin/bash\n_site=shop.dk\n_projectName=Shop\ncd {server.root}/var/www/shop.dk\necho published\n")
    (server.root / "etc/apache2/sites-available/shop.dk.conf").write_text(conf)
    (server.root / "etc/systemd/system/shop.dk.service").write_text(SERVICE)


@NEEDS_BASH_4
class CopySiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")

    def compare(self, *args, conf=CONF, env=None):
        for server in (self.old, self.new):
            make_site(server, conf)
        result = self.old.run_legacy("copy_site.sh", *args, env=env)
        system = self.new.system(answers={"systemctl is-active --quiet": ""})
        saved = dict(os.environ)
        os.environ.update(env or {})
        try:
            code = main(["site", "copy", *args], system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(self.new.files(), self.old.files())
        # The site's own publish script: the script ran it with bash, which is not recorded; harbor's call is
        self.assertEqual([line for line in harbor_commands(system) if not line.startswith("bash ")], self.old.commands())
        return result, system

    def test_a_site_with_a_service(self):
        result, system = self.compare("shop.dk", "copy.shop.dk", "copy.shop.dk")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Site copied: shop.dk -> copy.shop.dk (copy.shop.dk)", system.said)
        self.assertTrue((self.new.root / "var/lib/vns-roots/copy.shop.dk").is_dir())

    def test_an_https_site_gets_a_certificate_of_its_own(self):
        result, system = self.compare("shop.dk", "copy.shop.dk", "copy.shop.dk", conf=HTTPS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--issue", " ".join(system.commands))

    def test_a_new_project_is_built(self):
        result, system = self.compare("shop.dk", "copy.shop.dk", "copy.shop.dk", env={"VNS_COPY_PROJECT": "Copy"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bash /var/www/copy.shop.dk/scripts/publish.sh", harbor_commands(system))
        self.assertIn("published", result.stdout)

    def test_a_site_managed_from_another_server_is_refused(self):
        for server in (self.old, self.new):
            (server.root / "var/lib/vns-managed/shop.dk").write_text("main.dk\nadmin.other.dk\n")
        result, system = self.compare("shop.dk", "copy.shop.dk", "copy.shop.dk")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is an environment of the main site main.dk on admin.other.dk", system.said[0])


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        self.assertIn('bin/harbor" site copy "$@"', (APACHE / "copy_site.sh").read_text())


if __name__ == "__main__":
    unittest.main()
