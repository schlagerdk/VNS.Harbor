"""harbor self update: the newest release on GitHub, installed by its own code (cutover step 2c)"""
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
        self.assertEqual(update.newer(versions, "1.3.0", "schlagerdk/VNS.Linux"), ["1.1.0", "1.0.0"], "Harbor's own versions start again")
        self.assertEqual(update.newer(versions, "1.0.0", update.REPOSITORY), ["1.1.0"])
        self.assertEqual(update.newer(versions, "1.1.0", update.REPOSITORY), [])

    def test_the_installed_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(update.installed_repository(folder), "schlagerdk/VNS.Linux", "VNS 1.x had no REPOSITORY")
            Path(folder, "REPOSITORY").write_text("schlagerdk/VNS.Harbor\n")
            self.assertEqual(update.installed_repository(folder), "schlagerdk/VNS.Harbor")
        self.assertEqual((ROOT / "REPOSITORY").read_text().strip(), "schlagerdk/VNS.Linux", "this code's own")


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
        with mock.patch.object(update, "installed_repository", return_value="schlagerdk/VNS.Linux"):
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

    def test_an_older_release_is_installed_as_its_update_sh_did(self):
        system = RecordingSystem(root=Path("/"), answers={"git clone": self.fetched(finishes=False), "git -C": "b" * 40})
        with mock.patch.object(update, "admin_site", return_value="admin.example.test"):
            self.assertEqual(main(["self", "update", "--ref", "v1.2.2"], system), 0, system.said)
        self.assertTrue(any(command.startswith("bash ") and "harbor-update-" in command and command.endswith("/source") for command in system.commands))
        self.assertEqual(system.commands[-1], "bash /vns/apache/update_admin_site.sh admin.example.test")


class FinishTests(unittest.TestCase):
    def test_update_sh_then_the_admin_and_a_failure_stops_it(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / "update.sh").write_text("")
            system = RecordingSystem(root=Path("/"))
            with mock.patch.object(update, "admin_site", return_value="admin.example.test"):
                update.finish_update(system, source)
            self.assertEqual(system.commands, [f"bash {source}/update.sh {source}", "bash /vns/apache/update_admin_site.sh admin.example.test"])
            system = RecordingSystem(root=Path("/"), answers={f"bash {source}/update.sh": Result(7)})
            with mock.patch.object(update, "admin_site", return_value="admin.example.test"), self.assertRaises(Exception):
                update.finish_update(system, source)
            self.assertEqual(system.commands, [f"bash {source}/update.sh {source}"], "the admin is not updated after a failure")


if __name__ == "__main__":
    unittest.main()
