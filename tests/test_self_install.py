"""harbor self: Harbor installed as releases in /opt/harbor, with current, rollback and the three newest kept (phase 3a)"""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.install import release  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.root = self.folder / "server"
        self.root.mkdir()

    def source(self, revision, version="1.2.0", dev=False):
        source = self.folder / f"source-{revision}"
        shutil.rmtree(source, ignore_errors=True)
        for part in ("bin", "harbor", "admin", "lib", "templates", "harbor/tests", "tests", "nuget"):
            (source / part).mkdir(parents=True)
        (source / "bin/harbor").write_text("#!/usr/bin/python3\n")
        (source / "bin/harbor").chmod(0o700)
        (source / "harbor/cli.py").write_text(f"# {revision}\n")
        (source / "lib/common.sh").write_text("")
        (source / "harbor/tests/test_x.py").write_text("")
        (source / "VERSION").write_text(version + "\n")
        (source / ".vns-version").write_text(revision * 40 + "\n" if len(revision) == 1 else revision + "\n")
        if dev:
            (source / ".vns-dev").write_text("dev\n")
        return source

    def harbor(self, *argv):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(list(argv), system), 0, system.said)
        return system

    def current(self):
        return os.readlink(self.root / "opt/harbor/current")

    def test_a_release_is_installed_and_used(self):
        self.harbor("self", "install", str(self.source("a")))
        name = "1.2.0-" + "a" * 12
        installed = self.root / "opt/harbor/releases" / name
        self.assertEqual(self.current(), f"releases/{name}")
        self.assertEqual(os.readlink(self.root / "usr/local/bin/harbor"), "/opt/harbor/current/bin/harbor")
        self.assertTrue((installed / "lib/common.sh").is_file())
        # Only what Harbor runs: no tests, no NuGet package
        self.assertFalse((installed / "harbor/tests").exists())
        self.assertFalse((installed / "tests").exists())
        self.assertFalse((installed / "nuget").exists())
        self.assertEqual(json.loads((installed / "release.json").read_text())["version"], "1.2.0")
        self.assertEqual(stat.S_IMODE((installed / "bin/harbor").stat().st_mode), 0o755)
        self.assertFalse(any(path.name.startswith(".") for path in (self.root / "opt/harbor/releases").iterdir()), "no half release")

    def test_the_same_commit_again_is_only_used(self):
        self.harbor("self", "install", str(self.source("a")))
        installed = self.root / "opt/harbor/releases" / ("1.2.0-" + "a" * 12)
        marker = installed / "harbor/cli.py"
        before = marker.stat().st_mtime_ns
        self.harbor("self", "install", str(self.source("a")))
        self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_a_working_copy_is_copied_again(self):
        self.harbor("self", "install", str(self.source("a", dev=True)))
        source = self.source("a", dev=True)
        (source / "harbor/cli.py").write_text("# changed\n")
        self.harbor("self", "install", str(source))
        self.assertEqual((self.root / "opt/harbor/releases" / ("1.2.0-" + "a" * 12 + "-dev") / "harbor/cli.py").read_text(), "# changed\n")

    def test_rollback_and_only_three_are_kept(self):
        for index, revision in enumerate("abcd"):
            self.harbor("self", "install", str(self.source(revision, version=f"1.2.{index}")))
            # Installed one after the other: the times tell them apart
            info = self.root / "opt/harbor/releases" / f"1.2.{index}-{revision * 12}" / "release.json"
            data = json.loads(info.read_text())
            data["installed"] = f"2026-11-13T10:00:0{index}+00:00"
            info.write_text(json.dumps(data))
        self.harbor("self", "install", str(self.source("d", version="1.2.3")))
        names = sorted(path.name for path in (self.root / "opt/harbor/releases").iterdir())
        self.assertEqual(names, ["1.2.1-" + "b" * 12, "1.2.2-" + "c" * 12, "1.2.3-" + "d" * 12])
        self.harbor("self", "rollback")
        self.assertEqual(self.current(), "releases/1.2.2-" + "c" * 12)
        said = self.harbor("self", "releases").said
        self.assertTrue(said[1].startswith("* 1.2.2-"), said)
        self.harbor("self", "rollback")
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["self", "rollback"], system), 1)
        self.assertIn("no earlier release", system.said[-1])

    def installed(self, *names):
        """Releases installed one after the other, the last in use: (name, repository)"""
        for index, (name, repository) in enumerate(names):
            folder = self.root / "opt/harbor/releases" / name
            (folder / "bin").mkdir(parents=True)
            (folder / "bin/harbor").write_text("")
            (folder / "REPOSITORY").write_text(repository + "\n")
            (folder / "release.json").write_text(json.dumps({"version": name.split("-")[0], "installed": f"2026-11-13T10:00:0{index}+00:00"}))
        (self.root / "opt/harbor/current").symlink_to(f"releases/{names[-1][0]}")

    def test_rollback_moves_the_admin_site_to_the_earlier_release(self):
        self.installed(("1.0.0-a", "schlagerdk/VNS.Harbor"), ("1.0.1-b", "schlagerdk/VNS.Harbor"))
        (self.root / "var/www/admin.example.test/app").mkdir(parents=True)
        (self.root / "var/www/admin.example.test/app/config.json").write_text('{"secret_key": "k"}')
        from unittest import mock
        with mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}):
            system = self.harbor("self", "rollback")
        self.assertEqual(self.current(), "releases/1.0.0-a")
        self.assertIn("python3 /opt/harbor/current/bin/harbor admin update admin.example.test", system.commands)

    def test_no_rollback_to_a_release_from_before_harbor(self):
        """test-01 had the old installation's release beside 1.0.0 and 1.0.1"""
        self.installed(("1.3.0-old", "someone/Other"), ("1.0.0-a", "schlagerdk/VNS.Harbor"))
        self.assertEqual(release.rollback_target(RecordingSystem(root=self.root))[0], None)
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["self", "rollback"], system), 1)
        self.assertIn("from before VNS Harbor", system.said[-1])
        self.assertEqual(self.current(), "releases/1.0.0-a")

    def test_not_a_copy_of_harbor(self):
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["self", "install", str(self.folder)], system), 1)
        self.assertIn("is not a copy of Harbor", system.said[-1])


class PathTests(unittest.TestCase):
    def test_from_a_release_paths_name_current(self):
        from harbor.core import paths
        self.assertEqual(paths.install_root(), paths.PACKAGE_ROOT, "outside /opt/harbor/releases the package's own folder")
        original = paths.PACKAGE_ROOT
        try:
            paths.PACKAGE_ROOT = Path("/opt/harbor/releases/1.2.0-abc")
            self.assertEqual(paths.install_root(), Path("/opt/harbor/current"))
        finally:
            paths.PACKAGE_ROOT = original


if __name__ == "__main__":
    unittest.main()
