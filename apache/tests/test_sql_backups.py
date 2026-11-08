import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backups = self.root / "backup"
        self.backups.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands"
        for name in ("chown", "chmod", "sqlcmd"):
            path = self.bin / name
            path.write_text(
                '#!/bin/bash\nprintf "%s" "${0##*/}" >> "$LOG"\n'
                'printf " <%s>" "$@" >> "$LOG"\nprintf "\\n" >> "$LOG"\n'
                'if [[ ${0##*/} == sqlcmd ]]; then exit "$SQL_EXIT"; fi\n'
                'if [[ ${0##*/} == chown ]]; then exit "$CHOWN_EXIT"; fi\n')
            path.chmod(0o755)
        source = (ROOT / "sql/cron/backup_everything.sh").read_text()
        source = source.replace('[[ $EUID -eq 0 ]]', "true").replace(
            "/var/opt/mssql/backup", str(self.backups)).replace(
            "/opt/mssql-tools18/bin/sqlcmd", str(self.bin / "sqlcmd"))
        self.script = self.root / "backup.sh"
        self.script.write_text(source)

    def run_backup(self, sql_exit=0, chown_exit=0):
        return subprocess.run(
            ["/bin/bash", str(self.script)], capture_output=True, text=True, timeout=10,
            env=os.environ | {"PATH": str(self.bin) + ":" + os.environ["PATH"],
                              "LOG": str(self.log), "SQL_EXIT": str(sql_exit),
                              "CHOWN_EXIT": str(chown_exit)})

    def test_permissions_prepared_before_sql_and_other_files_untouched(self):
        for name in ("example.bak", "æøå.BAK", "leave.txt"):
            (self.backups / name).touch()
        (self.backups / "subfolder").mkdir()
        (self.backups / "subfolder/nested.bak").touch()
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        for name in ("example.bak", "æøå.BAK"):
            self.assertIn(f"chown <mssql:mssql> <{self.backups / name}>", log)
            self.assertIn(f"chmod <640> <{self.backups / name}>", log)
            self.assertLess(log.index(str(self.backups / name)), log.index("sqlcmd"))
        self.assertNotIn("leave.txt", log)
        self.assertNotIn("nested.bak", log)
        self.assertIn("sqlcmd <-b>", log)
        self.assertIn("Backup of everything done!", result.stdout)

    def test_sql_error_does_not_report_success(self):
        result = self.run_backup(sql_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Backup of everything done!", result.stdout)
        self.assertIn("SQL backup failed", result.stderr)

    def test_permission_error_stops_before_sql(self):
        result = self.run_backup(chown_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("sqlcmd", self.log.read_text())

    def test_symlink_is_rejected(self):
        outside = self.root / "outside.bak"
        outside.write_text("original")
        (self.backups / "link.bak").symlink_to(outside)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be symbolic links", result.stderr)
        self.assertNotIn("sqlcmd", self.log.read_text())
        self.assertEqual(outside.read_text(), "original")
