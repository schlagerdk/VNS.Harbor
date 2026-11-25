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
        self.assertEqual(found["sql_server"], {"installed": True, "version": "16.0.4185.3-1", "newer": "", "running": True, "packages_for": ""})
        self.assertEqual(found["sql_tools"], {"installed": True, "version": "18.4.1.1-1", "newer": ""})
        self.assertEqual(found["sa_password"], {"scripts": 2, "set": True})
        self.assertEqual(found["cron"], {"jobs": {"shrink_logs": False, "backup_everything": True, "postgres_backup": False}, "others": [CUSTOM],
                                         "schedules": {"shrink_logs": "0 */4 * * *", "backup_everything": "15 */4 * * *", "postgres_backup": "30 */4 * * *"},
                                         "labels": {"shrink_logs": "", "backup_everything": "", "postgres_backup": ""}})
        # Changed in Scheduled jobs: its schedule and label as root's crontab has them now
        self.crontab = "# Harbor label: Shrink Logs\n" + tools.JOBS["shrink_logs"].replace("0 */4", "0 */2", 1) + "\n"
        found = tools.status(system, sql)
        self.assertEqual((found["cron"]["schedules"]["shrink_logs"], found["cron"]["labels"]["shrink_logs"]), ("0 */2 * * *", "Shrink Logs"))
        self.assertEqual(found["dotnet"], [{"channel": "10.0", "sdk": "10.0.103", "runtime": "10.0.3", "update": None, "sites": []},
                                           {"channel": "8.0", "sdk": "8.0.404", "runtime": "8.0.11", "update": None, "sites": []}])
        self.assertEqual(found["node"], {"version": "22.11.0", "npm": "10.9.0", "newer": "", "sites": []})

    def test_whether_an_update_is_there(self):
        policy = "mssql-server:\n  Installed: 17.0.5035.5-3\n  Candidate: 17.0.5065.9-3\n"
        system = self.system(**{"apt-cache policy mssql-server": policy, "dpkg --compare-versions": Result(0)})
        self.assertEqual(tools.newer_package(system, "mssql-server"), "17.0.5065.9-3")
        system = self.system(**{"apt-cache policy mssql-server": "mssql-server:\n  Installed: 1.0\n  Candidate: 1.0\n"})
        self.assertEqual(tools.newer_package(system, "mssql-server"), "")
        releases = {"8.0": '{"latest-sdk": "8.0.428", "latest-runtime": "8.0.34"}', "10.0": "not json"}
        system = self.system(curl=lambda argv: releases["8.0" if "/8.0/" in argv[-1] else "10.0"])
        items = [{"channel": "8.0", "sdk": "8.0.427", "runtime": "8.0.33"}, {"channel": "10.0", "sdk": "10.0.404", "runtime": "10.0.15"}]
        found = tools.dotnet_updates(system, items)
        self.assertEqual(found[0], {"channel": "8.0", "sdk": "8.0.427", "runtime": "8.0.33", "update": True, "latest_sdk": "8.0.428", "latest_runtime": "8.0.34"})
        # A channel the list cannot tell keeps its Update button as it was
        self.assertIsNone(found[1]["update"])
        # Kept for six hours: asked again, it is not fetched again
        cached = self.system(curl=lambda argv: self.fail("fetched again"))
        self.assertTrue(tools.dotnet_updates(cached, items[:1])[0]["update"])

    def test_what_uses_dotnet_and_node(self):
        web = self.root / "var/www"
        (web / "shop.dk/Shop").mkdir(parents=True)
        (web / "shop.dk/Shop.csproj").write_text("<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>")
        (web / "shop.dk/Shop/package.json").write_text('{"name": "shop", "engines": {"node": ">=20 <23"}}')
        (web / "shop.dk/node_modules/left-pad").mkdir(parents=True)
        (web / "shop.dk/node_modules/left-pad/package.json").write_text('{"engines": {"node": ">=0.10"}}')
        (web / "old.dk").mkdir()
        (web / "old.dk/Old.runtimeconfig.json").write_text('{"runtimeOptions": {"tfm": "net8.0"}}')
        (web / "multi.dk").mkdir()
        (web / "multi.dk/Multi.csproj").write_text("<Project><PropertyGroup><TargetFrameworks>net8.0;net10.0</TargetFrameworks></PropertyGroup></Project>")
        (web / "multi.dk/Multi.runtimeconfig.json").write_text('{"runtimeOptions": {"tfm": "net8.0"}}')
        units = self.root / "etc/systemd/system"
        units.mkdir(parents=True)
        (units / "chat.dk.service").write_text("[Service]\nExecStart=/usr/bin/node /var/www/chat.dk/server.js\n")
        (units / "shop.dk.service").write_text("[Service]\nExecStart=/usr/lib/dotnet/dotnet Shop.dll\n")
        system = self.system()
        self.assertEqual(tools.dotnet_usage(system), {
            "10": [{"site": "multi.dk", "how": "project"}, {"site": "shop.dk", "how": "project"}],
            "8": [{"site": "multi.dk", "how": "project"}, {"site": "old.dk", "how": "published"}]})
        self.assertEqual(tools.sites_using(system, "8.0"), ["multi.dk", "old.dk"])
        self.assertEqual(tools.node_usage(system), [
            {"site": "chat.dk", "how": "service", "path": "chat.dk.service", "engines": ""},
            {"site": "shop.dk", "how": "package.json", "path": "Shop/package.json", "engines": ">=20 <23"}])

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
    def test_on_with_a_job_adds_only_that_one(self):
        """Scheduled jobs suggests each of Harbor's jobs that is not in cron, and adds only that one (1.0.70)"""
        self.crontab = CUSTOM + "\n"
        self.assertEqual(main(["server", "cron", "on", "--job", "shrink_logs"], self.system()), 0)
        self.assertEqual(self.crontab, CUSTOM + "\n" + tools.JOBS["shrink_logs"] + "\n")
        system = self.system()
        self.assertEqual(main(["server", "cron", "on", "--job", "shrink_logs"], system), 0)
        self.assertIn("The job is in root's crontab.", system.said)
        self.assertEqual(main(["server", "cron", "on", "--job", "backup_everything"], self.system()), 0)
        self.assertEqual(self.crontab, CUSTOM + "\n" + tools.JOBS["shrink_logs"] + "\n" + tools.JOBS["backup_everything"] + "\n")

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
        # Microsoft SQL Server's jobs: PostgreSQL's is turned on by itself (1.0.82)
        self.assertEqual(self.crontab, "".join(tools.JOBS[name] + "\n" for name in tools.MSSQL_JOBS))
        main(["server", "cron", "on", "--job", "postgres_backup"], self.system())
        self.assertIn("harbor postgres backup-everything", self.crontab)
        # Each added is noted as saved now: cron has not missed it (it said it should have run 14 days before)
        saved = tools.cron_state(self.system())["saved"]
        self.assertEqual(len(saved), 3)


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
        self.assertIn("apt-get install -y --allow-downgrades nodejs", system.commands)
        self.assertEqual(system.said[-1], "Node.js 22.11.0 with npm 10.9.0")

    def test_a_newer_version_is_replaced_by_an_older(self):
        versions = iter(["v24.2.0", "v22.11.0"])
        madison = (" nodejs | 22.11.0-1nodesource1 | https://deb.nodesource.com/node_22.x nodistro/main amd64 Packages\n"
                   " nodejs | 18.19.1+dfsg-6ubuntu5 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n")
        system = self.system(**{"node --version": lambda argv: next(versions), "npm --version": "10.9.0", "apt-cache madison nodejs": madison})
        system.programs.add("node")
        self.assertEqual(main(["server", "node", "install", "22"], system), 0, system.said)
        self.assertIn("apt-get install -y --allow-downgrades nodejs=22.11.0-1nodesource1", system.commands)
        self.assertEqual(system.said[-1], "Node.js 22.11.0 with npm 10.9.0, in place of 24.2.0")
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


class AdminConnectionTests(Server):
    def test_the_admins_connections_to_sql_server_here_get_the_new_sa_password(self):
        """A connection held its own copy of the sa password, which failed once SQL Server was installed again with
        another (1.0.69)"""
        import json
        app = self.root / "var/www/admin.example.com/app"
        app.mkdir(parents=True)
        (app / "config.json").write_text('{"secret_key": "x"}')
        connection = {"name": "Here", "host": "localhost", "port": 1433, "username": "sa", "password": "old", "backup_directory": "/b",
                      "trust_certificate": True, "ssh_enabled": False}
        stored = [connection | {"id": "1"}, connection | {"id": "2", "host": "web-01"}, connection | {"id": "3", "host": "198.51.100.7"},
                  connection | {"id": "4", "username": "app"}, connection | {"id": "5", "host": "sql.example.net"},
                  connection | {"id": "6", "host": "localhost", "port": 1434}]
        (app / "sql-connections.json").write_text(json.dumps(stored))
        (app / "sql-connections.json").chmod(0o600)
        system = self.system(**{"hostname -I": "198.51.100.7 2001:db8::7", "hostname -f": "web-01.example.com", "hostname": "web-01"})
        with mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}):
            server.save_sa_password(system, "New-Secret-1")
        saved = {item["id"]: item["password"] for item in json.loads((app / "sql-connections.json").read_text())}
        # Here by its names and addresses, as sa on 1433: not another login, server or port
        self.assertEqual(saved, {"1": "New-Secret-1", "2": "New-Secret-1", "3": "New-Secret-1", "4": "old", "5": "old", "6": "old"})
        self.assertEqual((app / "sql-connections.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("The SQL connection of admin.example.com to SQL Server here has the new sa password.", system.said)


    def test_the_admins_connections_go_with_the_sql_tools(self):
        import json
        app = self.root / "var/www/admin.example.com/app"
        app.mkdir(parents=True)
        (app / "config.json").write_text('{"secret_key": "x"}')
        (app / "sql-connections.json").write_text(json.dumps([{"id": "1", "host": "localhost"}, {"id": "2", "host": "sql.example.net"}]))
        system = self.system(**{"dpkg -s mssql-tools": Result(1)})
        with mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}):
            server.uninstall_sql_tools(system, True)
        self.assertEqual(json.loads((app / "sql-connections.json").read_text()), [])
        self.assertIn("The 2 SQL connections of admin.example.com removed.", system.said)


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


    def test_the_backup_log_starts_each_run_with_its_time(self):
        """The admin cuts a job's log into runs at that line: the permissions step said its line before it, which
        came out as a run of its own (1.0.68)"""
        from harbor.sql import databases
        (self.root / "var/opt/mssql/backup").mkdir(parents=True)
        system = self.system(answers={"/opt/mssql-tools18/bin/sqlcmd": "ROW|1|shop|1024|1.0"})
        with mock.patch.dict(os.environ, {"SQLCMDPASSWORD": "pw"}):
            databases.backup_everything(system)
        self.assertRegex(system.said[0], r"^----- \d\d\.\d\d\.\d{4} \d\d:\d\d -----$")
        self.assertNotIn("Ensure SQL Server can read and overwrite backup files", system.said)
        self.assertIn("chown mssql:mssql /var/opt/mssql/backup", system.commands)


class CronLabelAndClearTests(Server):
    def test_a_label_is_the_comment_line_above_its_job(self):
        self.crontab = "# made by hand\n# Harbor label: Delete old 2DM data\n" + CUSTOM + "\n# Harbor label: nothing under it\n"
        entries = tools.crontab_entries(self.system())
        self.assertEqual([(entry["kind"], entry.get("label", entry.get("line"))) for entry in entries],
                         [("other", "# made by hand"), ("job", "Delete old 2DM data"), ("other", "# Harbor label: nothing under it")])
        entries[1]["label"] = "  Old   2DM data\n"
        entries.append({"kind": "job", "schedule": "@daily", "command": "/usr/local/bin/x", "label": ""})
        tools.crontab_save(self.system(), entries)
        self.assertEqual(self.crontab.splitlines(), ["# made by hand", "# Harbor label: Old 2DM data", CUSTOM,
                                                     "# Harbor label: nothing under it", "@daily /usr/local/bin/x"])

    def test_the_label_goes_when_cron_takes_harbors_job_out(self):
        self.crontab = "# Harbor label: Nightly backup\n" + tools.JOBS["backup_everything"] + "\n" + CUSTOM + "\n"
        tools.cron(self.system(), False)
        self.assertEqual(self.crontab, CUSTOM + "\n")

    def test_clear_log_empties_only_a_jobs_own_file(self):
        log = self.root / "var/log/delete_old_2dm_data.log"
        log.parent.mkdir(parents=True)
        log.write_text("old output\n")
        self.crontab = CUSTOM + "\n"
        tools.crontab_clear_log(self.system(), CUSTOM.split(None, 5)[5])
        self.assertEqual(log.read_text(), "")
        # Not a job there, or a link to another file: refused
        with self.assertRaises(tools.Stop):
            tools.crontab_clear_log(self.system(), "/bin/true >> /var/log/delete_old_2dm_data.log 2>&1")
        (self.root / "etc/passwd").write_text("root\n")
        log.unlink()
        log.symlink_to(self.root / "etc/passwd")
        with self.assertRaises(tools.Stop):
            tools.crontab_clear_log(self.system(), CUSTOM.split(None, 5)[5])
        self.assertEqual((self.root / "etc/passwd").read_text(), "root\n")


class CronRunTests(Server):
    def test_a_job_runs_as_cron_runs_it(self):
        self.crontab = "PATH=/usr/local/bin:/usr/bin:/bin\nMAILTO=\"\"\n# Harbor label: Old 2DM data\n" + CUSTOM + "\n"
        system = self.system(**{"/usr/bin/env": ""})
        with mock.patch.dict(os.environ, {"HARBOR_PROGRESS": "1"}):
            self.assertEqual(main(["server", "crontab", "run", "--command", CUSTOM.split(None, 5)[5]], system), 0, system.said)
        run = next(command for command in system.commands if command.startswith("/usr/bin/env"))
        self.assertEqual(run, "/usr/bin/env -i HOME=/root LOGNAME=root USER=root SHELL=/bin/sh PATH=/usr/local/bin:/usr/bin:/bin MAILTO= "
                              "/bin/sh -c " + CUSTOM.split(None, 5)[5])
        self.assertIn(("step", "Run Old 2DM data"), system.events)
        self.assertIn("Its output is in /var/log/delete_old_2dm_data.log.", system.said[-1])

    def test_a_job_that_fails_fails_the_task_and_others_are_refused(self):
        self.crontab = CUSTOM + "\n"
        system = self.system(**{"/usr/bin/env": Result(3, "", "")})
        self.assertEqual(main(["server", "crontab", "run", "--command", CUSTOM.split(None, 5)[5]], system), 3)
        self.assertIn("exit code 3", system.said[-1])
        system = self.system()
        self.assertEqual(main(["server", "crontab", "run", "--command", "rm -rf /tmp/x"], system), 1)
        self.assertFalse(any(command.startswith("/usr/bin/env") for command in system.commands))

    def test_the_log_knows_when_a_command_was_saved_and_run_by_hand(self):
        # cron's journal knows only the commands it ran: a changed one has no runs there, Harbor tells since when
        self.crontab = CUSTOM + "\n"
        command = CUSTOM.split(None, 5)[5]
        changed = "/root/x.sh >> /var/log/x.log 2>&1"
        with mock.patch.object(tools.time, "time", return_value=1000):
            tools.crontab_save(self.system(), [{"kind": "job", "schedule": "0 2 * * *", "command": command},
                                               {"kind": "job", "schedule": "0 3 * * *", "command": changed}])
        with mock.patch.object(tools.time, "time", return_value=2000):
            main(["server", "crontab", "run", "--command", changed], self.system(**{"/usr/bin/env": Result(2, "", "")}))
        system = self.system(**{"journalctl": Result(1, "", "")})
        self.assertEqual((tools.crontab_log(system, command)["saved"], tools.crontab_log(system, command)["by_hand"]), (None, []))
        found = tools.crontab_log(system, changed)
        self.assertEqual((found["saved"], found["by_hand"]), (1000, [{"at": 2000, "code": 2}]))
        # A job that goes takes what Harbor knew of it along
        tools.crontab_save(self.system(), [{"kind": "job", "schedule": "0 2 * * *", "command": command}])
        self.assertEqual(json.loads((self.root / "var/lib/harbor/cron.json").read_text()), {"by_hand": {}, "saved": {}})

    def test_percent_signs_as_cron_reads_them(self):
        self.assertEqual(tools.cron_command("date +\\%d"), ("date +%d", None))
        self.assertEqual(tools.cron_command("mail -s hi root%line one%line two"), ("mail -s hi root", "line one\nline two\n"))


class ReportTests(Server):
    """backup-everything and shrink-logs write a table of what they did, without SQL Server's messages of it"""

    def run_sql(self, command, stdout, code=0):
        from harbor.sql import databases
        with mock.patch.dict(os.environ, {"SQLCMDPASSWORD": "pw"}), mock.patch.object(databases, "prepare_backup_permissions"):
            system = self.system(**{"/opt/mssql-tools18/bin/sqlcmd": Result(code, stdout, "")})
            return main(["sql", command], system), system

    def test_the_backup_as_a_table(self):
        out = ("Processed 9864 pages for database '2dm.dk', file '2dm.dk' on file 1.\n"
               "BACKUP DATABASE successfully processed 9865 pages in 1.329 seconds (57.986 MB/sec).\n"
               "ROW|1|2dm.dk|78920|1.3\n\nROW|2|anden.dk|1398920|22.9\n")
        code, system = self.run_sql("backup-everything", out)
        self.assertEqual(code, 0, system.said)
        self.assertIn(" -W -h -1 -s | ", next(command for command in system.commands if "sqlcmd" in command))
        self.assertEqual(system.said[1].splitlines(), [
            "| Nr | Database |    Size |   Time |     Speed |",
            "|----|----------|---------|--------|-----------|",
            "| 1  | 2dm.dk   | 77.1 MB |  1.3 s | 59.3 MB/s |",
            "| 2  | anden.dk |  1.3 GB | 22.9 s | 59.7 MB/s |"])
        self.assertTrue(system.said[2].startswith("Backup of everything done! ----- "))
        self.assertFalse(any("Processed" in line for line in system.said))

    def test_a_failed_backup_shows_what_was_done_and_the_error(self):
        out = "ROW|1|2dm.dk|78920|1.3\nMsg 3201, Level 16, State 1, Server x, Line 30\nCannot open backup device '/var/opt/mssql/backup/b.bak'.\n"
        code, system = self.run_sql("backup-everything", out, code=1)
        self.assertEqual(code, 1)
        text = "\n".join(system.said)
        self.assertIn("| 1  | 2dm.dk", text)
        self.assertIn("Cannot open backup device", text)
        self.assertNotIn("Backup of everything done!", text)

    def test_the_log_shrink_as_a_table(self):
        out = "7|2|128|128|128|128\nROW|1|2dm.dk|524288|1024|1\nROW|2|kiibee.dk|1024|1024|0\n"
        code, system = self.run_sql("shrink-logs", out)
        self.assertEqual(code, 0, system.said)
        self.assertEqual(system.said[1].splitlines(), [
            "| Nr | Database  | Log before | Log after | Shrunk              |",
            "|----|-----------|------------|-----------|---------------------|",
            "| 1  | 2dm.dk    |   512.0 MB |    1.0 MB | yes                 |",
            "| 2  | kiibee.dk |     1.0 MB |    1.0 MB | no, at its smallest |"])
        self.assertTrue(system.said[2].startswith("All logs now shrunk! ----- "))

    def test_a_log_shrink_that_could_not_run_says_so(self):
        code, system = self.run_sql("shrink-logs", "", code=127)
        self.assertEqual(code, 127)
        self.assertFalse(any(line.startswith("All logs now shrunk!") for line in system.said))


if __name__ == "__main__":
    unittest.main()


class CrontabTests(Server):
    ACME = '28 4,10,16,22 * * * "/etc/acmesh"/acme.sh --cron --home "/etc/acmesh" > /dev/null'

    def test_the_lines_are_read_as_jobs_and_what_else_is_kept(self):
        self.crontab = "MAILTO=\"\"\n# made by hand\n" + tools.JOBS["backup_everything"] + "\n" + self.ACME + "\n@reboot /usr/local/bin/x\n"
        entries = tools.crontab_entries(self.system())
        jobs = [entry for entry in entries if entry["kind"] == "job"]
        self.assertEqual([(job["schedule"], job["harbor"]) for job in jobs],
                         [("15 */4 * * *", "backup_everything"), ("28 4,10,16,22 * * *", ""), ("@reboot", "")])
        self.assertEqual(jobs[1]["command"], '"/etc/acmesh"/acme.sh --cron --home "/etc/acmesh" > /dev/null')
        self.assertEqual([entry["line"] for entry in entries if entry["kind"] == "other"], ['MAILTO=""', "# made by hand"])

    def test_saved_as_given_with_the_other_lines_kept(self):
        self.crontab = "MAILTO=\"\"\n" + self.ACME + "\n"
        entries = tools.crontab_entries(self.system())
        entries[1]["schedule"] = "0  3 * * *"
        entries.append({"kind": "job", "schedule": "@daily", "command": "/usr/local/bin/harbor sql backup-everything"})
        system = self.system()
        self.assertEqual(main(["server", "crontab", "save"], system) if False else tools.crontab_save(system, entries), None)
        self.assertEqual(self.crontab.splitlines(), ['MAILTO=""', '0 3 * * * "/etc/acmesh"/acme.sh --cron --home "/etc/acmesh" > /dev/null',
                                                     "@daily /usr/local/bin/harbor sql backup-everything"])

    def test_what_is_refused(self):
        for entry in ({"kind": "job", "schedule": "every day", "command": "x"}, {"kind": "job", "schedule": "* * * *", "command": "x"},
                      {"kind": "job", "schedule": "0 3 * * *", "command": ""}, {"kind": "job", "schedule": "0 3 * * *", "command": "x\nrm -rf /"},
                      {"kind": "other", "line": "0 3 * * * a job dressed as another line"}):
            with self.subTest(entry=entry):
                self.crontab = "1 1 * * * kept\n"
                with self.assertRaises(tools.Stop):
                    tools.crontab_save(self.system(), [entry])
                self.assertEqual(self.crontab, "1 1 * * * kept\n", "nothing is written")


class DotnetRemoveNewestTests(Server):
    def test_the_newest_goes_when_unused_and_its_host_stays(self):
        dotnet = self.root / "usr/lib/dotnet"
        for folder in ("sdk/8.0.404", "sdk/10.0.103", "shared/Microsoft.AspNetCore.App/8.0.11", "shared/Microsoft.AspNetCore.App/10.0.3",
                       "host/fxr/8.0.11", "host/fxr/10.0.3"):
            (dotnet / folder).mkdir(parents=True, exist_ok=True)
        (dotnet / "dotnet").write_text("")
        system = self.system(**{"/usr/lib/dotnet/dotnet --list-sdks": SDKS, "/usr/lib/dotnet/dotnet --list-runtimes": RUNTIMES})
        self.assertEqual(main(["server", "dotnet", "remove", "10.0"], system), 0, system.said)
        self.assertFalse((dotnet / "sdk/10.0.103").exists() or (dotnet / "shared/Microsoft.AspNetCore.App/10.0.3").exists())
        self.assertTrue((dotnet / "host/fxr/10.0.3").is_dir() and (dotnet / "host/fxr/8.0.11").is_dir())
        self.assertIn("Its host 10.0.3 stays", system.said[-1])

    def test_not_the_only_one(self):
        (self.root / "usr/lib/dotnet").mkdir(parents=True)
        (self.root / "usr/lib/dotnet/dotnet").write_text("")
        system = self.system(**{"/usr/lib/dotnet/dotnet --list-sdks": "8.0.404 [x]\n",
                                "/usr/lib/dotnet/dotnet --list-runtimes": "Microsoft.AspNetCore.App 8.0.11 [x]\n"})
        self.assertEqual(main(["server", "dotnet", "remove", "8.0"], system), 1)
        self.assertIn("the only version here", system.said[-1])


class DotnetRemoveTests(Server):
    def make(self):
        dotnet = self.root / "usr/lib/dotnet"
        for folder in ("sdk/8.0.404", "sdk/10.0.103", "shared/Microsoft.NETCore.App/8.0.11", "shared/Microsoft.AspNetCore.App/8.0.11",
                       "shared/Microsoft.AspNetCore.App/10.0.3", "host/fxr/8.0.11", "host/fxr/10.0.3", "packs/Microsoft.NETCore.App.Ref/8.0.11"):
            (dotnet / folder).mkdir(parents=True, exist_ok=True)
        (dotnet / "dotnet").write_text("")
        (self.root / "var/www/shop.dk").mkdir(parents=True, exist_ok=True)
        return self.system(**{"/usr/lib/dotnet/dotnet --list-sdks": SDKS, "/usr/lib/dotnet/dotnet --list-runtimes": RUNTIMES})

    def test_a_channel_goes_and_the_others_stay(self):
        system = self.make()
        self.assertEqual(main(["server", "dotnet", "remove", "8.0"], system), 0, system.said)
        dotnet = self.root / "usr/lib/dotnet"
        self.assertEqual(sorted(path.name for path in dotnet.rglob("8.0.*")), [])
        self.assertTrue((dotnet / "sdk/10.0.103").is_dir())
        self.assertTrue((dotnet / "host/fxr/10.0.3").is_dir())

    def test_not_the_newest_nor_one_a_site_uses(self):
        system = self.make()
        (self.root / "var/www/shop.dk/Shop.csproj").write_text("<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>")
        self.assertEqual(main(["server", "dotnet", "remove", "10.0"], system), 1)
        self.assertIn("used by shop.dk", system.said[-1])
        (self.root / "var/www/shop.dk/Shop.csproj").unlink()
        for name, text in (("Shop.csproj", "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"),
                           ("global.json", '{"sdk":{"version":"8.0.100"}}')):
            with self.subTest(name=name):
                for path in (self.root / "var/www/shop.dk").iterdir():
                    path.unlink()
                (self.root / "var/www/shop.dk" / name).write_text(text)
                system = self.make()
                self.assertEqual(main(["server", "dotnet", "remove", "8.0"], system), 1)
                self.assertIn("used by shop.dk", system.said[-1])
                self.assertTrue((self.root / "usr/lib/dotnet/sdk/8.0.404").is_dir())


class CrontabLogTests(Server):
    def test_runs_and_the_end_of_the_output(self):
        command = "/root/scripts/clean.sh >> /var/log/clean.log 2>&1"
        journal = ("2026-11-17T03:00:01+01:00 HOST CRON[11]: (root) CMD (/root/scripts/clean.sh >> /var/log/clean.log 2>&1)\n"
                   "2026-11-17T03:00:01+01:00 HOST CRON[12]: (root) CMD (/root/scripts/clean.sh >> /var/log/clean.log 2>&1 && echo more)\n"
                   "2026-11-18T03:00:01+01:00 HOST CRON[13]: (root) CMD (/root/scripts/clean.sh >> /var/log/clean.log 2>&1)\n")
        (self.root / "var/log").mkdir(parents=True)
        (self.root / "var/log/clean.log").write_text("cleaned 4 files\nrm: cannot remove '/tmp/x': Permission denied\ndone\n")
        found = tools.crontab_log(self.system(journalctl=journal), command)
        # Only this job's runs, the newest first
        self.assertEqual(found["runs"], ["2026-11-18T03:00:01+01:00", "2026-11-17T03:00:01+01:00"])
        self.assertEqual(found["output"]["where"], "/var/log/clean.log")
        self.assertEqual(found["output"]["lines"][-1], "done")
        self.assertEqual(found["errors"], ["rm: cannot remove '/tmp/x': Permission denied"])

    def test_where_the_output_goes(self):
        self.assertEqual(tools.job_output("x.py 2>&1 | /usr/bin/logger -t vns-keep-alive"), ("tag", "vns-keep-alive"))
        self.assertEqual(tools.job_output('x >> "/var/log/my.log"'), ("file", "/var/log/my.log"))
        for command in ("x > /dev/null 2>&1", "x 2>/tmp/err", "x"):
            self.assertIsNone(tools.job_output(command), command)


class OlderUbuntuTests(Server):
    def test_sql_server_from_the_packages_for_an_older_ubuntu_when_told(self):
        (self.root / "etc/os-release").write_text('PRETTY_NAME="Ubuntu 26.04 LTS"\nID=ubuntu\nVERSION_ID="26.04"\n')
        self.assertEqual(tools.status(self.system(**{"dpkg -s": Result(1)}), self.root / "none")["sql_server"]["packages_for"], "24.04")
        listing = lambda argv: Result(0) if "/24.04/mssql-server-2025" in argv[-1] else Result(22)  # noqa: E731
        answers = {"dpkg -s": Result(1), "curl -fsSI": listing, "curl -fsSL": "deb x", "apt-cache show libldap-2.5-0": Result(0)}
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        with mock.patch.object(server, "prepare_backup_permissions"), mock.patch.object(server, "install_tools"), \
                mock.patch.object(server, "SQL_DIR", self.root / "none"), mock.patch.dict(os.environ, {"HARBOR_SA_PASSWORD": "Secret-123"}):
            system = self.system(**answers)
            system.programs.add("gpg")
            self.assertEqual(main(["sql", "install-server", "--edition", "Developer", "--without-tools"], system), 1, "not without being told")
            system = self.system(**answers)
            system.programs.add("gpg")
            self.assertEqual(main(["sql", "install-server", "--edition", "Developer", "--without-tools", "--older-ubuntu"], system), 0, system.said)
        self.assertTrue((self.root / "etc/apt/sources.list.d/mssql-server-2025.list").is_file())
        self.assertIn("using the Ubuntu 24.04 repository", " ".join(system.said))


class DefaultPageTests(Server):
    def test_ubuntus_page_goes_and_a_page_of_ones_own_stays(self):
        from harbor.server.init import remove_default_page
        html = self.root / "var/www/html"
        html.mkdir(parents=True)
        (html / "index.html").write_text("<title>Apache2 Ubuntu Default Page: It works</title>")
        (self.root / "etc/apache2/sites-enabled").mkdir(parents=True)
        (self.root / "etc/apache2/sites-enabled/000-default.conf").write_text("")
        system = self.system()
        remove_default_page(system)
        self.assertFalse(html.exists())
        self.assertIn("a2dissite -q 000-default", system.commands)
        html.mkdir()
        (html / "index.html").write_text("<h1>Our own page</h1>")
        system = self.system()
        remove_default_page(system)
        self.assertTrue((html / "index.html").is_file())
        self.assertEqual(system.commands, [])


class SambaTests(Server):
    def answers(self, **more):
        return {"hostname -f": "linux-03.vns.dk", "dpkg -s samba": Result(0), "pdbedit -u root": Result(1, "", ""), **more}

    def test_the_server_is_shared_by_its_short_name_and_root_gets_the_password_given(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[global]\n   workgroup = WORKGROUP\n")
        system = RecordingSystem(root=self.root, answers=self.answers(**{"dpkg -s samba": Result(1)}))
        with mock.patch.dict(os.environ, {"HARBOR_SAMBA_PASSWORD": "secret"}):
            samba.share_over_samba(system)
        text = conf.read_text()
        self.assertTrue(text.startswith("[global]\n   workgroup = WORKGROUP\n"))
        self.assertIn("\n[linux-03]\n   path = /\n", text)
        self.assertIn("force user = root", text)
        self.assertIn("apt-get install -y samba", system.commands)
        self.assertEqual(system.inputs[system.commands.index("smbpasswd -a -s root")], "secret\nsecret\n")
        self.assertIn("systemctl enable --now smbd nmbd", system.commands)
        self.assertIn("systemctl reload smbd", system.commands)

    def test_in_a_terminal_root_is_asked_for_a_password(self):
        from harbor.server import samba
        system = RecordingSystem(root=self.root, answers=self.answers(), replies=["secret", "secret"])
        samba.share_over_samba(system)
        self.assertEqual(system.inputs[system.commands.index("smbpasswd -a -s root")], "secret\nsecret\n")

    def test_again_it_changes_nothing_and_without_a_password_it_only_says_how(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[linux-03]\n   path = /srv\n")
        system = RecordingSystem(root=self.root, answers=self.answers())
        samba.share_over_samba(system)
        self.assertEqual(conf.read_text(), "[linux-03]\n   path = /srv\n")
        self.assertFalse(any(line.startswith("smbpasswd") for line in system.commands))
        self.assertIn("Root has no Samba password yet: set it with smbpasswd -a root", system.said)
        self.assertNotIn("systemctl reload smbd", system.commands)

    def test_stop_sharing_takes_out_the_share_only(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[global]\n   workgroup = WORKGROUP\n\n[linux-03]\n   path = /\n   force user = root\n\n[other]\n   path = /srv\n")
        system = RecordingSystem(root=self.root, answers=self.answers())
        samba.stop_sharing(system)
        self.assertEqual(conf.read_text(), "[global]\n   workgroup = WORKGROUP\n\n[other]\n   path = /srv\n")
        self.assertIn("systemctl disable --now smbd nmbd", system.commands)
        self.assertFalse(samba.has_share(system))

    def test_an_older_share_of_the_whole_server_counts_by_its_path(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[global]\n   workgroup = WORKGROUP\n\n[printers]\n   path = /var/tmp\n\n[kikayo/]\n   path = /\n   force user = root\n")
        system = RecordingSystem(root=self.root, answers=self.answers())
        self.assertEqual(samba.root_share(system), "kikayo/")
        samba.share_over_samba(system)
        self.assertNotIn("[linux-03]", conf.read_text())
        samba.stop_sharing(system)
        self.assertEqual(conf.read_text(), "[global]\n   workgroup = WORKGROUP\n\n[printers]\n   path = /var/tmp\n")

    def test_shares_are_listed_added_changed_and_deleted(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        (self.root / "var/www").mkdir(parents=True)
        (self.root / "srv/files").mkdir(parents=True)
        conf.write_text("[global]\n   workgroup = WORKGROUP\n\n[printers]\n   path = /var/tmp\n\n[kikayo/]\n   path = /\n   force user = root\n   create mask = 0644\n")
        system = RecordingSystem(root=self.root, answers=self.answers(**{"testparm -s": Result(0)}))
        self.assertEqual(samba.shares(system), [{"name": "kikayo/", "path": "/", "read_only": True, "browseable": True, "guest_ok": False,
                                                 "force_user": "root", "valid_users": "", "comment": ""}])
        samba.save_share(system, {"name": "www", "path": "/var/www", "force_user": "www-data"})
        self.assertEqual([share["name"] for share in samba.shares(system)], ["kikayo/", "www"])
        self.assertIn("systemctl reload smbd", system.commands)
        # Changed: renamed, read and write; a line Server tools does not show stays
        samba.save_share(system, {"name": "root", "path": "/", "force_user": "root", "read_only": False}, "kikayo/")
        text = conf.read_text()
        self.assertNotIn("[kikayo/]", text)
        self.assertIn("[root]\n   path = /\n   force user = root\n   create mask = 0644\n   read only = no\n", text)
        self.assertIn("[www]\n   path = /var/www\n", text)
        samba.delete_share(system, "www")
        self.assertEqual([share["name"] for share in samba.shares(system)], ["root"])
        self.assertTrue(text.startswith("[global]\n   workgroup = WORKGROUP\n\n[printers]\n"))
        # Refused: no folder, a name there already, a section that is not a share
        for share, old in (({"name": "x", "path": "/nowhere"}, ""), ({"name": "ROOT", "path": "/srv/files"}, ""), ({"name": "global", "path": "/srv/files"}, "")):
            with self.assertRaises(Exception):
                samba.save_share(system, share, old)
        with self.assertRaises(Exception):
            samba.delete_share(system, "printers")

    def test_a_config_samba_refuses_is_taken_back(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[global]\n")
        (self.root / "srv").mkdir()
        system = RecordingSystem(root=self.root, answers=self.answers(**{"testparm -s": Result(1, "", "Unknown parameter")}))
        with self.assertRaises(Exception):
            samba.save_share(system, {"name": "files", "path": "/srv"})
        self.assertEqual(conf.read_text(), "[global]\n")

    def test_a_user_is_added_for_samba_alone_and_reaches_nothing_until_named(self):
        from harbor.server import samba
        conf = self.root / "etc/samba/smb.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("[global]\n   workgroup = WORKGROUP\n\n[kikayo/]\n   path = /\n   force user = root\n\n[www]\n   path = /var/www\n   valid users = root web\n")
        logins = {"value": "root:0:\n"}
        system = RecordingSystem(root=self.root, answers=self.answers(**{
            "testparm -s": Result(0), "id -u web": Result(1, "", "no such user"),
            "pdbedit -L": lambda argv: logins["value"],
            "smbpasswd -a -s web": lambda argv: logins.update(value="root:0:\nweb:998:\n") or ""}))
        system.programs.add("pdbedit")
        with mock.patch.dict(os.environ, {"HARBOR_SAMBA_PASSWORD": "webpass12"}):
            samba.add_user(system, "web")
        self.assertIn("useradd --system --no-create-home --shell /usr/sbin/nologin web", system.commands)
        self.assertEqual(system.inputs[system.commands.index("smbpasswd -a -s web")], "webpass12\nwebpass12\n")
        # The share any login could reach is root's alone now; the one that names its users stays
        self.assertIn("[kikayo/]\n   path = /\n   force user = root\n   valid users = root\n", conf.read_text())
        self.assertIn("[www]\n   path = /var/www\n   valid users = root web\n", conf.read_text())
        self.assertEqual(samba.users(system), [{"name": "root", "samba_only": False}, {"name": "web", "samba_only": True}])
        # Deleted: its login and the Linux user Harbor made
        samba.delete_user(system, "web")
        self.assertIn("smbpasswd -x web", system.commands)
        self.assertIn("userdel web", system.commands)
        for name in ("root", "Bad Name"):
            with self.assertRaises(Exception), mock.patch.dict(os.environ, {"HARBOR_SAMBA_PASSWORD": "webpass12"}):
                samba.add_user(system, name)
        with self.assertRaises(Exception):
            samba.delete_user(system, "root")

    def test_init_asks_nothing_of_samba(self):
        from harbor.server import init
        self.assertFalse(hasattr(init, "wants_samba") or hasattr(init, "share_over_samba"))



class SshRootLoginTests(Server):
    def test_root_login_is_set_in_harbors_own_file(self):
        from harbor.server import ssh
        modes = iter(["permitrootlogin prohibit-password\npasswordauthentication yes"])
        system = RecordingSystem(root=self.root, answers={"sshd -t": Result(0), "sshd -T": lambda argv: next(modes)})
        ssh.set_root_login(system, "prohibit-password")
        self.assertEqual((self.root / "etc/ssh/sshd_config.d/01-harbor-root-login.conf").read_text().splitlines()[-1], "PermitRootLogin prohibit-password")
        self.assertIn("systemctl try-reload-or-restart ssh", system.commands)
        self.assertEqual(system.said[-1], "Root may log in over SSH with a key only.")

    def test_a_change_sshd_refuses_is_taken_back(self):
        from harbor.server import ssh
        dropin = self.root / "etc/ssh/sshd_config.d/01-harbor-root-login.conf"
        dropin.parent.mkdir(parents=True)
        dropin.write_text("PermitRootLogin yes\n")
        system = RecordingSystem(root=self.root, answers={"sshd -t": Result(1, "", "bad option")})
        self.assertEqual(main(["server", "ssh-root", "no"], system), 1)
        self.assertEqual(dropin.read_text(), "PermitRootLogin yes\n")
        self.assertNotIn("systemctl try-reload-or-restart ssh", system.commands)

    def test_how_root_may_log_in_now(self):
        from harbor.server import ssh
        for said, expected in (("permitrootlogin yes", "yes"), ("permitrootlogin without-password", "prohibit-password"), ("", "")):
            system = RecordingSystem(root=self.root, answers={"sshd -T": said})
            self.assertEqual(ssh.root_login(system), expected)
        with self.assertRaises(Exception):
            ssh.set_root_login(RecordingSystem(root=self.root), "maybe")
