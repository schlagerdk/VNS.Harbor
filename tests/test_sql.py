from unittest import mock
"""harbor sql against the scripts in sql/ (frozen in tests/legacy/sql).

The database tools run against a fake sqlcmd on both sides: what is compared is the SQL it is given (rendered from
templates/sql), how it is called, and the backup folder's permissions. The script passed the sa password with -P; harbor
passes it in SQLCMDPASSWORD. The wrappers keep the password where set_sa_password.sh and update.sh find it.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.sql import server  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake  # noqa: E402

LEGACY = ROOT / "tests/legacy/sql"
# /vns/sql as VNS 1.3.0 left it on the servers: the wrappers that hold the sa password, and the custom scripts
VNS_SQL = ROOT / "tests/legacy/vns-sql"
PASSWORD = "s3cr'et pw"
WRAPPED = ("create_db.sh", "copy_db.sh", "restore_db.sh", "drop_db.sh", "list_dbs.sh", "cron/backup_everything.sh", "cron/shrink_logs.sh")
# Custom scripts for some servers, kept as they are outside harbor; they hold the password the old way
CUSTOM = ("clean_umbraco7_db.sh", "cron/delete_old_2dm_data.sh")


def quoted(password):
    return "'" + password.replace("'", "'\\''") + "'"


@NEEDS_BASH_4
class PasswordTests(unittest.TestCase):
    """The sa password stays where the scripts kept it, so set_sa_password.sh and updates go on working"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def copy(self, name):
        target = self.folder / name
        shutil.copytree(VNS_SQL, target)
        shutil.copytree(ROOT / "templates/sql", target / "scripts")
        return target

    def test_the_password_set_the_old_way_reaches_sqlcmd(self):
        sql = self.copy("sql")
        # As update.sh and install_server.sh set it, with the shell helper that stays in sql/lib
        subprocess.run(["bash", "-c", f'source "{sql}/lib/sa_password.sh"; sa_apply_quoted "{sql}" "$1"', "_", quoted(PASSWORD)], check=True)
        for name in WRAPPED:
            with self.subTest(name=name):
                line = next(line for line in (sql / name).read_text().splitlines() if line.startswith("sa "))
                shown = subprocess.run(["bash", "-c", f'sa() {{ export SQLCMDPASSWORD=$4; }}\n{line}\nprintf %s "$SQLCMDPASSWORD"'],
                                       capture_output=True, text=True, check=True).stdout
                self.assertEqual(shown, PASSWORD)
        # update.sh reads it back from drop_db.sh, to set it again after an update
        read = subprocess.run(["sed", "-n", r"s/.* -U sa -P \(.*\) -i scripts\/drop_db.sql$/\1/p", str(sql / "drop_db.sh")],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(read, quoted(PASSWORD))

    def test_harbor_sets_it_as_the_shell_helper_does(self):
        old, new = self.copy("old"), self.copy("new")
        subprocess.run(["bash", "-c", f'source "{old}/lib/sa_password.sh"; SA_PASSWORD="$1"; sa_apply "{old}"', "_", PASSWORD], check=True)
        server.sa_apply(PASSWORD, new)
        for path in sorted(old.rglob("*.sh")):
            self.assertEqual((new / path.relative_to(old)).read_text(), path.read_text(), path.name)
        # The scripts that hold it are found the same way
        found = subprocess.run(["bash", "-c", f'source "{old}/lib/sa_password.sh"; sa_scripts "{old}"'], capture_output=True, text=True).stdout
        self.assertEqual(sorted(str(path.relative_to(new)) for path in server.sa_scripts(None, new)),
                         sorted(str(Path(line).relative_to(old)) for line in found.splitlines()))
        self.assertEqual(sorted(str(path.relative_to(new)) for path in server.sa_scripts(None, new)), sorted(WRAPPED + CUSTOM))

    def test_set_sa_password_without_sqlcmd(self):
        new = self.copy("new")
        # harbor works on the sql folder it is installed beside: here it is given a copy
        system = RecordingSystem(root=self.folder, replies=[PASSWORD, PASSWORD])
        server.set_sa_password(system, new)
        self.assertIn("and in 9 script(s)", system.said[-1])
        for name in WRAPPED:
            self.assertIn(f"-U sa -P {quoted(PASSWORD)} -i", (new / name).read_text())
            self.assertEqual((new / name).stat().st_mode & 0o007, 0)


@NEEDS_BASH_4
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "legacy/var/opt/mssql/backup").mkdir(parents=True)
        (self.folder / "harbor/var/opt/mssql/backup").mkdir(parents=True)
        for side in ("legacy", "harbor"):
            (self.folder / side / "var/opt/mssql/backup/shop.bak").write_text("backup")
        # The scripts as they were, with the password set and their paths in the fake server
        self.sql = self.folder / "sql"
        shutil.copytree(LEGACY, self.sql)
        # Its helpers and SQL as they are now (the sql folder of VNS 1.x had them in lib/ and scripts/)
        shutil.copy(ROOT / "lib/backup_files.py", self.sql / "lib/backup_files.py")
        shutil.copy(ROOT / "lib/sql_admin.py", self.sql / "lib/admin.py")
        shutil.copytree(ROOT / "templates/sql", self.sql / "scripts")
        self.fake = Fake(self.folder, variables=("SQLCMDPASSWORD",))
        self.sent = self.folder / "sent.sql"
        self.fake.program("sqlcmd", f'while (($#)); do [[ $1 == -i ]] && cat "$2" >>"{self.sent}"; shift; done\n'
                                    'echo "name|state"; echo "----|-----"; echo "master|ONLINE"; echo "shop|ONLINE"\n')
        for name in ("clear", "chown", "chmod"):
            self.fake.program(name)
        self.fake.program("sudo", 'exec "$@"\n')
        for path in [*self.sql.rglob("*.sh")]:
            text = path.read_text().replace("/opt/mssql-tools18/bin/sqlcmd", str(self.fake.bin / "sqlcmd"))
            text = re.sub(r"(?m)^\[\[ \$EUID -eq 0 \]\] \|\| exec sudo .*$", "", text)
            text = text.replace("/var/opt/mssql", f"{self.folder}/legacy/var/opt/mssql").replace("'@SA_PASSWORD@'", quoted(PASSWORD))
            path.write_text(text)

    def legacy(self, script, *args, stdin=""):
        self.fake.log.write_text("")
        self.sent.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(self.sql / script), *args], input=stdin, capture_output=True, text=True, env=self.fake.env())
        calls = [line.replace(f"{self.folder}/legacy", "").replace(str(self.fake.bin) + "/", "") for line in self.fake.calls()
                 if not line.startswith(("clear", "sudo "))]
        return result, calls, self.sent.read_text() if self.sent.exists() else ""

    def harbor(self, argv, replies=()):
        sent = []

        def sqlcmd(argv):
            sql = argv[argv.index("-i") + 1]
            sent.append(Path(sql).read_text())
            return Result(0, "name|state\n----|-----\nmaster|ONLINE\nshop|ONLINE\n")
        os.environ["SQLCMDPASSWORD"] = PASSWORD
        try:
            system = RecordingSystem(root=self.folder / "harbor", answers={"/opt/mssql-tools18/bin/sqlcmd": sqlcmd}, replies=list(replies))
            code = main(["sql", *argv], system)
        finally:
            del os.environ["SQLCMDPASSWORD"]
        return code, system, "".join(sent)

    def compare(self, script, argv, args=(), replies=()):
        result, calls, sent = self.legacy(script, *args, stdin="".join(f"{reply}\n" for reply in replies))
        code, system, harbor_sent = self.harbor([*argv, *args], replies)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(harbor_sent, sent)
        # sqlcmd as the script called it, with the password in its environment instead of -P
        legacy_calls = [line.replace(f" -P {quoted(PASSWORD).replace(chr(39), '')}", "").replace(" -P s3cr\\'et\\ pw", "") for line in calls]
        harbor_calls = [re.sub(r" -i \S+", " -i SQL", command.replace("/opt/mssql-tools18/bin/", "")) for command in system.commands]
        legacy_calls = [re.sub(r" -i \S+", " -i SQL", line.rsplit(" |", 1)[0]) for line in legacy_calls]
        self.assertEqual(harbor_calls, legacy_calls)
        self.assertTrue(all(env.get("SQLCMDPASSWORD") == PASSWORD for command, env in zip(system.commands, system.environments) if "sqlcmd" in command))
        return result, system

    def test_create_copy_and_restore_with_a_new_login(self):
        login = ["Login p'w 1", "Login p'w 1"]
        for script, argv, args in (("create_db.sh", ["create"], ["shop"]), ("copy_db.sh", ["copy"], ["shop", "shop2"]),
                                   ("restore_db.sh", ["restore"], ["shop"])):
            with self.subTest(script=script):
                result, system = self.compare(script, argv, args, replies=login)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_login_password_is_asked_until_it_will_do(self):
        result, system = self.compare("create_db.sh", ["create"], ["shop"], replies=["short", "short", "Login pw 1", "other", "Login pw 1", "Login pw 1"])
        self.assertIn("Password must be at least 8 characters", system.said)
        self.assertIn("Passwords do not match", system.said)

    def test_drop_and_clean(self):
        for args in (["shop", "y"], ["shop", "n"]):
            with self.subTest(args=args):
                (self.folder / "legacy/var/opt/mssql/backup/shop.bak").write_text("backup")
                (self.folder / "harbor/var/opt/mssql/backup/shop.bak").write_text("backup")
                result, system = self.compare("drop_db.sh", ["drop"], args)
                self.assertEqual((self.folder / "harbor/var/opt/mssql/backup/shop.bak").exists(), args[1] == "n")

    def test_names_that_are_not_given_are_asked_for(self):
        result, system = self.compare("create_db.sh", ["create"], replies=["shop", "Login pw 1", "Login pw 1"])
        self.assertEqual(result.returncode, 0, result.stderr)
        result, system = self.compare("create_db.sh", ["create"], replies=[""])
        self.assertEqual((result.returncode, system.said[-1]), (1, "No database name given"))

    def test_list_and_the_cron_jobs(self):
        result, calls, sent = self.legacy("list_dbs.sh")
        code, system, harbor_sent = self.harbor(["list-databases"])
        self.assertEqual(system.said[-1], result.stdout.rstrip("\n"))
        for script, command in (("cron/shrink_logs.sh", "shrink-logs"), ("cron/backup_everything.sh", "backup-everything")):
            with self.subTest(script=script):
                result, system = self.compare(script, [command])
                self.assertEqual(result.returncode, 0, result.stderr)
                stamp = re.compile(r"\d\d\.\d\d\.\d{4} \d\d:\d\d")
                self.assertEqual([stamp.sub("TIME", line) for line in system.said if line.startswith(("-----", "Backup", "All"))],
                                 [stamp.sub("TIME", line) for line in result.stdout.splitlines() if line.startswith(("-----", "Backup", "All"))])

    def test_without_the_password_set(self):
        system = RecordingSystem(root=self.folder / "harbor")
        self.assertEqual(main(["sql", "shrink-logs"], system), 1)
        self.assertIn("harbor sql set-sa-password", system.said[-1])
        self.assertEqual([command for command in system.commands if "sqlcmd" in command], [])


class ServerToolTests(unittest.TestCase):
    def test_tools_are_removed_only_when_confirmed(self):
        system = RecordingSystem(root=tempfile.mkdtemp(), answers={"dpkg -s mssql-tools": Result(1)}, replies=["n"])
        self.assertEqual(main(["sql", "uninstall-sql-tools"], system), 0)
        self.assertEqual(system.said[-1], "SQL tools kept")
        system = RecordingSystem(root=tempfile.mkdtemp(), answers={"dpkg -s mssql-tools": Result(1)}, replies=["y"])
        main(["sql", "uninstall-sql-tools"], system)
        self.assertIn("apt-get remove --purge -y mssql-tools18", system.commands)

    def test_an_update_without_one_available(self):
        system = RecordingSystem(root=tempfile.mkdtemp(), answers={"dpkg-query": "16.0.4135.4", "apt list": "Listing...\n"})
        self.assertEqual(main(["sql", "update-server"], system), 0)
        self.assertIn("SQL Server is already up to date", system.said)
        self.assertNotIn("systemctl restart mssql-server", system.commands)

    def test_an_update_waits_for_the_upgrade_and_does_not_restart(self):
        root = Path(tempfile.mkdtemp())
        (root / "opt/mssql-tools18/bin").mkdir(parents=True)
        (root / "opt/mssql-tools18/bin/sqlcmd").write_text("")
        tries = iter([Result(1, "", "Server is in script upgrade mode"), Result(1), Result(0)])
        system = RecordingSystem(root=root, answers={"dpkg-query": "17.0.5065.9-3", "apt list": "Listing...\nmssql-server/noble 17.0.5065.9-3 amd64 [upgradable]\n",
                                                    "systemctl is-active mssql-server": "active", "/opt/mssql-tools18/bin/sqlcmd": lambda argv: next(tries)},
                                 replies=["y"])
        with mock.patch.dict(os.environ, {"SQLCMDPASSWORD": "pw"}):
            self.assertEqual(main(["sql", "update-server"], system), 0, system.said)
        self.assertNotIn("systemctl restart mssql-server", system.commands)
        self.assertNotIn("systemctl start mssql-server", system.commands, "the package started it")
        self.assertEqual(sum(command.startswith("/opt/mssql-tools18/bin/sqlcmd") for command in system.commands), 3)
        self.assertIn("SQL Server has upgraded its databases and lets logins in.", system.said)

    def test_install_tools_needs_ubuntu(self):
        root = Path(tempfile.mkdtemp())
        (root / "etc").mkdir()
        (root / "etc/os-release").write_text('ID=debian\nVERSION_ID="12"\n')
        system = RecordingSystem(root=root)
        self.assertEqual(main(["sql", "install-sql-tools"], system), 1)
        self.assertEqual(system.said[-1], "This script supports Ubuntu only (found: debian).")


if __name__ == "__main__":
    unittest.main()
