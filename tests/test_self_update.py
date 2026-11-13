"""harbor self update: the newest release on GitHub, installed by its own code (cutover step 2c)"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.install import update  # noqa: E402

TAGS = "".join(f"{'a' * 40}\trefs/tags/v{version}\n" for version in ("1.0.0", "1.1.0", "0.9.0", "nightly"))


class NewerTests(unittest.TestCase):
    def test_another_repository_makes_any_release_newer(self):
        versions = ["1.1.0", "1.0.0"]
        self.assertEqual(update.newer(versions, "1.3.0", "schlagerdk/another"), ["1.1.0", "1.0.0"], "Harbor's own versions start again")
        self.assertEqual(update.newer(versions, "1.0.0", update.REPOSITORY), ["1.1.0"])
        self.assertEqual(update.newer(versions, "1.1.0", update.REPOSITORY), [])

    def test_the_installed_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(update.installed_repository(folder), update.OTHER, "the scripts before Harbor had no REPOSITORY")
            Path(folder, "REPOSITORY").write_text("schlagerdk/VNS.Harbor\n")
            self.assertEqual(update.installed_repository(folder), "schlagerdk/VNS.Harbor")
        self.assertEqual((ROOT / "REPOSITORY").read_text().strip(), "schlagerdk/VNS.Harbor", "this code's own")


class UpdateTests(unittest.TestCase):
    def fetched(self, finishes=True):
        def clone(argv):
            source = Path(argv[-1])
            (source / "bin").mkdir(parents=True)
            (source / "bin/harbor").write_text("")
            if finishes:
                (source / "harbor/install").mkdir(parents=True)
                (source / "harbor/install/update.py").write_text("")
            (source / "update.sh").write_text("")
            return ""
        return clone

    def test_the_newest_release_installs_itself(self):
        system = RecordingSystem(root=Path("/"), answers={"git ls-remote": TAGS, "git clone": self.fetched(), "git -C": "b" * 40})
        with mock.patch.object(update, "installed_repository", return_value="schlagerdk/another"):
            self.assertEqual(main(["self", "update"], system), 0, system.said)
        clone = next(command for command in system.commands if command.startswith("git clone"))
        self.assertIn("--branch v1.1.0 https://github.com/schlagerdk/VNS.Harbor.git", clone)
        finish = system.commands[-1]
        self.assertRegex(finish, r"^python3 \S*harbor-update-\S*/source/bin/harbor self finish-update \S*harbor-update-\S*/source$")

    def test_nothing_to_update_to(self):
        system = RecordingSystem(root=Path("/"), answers={"git ls-remote": ""})
        self.assertEqual(main(["self", "update"], system), 1)
        self.assertIn("no release to update to yet", system.said[-1])
        self.assertFalse(any(command.startswith("git clone") for command in system.commands), "never a branch")
        system = RecordingSystem(root=Path("/"), answers={"git ls-remote": TAGS})
        with mock.patch.object(update, "installed_repository", return_value=update.REPOSITORY), \
                mock.patch.object(update, "installed_version", return_value="1.1.0"):
            self.assertEqual(main(["self", "update"], system), 1)
        self.assertIn("up to date", system.said[-1])
        self.assertEqual(main(["self", "update", "--ref", "main"], RecordingSystem(root=Path("/"))), 1)



class FinishTests(unittest.TestCase):
    def test_the_release_is_installed_then_finishes_from_where_it_is_installed(self):
        from harbor.install import release
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(release, "install") as install:
            system = RecordingSystem(root=Path("/"))
            update.finish_update(system, folder)
            install.assert_called_once_with(system, folder)
            # Never from the fetched copy, which is removed: what it writes down names /opt/harbor/current
            self.assertEqual(system.commands, ["python3 /opt/harbor/current/bin/harbor self after-update"])
            system = RecordingSystem(root=Path("/"), answers={"python3 /opt/harbor/current/bin/harbor": Result(7)})
            with self.assertRaises(Exception) as stopped:
                update.finish_update(system, folder)
            self.assertEqual(stopped.exception.code, 7)


class AfterUpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "vns/bin").mkdir(parents=True)
        (self.root / "vns/bin/harbor").write_text("#!/usr/bin/python3\n")
        admin = self.root / "var/www/admin.example.test/app"
        admin.mkdir(parents=True)
        (admin / "config.json").write_text('{"secret_key": "x"}')
        (self.root / "var/www/shop.dk").mkdir()

    def after_update(self, answers=None):
        from harbor.install import admin
        system = RecordingSystem(root=self.root, answers=answers or {})
        with mock.patch.dict("os.environ", {"VNS_WEB_ROOT": "/var/www"}), mock.patch.object(admin, "update") as moved:
            update.after_update(system)
        return system, moved

    def test_every_step_and_the_admin_moved_to_the_release(self):
        system, moved = self.after_update()
        harbor = [command.split("bin/harbor ", 1)[1] for command in system.commands if "bin/harbor " in command]
        self.assertEqual(harbor, ["server set-servername", "server install-tls-policy", "server install-apache-conf", "state sync", "migrate",
                                  "sql import-sa-password", "server cron upgrade"])
        moved.assert_called_once_with(system, "admin.example.test")
        # Nothing names /vns any more: backed up, then removed (phase 7)
        self.assertTrue(any(command.startswith("tar -czf /var/lib/harbor/backups/vns-") and command.endswith(" -C / vns")
                            for command in system.commands), system.commands)
        self.assertFalse((self.root / "vns").exists())

    def test_vns_stays_while_the_update_was_started_from_it(self):
        """The admin of VNS 1.x runs /vns/bin/harbor self update, which waits for the release to finish: removing /vns
        under it ended that process with ModuleNotFoundError (sql-01)"""
        with mock.patch.object(update, "started_from_vns", return_value=True):
            system, _ = self.after_update()
        self.assertTrue((self.root / "vns").is_dir())
        self.assertTrue(any("goes at the next update" in line for line in system.said), system.said)

    def test_vns_stays_while_something_names_it(self):
        (self.root / "etc/cron.d").mkdir(parents=True)
        (self.root / "etc/cron.d/custom").write_text("0 3 * * * root /vns/sql/cron/delete_old_2dm_data.sh\n")
        system, _ = self.after_update()
        self.assertTrue((self.root / "vns").is_dir())
        self.assertTrue(any("/vns stays: it is still named in /etc/cron.d/custom" in line for line in system.said), system.said)
        # Its harbor is the one in use meanwhile
        self.assertEqual(os.readlink(self.root / "vns/bin/harbor"), "/usr/local/bin/harbor")
        self.assertFalse(any(command.startswith("tar ") for command in system.commands))

    def test_vns_stays_while_it_holds_the_sa_password_harbor_lacks(self):
        (self.root / "vns/sql").mkdir()
        (self.root / "vns/sql/create_db.sh").write_text("sa -U sa -P 's3cret' -i scripts/create_db.sql\n")
        system, _ = self.after_update({"python3": Result(0), "crontab -l": Result(1)})
        self.assertTrue((self.root / "vns").is_dir())
        self.assertTrue(any("sa password" in line for line in system.said), system.said)

    def test_other_paths_with_vns_in_them_are_not_vns(self):
        (self.root / "etc/systemd/system").mkdir(parents=True)
        (self.root / "etc/systemd/system/shop.dk.service").write_text("RootDirectory=/var/lib/vns-roots/shop.dk\nBindPaths=/var/lib/vns/x\n")
        (self.root / "etc/apache2/conf-available").mkdir(parents=True)
        (self.root / "etc/apache2/conf-available/vns-offline.conf").write_text("Alias /vns-offline/ /var/lib/vns/offline/\nErrorDocument 503 /vns-offline/x\n")
        # A comment runs nothing (the services Harbor made said "not /root, /home, /vns or other sites")
        (self.root / "etc/systemd/system/blog.dk.service").write_text("# Protect: not /root, /home, /vns or other sites\nExecStart=/usr/bin/x\n")
        system, _ = self.after_update()
        self.assertFalse((self.root / "vns").exists())

    def test_a_failed_step_is_told_and_the_rest_runs(self):
        system, moved = self.after_update({"python3": lambda argv: Result(1 if "migrate" in argv else 0)})
        self.assertTrue(any("harbor migrate --dry-run" in line for line in system.said), system.said)
        self.assertTrue(any(command.endswith("server cron upgrade") for command in system.commands))
        moved.assert_called_once()
        # /vns stays: the update did not work in full
        self.assertTrue((self.root / "vns").is_dir())
        self.assertTrue(any("stays until an update has worked in full" in line for line in system.said), system.said)


if __name__ == "__main__":
    unittest.main()
