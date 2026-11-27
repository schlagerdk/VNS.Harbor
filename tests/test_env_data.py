"""harbor env data-sync against data_sync.sh (frozen in tests/legacy), which ran sync_env.sh (frozen too). Files are
synced for real on both sides, to an environment on this server, and what they hold afterwards is compared."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import Result  # noqa: E402
from tests.test_actions import legacy_script  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_env_sync import SyncTests  # noqa: E402

SETTINGS = {"main": {"enabled": True, "send": True, "content": True, "media": True},
            "staging": {"enabled": True, "receive": True, "content": True, "media": True}}


@NEEDS_BASH_4
class DataSyncTests(SyncTests):
    # The settings sync's own tests run in test_env_sync
    for _name in [name for name in dir(SyncTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        for path in ("shop.dk/appsettings.json", "shop.dk/_publish/appsettings.json", "shop.dk/_staging/appsettings.json"):
            (self.web / path).write_text('{\n  "Umbraco": {}\n}\n')
        (self.web / "shop.dk/scripts/data_sync.json").write_text(json.dumps(SETTINGS))
        # The script ran sync_env.sh with bash: the frozen one, from the same fake server
        legacy_sync = self.folder / "legacy_sync_env.sh"
        legacy_sync.write_text(legacy_script("sync_env.sh", self.folder, self.web).replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', ""))
        self.fake.program("bash", f'if [[ $1 == sync_env.sh ]]; then shift; exec /bin/bash "{legacy_sync}" "$@"; fi\nexec /bin/bash "$@"\n')
        self.fake.program("systemctl", "")
        self.fake.program("chown", "")

    def real(self, program):
        def run(argv, stdin=None):
            # A path on the server, not one harbor already gave under the fake server's folder
            argv = [re.sub(r"(?<![\w.-])/var/www", str(self.web), arg) for arg in argv[1:]]
            # With the fake server's programs, as the script had them
            done = subprocess.run([program, *argv], capture_output=True, text=True, input=stdin, env=self.fake.env())
            return Result(done.returncode, done.stdout, done.stderr)
        return run

    def answers(self):
        return {"rsync": self.real("/usr/bin/rsync"), "python3": self.real("python3"),
                # As root here: the tests run as a user
                "sudo": lambda argv, stdin: self.real(argv[1])(argv[1:], stdin)}

    def sync_settings_first(self, destination="staging"):
        """Data only goes to a site whose settings are in sync: harbor syncs them, and keeps that in its history"""
        code, system, _ = self.harbor(["env", "sync", "shop.dk", "main", destination], answers=self.answers())
        self.assertEqual(code, 0, system.said)

    def compare(self, *args):
        before = self.snapshot()
        result, _, _, _ = self.legacy("data_sync.sh", "shop.dk", *args)
        legacy_files = self.snapshot()
        restarts = [line.rsplit(" |", 1)[0] for line in self.fake.calls() if line.startswith("systemctl ")]
        self.restore(before)
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", *args], answers=self.answers())
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        # A site without the VNS.Harbor package (as this one) imports at startup and is restarted, as the script did
        self.assertEqual(self.snapshot(), legacy_files)
        # Here or in an environment on this server (through sudo bash -c), as the fake systemctl saw them
        commands = [command[len("sudo bash -c "):] if command.startswith("sudo bash -c ") else command for command in system.commands]
        self.assertEqual([command for command in commands if command.startswith("systemctl ")], restarts)
        if result.returncode:
            self.assertEqual(system.said[-1].splitlines()[-1], result.stderr.strip().splitlines()[-1])
        return result, system

    def test_settings_are_set_in_appsettings(self):
        for site_id in ("main", "staging"):
            with self.subTest(site_id=site_id):
                result, system = self.compare("settings", site_id)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_data_to_an_environment(self):
        # The script synced the settings first: harbor sends only the data, to a site whose settings are in sync
        self.sync_settings_first()
        for option in ("--dry-run", ""):
            with self.subTest(option=option):
                result, system = self.compare("sync", "main", "staging", *([option] if option else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(line.startswith("Sync settings") for line in system.said), system.said)
        staging = self.web / "shop.dk/_staging"
        self.assertTrue((staging / "uSync/v17/Content/item.config").is_file())
        self.assertTrue((staging / "uSync/v17/DataTypes/item.config").is_file(), "the settings synced first")

    def test_data_is_imported_without_a_restart_where_the_package_can(self):
        """A data sync restarted its destination, so uSync imported at startup: the package imports it while it runs (1.0.92)"""
        self.sync_settings_first()
        with mock.patch("harbor.umbraco.backoffice.usync_import", return_value=True) as imported:
            code, system, _ = self.harbor(["env", "data-sync", "shop.dk", "sync", "main", "staging"], answers=self.answers())
        self.assertEqual(code, 0, system.said)
        self.assertEqual(imported.call_args.args[3], ("Settings", "Content"))
        self.assertFalse([command for command in system.commands if "systemctl restart" in command], "not restarted")

    def test_import_at_startup_goes_off_with_the_update_without_a_restart(self):
        """The update turns ImportAtStartup off on the sites with the VNS.Harbor package and their environments; nothing
        is restarted, and it is done once; a site without the package (Umbraco 13) is left importing at startup (1.0.92)"""
        (self.web / "shop.dk/scripts/data_sync.json").write_text(json.dumps({"staging": {"settings_import": True}}))
        (self.web / "shop.dk/Shop.csproj").write_text('<Project><ItemGroup><PackageReference Include="Umbraco.Cms" />'
                                                      '<PackageReference Include="VNS.Harbor" /></ItemGroup></Project>\n')
        staging = self.web / "shop.dk/_staging/appsettings.json"
        before = json.loads(staging.read_text())
        before.setdefault("uSync", {}).setdefault("Settings", {})["ImportAtStartup"] = "Settings"
        staging.write_text(json.dumps(before))
        code, system, _ = self.harbor(["env", "import-at-startup-off"], answers=self.answers())
        self.assertEqual(code, 0, system.said)
        self.assertEqual(json.loads(staging.read_text())["uSync"]["Settings"]["ImportAtStartup"], "None")
        self.assertFalse([command for command in system.commands if "systemctl" in command and "restart" in command], "not restarted")
        # The environment on another server is not reached here (its appsettings.json cannot be read): told, and
        # done again with the next update, as nothing is marked done
        self.assertIn("Not reached, so ImportAtStartup is left there until the next update: shop.dk (production)", system.said)
        self.assertFalse((self.folder / "var/lib/harbor/import-at-startup-off").exists())
        # A site without the package (Umbraco 13) keeps importing at startup: the update leaves it
        (self.web / "shop.dk/Shop.csproj").write_text('<Project><ItemGroup><PackageReference Include="Umbraco.Cms" /></ItemGroup></Project>\n')
        before["uSync"]["Settings"]["ImportAtStartup"] = "Settings"
        staging.write_text(json.dumps(before))
        code, system, _ = self.harbor(["env", "import-at-startup-off"], answers=self.answers())
        self.assertEqual(json.loads(staging.read_text())["uSync"]["Settings"]["ImportAtStartup"], "Settings")

    def test_no_data_to_a_site_whose_settings_are_behind(self):
        """Data merged into other document types gives problems there: Sync settings (or Sync everything) first"""
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", "sync", "main", "staging"], answers=self.answers())
        self.assertNotEqual(code, 0)
        self.assertTrue(any("have not been synced" in line and "Sync settings first" in line for line in system.said), system.said)
        self.assertFalse((self.web / "shop.dk/_staging/uSync/v17/Content/item.config").exists())
        # Synced, then a setting changed on the main site that no changed content uses: the data may go
        # (tests/test_sync_dependencies.py has the settings that do hold it back)
        self.sync_settings_first()
        (self.web / "shop.dk/_publish/uSync/v17/DataTypes/item.config").write_text("<changed/>")
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", "sync", "main", "staging"], answers=self.answers())
        self.assertEqual(code, 0, system.said)
        self.assertTrue((self.web / "shop.dk/_staging/uSync/v17/Content/item.config").exists())

    def test_sync_everything_sends_the_settings_first(self):
        """--settings-first (Sync everything): the settings where they are behind, then the data, in one task"""
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", "sync", "main", "staging", "--settings-first"], answers=self.answers())
        self.assertEqual(code, 0, system.said)
        staging = self.web / "shop.dk/_staging"
        self.assertTrue((staging / "uSync/v17/DataTypes/item.config").is_file())
        self.assertTrue((staging / "uSync/v17/Content/item.config").is_file())
        # In sync now: the next time only the data goes
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", "sync", "main", "staging", "--settings-first"], answers=self.answers())
        self.assertEqual(code, 0, system.said)
        self.assertFalse(any(line.startswith("Settings from") for line in system.said), system.said)

    def test_without_content_written_by_usync(self):
        for path in (self.web / "shop.dk/_publish/uSync/v17/Content").iterdir():
            path.unlink()
        result, system = self.compare("sync", "main", "staging")
        self.assertEqual(result.returncode, 1)

    def test_what_is_refused(self):
        for args in (("sync", "main", "nowhere"), ("sync", "nowhere", "staging"), ("sync", "staging", "main"), ("sync", "main", ""),
                     ("sync", "main", "staging", "--fast"), ("settings", "nowhere")):
            with self.subTest(args=args):
                result, system = self.compare(*args)
                self.assertNotEqual(result.returncode, 0)


del SyncTests


if __name__ == "__main__":
    unittest.main()
