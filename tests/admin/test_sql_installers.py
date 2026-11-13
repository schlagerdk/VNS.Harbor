import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT = ROOT_DIR


def frozen(path):
    """A script in sql/ as it was before it ran harbor (tests/legacy/sql): harbor is compared with it in tests/test_sql.py"""
    copy = ROOT / "tests/legacy" / path.relative_to(ROOT)
    return copy if copy.is_file() else path



class SqlInstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sql = self.root / "sql"
        self.sql.mkdir()
        (self.sql / "lib").mkdir()
        for name in ("install_server.sh", "uninstall_server.sh", "uninstall_tools.sh"):
            shutil.copyfile(frozen(ROOT / "sql" / name), self.sql / name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands"
        self.env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "MOCK_LOG": str(self.log),
            "MOCK_PACKAGES": "mssql-server mssql-tools18",
            "MOCK_APT_FAIL": "0",
        }
        for name in ("clear", "id", "dpkg", "apt-get", "rm", "systemctl", "gpg"):
            path = self.bin / name
            path.write_text("""#!/bin/bash
name="${0##*/}"
echo "$name $*" >> "$MOCK_LOG"
case "$name" in
  id) echo 0 ;;
  dpkg) [[ " $MOCK_PACKAGES " == *" $2 "* ]] ;;
  apt-get) exit "$MOCK_APT_FAIL" ;;
esac
""")
            path.chmod(0o755)
        (self.sql / "lib/microsoft_repo.sh").write_text("install_microsoft_keys() { :; }\n")
        (self.sql / "lib/install_tools.sh").write_text(
            'install_tools() { echo install_tools >> "$MOCK_LOG"; }\n')
        (self.sql / "lib/sa_password.sh").write_text(
            'sa_prompt_and_apply() { echo sa_password >> "$MOCK_LOG"; }\n')
        (self.sql / "lib/backup_permissions.sh").write_text(
            'prepare_backup_permissions() { echo backup_permissions >> "$MOCK_LOG"; }\n')
        (self.sql / "placeholder.sh").write_text("# '@SA_PASSWORD@'\n")

    def run_script(self, name, answers, packages=None, apt_fail=False):
        env = self.env.copy()
        if packages is not None:
            env["MOCK_PACKAGES"] = packages
        env["MOCK_APT_FAIL"] = "1" if apt_fail else "0"
        wrapper = """
source() {
    if [[ "$1" == /etc/os-release ]]; then
        ID=ubuntu; VERSION_ID=24.04
    else
        builtin source "$@"
    fi
}
source "$1"
"""
        return subprocess.run(
            ["/bin/bash", "-c", wrapper, str(self.sql / name), str(self.sql / name)],
            input=answers, text=True, capture_output=True, env=env, timeout=10)

    def commands(self):
        return self.log.read_text()

    def test_install_decline_keeps_tools_optional_after_password(self):
        result = self.run_script("install_server.sh", "\n", packages="mssql-server")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sa_password", self.commands())
        self.assertNotIn("install_tools", self.commands())
        self.assertIn("SQL tools installation skipped", result.stdout)

    def test_install_accept_runs_tools_after_password(self):
        result = self.run_script("install_server.sh", "YES\n", packages="mssql-server")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands()
        self.assertLess(calls.index("sa_password"), calls.index("install_tools"))

    def test_install_accept_skips_existing_tools(self):
        result = self.run_script("install_server.sh", "y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("install_tools", self.commands())
        self.assertIn("skipping tools install", result.stdout)

    def test_server_uninstall_can_keep_tools_and_data(self):
        result = self.run_script("uninstall_server.sh", "y\n\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands()
        self.assertIn("apt-get remove --purge -y mssql-server\n", calls)
        self.assertNotIn("apt-get remove --purge -y mssql-tools", calls)
        self.assertNotIn("mssql-release.list", calls)
        self.assertNotIn("/etc/profile.d/mssql-tools.sh", calls)
        self.assertNotIn("rm -rf /var/opt/mssql", calls)
        self.assertIn("SQL tools kept", result.stdout)

    def test_server_uninstall_removes_tools_only_after_server_and_data_choice(self):
        result = self.run_script("uninstall_server.sh", "y\n\nYES\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands()
        self.assertLess(calls.index("apt-get remove --purge -y mssql-server"),
                        calls.index("apt-get remove --purge -y mssql-tools18"))
        self.assertLess(result.stdout.index("/var/opt/mssql kept"),
                        result.stdout.index("SQL tools removed"))
        self.assertNotIn("msodbcsql18", calls)
        self.assertNotIn("unixodbc-dev", calls)

    def test_standalone_uninstall_handles_both_tool_versions_without_server(self):
        result = self.run_script("uninstall_tools.sh", "y\n",
                                 packages="mssql-tools18 mssql-tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("apt-get remove --purge -y mssql-tools18 mssql-tools\n", self.commands())
        self.assertNotIn("mssql-server", self.commands())

    def test_tools_failure_is_reported_and_profile_is_preserved(self):
        result = self.run_script("uninstall_tools.sh", "y\n", apt_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("/etc/profile.d/mssql-tools.sh", self.commands())
        self.assertNotIn("SQL tools removed", result.stdout)

    def test_no_server_still_offers_tools_removal(self):
        result = self.run_script("uninstall_server.sh", "y\n", packages="mssql-tools18")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SQL tools removed", result.stdout)
        self.assertNotIn("rm -rf /var/opt/mssql", self.commands())

    def test_no_installed_tools_is_a_noop(self):
        result = self.run_script("uninstall_tools.sh", "", packages="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("apt-get", self.commands())


if __name__ == "__main__":
    unittest.main()
