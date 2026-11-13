import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch


ROOT_DIR = Path(__file__).resolve().parents[2]
LIB = ROOT_DIR / "lib"
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("sql_ready", LIB / "sql_ready.py")
ready = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ready)
sys.path.pop(0)


class SqlReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "appsettings.json").write_text(json.dumps({
            "ConnectionStrings": {"db": "Server=.;Database=site;User ID=siteuser;Password=private;TrustServerCertificate=true"}
        }))

    def test_probe_uses_site_database_and_password_only_in_environment(self):
        commands = ready.probes(self.root, "Test", {}, "sqlcmd")
        self.assertEqual(len(commands), 1)
        command, env = commands[0]
        self.assertEqual(command[command.index("-S") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("-d") + 1], "site")
        self.assertEqual(command[command.index("-U") + 1], "siteuser")
        self.assertNotIn("private", " ".join(command))
        self.assertEqual(env["SQLCMDPASSWORD"], "private")
        self.assertIn("-C", command)
        self.assertIn("SET NOCOUNT ON; SELECT 1", command)

    def test_environment_override_and_tcp_port(self):
        variables = {"ConnectionStrings__db": "Server=tcp:.,1444;Database=override;UID=user;PWD=secret"}
        command, _ = ready.probes(self.root, "Test", variables, "sqlcmd")[0]
        self.assertIn("tcp:127.0.0.1,1444", command)
        self.assertIn("override", command)
        self.assertNotIn("-C", command)

    def test_all_local_databases_are_checked(self):
        variables = {"ConnectionStrings__second": "Server=localhost;Database=second;UID=user;PWD=secret"}
        self.assertEqual(len(ready.probes(self.root, "", variables, "sqlcmd")), 2)

    def test_failed_connection_retried_until_real_query_succeeds(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        commands = ready.probes(self.root, "", {}, "sqlcmd")
        with patch.object(ready.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(ready.time, "sleep", side_effect=sleep), \
                patch.object(ready.subprocess, "run", side_effect=[
                    Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="1\n")]) as run:
            ready.wait_ready(commands)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(clock[0], 5)

    def test_exact_120_second_limit_without_logging_sql_output(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        output = io.StringIO()
        commands = ready.probes(self.root, "", {}, "sqlcmd")
        with patch.object(ready.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(ready.time, "sleep", side_effect=sleep), \
                patch.object(ready.subprocess, "run", return_value=Mock(returncode=1, stdout="private")), \
                redirect_stdout(output):
            with self.assertRaises(TimeoutError):
                ready.wait_ready(commands)
        self.assertEqual(clock[0], 120)
        self.assertNotIn("private", output.getvalue())

    def test_successful_exit_without_select_result_is_not_ready(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(ready.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(ready.time, "sleep", side_effect=sleep), \
                patch.object(ready.subprocess, "run", return_value=Mock(returncode=0, stdout="")):
            with self.assertRaises(TimeoutError):
                ready.wait_ready(ready.probes(self.root, "", {}, "sqlcmd"))

    def test_missing_tools_returns_clear_failure(self):
        error = io.StringIO()
        with patch.object(sys, "argv", ["sql_ready.py", str(self.root)]), \
                patch.object(ready._sql_admin, "sqlcmd_path", return_value=None), \
                redirect_stderr(error):
            self.assertEqual(ready.main(), 1)
        self.assertIn("Install SQL tools", error.getvalue())

    def test_subprocess_timeout_is_retried(self):
        clock = [0]
        with patch.object(ready.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(ready.time, "sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)), \
                patch.object(ready.subprocess, "run", side_effect=[
                    subprocess.TimeoutExpired("sqlcmd", 5), Mock(returncode=0, stdout="1")]):
            ready.wait_ready(ready.probes(self.root, "", {}, "sqlcmd"))
