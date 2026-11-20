"""harbor env sync against sync_env.sh (frozen in tests/legacy). rsync runs for real on both sides, so what lands in
the environments is compared, with the commands that put it there."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import Result  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_sites_adopt import Base  # noqa: E402

TEMPORARY = re.compile(r"/tmp/tmp[.\w]+")
REMOTE = "PROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='pw'\nPROD_FOLDER='shop-prod.dk'\nPROD_BINDING='shop.dk'\n"


@NEEDS_BASH_4
class SyncTests(Base):
    def setUp(self):
        super().setUp()
        (self.folder / "etc/systemd/system/shop.dk.service").write_text("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n")
        main = self.web / "shop.dk/_publish"
        for folder in ("uSync/v17/ContentTypes", "uSync/v17/DataTypes", "uSync/v17/Content", "uSync/v17/Media", "uSync/v17/Dictionary",
                       "uSync/Other/ContentTypes", "Views/Partials", "wwwroot/css", "wwwroot/media/1", "wwwroot/scripts"):
            (main / folder).mkdir(parents=True)
            (main / folder / "item.config").write_text(folder)
        (main / "uSync/v17/usync.config").write_text("<config/>")
        self.environment("staging", "PROD_LOCAL_PATH='/var/www/shop.dk/_staging'\nPROD_FOLDER='shop-staging.dk'\nPROD_BINDING='s.shop.dk'\n")
        staging = self.web / "shop.dk/_staging"
        for folder in ("uSync/v17/ContentTypes", "uSync/v17/Content", "Views"):
            (staging / folder).mkdir(parents=True)
            (staging / folder / "old.config").write_text("old " + folder)
        self.environment("production", REMOTE)
        # rsync runs, and is recorded; to the remote server it only says it worked, as the server answers
        self.fake.program("rsync", 'case "$*" in *@10.0.0.5:*) exit 0 ;; esac\nexec /usr/bin/rsync "$@"\n')

    def environment(self, target, text):
        folder = self.web / f"shop.dk/scripts/targets/{target}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "publish_prod.sh").write_text(text)

    def snapshot(self):
        return {str(path.relative_to(self.web)): path.read_text() for path in sorted((self.web / "shop.dk").rglob("*"))
                if path.is_file() and "/scripts/" not in f"/{path.relative_to(self.web)}"}

    def restore(self, files):
        shutil.rmtree(self.web / "shop.dk/_staging", ignore_errors=True)
        for name, content in files.items():
            (self.web / name).parent.mkdir(parents=True, exist_ok=True)
            (self.web / name).write_text(content)

    def commands(self, lines):
        return [TEMPORARY.sub("TMP", line).replace(str(self.folder), "") for line in lines]

    def compare(self, *args, answers=None):
        before = self.snapshot()
        result, _, _, _ = self.legacy("sync_env.sh", "shop.dk", *args)
        legacy_files = self.snapshot()
        legacy_calls = [line.rsplit(" |", 1)[0] for line in self.fake.calls() if line.startswith(("rsync ", "ssh "))]
        self.restore(before)

        def rsync(argv):
            if any("@10.0.0.5:" in arg for arg in argv):
                return Result(0)
            run = subprocess.run(["/usr/bin/rsync", *argv[1:]], capture_output=True, text=True)
            return Result(run.returncode, run.stdout, run.stderr)

        def python3(argv):
            run = subprocess.run(["python3", *argv[1:]], capture_output=True, text=True)
            return Result(run.returncode, run.stdout, run.stderr)
        code, system, _ = self.harbor(["env", "sync", "shop.dk", *args], answers={"rsync": rsync, "python3": python3, **(answers or {})})
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(self.snapshot(), legacy_files)
        recorded = [command[len("sshpass -e "):] if command.startswith("sshpass -e ") else command for command in system.commands
                    if command.startswith(("rsync ", "ssh ", "sshpass "))]
        # The fake programs wrote their arguments with printf %q
        self.assertEqual(self.commands(recorded), self.commands(" ".join(shlex.split(line)) for line in legacy_calls))
        if result.returncode:
            self.assertEqual(system.said[-1], result.stderr.strip().splitlines()[-1])
        return result, system

    def test_settings_from_the_main_site_to_every_environment(self):
        result, system = self.compare("main", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        staging = self.web / "shop.dk/_staging"
        self.assertTrue((staging / "uSync/v17/DataTypes/item.config").is_file())
        self.assertFalse((staging / "uSync/v17/ContentTypes/old.config").exists(), "settings are mirrored")
        self.assertTrue((staging / "uSync/v17/Content/old.config").is_file(), "content is never touched")
        self.assertFalse((staging / "uSync/v17/Content/item.config").exists())

    def test_a_dry_run_and_one_environment(self):
        result, system = self.compare("main", "staging", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.web / "shop.dk/_staging/uSync/v17/ContentTypes/old.config").is_file())

    def test_data_is_merged(self):
        result, system = self.compare("main", "staging", "--data", "content,media")
        self.assertEqual(result.returncode, 0, result.stderr)
        staging = self.web / "shop.dk/_staging"
        self.assertTrue((staging / "uSync/v17/Content/item.config").is_file())
        self.assertTrue((staging / "uSync/v17/Content/old.config").is_file())
        self.assertTrue((staging / "wwwroot/media/1/item.config").is_file())
        self.assertFalse((staging / "uSync/v17/DataTypes").exists())

    def test_from_an_environment_back_to_the_main_site(self):
        result, system = self.compare("staging", "main", "--data", "content")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.web / "shop.dk/_publish/uSync/v17/Content/old.config").is_file())

    def test_sync_setup_decides_who_may_receive(self):
        (self.web / "shop.dk/scripts/data_sync.json").write_text(json.dumps({"staging": {"settings_receive": False}}))
        result, system = self.compare("main", "staging")
        self.assertEqual(result.returncode, 1)
        result, system = self.compare("main", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("s.shop.dk (staging): skipped", "\n".join(system.said))

    def test_only_a_site_set_to_import_at_startup_is_restarted(self):
        (self.web / "shop.dk/scripts/data_sync.json").write_text(json.dumps({"staging": {"settings_import": True},
                                                                              "production": {"settings_import": False}}))
        def rsync(argv):
            if any("@10.0.0.5:" in arg for arg in argv):
                return Result(0)
            run = subprocess.run(["/usr/bin/rsync", *argv[1:]], capture_output=True, text=True)
            return Result(run.returncode, run.stdout, run.stderr)

        def python3(argv):
            run = subprocess.run(["python3", *argv[1:]], capture_output=True, text=True)
            return Result(run.returncode, run.stdout, run.stderr)
        restarted = []
        with mock.patch("harbor.environments.data.DataSync.apply_settings", lambda self, site_id, force=False: restarted.append((site_id, force))):
            code, system, _ = self.harbor(["env", "sync", "shop.dk", "main", "all"], answers={"rsync": rsync, "python3": python3})
            self.assertEqual(code, 0, system.said)
            self.assertEqual(restarted, [("staging", True)])
            # A dry run restarts nothing
            restarted.clear()
            code, system, _ = self.harbor(["env", "sync", "shop.dk", "main", "all", "--dry-run"], answers={"rsync": rsync, "python3": python3})
            self.assertEqual((code, restarted), (0, []))

    def test_what_is_refused(self):
        for args in (("main", "main"), ("nowhere", "all"), ("main", "nowhere"), ("main", "main,staging"), ("main", "all", "--data", "content"),
                     ("main", "staging", "--data", "settings")):
            with self.subTest(args=args):
                result, system = self.compare(*args)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
