import importlib.util
import ast
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("sql_startup", LIB / "sql_startup.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.path.pop(0)


class SqlStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.service = self.root / "example.test.service"
        self.unit_text = "[Unit]\nAfter=custom.service\n[Service]\nExecStart=/usr/lib/dotnet/dotnet app.dll\n"
        self.service.write_text(self.unit_text)
        self.drop_in = self.root / (self.service.name + ".d") / module.DROP_IN
        tools = patch.object(module, "sqlcmd_path", return_value="/opt/mssql-tools18/bin/sqlcmd")
        tools.start()
        self.addCleanup(tools.stop)

    def settings(self, connection, name="appsettings.json"):
        import json
        (self.root / name).write_text(json.dumps({"ConnectionStrings": {"db": connection}}))

    def test_local_sql_ordering_is_idempotent_and_preserves_dependencies(self):
        self.settings("Server=.;Database=example;Password=not-for-output")
        self.assertTrue(module.configure(self.service, self.root, {}))
        self.assertFalse(self.drop_in.exists())
        self.assertIn("After=custom.service\n", self.service.read_text())
        self.assertIn("Wants=mssql-server.service\n", self.service.read_text())
        self.assertIn("After=network-online.target mssql-server.service\n", self.service.read_text())
        self.assertIn('ExecStartPre=/usr/bin/python3 "', self.service.read_text())
        self.assertIn("sql_ready.py", self.service.read_text())
        self.assertIn("TimeoutStartSec=150", self.service.read_text())
        self.assertFalse(module.configure(self.service, self.root, {}))

    def test_environment_file_and_variables_override_base_settings(self):
        self.settings("Server=remote.test;Database=db")
        self.settings("Data Source=.;Database=db", "appsettings.Test.json")
        variables = {"DOTNET_ENVIRONMENT": "Test"}
        self.assertTrue(module.configure(self.service, self.root, variables))
        variables["ConnectionStrings__db"] = "Server=remote.test;Database=db"
        self.assertFalse(module.configure(self.service, self.root, variables))
        self.assertFalse(self.drop_in.exists())
        self.assertFalse(module.configure(self.service, self.root, variables))

    def test_nonlocal_and_non_mssql_do_not_add_ordering(self):
        for connection in ("Server=sql.example.test;Database=db",
                           "Data Source=Umbraco.sqlite.db", "Server=.\\SQLEXPRESS;Database=db"):
            with self.subTest(connection=connection):
                self.settings(connection)
                self.assertFalse(module.configure(self.service, self.root, {}))
                self.assertFalse(self.drop_in.exists())

    def test_datasource_spelling_and_comment_supported(self):
        (self.root / "appsettings.json").write_text(
            '{// comment\n"ConnectionStrings":{"db":"Datasource=.;Database=db",},}')
        self.assertTrue(module.configure(self.service, self.root, {}))

    def test_invalid_config_is_reported_without_changing_dropin(self):
        self.settings("Server=.;Database=db")
        module.configure(self.service, self.root, {})
        (self.root / "appsettings.json").write_text("{invalid")
        with self.assertRaises(ValueError):
            module.configure(self.service, self.root, {})
        self.assertIn("Wants=mssql-server.service", self.service.read_text())

    def test_non_dotnet_service_rejected(self):
        self.service.write_text("[Service]\nExecStart=/usr/bin/python3 app.py\n")
        with self.assertRaisesRegex(ValueError, ".NET"):
            module.configure(self.service, self.root, {})

    def test_cli_reads_service_configuration_and_reloads_without_restart(self):
        results = [Mock(), Mock(stdout=str(self.root)), Mock(stdout='"DOTNET_ENVIRONMENT=Test"'),
                   Mock()]
        with patch.object(sys, "argv", ["sql_startup.py", "æøå.test"]), \
                patch.object(module.subprocess, "run", side_effect=results) as run, \
                patch.object(module, "configure", return_value=True) as configure:
            module.main()
        unit = "xn--5cab8c.test.service"
        configure.assert_called_once_with(Path("/etc/systemd/system") / unit, str(self.root),
                                          {"DOTNET_ENVIRONMENT": "Test"})
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "daemon-reload"],
            ["systemctl", "show", unit, "--property=WorkingDirectory", "--value"],
            ["systemctl", "show", unit, "--property=Environment", "--value"],
            ["systemctl", "daemon-reload"],
        ])

    def test_automatic_hooks_run_before_service_start(self):
        apache = LIB.parent
        creation = (LIB / "common.sh").read_text().split("install_service() {", 1)[1]
        self.assertLess(creation.index("sql_startup.py"), creation.index('systemctl restart'))
        publish = (apache.parent / "tests/legacy/solutionfiles/csharp/scripts/publish.sh").read_text()
        self.assertLess(publish.index("sql_startup.py"), publish.rindex("\nstart_service\n"))
        production = (apache / "solutionfiles/csharp/prod-scripts/publish_prod.sh").read_text()
        self.assertLess(production.index("sql_startup.py"), production.index("systemctl start"))

    def test_local_host_variants(self):
        for host in (".", "localhost", "127.0.0.1", "::1", "[::1]", "tcp:localhost,1433"):
            self.assertTrue(module.local_sql_server(host), host)
        for host in ("sql.test", "localhost.test", ".\\SQLEXPRESS"):
            self.assertFalse(module.local_sql_server(host), host)

    def test_legacy_dropin_migrates_into_main_unit(self):
        self.settings("Server=.;Database=db")
        self.drop_in.parent.mkdir()
        self.drop_in.write_text(module.ORDERING)
        self.assertTrue(module.configure(self.service, self.root, {}))
        self.assertFalse(self.drop_in.exists())
        self.assertFalse(self.drop_in.parent.exists())
        self.assertIn("Wants=mssql-server.service", self.service.read_text())

    def test_missing_sql_tools_does_not_modify_service(self):
        self.settings("Server=.;Database=db")
        with patch.object(module, "sqlcmd_path", side_effect=ValueError("Install SQL tools")):
            with self.assertRaisesRegex(ValueError, "SQL tools"):
                module.configure(self.service, self.root, {})
        self.assertEqual(self.service.read_text(), self.unit_text)

    def test_chroot_retained_for_web_process_but_not_readiness_precommand(self):
        self.settings("Server=.;Database=db")
        sandbox = self.root / "sandbox"
        self.service.write_text(self.unit_text + f"RootDirectory={sandbox}\n")
        module.configure(self.service, self.root, {})
        self.assertIn(f"RootDirectory={sandbox}\n", self.service.read_text())
        self.assertTrue((sandbox / self.root.relative_to("/")).is_dir())
        self.assertIn("RootDirectoryStartOnly=true\n", self.service.read_text())
        self.assertNotIn("ExecStartPre=+", self.service.read_text())
        self.assertFalse(module.configure(self.service, self.root, {}))

    def test_existing_readiness_unit_gets_missing_precommand_cwd(self):
        self.settings("Server=.;Database=db")
        module.configure(self.service, self.root, {})
        sandbox = self.root / "empty-root"
        self.service.write_text(self.service.read_text() + f"\nRootDirectory={sandbox}\n")
        before = self.service.read_text()
        self.assertTrue(module.configure(self.service, self.root, {}))
        self.assertEqual(self.service.read_text(), before)
        self.assertTrue((sandbox / self.root.relative_to("/")).is_dir())

    def test_scan_restarts_changed_site_once_and_skips_cached_site(self):
        web = self.root / "www"
        folder = web / "example.test"
        folder.mkdir(parents=True)
        self.settings("Server=.;Database=db")
        state = self.root / "state/checked.json"
        with patch.object(module, "service_configuration", return_value=(str(self.root), {})) as config, \
                patch.object(module.subprocess, "run") as run:
            module.scan(web, self.root, state)
            module.scan(web, self.root, state)
        self.assertEqual(config.call_count, 1)
        self.assertEqual(sum(call.args[0] == ["systemctl", "restart", "example.test.service"]
                             for call in run.call_args_list), 1)

    def test_failed_restart_is_retried_on_next_scan(self):
        web = self.root / "www"
        (web / "example.test").mkdir(parents=True)
        self.settings("Server=.;Database=db")
        state = self.root / "state/checked.json"
        def fail_restart(command, **kwargs):
            if command[1] == "restart":
                raise module.subprocess.CalledProcessError(1, command)
            return Mock()
        with patch.object(module, "service_configuration", return_value=(str(self.root), {})), \
                patch.object(module.subprocess, "run", side_effect=fail_restart):
            with self.assertRaises(RuntimeError):
                module.scan(web, self.root, state)
        with patch.object(module, "service_configuration", return_value=(str(self.root), {})), \
                patch.object(module.subprocess, "run") as run:
            module.scan(web, self.root, state)
        self.assertIn(["systemctl", "restart", "example.test.service"],
                      [call.args[0] for call in run.call_args_list])

    def test_correct_unit_does_not_restart_and_ignores_other_services(self):
        web = self.root / "www"
        for name in ("example.test", "static.test", "admin.test"):
            (web / name).mkdir(parents=True)
        self.settings("Server=127.0.0.1;Database=db")
        module.configure(self.service, self.root, {})
        (self.root / "static.test.service").write_text("ExecStart=/usr/bin/python3 app.py")
        state = self.root / "state/checked.json"
        with patch.object(module, "service_configuration", return_value=(str(self.root), {})), \
                patch.object(module.subprocess, "run") as run:
            module.scan(web, self.root, state, "admin.test")
        self.assertFalse(any(call.args[0][1] == "restart" for call in run.call_args_list))

    def test_admin_startup_runs_scan_and_logs_failures(self):
        path = LIB.parent / "solutionfiles/admin/app/app.py"
        source = path.read_text()
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "configure_sql_startup_on_admin_start")
        runner = Mock()
        logger = Mock()
        ns = {"subprocess": module.subprocess, "sys": sys, "os": module.os,
              "APACHE_DIR": str(LIB.parent), "WEB_ROOT": "/var/www",
              "ADMIN_SITE": "admin.test", "app": Mock(logger=logger)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "admin startup", "exec"), ns)
        with patch.object(module.subprocess, "run", runner):
            ns[node.name]()
            runner.assert_called_once_with(
                [sys.executable, str(LIB / "sql_startup.py"), "--scan", "/var/www",
                 "--exclude", "admin.test"], check=True, timeout=600)
            runner.side_effect = module.subprocess.CalledProcessError(1, ["scan"])
            ns[node.name]()
        logger.exception.assert_called_once()
        self.assertIn("threading.Thread(target=configure_sql_startup_on_admin_start, daemon=True).start()", source)
