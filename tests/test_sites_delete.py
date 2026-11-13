"""harbor site delete against delete_site.sh (frozen in tests/legacy): the same files left and the same programs run"""
import os
import tempfile
import unittest

from tests.harness import APACHE, Server, harbor_commands, sent_today
from tests.test_core_bindings import NEEDS_BASH_4
from tests.test_environments import Fake, split_call
from harbor.cli import main

CONF = "<VirtualHost *:443>\n  ServerName {binding}\n  ServerAlias www.{binding}\n  SSLEngine On\n</VirtualHost>\n"
SERVICE = "[Service]\nExecStart=/usr/lib/dotnet/dotnet /var/www/{name}/App.dll --urls \"http://127.0.0.1:4000\"\n"


def make(server, environment="local", windows=False, legacy=False):
    """shop.dk with an environment: in its folder (local), on another server (remote) or on Windows. For the script
    (legacy), the server paths in its settings point into its root, as the script's own paths do."""
    root = server.root
    here = server.redirect if legacy else (lambda text: text)
    site = root / "var/www/shop.dk"
    (site / "scripts/targets/staging").mkdir(parents=True)
    (site / "_staging").mkdir()
    (site / "_staging/App.dll").write_text("build")
    (site / "App.dll").write_text("build")
    (site / "scripts/publish_local.sh").write_text("# local\n")
    (site / "scripts/clear_logs_local.sh").write_text("# clear\n")
    (site / ".vns-publish").mkdir()
    (site / ".vns-publish/target-staging.json").write_text("{}")
    for name, binding in (("shop.dk", "shop.dk"), ("staging-shop.dk", "staging.shop.dk")):
        (root / f"etc/apache2/sites-available/{name}.conf").write_text(CONF.format(binding=binding))
        (root / f"etc/apache2/sites-enabled/{name}.conf").symlink_to(f"../sites-available/{name}.conf")
        (root / f"etc/systemd/system/{name}.service").write_text(SERVICE.format(name=name))
        (root / f"var/lib/vns-roots/{name}").mkdir()
        (root / f"etc/ssl/certs/{binding}_ecc").mkdir()
        (root / f"etc/ssl/certs/{binding}_ecc/fullchain.cer").write_text("cert")
    if windows:
        text = "PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//10.0.0.9/D$/WEB/shop'\nPROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\n"
    elif environment == "local":
        text = here("PROD_LOCAL_PATH='/var/www/shop.dk/_staging'\nPROD_FOLDER='staging-shop.dk'\nPROD_BINDING='staging.shop.dk'\n")
    else:
        text = "PROD_IP='10.0.0.5'\nPROD_USER='root'\nPROD_PASSWORD=''\nPROD_SSH_PORT='22'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n"
    (site / "scripts/targets/staging/publish_prod.sh").write_text(text)


@NEEDS_BASH_4
class DeleteSiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.old = Server(folder.name, "old")
        self.new = Server(folder.name, "new")

    def compare(self, *args, target="", **make_args):
        make(self.old, legacy=True, **make_args)
        make(self.new, **make_args)
        env = {"VNS_TARGET": target} if target else {}
        result = self.old.run_legacy("delete_site.sh", *args, env=env)
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            system = self.new.system()
            code = main(["site", "delete", *args], system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(self.new.files(), self.old.files())
        self.assertEqual(harbor_commands(system), [line for line in self.old.commands() if not line.startswith("clear")])
        return result, system

    def test_everything_with_certificates(self):
        result, system = self.compare("shop.dk", "y", "all")
        self.assertFalse((self.new.root / "var/www/shop.dk").exists())
        self.assertIn("All is removed!", system.said)

    def test_the_site_itself_keeps_its_environment_folder(self):
        result, system = self.compare("shop.dk", "n", "test")
        self.assertTrue((self.new.root / "var/www/shop.dk/_staging/App.dll").exists())
        self.assertFalse((self.new.root / "var/www/shop.dk/App.dll").exists())

    def test_one_environment_and_the_scripts_go_back(self):
        result, system = self.compare("shop.dk", "n", "prod", target="staging")
        self.assertTrue((self.new.root / "var/www/shop.dk/scripts/publish.sh").exists())
        self.assertFalse((self.new.root / "var/www/shop.dk/scripts/targets/staging").exists())

    def test_windows_is_only_removed_here(self):
        result, system = self.compare("shop.dk", "n", "prod", target="staging", windows=True)
        self.assertIn("remove the IIS site and its folder at //10.0.0.9/D$/WEB/shop yourself", " ".join(system.said))

    def test_another_server_gets_the_same_script(self):
        stdin = os.path.join(self.folder, "stdin")
        for server in (self.old,):
            server.programs()
            (server.folder / "old-bin/ssh").write_text(f'#!/bin/bash\necho "ssh $*" >>"{server.log}"\ncat >"{stdin}"\n')
            (server.folder / "old-bin/ssh").chmod(0o755)
        result, system = self.compare("shop.dk", "y", "prod", target="staging", environment="remote")
        # The script's paths point into its root in the test; on a server they are the same as harbor's
        self.assertEqual(system.inputs[0], sent_today(open(stdin).read().replace(str(self.old.root), "")))

    def test_ubuntus_default_site(self):
        def html(server, legacy=False):
            here = server.redirect if legacy else (lambda text: text)
            (server.root / "var/www/html").mkdir()
            (server.root / "var/www/html/index.html").write_text("It works")
            (server.root / "etc/apache2/sites-available/000-default.conf").write_text(here("<VirtualHost *:80>\n  DocumentRoot /var/www/html\n</VirtualHost>\n"))
            (server.root / "etc/apache2/sites-enabled/000-default.conf").symlink_to("../sites-available/000-default.conf")
        html(self.old, legacy=True)
        html(self.new)
        result = self.old.run_legacy("delete_site.sh", "html", "n", "all")
        system = self.new.system()
        self.assertEqual(main(["site", "delete", "html", "n", "all"], system), result.returncode)
        self.assertEqual(self.new.files(), self.old.files())
        self.assertFalse((self.new.root / "etc/apache2/sites-available/000-default.conf").exists())


class WrapperTests(unittest.TestCase):
    def test_the_script_runs_harbor(self):
        self.assertIn('bin/harbor" site delete "$@"', (APACHE / "delete_site.sh").read_text())


if __name__ == "__main__":
    unittest.main()
