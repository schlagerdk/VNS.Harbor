"""harbor server status, cron, dotnet and node, and the SQL commands without questions (phase 4)"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import Result, RecordingSystem  # noqa: E402
from harbor.server import tools  # noqa: E402
from harbor.sql import server  # noqa: E402

SDKS = "8.0.404 [/usr/lib/dotnet/sdk]\n10.0.100 [/usr/lib/dotnet/sdk]\n10.0.103 [/usr/lib/dotnet/sdk]\n"
RUNTIMES = ("Microsoft.AspNetCore.App 8.0.11 [/usr/lib/dotnet/shared/Microsoft.AspNetCore.App]\n"
            "Microsoft.AspNetCore.App 10.0.3 [/usr/lib/dotnet/shared/Microsoft.AspNetCore.App]\n"
            "Microsoft.NETCore.App 10.0.3 [/usr/lib/dotnet/shared/Microsoft.NETCore.App]\n")
CUSTOM = "0 2 * * * /vns/sql/cron/delete_old_2dm_data.sh >> /var/log/delete_old_2dm_data.log 2>&1"


class Server(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "etc").mkdir()
        (self.root / "etc/os-release").write_text('PRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n')
        self.crontab = ""

    def system(self, **answers):
        def crontab(argv, given):
            if argv[1] == "-":
                self.crontab = given
                return ""
            return Result(0, self.crontab) if self.crontab else Result(1, "", "no crontab for root")
        return RecordingSystem(root=self.root, answers={"crontab": crontab, **answers})


class StatusTests(Server):
    def test_what_the_server_has(self):
        (self.root / "usr/lib/dotnet").mkdir(parents=True)
        (self.root / "usr/lib/dotnet/dotnet").write_text("")
        (self.root / "opt/mssql-tools18/bin").mkdir(parents=True)
        (self.root / "opt/mssql-tools18/bin/sqlcmd").write_text("")
        sql = self.root / "sql"
        (sql / "cron").mkdir(parents=True)
        (sql / "drop_db.sh").write_text("sa -U sa -P 'secret1!' -i scripts/drop_db.sql\n")
        (sql / "cron/shrink_logs.sh").write_text("sa -U sa -P 'secret1!' -i scripts/shrink_logs.sql\n")
        self.crontab = tools.JOBS["backup_everything"] + "\n" + CUSTOM + "\n"
        system = self.system(**{"dpkg -s mssql-tools18": Result(0), "dpkg -s mssql-server": Result(0),
                                "dpkg-query -W -f=${Version} mssql-server": "16.0.4185.3-1", "dpkg-query -W -f=${Version} mssql-tools18": "18.4.1.1-1",
                                "systemctl is-active mssql-server": "active", "node --version": "v22.11.0", "npm --version": "10.9.0",
                                "/usr/lib/dotnet/dotnet --list-sdks": SDKS, "/usr/lib/dotnet/dotnet --list-runtimes": RUNTIMES})
        system.programs.add("node")
        found = tools.status(system, sql)
        self.assertEqual(found["os"]["name"], "Ubuntu 24.04.1 LTS")
        self.assertEqual(found["sql_server"], {"installed": True, "version": "16.0.4185.3-1", "running": True})
        self.assertEqual(found["sql_tools"], {"installed": True, "version": "18.4.1.1-1"})
        self.assertEqual(found["sa_password"], {"scripts": 2, "set": True})
        self.assertEqual(found["cron"], {"jobs": {"shrink_logs": False, "backup_everything": True}, "others": [CUSTOM]})
        self.assertEqual(found["dotnet"], [{"channel": "10.0", "sdk": "10.0.103", "runtime": "10.0.3"},
                                           {"channel": "8.0", "sdk": "8.0.404", "runtime": "8.0.11"}])
        self.assertEqual(found["node"], {"version": "22.11.0", "npm": "10.9.0"})

    def test_a_bare_server(self):
        sql = self.root / "sql"
        sql.mkdir()
        (sql / "drop_db.sh").write_text("sa -U sa -P '@SA_PASSWORD@' -i scripts/drop_db.sql\n")
        system = self.system(**{"dpkg -s": Result(1), "systemctl is-active mssql-server": Result(3, "inactive")})
        found = tools.status(system, sql)
        self.assertFalse(found["sql_server"]["installed"] or found["sql_server"]["running"] or found["sql_tools"]["installed"])
        self.assertEqual(found["sa_password"], {"scripts": 1, "set": False})
        self.assertEqual((found["dotnet"], found["node"]["version"]), ([], ""))

    def test_the_command_prints_json(self):
        system = self.system(**{"dpkg -s": Result(1)})
        with mock.patch("builtins.print") as printed:
            self.assertEqual(main(["server", "status"], system), 0)
        self.assertIn("sql_server", json.loads(printed.call_args[0][0]))


class CronTests(Server):
    def test_on_adds_each_job_once_and_off_leaves_the_others(self):
        self.crontab = CUSTOM + "\n" + tools.JOBS["shrink_logs"] + "\n"
        self.assertEqual(main(["server", "cron", "on"], self.system()), 0)
        self.assertEqual(self.crontab, CUSTOM + "\n" + tools.JOBS["shrink_logs"] + "\n" + tools.JOBS["backup_everything"] + "\n")
        system = self.system()
        main(["server", "cron", "on"], system)
        self.assertNotIn("crontab -", system.commands, "nothing to add")
        main(["server", "cron", "off"], self.system())
        self.assertEqual(self.crontab, CUSTOM + "\n")

    def test_old_jobs_are_upgraded_to_harbor(self):
        # The jobs as VNS 1.x added them
        OLD = ("0 */4 * * * /vns/sql/cron/shrink_logs.sh >> /var/log/sql_shrink_logs.log 2>&1",
               "15 */4 * * * /vns/sql/cron/backup_everything.sh >> /var/log/sql_backup_everything.log 2>&1")
        self.crontab = OLD[0] + "\n" + CUSTOM + "\n" + OLD[1] + "\n"
        main(["server", "cron", "upgrade"], self.system())
        self.assertEqual(self.crontab.splitlines(), [
            "0 */4 * * * /usr/local/bin/harbor sql shrink-logs >> /var/log/sql_shrink_logs.log 2>&1", CUSTOM,
            "15 */4 * * * /usr/local/bin/harbor sql backup-everything >> /var/log/sql_backup_everything.log 2>&1"])
        # Seen as on, and only once
        system = self.system()
        main(["server", "cron", "on"], system)
        self.assertNotIn("crontab -", system.commands)
        system = self.system()
        main(["server", "cron", "upgrade"], system)
        self.assertNotIn("crontab -", system.commands)

    def test_on_with_no_crontab(self):
        main(["server", "cron", "on"], self.system())
        self.assertEqual(self.crontab, "".join(line + "\n" for line in tools.JOBS.values()))


class DotnetTests(Server):
    def test_a_channel_is_installed_and_linked(self):
        (self.root / "usr/lib/dotnet").mkdir(parents=True)
        (self.root / "usr/lib/dotnet/dotnet").write_text("")
        (self.root / "usr/bin").mkdir(parents=True)
        installed = {"sdks": "10.0.103 [/usr/lib/dotnet/sdk]\n", "runtimes": "Microsoft.AspNetCore.App 10.0.3 [x]\n"}

        def install(argv):
            if "--runtime" in argv:
                installed["runtimes"] += "Microsoft.AspNetCore.App 8.0.11 [x]\n"
            else:
                installed["sdks"] += "8.0.404 [x]\n"
            return ""
        system = self.system(**{"/usr/lib/dotnet/dotnet --list-sdks": lambda argv: installed["sdks"],
                                "/usr/lib/dotnet/dotnet --list-runtimes": lambda argv: installed["runtimes"], "bash": install})
        self.assertEqual(main(["server", "dotnet", "install", "8.0"], system), 0, system.said)
        installs = [command for command in system.commands if command.startswith("bash ")]
        self.assertTrue(all("--channel 8.0" in command and "--skip-non-versioned-files" in command for command in installs), installs)
        self.assertEqual(os.readlink(self.root / "usr/bin/dotnet"), "/usr/lib/dotnet/dotnet")
        self.assertIn(".NET 8.0: SDK 8.0.404, runtime 8.0.11.", system.said[-1])
        # There: install does nothing, update asks the installer for the newest patch
        system = self.system(**{"/usr/lib/dotnet/dotnet --list-sdks": lambda argv: installed["sdks"],
                                "/usr/lib/dotnet/dotnet --list-runtimes": lambda argv: installed["runtimes"]})
        main(["server", "dotnet", "install", "8.0"], system)
        self.assertFalse(any(command.startswith("bash ") for command in system.commands))
        main(["server", "dotnet", "update", "8.0"], system)
        self.assertTrue(any(command.startswith("bash ") and "--channel 8.0" in command for command in system.commands))
        self.assertIn(".NET 8.0 is up to date", system.said[-1])

    def test_not_a_channel(self):
        for channel in ("7.0", "10", "10.0; rm -rf /"):
            system = self.system()
            self.assertEqual(main(["server", "dotnet", "install", channel], system), 1)
            self.assertIn("Not a .NET channel", system.said[-1])


class NodeTests(Server):
    def test_install_from_nodesource(self):
        system = self.system(**{"node --version": "v22.11.0", "npm --version": "10.9.0"})
        self.assertEqual(main(["server", "node", "install", "22"], system), 0, system.said)
        self.assertTrue(any(command.startswith("curl -fsSL https://deb.nodesource.com/setup_22.x -o ") for command in system.commands))
        self.assertIn("apt-get install -y nodejs", system.commands)
        self.assertEqual(system.said[-1], "Node.js 22.11.0 with npm 10.9.0")
        system = self.system(**{"node --version": "v20.1.0"})
        self.assertEqual(main(["server", "node", "install", "24"], system), 1)
        for major in ("16", "", "22; id"):
            self.assertEqual(main(["server", "node", "install", major], self.system()), 1)

    def test_remove(self):
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        (self.root / "etc/apt/sources.list.d/nodesource.list").write_text("deb x\n")
        system = self.system()
        main(["server", "node", "remove"], system)
        self.assertIn("apt-get remove --purge -y nodejs", system.commands)
        self.assertFalse((self.root / "etc/apt/sources.list.d/nodesource.list").exists())


class SqlWithoutQuestionsTests(Server):
    def test_install_server_with_an_edition_asks_nothing(self):
        sql = self.root / "sql"
        sql.mkdir()
        (sql / "drop_db.sh").write_text("sa -U sa -P '@SA_PASSWORD@' -i scripts/drop_db.sql\n")
        system = self.system(**{"dpkg -s": Result(1), "curl -fsSI": Result(0), "curl -fsSL": "deb x", "apt-cache show libldap-2.5-0": Result(0)})
        system.programs.add("gpg")
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        with mock.patch.object(server, "SQL_DIR", sql), mock.patch.object(server, "prepare_backup_permissions"), \
                mock.patch.object(server, "install_tools"), mock.patch.dict(os.environ, {"HARBOR_SA_PASSWORD": "Secret-123"}):
            self.assertEqual(main(["sql", "install-server", "--edition", "Express", "--with-tools"], system), 0, system.said)
        self.assertEqual(system.asked, [])
        setup = system.commands.index("/opt/mssql/bin/mssql-conf -n setup accept-eula")
        self.assertEqual(system.environments[setup], {"ACCEPT_EULA": "Y", "MSSQL_PID": "Express", "MSSQL_SA_PASSWORD": "Secret-123"})
        self.assertNotIn("Secret-123", " ".join(system.commands))
        self.assertIn("-P 'Secret-123'", (sql / "drop_db.sh").read_text())

    def test_an_edition_needs_the_password(self):
        system = self.system()
        with mock.patch.dict(os.environ, {"HARBOR_SA_PASSWORD": ""}):
            self.assertEqual(main(["sql", "install-server", "--edition", "Express"], system), 1)
        self.assertIn("HARBOR_SA_PASSWORD", system.said[-1])
        self.assertEqual(main(["sql", "install-server", "--edition", "Free"], self.system()), 1)

    def test_uninstall_server_keeps_the_data_when_told(self):
        data = self.root / "var/opt/mssql"
        data.mkdir(parents=True)
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        system = self.system()
        self.assertEqual(main(["sql", "uninstall-server", "--yes", "--keep-data", "--without-tools"], system), 0, system.said)
        self.assertEqual(system.asked, [])
        self.assertTrue(data.is_dir())
        main(["sql", "uninstall-server", "--yes", "--delete-data"], self.system())
        self.assertFalse(data.exists())

    def test_set_sa_password_from_the_admin(self):
        sql = self.root / "sql"
        sql.mkdir()
        (sql / "drop_db.sh").write_text("sa -U sa -P '@SA_PASSWORD@' -i scripts/drop_db.sql\n")
        (self.root / "opt/mssql-tools18/bin").mkdir(parents=True)
        (self.root / "opt/mssql-tools18/bin/sqlcmd").write_text("")
        rejected = self.system(**{"/opt/mssql-tools18/bin/sqlcmd": Result(1)})
        with mock.patch.object(server, "SQL_DIR", sql), mock.patch.dict(os.environ, {"HARBOR_SA_PASSWORD": "Secret-123"}):
            with self.assertRaises(Exception):
                server.set_sa_password(rejected, sql)
            self.assertEqual(rejected.asked, [])
            self.assertIn("@SA_PASSWORD@", (sql / "drop_db.sh").read_text())
            accepted = self.system()
            server.set_sa_password(accepted, sql)
        self.assertEqual(accepted.asked, [])
        self.assertIn("-P 'Secret-123'", (sql / "drop_db.sh").read_text())


class SaSecretTests(Server):
    def test_the_password_moves_into_harbor_and_is_used_from_there(self):
        import stat
        from harbor.sql import databases
        sql = self.root / "sql"
        (sql / "cron").mkdir(parents=True)
        (sql / "drop_db.sh").write_text("sa -U sa -P 'it'\\''s secret' -i scripts/drop_db.sql\n")
        with mock.patch.object(server, "SQL_DIR", sql):
            system = self.system()
            self.assertEqual(main(["sql", "import-sa-password"], system), 0, system.said)
        secret = self.root / "etc/harbor/secrets/sql.json"
        self.assertEqual(json.loads(secret.read_text()), {"sa_password": "it's secret"})
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        # Without a wrapper (no SQLCMDPASSWORD) sqlcmd gets it from Harbor, in its environment
        (self.root / "var/opt/mssql/backup").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"SQLCMDPASSWORD": ""}), mock.patch.object(databases, "prepare_backup_permissions"):
            system = self.system()
            self.assertEqual(main(["sql", "backup-everything"], system), 0, system.said)
        run = next(index for index, command in enumerate(system.commands) if command.startswith("/opt/mssql-tools18/bin/sqlcmd"))
        self.assertEqual(system.environments[run]["SQLCMDPASSWORD"], "it's secret")
        self.assertNotIn("secret", system.commands[run])


if __name__ == "__main__":
    unittest.main()
