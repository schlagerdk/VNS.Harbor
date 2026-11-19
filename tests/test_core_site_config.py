"""harbor.core.site_config against the functions it replaces in lib/common.sh: the same files and the same programs run.
The script ran them with sudo; harbor runs as root and does not."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core import site_config  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.core.templates import Site  # noqa: E402
from tests.test_core_bindings import COMMON, NEEDS_BASH_4  # noqa: E402

# The Harbor installation (what was the apache folder: lib/ and the templates)
APACHE = ROOT
LOGGED = ("apache2ctl", "a2ensite", "a2enmod", "systemctl")


class Server:
    """A fake server: /etc under a temporary folder, and the programs that change it recorded"""

    def __init__(self, folder, name):
        self.root = Path(folder) / name
        for path in ("etc/apache2/sites-available", "etc/apache2/sites-enabled", "etc/systemd/system", "etc/ssl/certs",
                     "etc/acmesh", "var/www/shop.dk/_publish/wwwroot", "bin"):
            (self.root / path).mkdir(parents=True)
        self.log = self.root / "commands.log"
        self.log.write_text("")
        for program in LOGGED:
            self.program(program, f'echo "{program} $*" >>"{self.log}"\nexit ${{FAIL_{program.upper().replace("2", "_")}:-0}}\n')
        self.program("acme.sh", f'echo "/etc/acmesh/acme.sh $*" >>"{self.log}"\n', folder="etc/acmesh")
        self.program("sudo", 'exec "$@"\n')
        # python3 runs as itself, but the SQL startup helper is only recorded
        self.program("python3", f'if [[ $1 == *sql_startup.py ]]; then echo "python3 $*" >>"{self.log}"; exit 0; fi\nexec {sys.executable} "$@"\n')

    def program(self, name, body, folder="bin"):
        path = self.root / folder / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def bash(self, snippet, *args, env=None):
        common = self.root / "common.sh"
        common.write_text(COMMON.read_text().replace("/etc/", f"{self.root}/etc/"))
        script = (f"source {shlex.quote(str(common))}\n_apacheDir={shlex.quote(str(APACHE))}\n"
                  f"_solutionFolder={shlex.quote(str(APACHE / 'templates/sites'))}\n_rootPath={self.root}/var/www\n{snippet}")
        result = subprocess.run(["bash", "-c", script, "bash", *map(str, args)], capture_output=True, text=True,
                                env=os.environ | {"PATH": f"{self.root}/bin:{os.environ['PATH']}"} | (env or {}))
        return result

    def files(self):
        """The server's files, with this fake server's folder written as / again"""
        return {str(path.relative_to(self.root)): (os.readlink(path) if path.is_symlink() else path.read_text().replace(str(self.root), ""))
                for path in sorted((self.root / "etc").rglob("*")) if (path.is_file() or path.is_symlink()) and path.name != "acme.sh"}

    def commands(self):
        return [line.replace(str(self.root), "") for line in self.log.read_text().splitlines()]


class SiteConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")
        self.system = RecordingSystem(root=self.new.root)
        self.site = Site("shop.dk", binding="shop.dk", project_name="Shop", port_http="4010", harbor_dir=str(APACHE),
                         doc_root="/var/www/shop.dk/_publish/wwwroot", root_path="/var/www")

    def same(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.new.files(), self.old.files())
        # harbor sets up the service's security settings (lib/harden_service.py), which the script did not
        self.assertEqual([line for line in self.system.commands if "harden_service.py" not in line], self.old.commands())

    def site_vars(self):
        return ('_site=shop.dk; _binding=shop.dk; _projectName=Shop; _portHttp=4010; set_site_vars; '
                f'_docRoot={self.old.root}/var/www/shop.dk/_publish/wwwroot; ')

    @NEEDS_BASH_4
    def test_apache_config_with_and_without_a_certificate(self):
        for certificate in ("y", "n"):
            for server in (self.old, self.new):
                for path in (server.root / "etc/apache2/sites-available").glob("*"):
                    path.unlink()
                server.log.write_text("")
            self.system.commands.clear()
            template = APACHE / "templates/sites/csharp/umbraco/apache.conf.tpl"
            result = self.old.bash(self.site_vars() + f'_createCertificate={certificate}; install_apache_config "$1"', template)
            site_config.install_apache_config(self.system, self.site, template, certificate=certificate == "y")
            self.same(result)

    def test_service_waits_for_sql_server_when_it_runs_dotnet(self):
        template = APACHE / "templates/sites/csharp/service.tpl"
        result = self.old.bash(self.site_vars() + 'install_service "$1"', template)
        site_config.install_service(self.system, self.site, template)
        self.same(result)
        self.assertIn("systemctl restart shop.dk", self.system.commands)
        self.assertTrue(any("sql_startup.py shop.dk" in line for line in self.system.commands))

    def test_https_config_from_an_http_one_and_kept_when_apache_refuses_it(self):
        http = ("<VirtualHost *:80>\n  ServerName shop.dk\n  ProxyRequests Off\n  RequestHeader set X-Forwarded-Proto \"http\"\n"
                "  ProxyPass / http://127.0.0.1:4010/\n  ErrorLog /var/log/apache2/shop.dk-80-error.log\n</VirtualHost>\n")
        for server in (self.old, self.new):
            (server.root / "etc/apache2/sites-available/shop.dk.conf").write_text(http)
        result = self.old.bash(self.site_vars() + "enable_https_config")
        site_config.enable_https_config(self.system, self.site, "shop.dk")
        self.same(result)
        https = (self.new.root / "etc/apache2/sites-available/shop.dk.conf").read_text()
        self.assertIn("<VirtualHost *:443>", https)
        # The HTTPS site logs as a new one does, not to the log of port 80
        self.assertIn("ErrorLog /var/log/apache2/shop.dk-443-error.log", https)
        self.assertNotIn("-80-error.log", https)
        # Twice changes nothing more
        self.system.commands.clear()
        site_config.enable_https_config(self.system, self.site, "shop.dk")
        self.assertEqual(self.system.commands, [])

    def test_umbraco_https_settings(self):
        settings = {"Umbraco": {"CMS": {"Global": {"Id": "x"}, "WebRouting": {"UmbracoApplicationUrl": "http://old.dk"}}}, "Other": [1, "æ"]}
        for secure in ("y", "n"):
            for server in (self.old, self.new):
                (server.root / "etc/appsettings.json").write_text(json.dumps(settings))
            result = self.old.bash('configure_umbraco_https "$1" shop.dk "$2"', self.old.root / "etc/appsettings.json", secure)
            changed = site_config.configure_umbraco_https(self.system, "/etc/appsettings.json", "shop.dk", secure == "y")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip() == "changed", changed)
            self.assertEqual(self.new.files(), self.old.files())

    def test_default_site(self):
        result = self.old.bash(self.site_vars() + "make_default_site && default_site")
        site_config.make_default_site(self.system, self.site)
        self.same(result)
        self.assertEqual(site_config.default_site(self.system), result.stdout.splitlines()[-1])
        other = Site("other.dk")
        with self.assertRaisesRegex(RuntimeError, "shop.dk already answers any name"):
            site_config.make_default_site(self.system, other)

    def test_certificate_for_the_binding_and_its_aliases(self):
        conf = "<VirtualHost *:80>\n  ServerName shop.dk\n  ServerAlias www.shop.dk Shop.dk old.shop.dk\n</VirtualHost>\n"
        for server in (self.old, self.new):
            (server.root / "etc/apache2/sites-available/shop.dk.conf").write_text(conf)
            # Left behind by a deleted site, and used by no enabled site
            (server.root / "etc/ssl/certs/old.shop.dk_ecc").mkdir()
            (server.root / "etc/ssl/certs/old.shop.dk_ecc/key").write_text("old")
            (server.root / "etc/acmesh/acme.sh").chmod(0o755)
        result = self.old.bash(self.site_vars() + "issue_certificate")
        site_config.issue_certificate(self.system, self.site, "shop.dk")
        self.same(result)
        self.assertFalse((self.new.root / "etc/ssl/certs/old.shop.dk_ecc").exists())
        self.assertIn("-d shop.dk -d www.shop.dk -d old.shop.dk", " ".join(self.system.commands))
        self.assertEqual(self.system.said, [line for line in result.stdout.splitlines()])

    def test_production_tls_config(self):
        conf = "<VirtualHost *:80>\n  ServerName Shop.dk\n  ServerAlias www.shop.dk WWW.shop.dk shop.dk # note\n</VirtualHost>\n"
        service = 'ExecStart=/usr/lib/dotnet/dotnet /var/www/shop/App.dll --urls "http://localhost:4100"\n'
        for umbraco in ("0", "1"):
            with tempfile.TemporaryDirectory() as folder:
                config = Path(folder) / "site.conf"
                config.write_text(conf)
                (Path(folder) / "site.service").write_text(service)
                result = self.old.bash('ensure_production_tls_config "$@"', config, Path(folder) / "site.service", "shop",
                                       "Shop.dk", "/var/www/shop", "/etc/ssl/certs/Shop.dk_ecc", "it@x.dk", umbraco)
                self.assertEqual(result.returncode, 0, result.stderr)
                ours = site_config.production_tls_config(conf, service, "shop", "Shop.dk", "/var/www/shop", "/etc/ssl/certs/Shop.dk_ecc",
                                                         "it@x.dk", umbraco == "1")
                self.assertEqual(ours, config.read_text(), umbraco)
                self.assertIsNone(site_config.production_tls_config(ours, service, "shop", "Shop.dk", "/var/www/shop", "x", "m", False))


if __name__ == "__main__":
    unittest.main()
