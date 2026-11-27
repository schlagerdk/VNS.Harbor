"""PostgreSQL here (phase 10): its databases in /var/opt/postgresql, as Microsoft SQL Server's in /var/opt/mssql"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.sql import postgres  # noqa: E402


class DataTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_ubuntu_makes_the_clusters_in_var_opt(self):
        config = self.root / "etc/postgresql-common/createcluster.conf"
        config.parent.mkdir(parents=True)
        config.write_text("# Ubuntu's\ncreate_main_cluster = true\n#data_directory = '/var/lib/postgresql/%v/%c'\n")
        system = RecordingSystem(root=self.root)
        postgres.data_here(system)
        text = config.read_text()
        self.assertIn("create_main_cluster = true", text)
        # One data_directory in force (Ubuntu's commented example stays as it was)
        self.assertEqual([line for line in text.splitlines() if line.strip().startswith("data_directory")],
                         ["data_directory = '/var/opt/postgresql/%v/%c'"])
        # Once: run again, it stays as it is
        postgres.data_here(system)
        self.assertEqual(config.read_text(), text)
        self.assertTrue((self.root / "var/opt/postgresql").is_dir())

    def test_an_empty_cluster_elsewhere_is_made_again_one_with_databases_is_left(self):
        (self.root / "etc/postgresql/18/main").mkdir(parents=True)
        query = "runuser -u postgres -- psql -X -At -d postgres -c"
        system = RecordingSystem(root=self.root, answers={query: "UTF8\n0\n/var/lib/postgresql/18/main"})
        postgres.made_again(system)
        self.assertIn("pg_dropcluster --stop 18 main", system.commands)
        self.assertIn("pg_createcluster --encoding=UTF8 --locale=C.UTF-8 --datadir /var/opt/postgresql/18/main --start 18 main", system.commands)
        system = RecordingSystem(root=self.root, answers={query: "UTF8\n2\n/var/lib/postgresql/18/main"})
        postgres.made_again(system)
        self.assertFalse(any(command.startswith("pg_dropcluster") for command in system.commands))
        system = RecordingSystem(root=self.root, answers={query: "UTF8\n0\n/var/opt/postgresql/18/main"})
        postgres.made_again(system)
        self.assertFalse(any(command.startswith("pg_dropcluster") for command in system.commands))


class UninstallTests(unittest.TestCase):
    def test_harbors_settings_go_with_the_databases(self):
        """dpkg left /etc/postgresql, as Harbor's conf.d/harbor.conf is no package's file (1.0.82)"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "etc/postgresql/18/main/conf.d").mkdir(parents=True)
            (root / "etc/postgresql/18/main/conf.d/harbor.conf").write_text("listen_addresses = '*'\n")
            (root / "var/opt/postgresql/18/main").mkdir(parents=True)
            system = RecordingSystem(root=root, answers={"dpkg -s postgresql": "Status: install ok installed"})
            postgres.uninstall(system, delete_data=True)
            self.assertFalse((root / "etc/postgresql").exists())
            self.assertFalse((root / "var/opt/postgresql").exists())
            self.assertIn("apt-get remove --purge -y postgresql postgresql-18", system.commands)

    def test_its_backup_job_goes_with_it(self):
        """Removed, PostgreSQL's backup job stayed in root's crontab, failing every four hours (and mailed where it was
        watched): it goes, with its label and watch lines; Microsoft SQL Server's jobs stay"""
        table = ("# Harbor label: Microsoft SQL Server backup\n15 */4 * * * /usr/local/bin/harbor sql backup-everything >> /var/log/sql_backup_everything.log 2>&1\n"
                 "# Harbor label: PostgreSQL backup\n30 */4 * * * /usr/local/bin/harbor postgres backup-everything >> /var/log/postgres_backup_everything.log 2>&1\n")
        with tempfile.TemporaryDirectory() as folder:
            system = RecordingSystem(root=Path(folder), answers={"dpkg -s postgresql": "Status: install ok installed", "crontab -l": table})
            postgres.uninstall(system, delete_data=False)
        written = next(text for command, text in zip(system.commands, system.inputs) if command == "crontab -")
        self.assertNotIn("postgres", written)
        self.assertIn("harbor sql backup-everything", written)
        self.assertIn("Microsoft SQL Server backup", written)

    def test_kept_databases_keep_their_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "etc/postgresql/18/main/conf.d").mkdir(parents=True)
            (root / "etc/postgresql/18/main/conf.d/harbor.conf").write_text("listen_addresses = '*'\n")
            system = RecordingSystem(root=root, answers={"dpkg -s postgresql": "Status: install ok installed"})
            postgres.uninstall(system, delete_data=False)
            self.assertTrue((root / "etc/postgresql/18/main/conf.d/harbor.conf").exists())


class ConnectionTests(unittest.TestCase):
    def test_each_install_adds_its_connection_once(self):
        """Installing Microsoft SQL Server or PostgreSQL adds a connection to it in the admin, as its own login; again,
        the one there gets the password"""
        import json
        from unittest import mock
        from harbor.sql import server
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / "var/www/admin.example.com/app"
            app.mkdir(parents=True)
            system = RecordingSystem(root=root, answers={"hostname -s": "web-01"})
            (app / "sql-connections.json").write_text(json.dumps([{"id": "a", "name": "Local", "host": "localhost", "port": 1433, "username": "sa", "password": "old"}]))
            with mock.patch("harbor.install.admin.admin_sites", return_value=["admin.example.com"]), \
                    mock.patch("harbor.install.admin.web_root", return_value="/var/www"):
                server.add_admin_connection(system, "mssql", None, 1433, "sa", "first", "/var/opt/mssql/backup")
                server.add_admin_connection(system, "postgres", None, 5432, "postgres", "secret", "/var/opt/postgresql/backup")
                server.add_admin_connection(system, "mssql", None, 1433, "sa", "second", "/var/opt/mssql/backup")
            items = json.loads((app / "sql-connections.json").read_text())
            self.assertEqual([(item.get("engine", "mssql"), item["username"], item["password"]) for item in items],
                             [("mssql", "sa", "second"), ("postgres", "postgres", "secret")])
            # Named by the server, not Local: the one called Local before too
            self.assertEqual([item["name"] for item in items], ["web-01", "web-01"])
            self.assertEqual(items[1]["backup_directory"], "/var/opt/postgresql/backup")


if __name__ == "__main__":
    unittest.main()
