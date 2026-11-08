import importlib.util
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

    def settings(self, connection, name="appsettings.json"):
        import json
        (self.root / name).write_text(json.dumps({"ConnectionStrings": {"db": connection}}))

    def test_local_sql_ordering_is_idempotent_and_preserves_unit(self):
        self.settings("Server=.;Database=example;Password=not-for-output")
        self.assertTrue(module.configure(self.service, self.root, {}))
        self.assertEqual(self.drop_in.read_text(), module.ORDERING)
        self.assertEqual(self.service.read_text(), self.unit_text)
        self.assertFalse(module.configure(self.service, self.root, {}))

    def test_environment_file_and_variables_override_base_settings(self):
        self.settings("Server=remote.test;Database=db")
        self.settings("Data Source=.;Database=db", "appsettings.Test.json")
        variables = {"DOTNET_ENVIRONMENT": "Test"}
        self.assertTrue(module.configure(self.service, self.root, variables))
        variables["ConnectionStrings__db"] = "Server=remote.test;Database=db"
        self.assertTrue(module.configure(self.service, self.root, variables))
        self.assertFalse(self.drop_in.exists())
        self.assertFalse(module.configure(self.service, self.root, variables))

    def test_nonlocal_and_non_mssql_do_not_add_ordering(self):
        for connection in ("Server=localhost;Database=db", "Server=sql.example.test;Database=db",
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
        self.assertEqual(self.drop_in.read_text(), module.ORDERING)

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
        publish = (apache / "solutionfiles/csharp/scripts/publish.sh").read_text()
        self.assertLess(publish.index("sql_startup.py"), publish.rindex("\nstart_service\n"))
        production = (apache / "solutionfiles/csharp/prod-scripts/publish_prod.sh").read_text()
        self.assertLess(production.index("sql_startup.py"), production.index("systemctl start"))
