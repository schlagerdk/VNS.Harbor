"""harbor env data-sync against data_sync.sh (frozen in tests/legacy), which ran sync_env.sh (frozen too). Files are
synced for real on both sides, to an environment on this server, and what they hold afterwards is compared."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

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

    def compare(self, *args):
        before = self.snapshot()
        result, _, _, _ = self.legacy("data_sync.sh", "shop.dk", *args)
        legacy_files = self.snapshot()
        restarts = [line.rsplit(" |", 1)[0] for line in self.fake.calls() if line.startswith("systemctl ")]
        self.restore(before)

        def real(program):
            def run(argv, stdin=None):
                # A path on the server, not one harbor already gave under the fake server's folder
                argv = [re.sub(r"(?<![\w.-])/var/www", str(self.web), arg) for arg in argv[1:]]
                # With the fake server's programs, as the script had them
                done = subprocess.run([program, *argv], capture_output=True, text=True, input=stdin, env=self.fake.env())
                return Result(done.returncode, done.stdout, done.stderr)
            return run
        code, system, _ = self.harbor(["env", "data-sync", "shop.dk", *args],
                                      answers={"rsync": real("/usr/bin/rsync"), "python3": real("python3"),
                                               # As root here: the tests run as a user
                                               "sudo": lambda argv, stdin: real(argv[1])(argv[1:], stdin)})
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
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
        for option in ("--dry-run", ""):
            with self.subTest(option=option):
                result, system = self.compare("sync", "main", "staging", *([option] if option else []))
                self.assertEqual(result.returncode, 0, result.stderr)
        staging = self.web / "shop.dk/_staging"
        self.assertTrue((staging / "uSync/v17/Content/item.config").is_file())
        self.assertTrue((staging / "uSync/v17/DataTypes/item.config").is_file(), "settings came first")

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
