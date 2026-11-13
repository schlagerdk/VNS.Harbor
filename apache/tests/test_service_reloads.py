import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


APACHE = Path(__file__).resolve().parents[1]


def script_text(name):
    """A script as it was before it ran harbor (tests/legacy), else as it is: harbor is compared with it in tests/"""
    legacy = APACHE.parent / "tests/legacy" / name
    return (legacy if legacy.exists() else APACHE / name).read_text()


def run_shell(code):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + code],
                          capture_output=True, text=True)


class ApacheReloadTests(unittest.TestCase):
    def test_failed_configtest_stops_delete(self):
        delete = (APACHE.parent / "tests/legacy/delete_site.sh").read_text()  # as it was; harbor is compared with it in tests/
        blocks = [delete[delete.index('echo "Reload daemon"'):]]
        for block in blocks:
            result = run_shell("""
sudo() {
    echo "CALL:$*"
    if [[ $1 == apache2ctl ]]; then return 1; fi
}
""" + block)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("CALL:systemctl reload apache2", result.stdout)
            self.assertNotIn("CALL:systemctl start apache2", result.stdout)


class ApplicationReloadTests(unittest.TestCase):
    def test_https_settings_report_only_real_changes(self):
        source = (APACHE / "lib/common.sh").read_text()
        start = source.index("configure_umbraco_https() {")
        helper = source[start:source.index("\n}", start) + 2]
        for settings in ({}, {"Other": True}, {"Umbraco": {"CMS": {}}}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "appsettings.json"
                original = json.dumps(settings)
                path.write_text(original)
                code = ('sudo() { "$@"; }\n' + helper +
                        "\nconfigure_umbraco_https " + shlex.quote(str(path)) + " example.test y\n")
                first = run_shell(code)
                self.assertEqual(first.returncode, 0, first.stderr)
                updated = "Umbraco" in settings
                self.assertEqual(first.stdout.strip(), "changed" if updated else "")
                if not updated:
                    self.assertEqual(path.read_text(), original)
                before = path.read_bytes()
                second = run_shell(code)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stdout, "")
                self.assertEqual(path.read_bytes(), before)
                if updated:
                    cms = json.loads(path.read_text())["Umbraco"]["CMS"]
                    self.assertTrue(cms["Global"]["UseHttps"])
                    self.assertEqual(cms["WebRouting"]["UmbracoApplicationUrl"], "https://example.test")

    def test_certificate_action_restarts_only_after_settings_change(self):
        source = (APACHE.parent / "tests/legacy" / "test_action.sh").read_text()  # as it was; harbor is compared with it in tests/
        start = source.index("\t\t\t_settingsChanged=")
        block = source[start:source.index("\n\t\tfi", start)]
        for changed in (True, False):
            mock = """
_working=/fixture
_binding=example.test
_site=example.test
_serviceName=example.test
configure_umbraco_https() { OUTPUT; }
sudo() { echo "CALL:$*"; }
""".replace("OUTPUT", "echo changed" if changed else ":")
            result = run_shell(mock + block)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual("CALL:systemctl restart example.test" in result.stdout, changed)

    def test_binding_changes_restart_only_when_appsettings_changed(self):
        for remote in (False, True):
            source = script_text("change_binding_prod.sh" if remote else "change_binding.sh")
            marker = "if [[ -f ${backup}/appsettings.json ]]" if remote else "if [[ -f ${_backupDir}/appsettings.json ]]"
            start = source.rindex(marker)
            end_marker = "\n# 4." if remote else "\n# The site that answers any name has a second link to its config"
            block = source[start:source.index(end_marker, start)]
            for state in ("absent", "unchanged", "changed"):
                with self.subTest(remote=remote, state=state), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    backup = root / "backup"
                    backup.mkdir()
                    settings = root / "appsettings.json"
                    if state != "absent":
                        (backup / "appsettings.json").write_text("old")
                        settings.write_text("new" if state == "changed" else "old")
                    prefix = """
sudo() { "$@"; }
systemctl() { echo "CALL:$*"; }
rollback() { echo rollback; }
_rollback() { echo rollback; }
SITE=example.test
_site=example.test
_serviceName=example.test
unit=example.test
app_restart_attempted=n
_appRestartAttempted=n
"""
                    prefix += "\nbackup=" + shlex.quote(str(backup))
                    prefix += "\n_backupDir=" + shlex.quote(str(backup))
                    prefix += "\nsettings=" + shlex.quote(str(settings))
                    prefix += "\n_appSettings=" + shlex.quote(str(settings)) + "\n"
                    result = run_shell(prefix + block)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("CALL:restart example.test" in result.stdout, state == "changed")


if __name__ == "__main__":
    unittest.main()
