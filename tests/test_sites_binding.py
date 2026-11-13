"""harbor site binding against change_binding.sh (frozen in tests/legacy): the same files and the same programs run"""
import tempfile
import unittest

from tests.harness import APACHE, Server, harbor_commands
from tests.test_core_bindings import NEEDS_BASH_4
from harbor.cli import main

HTTP = ("<VirtualHost *:80>\n  ServerAdmin it@shop.dk\n  ServerName shop.dk\n  ServerAlias www.shop.dk\n  DocumentRoot /var/www/shop.dk/_publish/wwwroot\n"
        "  ProxyRequests Off\n  ProxyPass / http://127.0.0.1:4000/\n</VirtualHost>\n")
HTTPS = ("<VirtualHost *:80>\n  ServerName shop.dk\n  Redirect permanent / https://shop.dk/\n</VirtualHost>\n\n"
         "<VirtualHost *:443>\n  ServerAdmin it@shop.dk\n  ServerName shop.dk\n  ServerAlias www.shop.dk\n  ProxyPass / http://127.0.0.1:4000/\n"
         "  SSLEngine On\n  SSLCertificateFile /etc/ssl/certs/shop.dk_ecc/fullchain.cer\n  SSLCertificateKeyFile /etc/ssl/certs/shop.dk_ecc/shop.dk.key\n</VirtualHost>\n")
SETTINGS = '{\n  "Umbraco": { "CMS": { "WebRouting": { "UmbracoApplicationUrl": "https://shop.dk" } } }\n}\n'


def make(server, conf=HTTP, service=True, settings=True):
    root = server.root
    (root / "var/www/shop.dk/_publish").mkdir(parents=True)
    (root / "etc/apache2/sites-available/shop.dk.conf").write_text(conf)
    (root / "etc/apache2/sites-enabled/shop.dk.conf").symlink_to("../sites-available/shop.dk.conf")
    (root / "etc/apache2/sites-available/other.dk.conf").write_text("<VirtualHost *:80>\n  ServerName other.dk\n</VirtualHost>\n")
    (root / "etc/apache2/sites-enabled/other.dk.conf").symlink_to("../sites-available/other.dk.conf")
    if service:
        (root / "etc/systemd/system/shop.dk.service").write_text("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n")
    if settings:
        (root / "var/www/shop.dk/_publish/appsettings.json").write_text(SETTINGS)
    (root / "etc/ssl/certs/shop.dk_ecc").mkdir()
    (root / "etc/ssl/certs/shop.dk_ecc/fullchain.cer").write_text("cert")
    (root / "etc/ssl/certs/shop.dk_ecc/shop.dk.key").write_text("key")


@NEEDS_BASH_4
class ChangeBindingTests(unittest.TestCase):
    def compare(self, args, **make_args):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        old, new = Server(folder.name, "old"), Server(folder.name, "new")
        make(old, **make_args)
        make(new, **make_args)
        result = old.run_legacy("change_binding.sh", *args)
        system = new.system()
        code = main(["site", "binding", *args], system)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(new.files(), old.files())
        self.assertEqual(harbor_commands(system), old.commands())
        return result, system, new

    def test_http_binding_and_aliases(self):
        result, system, server = self.compare(["shop.dk", "new.shop.dk", "a.shop.dk, b.shop.dk", "--yes"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"UmbracoApplicationUrl": "http://new.shop.dk"', (server.root / "var/www/shop.dk/_publish/appsettings.json").read_text())

    def test_unchanged(self):
        result, system, server = self.compare(["shop.dk", "shop.dk", "www.shop.dk", "--yes"])
        self.assertIn("The binding and aliases are unchanged.", system.said)

    def test_a_site_without_a_servername_gets_one(self):
        self.compare(["shop.dk", "shop.dk", "", "--yes"], conf=HTTP.replace("  ServerName shop.dk\n", ""), settings=False)

    def test_https_gets_a_new_certificate_and_the_old_goes(self):
        result, system, server = self.compare(["shop.dk", "new.shop.dk", "www.new.shop.dk", "--yes"], conf=HTTPS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/etc/acmesh/acme.sh --revoke -d shop.dk", system.commands)

    def test_answering_any_name(self):
        self.compare(["shop.dk", "*", "", "--yes"], settings=False)

    def test_a_name_another_site_has(self):
        result, system, server = self.compare(["shop.dk", "other.dk", "", "--yes"])
        self.assertEqual(result.returncode, 1)


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        self.assertIn('bin/harbor" site binding "$@"', (APACHE / "change_binding.sh").read_text())


if __name__ == "__main__":
    unittest.main()
