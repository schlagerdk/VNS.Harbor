"""harbor migrate (phase 6): where each file in a site's scripts folder goes, and whether a site is ready"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.state import migrate  # noqa: E402

SETTINGS = "PROD_LOCAL_PATH='/var/www/shop.dk/_test'\nPROD_FOLDER='test.shop.dk'\nPROD_BINDING='test.shop.dk'\nPROD_HTTPS=n\n"


class MigratePlanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        scripts = self.root / "var/www/shop.dk/scripts"
        (scripts / "targets/test").mkdir(parents=True)
        (scripts / "targets/test/publish_prod.sh").write_text(SETTINGS)
        (scripts / "data_sync.json").write_text('{"main": {"send": true}}')
        # The wrapper of today, an old rendered version, one changed by hand, and the site's own
        (scripts / "change_env.sh").write_text('#!/bin/bash\nexec "/vns/bin/harbor" site action shop.dk change_env "$@"\n')
        history = json.loads((ROOT / "harbor/state/script_history.json").read_text())
        old = next(text for text in history["issue_certificat.sh"] if "/bin/harbor" not in text and "@SITE@" in text)
        (scripts / "issue_certificat.sh").write_text(old.replace("@APACHE_DIR@", "/vns/apache").replace("@SITE@", "shop.dk"))
        (scripts / "outdated.sh").write_text("#!/bin/bash\necho my own outdated\n")
        (scripts / "hello_world.sh").write_text("echo Hello world!\n")
        publish = self.root / "var/www/shop.dk/.vns-publish"
        publish.mkdir()
        (publish / "test.json").write_text('{"status": "ok"}')
        self.system = RecordingSystem(root=self.root)

    def harbor(self, *argv):
        self.system = RecordingSystem(root=self.root)
        return main(list(argv), self.system)

    def test_each_file_has_its_place(self):
        self.assertEqual(self.harbor("state", "sync", "shop.dk"), 0)
        plan = migrate.plan(self.system, "shop.dk")
        self.assertTrue(plan["ready"], plan["problems"])
        self.assertEqual(plan["environments"], [{"id": "test", "in_copy": True, "same": True}])
        self.assertEqual(sorted(plan["backup"]), [".vns-publish/", "scripts/change_env.sh", "scripts/data_sync.json",
                                                  "scripts/issue_certificat.sh", "scripts/targets/test/publish_prod.sh"])
        self.assertEqual(plan["changed"], ["scripts/outdated.sh"])
        self.assertEqual(plan["own"], ["scripts/hello_world.sh"])

    def test_migrate_and_back(self):
        folder = self.root / "var/www/shop.dk"
        (folder / "scripts/publish_local.sh").write_text('#!/bin/bash\nexec "/vns/bin/harbor" site publish shop.dk "${1:-Release}" --folder _out\n')
        before = {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(self.harbor("migrate", "shop.dk"), 0, self.system.said)
        # Only the site's own script is left in its scripts folder
        self.assertEqual(sorted(str(path.relative_to(folder)) for path in (folder / "scripts").rglob("*")), ["scripts/hello_world.sh"])
        self.assertFalse((folder / ".vns-publish").exists())
        state = self.root / "var/lib/harbor/sites/shop.dk"
        record = json.loads((state / "site.json").read_text())
        self.assertEqual((record["configuration"], record["folder"]), ("Release", "_out"))
        self.assertEqual(record["targets"], {"test": ["publish_prod.sh"]})
        self.assertIn("publish_local.sh", record["scripts"])
        marker = json.loads((state / "migrated.json").read_text())
        self.assertEqual(marker["changed"], ["scripts/outdated.sh"])
        backup = self.root / marker["backup"].lstrip("/")
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        # Harbor's copy is the truth now: a sync leaves it, the settings are still read
        self.harbor("state", "sync", "shop.dk")
        from harbor.environments import settings
        self.assertEqual(settings.values(self.system, "/var/www/shop.dk", "test")["BINDING"], "test.shop.dk")
        self.assertEqual(self.harbor("migrate", "shop.dk"), 0)
        self.assertIn("shop.dk is migrated already.", self.system.said)
        # And back: the folder as it was
        self.assertEqual(self.harbor("migrate", "--rollback", "shop.dk"), 0, self.system.said)
        after = {path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((state / "migrated.json").exists())

    def test_not_ready_while_harbors_copy_differs(self):
        self.harbor("state", "sync", "shop.dk")
        (self.root / "var/www/shop.dk/scripts/targets/test/publish_prod.sh").write_text(SETTINGS.replace("PROD_HTTPS=n", "PROD_HTTPS=y"))
        plan = migrate.plan(self.system, "shop.dk")
        self.assertFalse(plan["ready"])
        self.assertIn("test: Harbor's copy differs", plan["problems"][0])
        self.assertEqual(self.harbor("migrate", "--dry-run", "shop.dk"), 2)
        self.assertIn("shop.dk: NOT ready", self.system.said)

    def test_a_renamed_publish_script_is_harbors(self):
        history = json.loads((ROOT / "harbor/state/script_history.json").read_text())
        old = next(text for text in history["publish.sh"] if "/bin/harbor" not in text)
        path = self.root / "var/www/shop.dk/scripts/publish_local.sh"
        path.write_text(old.replace("@SITE@", "shop.dk").replace("@PUBLISH_FOLDER@", "_publish").replace("@APACHE_DIR@", "/vns/apache"))
        self.assertTrue(migrate.harbors(path))
        self.assertIn("publish_local.sh", migrate.system_script_names())



class SettingsWriteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.script = self.root / "var/www/shop.dk/scripts/targets/test/publish_prod.sh"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/bin/bash\n" + SETTINGS + "PROD_PASSWORD='old'\n\necho the rest\n")
        self.system = RecordingSystem(root=self.root)

    def test_in_the_script_while_it_is_there(self):
        from harbor.environments import settings
        settings.update(self.system, "/var/www/shop.dk", "test", {"HTTPS": "y", "SSH_PORT": "2222"})
        text = self.script.read_text()
        self.assertIn("PROD_HTTPS='y'\n", text)
        self.assertIn("PROD_PASSWORD='old'\nPROD_SSH_PORT='2222'\n", text)
        self.assertTrue(text.endswith("echo the rest\n"))
        self.assertEqual(settings.values(self.system, "/var/www/shop.dk", "test")["SSH_PORT"], "2222")

    def test_in_harbors_copy_once_migrated(self):
        from harbor.environments import settings
        main(["state", "sync", "shop.dk"], RecordingSystem(root=self.root))
        (self.root / "var/lib/harbor/sites/shop.dk/migrated.json").write_text("{}")
        self.script.unlink()
        settings.update(self.system, "/var/www/shop.dk", "test", {"HTTPS": "y", "PASSWORD": "new pass"})
        copy = json.loads((self.root / "var/lib/harbor/sites/shop.dk/environments/test.json").read_text())
        self.assertEqual(copy["HTTPS"], "y")
        self.assertNotIn("PASSWORD", copy)
        self.assertEqual(json.loads((self.root / "etc/harbor/secrets/sites/shop.dk/test.json").read_text())["PASSWORD"], "new pass")
        # The sync leaves a migrated site's copy alone: there is nothing in its folder to make it from
        main(["state", "sync", "shop.dk"], RecordingSystem(root=self.root))
        self.assertEqual(settings.target_ids(self.system, "/var/www/shop.dk"), ["test"])
        self.assertTrue(settings.migrated(self.system, "/var/www/shop.dk"))
        settings.remove(self.system, "/var/www/shop.dk", "test")
        self.assertEqual(settings.target_ids(self.system, "/var/www/shop.dk"), [])



class MigratedCreateTests(unittest.TestCase):
    """A new environment of a migrated site: its settings in Harbor's copy, nothing in the site folder"""

    def test_a_remote_environment(self):
        from unittest import mock
        from harbor.environments.create import Main, create_environment
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "var/www/shop.dk").mkdir(parents=True)
        state = root / "var/lib/harbor/sites/shop.dk"
        (state / "environments").mkdir(parents=True)
        (state / "migrated.json").write_text("{}")
        (state / "environments/test.json").write_text(json.dumps({"LOCAL_PATH": "/var/www/shop.dk/_test", "FOLDER": "test.shop.dk",
                                                                  "BINDING": "test.shop.dk", "HTTPS": "n"}))
        system = RecordingSystem(root=root, answers={"sshpass": "", "ssh": ""})
        dialog = {"VNS_PROD_MODAL": "1", "VNS_PROD_TARGET": "remote", "VNS_PROD_IP": "10.0.0.9", "VNS_PROD_USER": "root",
                  "VNS_PROD_PASSWORD": "pw 1", "VNS_PROD_FOLDER": "shop.dk", "VNS_PROD_BINDING": "shop.dk",
                  "VNS_PROD_ENVIRONMENT": "Production", "VNS_PROD_HTTPS": "y"}
        main_site = Main("shop.dk", "/var/www/shop.dk", "shop.dk", "Shop", "_publish", "admin@shop.dk", "umbraco")
        with mock.patch.dict("os.environ", dialog):
            create_environment(system, main_site)
        self.assertFalse((root / "var/www/shop.dk/scripts").exists(), "nothing in the site folder")
        copy = json.loads((state / "environments/production.json").read_text())
        self.assertEqual((copy["IP"], copy["FOLDER"], copy["HTTPS"], copy["ENVIRONMENT"]), ("10.0.0.9", "shop.dk", "y", "Production"))
        self.assertNotIn("PASSWORD", copy)
        self.assertEqual(json.loads((root / "etc/harbor/secrets/sites/shop.dk/production.json").read_text())["PASSWORD"], "pw 1")
        publish = next(index for index, command in enumerate(system.commands) if command.endswith("env publish shop.dk"))
        self.assertEqual(system.environments[publish], {"VNS_TARGET": "production"})
        self.assertTrue(any(command.endswith("env sync shop.dk main production") for command in system.commands))



class MigratedMoveTests(unittest.TestCase):
    def test_the_state_arrives_under_the_new_name(self):
        import stat
        from harbor.sites.move import receive_state
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        incoming = root / "incoming"
        (incoming / "harbor-state/environments").mkdir(parents=True)
        (incoming / "harbor-state/migrated.json").write_text("{}")
        (incoming / "harbor-state/environments/test.json").write_text(json.dumps({"LOCAL_PATH": "/var/www/shop.dk/_test", "FOLDER": "test.shop.dk"}))
        (incoming / "harbor-secrets").mkdir()
        (incoming / "harbor-secrets/test.json").write_text('{"PASSWORD": "x"}')
        (incoming / "harbor-site").write_text("shop.dk\nnew-shop.dk\n")
        system = RecordingSystem(root=root)
        receive_state(system, incoming, "/var/www")
        state = root / "var/lib/harbor/sites/new-shop.dk"
        self.assertTrue((state / "migrated.json").is_file())
        self.assertEqual(json.loads((state / "environments/test.json").read_text())["LOCAL_PATH"], "/var/www/new-shop.dk/_test")
        secret = root / "etc/harbor/secrets/sites/new-shop.dk/test.json"
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
        with self.assertRaises(Exception):
            receive_state(system, incoming, "/var/www")

    def test_a_folder_that_came_closed_is_opened_for_www_data(self):
        from harbor.sites.move import reopen_folder
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        site = root / "var/www/shop.dk"
        site.mkdir(parents=True)
        site.chmod(0o1770)
        reopen_folder(RecordingSystem(root=root), "/var/www/shop.dk")
        self.assertEqual(oct(site.stat().st_mode & 0o7777), "0o1775")



class BornMigratedTests(unittest.TestCase):
    def test_a_new_site_on_a_migrated_server_has_no_scripts(self):
        from harbor.core.templates import Site
        from harbor.sites.create import SOLUTION, install_scripts
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "var/www/new.dk").mkdir(parents=True)
        system = RecordingSystem(root=root)
        site = Site("new.dk")
        site.build_config = "Release"
        install_scripts(system, site, SOLUTION / "common/scripts")
        self.assertTrue((root / "var/www/new.dk/scripts").is_dir(), "before the server is migrated: as always")
        (root / "var/www/new.dk/scripts").rename(root / "old-scripts")
        (root / "var/lib/harbor").mkdir(parents=True)
        (root / "var/lib/harbor/migrated.json").write_text("{}")
        install_scripts(system, site, SOLUTION / "common/scripts")
        install_scripts(system, site, SOLUTION / "csharp/scripts")
        self.assertFalse((root / "var/www/new.dk/scripts").exists())
        record = json.loads((root / "var/lib/harbor/sites/new.dk/site.json").read_text())
        self.assertIn("edit_conf.sh", record["scripts"])
        self.assertIn("publish.sh", record["scripts"])
        self.assertEqual(record["configuration"], "Release")
        self.assertTrue((root / "var/lib/harbor/sites/new.dk/migrated.json").is_file())


if __name__ == "__main__":
    unittest.main()
