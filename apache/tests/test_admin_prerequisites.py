import os
from pathlib import Path
import subprocess
import tempfile
import unittest


APACHE = Path(__file__).resolve().parents[1]


class AdminPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.marker = self.root / "apache-initialized"
        (self.root / "lib").mkdir()
        for name in ("init.sh", "install_admin_site.sh"):
            text = (APACHE / name).read_text().replace(
                "/var/lib/vns/apache-initialized", str(self.marker))
            (self.root / name).write_text(text)
        (self.root / "lib/dotnet.sh").write_text("ensure_dotnet_channel() { :; }\n")
        # Reaching this helper proves the guard allowed installation to proceed.
        (self.root / "lib/common.sh").write_text('echo "Installer allowed"\nexit 0\n')
        self.env = os.environ | {"TEST_MARKER": str(self.marker)}

    def run_script(self, name, fail=False, answers="n\n"):
        wrapper = """
sudo() {
    if [[ "$1" == apt-get && "$2" == update && "$TEST_FAIL" == 1 ]]; then
        return 1
    fi
    if [[ "$1" == apt-get && "$2" == install ]]; then
        echo "APT_INSTALL:$*"
    fi
    if [[ "$1" == bash && "$#" == 1 ]]; then
        command bash
        return $?
    fi
    if [[ "$1" == touch ]]; then
        command touch "$TEST_MARKER"
    fi
    if [[ "$1" == */install_admin_site.sh ]]; then
        /bin/bash "$1"
    fi
}
clear() { :; }
curl() {
    if [[ "$*" == *https://rclone.org/install.sh* ]]; then
        printf 'echo RCLONE_INSTALLER_RAN\\n'
    fi
}
git() { :; }
rm() { :; }
mktemp() { printf '%s\\n' "$TEST_TMP"; }
dotnet() { :; }
source "$1"
"""
        return subprocess.run(
            ["/bin/bash", "-c", wrapper, str(self.root / name), str(self.root / name)],
            env=self.env | {"TEST_FAIL": str(int(fail)), "TEST_TMP": str(self.root)},
            input=answers, text=True, capture_output=True, timeout=10)

    def test_missing_marker_blocks_before_prompts_or_helpers(self):
        result = self.run_script("install_admin_site.sh", answers="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("initialization is required", result.stderr)
        self.assertIn(str(self.root / "init.sh"), result.stderr)
        self.assertNotIn("Installer allowed", result.stdout)
        self.assertNotIn("Enter admin", result.stdout + result.stderr)

    def test_completed_initialization_allows_standalone_installer(self):
        result = self.run_script("init.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APT_INSTALL:apt-get install -y curl unzip tar git ca-certificates uuid-runtime python3 python3-venv python3-pip cron",
                      result.stdout)
        self.assertIn("RCLONE_INSTALLER_RAN", result.stdout)
        self.assertTrue(self.marker.is_file())
        result = self.run_script("install_admin_site.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installer allowed", result.stdout)

    def test_failed_initialization_does_not_unlock_installer(self):
        result = self.run_script("init.sh", fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.run_script("install_admin_site.sh").returncode, 1)

    def test_init_offer_can_install_admin_after_marker_is_written(self):
        result = self.run_script("init.sh", answers="Y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installer allowed", result.stdout)
