import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from flask import Flask, abort, jsonify, request
from werkzeug.exceptions import BadRequest, InternalServerError
from tests.admin.app_targets import add_target_helpers


ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT = ROOT_DIR


def frozen(path):
    """A script in sql/ as it was before it ran harbor (tests/legacy/sql): harbor is compared with it in tests/test_sql.py"""
    copy = ROOT / "tests/legacy" / path.relative_to(ROOT)
    return copy if copy.is_file() else path

spec = importlib.util.spec_from_file_location("sql_admin", ROOT / "lib/sql_admin.py")
sql = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sql)


class BackupAccessTests(unittest.TestCase):
    def test_local_and_ssh_preparation_use_specific_file(self):
        for local, ssh in ((True, False), (False, True)):
            settings = {"backup_directory": "/backup", "ssh_enabled": ssh}
            with patch.object(sql, "local_backup_access", return_value=local), \
                    patch.object(sql, "file_operation") as operation:
                sql.prepare_backup_access(settings, "/backup/site.bak", write=True)
                operation.assert_called_once_with(settings, "prepare_write", {"/backup/site.bak": []})

    def test_restore_prepares_read_access(self):
        with patch.object(sql, "local_backup_access", return_value=True), \
                patch.object(sql, "file_operation") as operation:
            sql.prepare_backup_access({"backup_directory": "/backup"}, "/backup/site.bak", write=False)
        self.assertEqual(operation.call_args.args[1], "prepare_read")

    def test_remote_permission_failure_stops_before_sql_backup(self):
        settings = {"backup_directory": "/backup", "ssh_enabled": True}
        with patch.object(sql, "local_backup_access", return_value=False), \
                patch.object(sql, "file_operation", side_effect=RuntimeError("sudo permission denied")), \
                patch.object(sql, "execute") as execute:
            with self.assertRaisesRegex(RuntimeError, "sudo permission denied"):
                sql.backup(settings, "site")
        execute.assert_not_called()


class SqlQueryTests(unittest.TestCase):
    settings = {"host": "db.test", "port": 1433, "username": "sa", "password": "secret", "trust_certificate": True}

    def test_script_runs_against_the_chosen_database_with_the_password_out_of_argv(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout="id|name\n1|x\n", stderr="")

        with patch.object(sql, "sqlcmd_path", return_value="/opt/sqlcmd"), patch.object(sql.subprocess, "run", fake_run):
            self.assertEqual(sql.run_query(self.settings, "Kunde DB", "SELECT 1"), "id|name\n1|x\n")
        args, kwargs = calls[0]
        self.assertEqual(args[args.index("-d") + 1], "Kunde DB")
        self.assertEqual(args[args.index("-s") + 1], sql.COLUMN_SEPARATOR)
        self.assertIn("-k1", args)
        self.assertNotIn("secret", " ".join(args))
        self.assertEqual(kwargs["env"]["SQLCMDPASSWORD"], "secret")
        self.assertEqual(kwargs["timeout"], 300)

    def test_tables_are_read_from_the_chosen_database(self):
        with patch.object(sql, "execute", return_value='vns_json\n--------\n[{"schema":"dbo","name":"umbracoNode","rows":3,"size_mb":0.1}]\n') as execute:
            self.assertEqual(sql.tables({}, "Kunde DB"), [{"schema": "dbo", "name": "umbracoNode", "rows": 3, "size_mb": 0.1}])
        self.assertEqual(execute.call_args.kwargs["database"], "Kunde DB")
        self.assertIn("sys.tables", execute.call_args.args[1])
        with self.assertRaises(ValueError):
            sql.tables({}, "")

    def test_results_are_split_into_tables_and_messages(self):
        s = sql.COLUMN_SEPARATOR
        output = (f"id{s}name{s}note\n--{s}----{s}----\n1{s}a | b{s}NULL\n2{s}x{s}\n\n"
                  "Done with the update\n\n"
                  f"total\n-----\n2\n\n"
                  f"empty{s}set\n-----{s}---\n")
        results, messages = sql.parse_results(output)
        self.assertEqual(results, [
            {"columns": ["id", "name", "note"], "rows": [["1", "a | b", "NULL"], ["2", "x", ""]]},
            {"columns": ["total"], "rows": [["2"]]},
            {"columns": ["empty", "set"], "rows": []}])
        self.assertEqual(messages, ["Done with the update"])

    def test_values_with_line_breaks_stay_in_their_row(self):
        s = sql.COLUMN_SEPARATOR
        output = f"id{s}text{s}date\n--{s}----{s}----\n1{s}<h2>A</h2>\n\n  more{s}2001\n2{s}b{s}2002\n"
        results, messages = sql.parse_results(output)
        self.assertEqual(results, [{"columns": ["id", "text", "date"],
                                    "rows": [["1", "<h2>A</h2>\n\n  more", "2001"], ["2", "b", "2002"]]}])
        self.assertEqual(messages, [])

    def test_a_block_with_uneven_rows_is_shown_as_text(self):
        s = sql.COLUMN_SEPARATOR
        results, messages = sql.parse_results(f"a{s}b\n-{s}-\n1{s}2{s}3\n")
        self.assertEqual(results, [])
        self.assertEqual(messages, ["a | b\n- | -\n1 | 2 | 3"])

    def test_invalid_input_is_refused_before_sqlcmd_runs(self):
        with patch.object(sql.subprocess, "run") as run:
            for database, query in (("", "SELECT 1"), ("a\nb", "SELECT 1"), ("db", ""), ("db", "   "), ("db", "x" * (1024 * 1024 + 1))):
                with self.subTest(database=database[:5], query=query[:5]), self.assertRaises(ValueError):
                    sql.run_query(self.settings, database, query)
            run.assert_not_called()

    def test_sqlcmd_never_reads_a_typed_line(self):
        """!! id in Query ran id as root on the admin server, and :r read a file: the typed script reaches sqlcmd only as
        hex, run by sp_executesql (1.0.70)"""
        script = "!! id\n:r /etc/passwd\nSELECT N'ø' AS x\ngo\nPRINT 1\nGO 3 -- three times"
        sealed = sql.sealed_script(script)
        for line in sealed.splitlines():
            self.assertFalse(line.lstrip().startswith(("!!", ":")), line)
        self.assertNotIn("passwd", sealed)
        self.assertEqual(sealed.count("EXEC sys.sp_executesql @vns_batch;"), 2)
        self.assertIn("\nGO 3", sealed)
        first = sealed.split("CAST(0x", 1)[1].split(" AS", 1)[0]
        self.assertEqual(bytes.fromhex(first).decode("utf-16-le"), "!! id\n:r /etc/passwd\nSELECT N'ø' AS x")
        self.assertEqual(sql.script_batches("SELECT 1\nGO\n\nGO\n"), [("SELECT 1", 1)])
        self.assertNotIn("EXECUTE AS", sealed)

    def test_a_user_who_is_not_an_administrator_is_kept_to_the_database(self):
        """Run queries ran as the connection's login, sa: a user reached every database and the server. Their scripts
        run as the database's dbo, which cannot revert; SQL Server's own and TRUSTWORTHY databases are refused (1.0.70)"""
        sent = []
        with patch.object(sql, "execute", side_effect=lambda settings, text, **kw: sent.append((text, kw)) or ""), \
                patch.object(sql, "query_json", return_value=[{"trustworthy": 0}]):
            sql.run_query(self.settings, "Shop", "SELECT 1", sandbox=True)
        self.assertTrue(sent[0][0].startswith("EXECUTE AS USER = N'dbo' WITH NO REVERT;\nGO\n"))
        self.assertEqual(sent[0][1]["database"], "Shop")
        with patch.object(sql, "execute") as execute:
            for database, trustworthy in (("master", 0), ("TempDB", 0), ("Shop", 1)):
                with patch.object(sql, "query_json", return_value=[{"trustworthy": trustworthy}]), self.assertRaises(ValueError):
                    sql.run_query(self.settings, database, "SELECT 1", sandbox=True)
            execute.assert_not_called()

    def test_errors_from_sql_server_are_raised_for_the_dialog(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="Msg 208, Invalid object name 'x'.")
        with patch.object(sql, "sqlcmd_path", return_value="/opt/sqlcmd"), patch.object(sql.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "Invalid object name"):
                sql.run_query(self.settings, "db", "SELECT * FROM x")


class SqlAdminTests(unittest.TestCase):
    def test_sqlcmd_detection_requires_executable_file_or_path_lookup(self):
        with patch.object(sql.Path, "is_file", return_value=True), \
                patch.object(sql.os, "access", return_value=False), \
                patch.object(sql.shutil, "which", return_value=None):
            self.assertIsNone(sql.sqlcmd_path())
        with patch.object(sql.Path, "is_file", return_value=True), \
                patch.object(sql.os, "access", return_value=True):
            self.assertEqual(sql.sqlcmd_path(), "/opt/mssql-tools18/bin/sqlcmd")

    def setUp(self):
        permissions = patch.object(sql, "prepare_backup_access")
        self.prepare_permissions = permissions.start()
        self.addCleanup(permissions.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Path(self.directory.name) / "connections.json"
        self.settings = dict(name="SQL", host="localhost", port=1433, username="sa",
                             password="test-only-password", backup_directory="/var/opt/mssql/backup",
                             trust_certificate=False)
        self.rows = [{"id": 1, "name": "master", "state": "ONLINE"},
                     {"id": 5, "name": "Example", "state": "ONLINE"},
                     {"id": 6, "name": "Offline", "state": "OFFLINE"}]

    def test_connections_permissions_and_redaction(self):
        identifier = sql.add_connection(self.store, self.settings)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        public = sql.public_connections(self.store)
        self.assertNotIn("password", public[0])
        self.assertEqual(sql.connection(self.store, identifier)["password"], self.settings["password"])
        sql.remove_connection(self.store, identifier)
        self.assertEqual(sql.public_connections(self.store), [])
        with self.assertRaises(ValueError):
            sql.connection(self.store, identifier)

    def test_connection_validation(self):
        for change in ({"host": "https://localhost"}, {"port": True}, {"port": 70000},
                       {"password": ""}, {"trust_certificate": "yes"}, {"backup_directory": "\0"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                sql.add_connection(self.store, self.settings | change)

    def test_edit_connection_keeps_password_id_and_rejects_duplicates(self):
        first = sql.add_connection(self.store, self.settings)
        second = sql.add_connection(self.store, self.settings | {"host": "other.example"})
        sql.edit_connection(self.store, self.settings | {"name": "Renamed", "password": ""}, first)
        saved = sql.connection(self.store, first)
        self.assertEqual(saved["name"], "Renamed")
        self.assertEqual(saved["password"], self.settings["password"])
        sql.edit_connection(self.store, self.settings | {"password": "replacement"}, first)
        self.assertEqual(sql.connection(self.store, first)["password"], "replacement")
        with self.assertRaisesRegex(ValueError, "already exists"):
            sql.edit_connection(self.store, self.settings, second)
        with self.assertRaisesRegex(ValueError, "Unknown SQL connection"):
            sql.edit_connection(self.store, self.settings, "missing")
        self.assertEqual(len(sql.connections(self.store)), 2)

    def test_a_connection_that_cannot_log_in_is_not_saved(self):
        """A wrong password was saved, and every action then failed with Login failed for user 'sa' (1.0.70)"""
        def refused(settings):
            raise RuntimeError("Sqlcmd: Error: Microsoft ODBC Driver 18 for SQL Server : Login failed for user 'sa'..")
        with patch.object(sql, "databases", side_effect=refused):
            with self.assertRaisesRegex(ValueError, "refused the login of sa"):
                sql.add_connection(self.store, self.settings, check=sql.check_login)
        self.assertEqual(sql.connections(self.store), [])
        with patch.object(sql, "databases", return_value=self.rows):
            sql.add_connection(self.store, self.settings, check=sql.check_login)
        self.assertEqual(len(sql.connections(self.store)), 1)
        with self.assertRaisesRegex(ValueError, "Type the password"):
            sql.add_connection(self.store, self.settings | {"host": "other.example", "password": ""})

    def test_a_refused_login_says_which_password_it_tried_without_showing_it(self):
        """sa was refused on another server while the right password was typed: the message said nothing of what was
        tried (1.0.70)"""
        refused = RuntimeError("Login failed for user 'sa'.")
        settings = self.settings | {"password": "Secret pass "}
        said = str(sql.connect_error(settings, refused, "typed"))
        self.assertIn("It tried the typed password of 12 characters. It begins or ends with a blank.", said)
        self.assertNotIn("Secret", said)
        said = str(sql.connect_error(self.settings | {"password": "abcdef"}, refused))
        self.assertIn("the saved password of 6 characters.", said)
        self.assertNotIn("blank", said)

    def test_ssh_connection_credentials_are_validated_preserved_and_redacted(self):
        fields = dict(ssh_enabled=True, ssh_host="linux.example", ssh_port=2222,
                      ssh_username="backup", ssh_password="ssh-only-test-secret")
        connection = sql.add_connection(self.store, self.settings | fields)
        public = sql.public_connections(self.store)[0]
        self.assertEqual(public["ssh_host"], "localhost")
        self.assertNotIn("ssh_password", public)
        sql.edit_connection(self.store, self.settings | fields | {"ssh_password": ""}, connection)
        self.assertEqual(sql.connection(self.store, connection)["ssh_password"], fields["ssh_password"])
        for invalid in ({"ssh_username": "-root"}, {"ssh_port": True},
                        {"ssh_port": 0}, {"ssh_password": "\0"}, {"backup_directory": "C:\\Backups"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sql.edit_connection(self.store, self.settings | fields | invalid, connection)
        sql.edit_connection(self.store, self.settings | {"ssh_enabled": False}, connection)
        self.assertNotIn("ssh_password", sql.connection(self.store, connection))

    def test_saved_ssh_host_follows_sql_host_and_ignores_submitted_ssh_host(self):
        fields = self.settings | dict(ssh_enabled=True, ssh_host="wrong.example", ssh_username="backup", ssh_port=22)
        connection = sql.add_connection(self.store, fields)
        self.assertEqual(sql.connection(self.store, connection)["ssh_host"], "localhost")
        sql.edit_connection(self.store, fields | {"host": "sql.example"}, connection)
        self.assertEqual(sql.connection(self.store, connection)["ssh_host"], "sql.example")
        sql.edit_connection(self.store, fields | {"host": "."}, connection)
        self.assertEqual(sql.connection(self.store, connection)["ssh_host"], "localhost")

    def test_duplicate_host_and_port_rejected_regardless_of_login(self):
        sql.add_connection(self.store, self.settings)
        original = self.store.read_bytes()
        for change in ({"name": "Other"}, {"username": "other"}, {"host": " LOCALHOST. "}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "already exists"):
                sql.add_connection(self.store, self.settings | change)
            self.assertEqual(self.store.read_bytes(), original)
        sql.add_connection(self.store, self.settings | {"port": 1434})
        sql.add_connection(self.store, self.settings | {"host": "other.example"})
        self.assertEqual(len(sql.connections(self.store)), 3)

    def test_sqlcmd_uses_private_query_file_and_password_environment(self):
        filenames = []
        def run(args, **kwargs):
            filename = args[args.index("-i") + 1]
            filenames.append(filename)
            self.assertEqual(os.stat(filename).st_mode & 0o777, 0o600)
            self.assertNotIn(self.settings["password"], json.dumps(args))
            self.assertEqual(kwargs["env"]["SQLCMDPASSWORD"], self.settings["password"])
            self.assertIn("-b", args)
            self.assertIn("-x", args)
            self.assertNotIn("-C", args)
            self.assertNotIn("-h", args)
            self.assertEqual(args[args.index("-y") + 1], "0")
            self.assertIn("SET NOCOUNT ON;", Path(filename).read_text())
            self.assertIn("FOR JSON PATH), N'[]') AS vns_json;", Path(filename).read_text())
            return Mock(returncode=0, stdout="[]\n", stderr="")
        with patch.object(sql.shutil, "which", return_value="/test/sqlcmd"), patch.object(sql.subprocess, "run", side_effect=run):
            self.assertEqual(sql.query_json(self.settings, "SELECT 1 AS value"), [])
        self.assertFalse(Path(filenames[0]).exists())

    def test_long_json_chunks_preserve_spaces_at_chunk_boundaries(self):
        expected = [{"name": "a" * 3985 + " middle " + "b" * 5000}]
        encoded = json.dumps(expected)
        boundary = encoded.index(" middle") + 1
        output = "vns_json\n--------\n" + encoded[:boundary] + "\n" + encoded[boundary:] + "\n"
        with patch.object(sql, "execute", return_value=output):
            self.assertEqual(sql.query_json(self.settings, "SELECT name"), expected)

    def test_json_uses_result_set_instead_of_print_messages(self):
        expected = [{"name": "Example", "id": 5}]
        output = "vns_json\n--------\n" + json.dumps(expected) + "\n"

        def run(args, **kwargs):
            query = Path(args[args.index("-i") + 1]).read_text()
            self.assertNotIn("PRINT", query)
            return Mock(returncode=0, stdout=output, stderr="")

        with patch.object(sql.shutil, "which", return_value="/test/sqlcmd"), \
                patch.object(sql.subprocess, "run", side_effect=run):
            self.assertEqual(sql.query_json(self.settings, "SELECT name, database_id AS id FROM sys.databases"), expected)

    def test_empty_json_response_has_actionable_error(self):
        with patch.object(sql, "execute", return_value=" \r\n"):
            with self.assertRaisesRegex(ValueError, "sqlcmd returned no JSON output"):
                sql.query_json(self.settings, "SELECT 1")

    def test_actual_tools18_headerless_database_response(self):
        output = '[{"name":"master"},{"name":"tempdb"},{"name":"model"},{"name":"msdb"}]\n'
        with patch.object(sql, "execute", return_value=output):
            self.assertEqual(sql.query_json(self.settings, "SELECT name FROM sys.databases"),
                             [{"name": name} for name in ("master", "tempdb", "model", "msdb")])

    def test_empty_result_is_coalesced_to_json_array_in_sql(self):
        def run(args, **kwargs):
            query = Path(args[args.index("-i") + 1]).read_text()
            output = "[]\n" if "COALESCE((" in query and "), N'[]')" in query else "NULL\n"
            return Mock(returncode=0, stdout=output, stderr="")

        with patch.object(sql.shutil, "which", return_value="/test/sqlcmd"), \
                patch.object(sql.subprocess, "run", side_effect=run):
            self.assertEqual(sql.query_json(self.settings, "SELECT 1 AS value WHERE 1=0"), [])
            self.assertEqual(sql.backups(self.settings), [])

    def test_uploaded_backup_is_discovered_and_restore_rechecks_metadata(self):
        header = dict(BackupType="1", Position="2", BackupSetGUID="guid-1", DatabaseName="Uploaded",
                      BackupFinishDate="2026-11-07 12:00:00", BackupSize="1048576", IsDamaged="0")
        with patch.object(sql, "query_json", side_effect=[[], [{"name": "uploaded.bak"}]]) as query, \
                patch.object(sql, "backup_table", side_effect=[[{"FamilyCount": "1"}], [header]]):
            records = sql.backups(self.settings)
        self.assertIn("xp_dirtree", query.call_args.kwargs["preamble"])
        selected = records[0]
        self.assertEqual(selected["path"], "/var/opt/mssql/backup/uploaded.bak")
        self.assertEqual(selected["position"], 2)
        self.assertEqual(selected["size_mb"], 1)
        self.assertEqual(selected["media_count"], 1)
        self.assertRegex(selected["id"], r"^file-[0-9a-f]{64}$")
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "backups", return_value=records), patch.object(sql, "restore") as restore, \
                patch.object(sql, "query_json", return_value=[]), patch.object(sql, "create_restore_login") as create_login:
            sql.run_action(self.settings, dict(action="restore", database="Uploaded", target="New",
                                               backup_id=selected["id"], login="New", login_password="test-secret"))
            restore.assert_called_once_with(self.settings, "New", selected["path"], 2, False)
            create_login.assert_called_once_with(self.settings, "New", "New", "test-secret")
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "backups", return_value=[]), patch.object(sql, "restore") as restore:
            with self.assertRaisesRegex(ValueError, "Backup not found"):
                sql.run_action(self.settings, dict(action="restore", database="Uploaded", target="New",
                                                   backup_id=selected["id"]))
            restore.assert_not_called()

    def test_history_files_are_not_duplicated_and_unknown_files_report_errors(self):
        record = dict(id=1, path="/var/opt/mssql/backup/known.bak")
        with patch.object(sql, "query_json", side_effect=[[record], [{"name": "known.bak"}, {"name": "bad.bak"}]]), \
                patch.object(sql, "local_backup_access", return_value=False), \
                patch.object(sql, "backup_table", side_effect=RuntimeError("Cannot open backup")), \
                patch.object(sys, "stderr"):
            records = sql.backups(self.settings)
        self.assertEqual(records[0], record)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["media_count"], 0)
        self.assertIn("Cannot open backup", records[1]["error"])

    def test_backup_metadata_parser_handles_numeric_padding(self):
        with patch.object(sql, "execute", return_value="Position|BackupType|DatabaseName\n--------|----------|------------\n    2|    1|Uploaded\n"):
            self.assertEqual(sql.backup_table(self.settings, "RESTORE HEADERONLY"),
                             [dict(Position="2", BackupType="1", DatabaseName="Uploaded")])

    def test_directory_batch_declarations_are_outside_json_subquery(self):
        queries = []

        def execute(settings, query, **kwargs):
            queries.append(query)
            return "[]\n"

        with patch.object(sql, "execute", side_effect=execute):
            self.assertEqual(sql.backups(self.settings), [])
        listing = queries[1]
        self.assertLess(listing.index("DECLARE @files"), listing.index("INSERT INTO @files"))
        self.assertLess(listing.index("INSERT INTO @files"), listing.index("SELECT COALESCE(("))
        self.assertIn("SELECT COALESCE((SELECT subdirectory", listing)
        self.assertNotIn("DECLARE", listing[listing.index("SELECT COALESCE(("):])
        self.assertIn("N'/var/opt/mssql/backup'", listing)

    def test_directory_filenames_cannot_escape_backup_directory(self):
        with patch.object(sql, "query_json", side_effect=[[], [{"name": "../other.bak"}]]):
            with self.assertRaisesRegex(ValueError, "invalid backup filename"):
                sql.backups(self.settings)

    def test_discovered_striped_or_damaged_backups_cannot_be_restored(self):
        header = dict(BackupType="1", Position="1", BackupSetGUID="guid", DatabaseName="Uploaded",
                      BackupFinishDate="2026-11-07 12:00:00", BackupSize="1", IsDamaged="0")
        for family_count, damaged in (("2", "0"), ("1", "1")):
            with self.subTest(family_count=family_count, damaged=damaged):
                with patch.object(sql, "query_json", side_effect=[[], [{"name": "uploaded.bak"}]]), \
                        patch.object(sql, "backup_table", side_effect=[
                            [{"FamilyCount": family_count}], [header | {"IsDamaged": damaged}]]):
                    records = sql.backups(self.settings)
                self.assertNotEqual(records[0]["media_count"], 1)
                with patch.object(sql, "databases", return_value=self.rows), \
                        patch.object(sql, "backups", return_value=records), patch.object(sql, "restore") as restore:
                    with self.assertRaisesRegex(ValueError, "Backup not found"):
                        sql.run_action(self.settings, dict(action="restore", database="Uploaded", target="New",
                                                           backup_id=records[0]["id"]))
                    restore.assert_not_called()

    def test_sql_errors_never_become_success(self):
        with patch.object(sql.shutil, "which", return_value="/test/sqlcmd"), patch.object(
                sql.subprocess, "run", return_value=Mock(returncode=1, stdout="", stderr="Access denied")):
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                sql.execute(self.settings, "SELECT 1")
        with patch.object(sql, "execute", return_value="not JSON"):
            with self.assertRaises(ValueError):
                sql.query_json(self.settings, "SELECT 1")

    def test_create_and_drop_escape_names_and_require_confirmation(self):
        name = "odd]name';--"
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "execute") as execute, \
                patch.object(sql, "exclusive_database_logins", return_value=[]):
            sql.run_action(self.settings, {"action": "create", "database": name})
        self.assertIn("[odd]]name';--]", execute.call_args.args[1])
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "execute") as execute, \
                patch.object(sql, "exclusive_database_logins", return_value=[]):
            for data in ({"action": "drop", "database": "master", "confirmation": "master"},
                         {"action": "drop", "database": "Example", "confirmation": "wrong"}):
                with self.assertRaises(ValueError):
                    sql.run_action(self.settings, data)
            execute.assert_not_called()
            sql.run_action(self.settings, {"action": "drop", "database": "Example", "confirmation": "Example"})
            self.assertIn("SET MULTI_USER", execute.call_args.args[1])

    def test_create_makes_a_login_that_owns_the_new_database(self):
        """Create database made no login, where harbor sql create (create_db.sh) made one of the database's name, db_owner
        in it (1.0.70)"""
        data = {"action": "create", "database": "Shop", "login": "Shop", "login_password": "shop12"}
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "execute") as execute, \
                patch.object(sql, "query_json", return_value=[]):
            sql.run_action(self.settings, data)
        sent = [call.args[1] for call in execute.call_args_list]
        self.assertEqual(sent[0], "CREATE DATABASE [Shop];")
        self.assertIn("CREATE LOGIN [Shop] WITH PASSWORD=N'shop12'", sent[1])
        self.assertIn("ALTER ROLE [db_owner] ADD MEMBER [Shop];", sent[1])
        # A login of that name already there: nothing is made, not even the database
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "execute") as execute, \
                patch.object(sql, "query_json", return_value=[{"name": "Shop"}]):
            with self.assertRaisesRegex(ValueError, "There is a SQL login Shop already"):
                sql.run_action(self.settings, data)
            execute.assert_not_called()

    def test_backup_all_skips_system_and_offline_databases(self):
        rows = self.rows + [{"id": 7, "name": "Standby", "state": "ONLINE", "is_in_standby": True}]
        with patch.object(sql, "databases", return_value=rows), patch.object(sql, "backup") as backup:
            sql.run_action(self.settings, {"action": "backup_all"})
        backup.assert_called_once_with(self.settings, "Example")

    def test_backup_uses_database_filename_and_overwrites_backup_sets(self):
        for directory, expected in (
                ("/var/opt/mssql/backup", "/var/opt/mssql/backup/test.example.dk.bak"),
                ("D:\\SQL_Server\\Backup", "D:\\SQL_Server\\Backup\\test.example.dk.bak")):
            with self.subTest(directory=directory), patch.object(sql, "execute") as execute:
                settings = self.settings | {"backup_directory": directory}
                self.assertEqual(sql.backup(settings, "test.example.dk"), expected)
                self.prepare_permissions.assert_called_with(settings, expected, write=True)
                query = execute.call_args.args[1]
                self.assertIn("TO DISK=" + sql.literal(expected), query)
                self.assertTrue(query.endswith("WITH INIT;"))
                self.assertNotIn("COPY_ONLY", query)
                self.assertNotIn("CHECKSUM", query)
                self.assertEqual(sql.backup(settings, "test.example.dk"), expected)

    def test_single_backup_uses_actual_database_name_and_rejects_offline_or_standby(self):
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "backup") as backup:
            sql.run_action(self.settings, {"action": "backup", "database": "example"})
            backup.assert_called_once_with(self.settings, "Example")
        for state, standby in (("OFFLINE", False), ("ONLINE", True)):
            rows = [{"id": 5, "name": "Example", "state": state, "is_in_standby": standby}]
            with patch.object(sql, "databases", return_value=rows), patch.object(sql, "backup") as backup:
                with self.assertRaisesRegex(ValueError, "not in standby"):
                    sql.run_action(self.settings, {"action": "backup", "database": "Example"})
                backup.assert_not_called()

    def test_backup_rejects_unsafe_database_filenames_before_execution(self):
        with patch.object(sql, "execute") as execute:
            for name in ("../escape", "folder\\escape", "drive:name", "bad*name", "bad?name"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "backup filename"):
                    sql.backup(self.settings, name)
            with self.assertRaisesRegex(ValueError, "reserved Windows"):
                sql.backup(self.settings | {"backup_directory": "D:\\Backup"}, "CON")
            execute.assert_not_called()

    def test_restore_rechecks_destination_backup_and_confirmation(self):
        record = dict(id=10, path="/backup/example.bak", position=2, media_count=1)
        data = dict(action="restore", database="Example", target="Example", backup_id=10)
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "backups", return_value=[record]), \
                patch.object(sql, "restore") as restore:
            with self.assertRaises(ValueError):
                sql.run_action(self.settings, data)
            restore.assert_not_called()
            sql.run_action(self.settings, data | {"overwrite": True, "confirmation": "Example"})
            restore.assert_called_once_with(self.settings, "Example", record["path"], 2, True)
            record["media_count"] = 2
            with self.assertRaises(ValueError):
                sql.run_action(self.settings, data | {"overwrite": True, "confirmation": "Example"})

    def test_restore_uses_logical_files_and_unique_server_paths(self):
        output = "LogicalName|PhysicalName|Type|Other\n-----------|------------|----|-----\nData|C:\\old.mdf|D|0\nLog|C:\\old.ldf|L|0\n"
        with patch.object(sql, "execute", side_effect=[output, ""]) as execute, patch.object(sql, "query_json", return_value=[
                {"data_path": "C:\\Data\\", "log_path": "D:\\Logs\\"}]):
            sql.restore(self.settings, "New", "/backup/file.bak", 3, True)
        query = execute.call_args.args[1]
        self.assertIn("MOVE N'Data' TO N'C:\\Data\\vns-", query)
        self.assertIn("MOVE N'Log' TO N'D:\\Logs\\vns-", query)
        self.assertIn("FILE=3", query)
        self.assertIn("REPLACE", query)
        self.assertIn("BEGIN CATCH", query)
        self.assertIn("SET MULTI_USER", query)

    def test_restore_rejects_unsupported_logical_files_before_changing_database(self):
        output = "LogicalName|PhysicalName|Type|Other\n-----------|------------|----|-----\nData|/old.mdf|S|0\n"
        with patch.object(sql, "execute", return_value=output) as execute:
            with self.assertRaises(ValueError):
                sql.restore(self.settings, "New", "/backup/file.bak", 1, True)
        self.assertEqual(execute.call_count, 1)

    def test_restore_existing_login_is_rejected_before_restore(self):
        data = dict(action="restore", database="Uploaded", target="New", backup_id=1,
                    login="AlreadyExists", login_password="test-secret")
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "backups", return_value=[dict(id=1, path="/backup/file.bak", position=1, media_count=1)]), \
                patch.object(sql, "query_json", return_value=[{"name": "AlreadyExists"}]), \
                patch.object(sql, "restore") as restore:
            with self.assertRaisesRegex(ValueError, "Choose another username"):
                sql.run_action(self.settings, data)
            restore.assert_not_called()

    def test_restore_login_mapping_and_password_validation(self):
        for password in ("", "short", "has\nnewline", "x" * 129):
            with self.assertRaises(ValueError):
                sql.validate_restore_login(dict(login="New", login_password=password))
        with patch.object(sql, "execute") as execute:
            sql.create_restore_login(self.settings, "New", "user]name", "test'password")
        query = execute.call_args.args[1]
        self.assertIn("CREATE LOGIN [user]]name]", query)
        self.assertIn("N'test''password'", query)
        self.assertIn("ALTER USER [user]]name]", query)
        self.assertIn("ALTER ROLE [db_owner] ADD MEMBER", query)

    def test_copy_only_to_new_database(self):
        with patch.object(sql, "databases", return_value=self.rows), patch.object(sql, "backup", return_value="/backup/copy.bak"), \
                patch.object(sql, "restore") as restore, patch.object(sql, "query_json", return_value=[]), \
                patch.object(sql, "create_restore_login") as create_login:
            sql.run_action(self.settings, {"action": "copy", "database": "Example", "target": "New",
                                          "login": "New", "login_password": "test-secret"})
            restore.assert_called_once_with(self.settings, "New", "/backup/copy.bak", 1, False)
            create_login.assert_called_once_with(self.settings, "New", "New", "test-secret")
            with self.assertRaises(ValueError):
                sql.run_action(self.settings, {"action": "copy", "database": "Example", "target": "master"})

    def test_copy_rejects_existing_login_before_backing_up(self):
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "query_json", return_value=[{"name": "New"}]), patch.object(sql, "backup") as backup:
            with self.assertRaisesRegex(ValueError, "already exists"):
                sql.run_action(self.settings, dict(action="copy", database="Example", target="New",
                                                   login="New", login_password="test-secret"))
            backup.assert_not_called()

    def test_dot_host_is_local_but_remote_host_is_not(self):
        self.assertTrue(sql.local_backup_access(self.settings | {"host": "."}))
        self.assertTrue(sql.local_backup_access(self.settings | {"host": "localhost"}))
        self.assertFalse(sql.local_backup_access(self.settings | {"host": "remote.example"}))

    def test_local_backup_delete_guards_and_whole_file_removal(self):
        root = Path(self.directory.name)
        path = root / "example.bak"
        path.write_bytes(b"backup")
        settings = self.settings | {"backup_directory": str(root)}
        record = dict(id=1, database_name="Example", path=str(path))
        with patch.object(sql, "backups", return_value=[record]), \
                patch.object(sql, "backup_table", return_value=[{"DatabaseName": "Example"}]):
            paths = sql.backup_files_for_deletion(settings, "Example", 1)
        sql.delete_backup_files(paths)
        self.assertFalse(path.exists())
        path.write_bytes(b"backup")
        for change, header in (({"host": "remote.example"}, "Example"), ({}, "Other")):
            with patch.object(sql, "backups", return_value=[record]), \
                    patch.object(sql, "backup_table", return_value=[{"DatabaseName": header}]):
                with self.assertRaises(ValueError):
                    sql.backup_files_for_deletion(settings | change, "Example", 1)
            self.assertTrue(path.exists())
        link = root / "link.bak"
        link.symlink_to(path)
        with patch.object(sql, "backups", return_value=[record | {"path": str(link)}]):
            with self.assertRaises(ValueError):
                sql.backup_files_for_deletion(settings, "Example", 1)
        self.assertTrue(path.exists())

    def test_remote_linux_backup_delete_uses_ssh_without_sudo_or_password_arguments(self):
        root = Path(self.directory.name)
        path = root / "remote.bak"
        path.write_bytes(b"backup")
        settings = self.settings | dict(host="sql.example", backup_directory=str(root),
                                        ssh_enabled=True, ssh_host="linux.example", ssh_port=2222,
                                        ssh_username="backup", ssh_password="ssh-only-test-secret")
        record = dict(id=1, database_name="Example", path=str(path))
        commands = []

        def run(args, **kwargs):
            commands.append((args, kwargs))
            self.assertNotIn(settings["ssh_password"], json.dumps(args))
            self.assertEqual(kwargs["env"]["SSHPASS"], settings["ssh_password"])
            self.assertEqual(args[:2], ["/test/sshpass", "-e"])
            self.assertIn("StrictHostKeyChecking=accept-new", args)
            self.assertEqual(args[-2], "backup@linux.example")
            self.assertNotIn("sudo", args[-1])
            response = sql._file_module.operate(json.loads(kwargs["input"]))
            return Mock(returncode=0, stdout=json.dumps(response), stderr="")

        with patch.object(sql.shutil, "which", side_effect=lambda tool: "/test/" + tool), \
                patch.object(sql.subprocess, "run", side_effect=run), \
                patch.object(sql, "backups", return_value=[record]), \
                patch.object(sql, "backup_table", return_value=[{"DatabaseName": "Example"}]):
            files = sql.backup_files_for_deletion(settings, "Example", 1)
            sql.delete_backup_files(files, settings)
        self.assertFalse(path.exists())
        self.assertEqual([json.loads(kwargs["input"])["operation"] for _, kwargs in commands], ["inspect", "delete"])

    def test_remote_backup_failures_are_explicit_and_key_login_uses_batch_mode(self):
        settings = self.settings | dict(host="sql.example", ssh_enabled=True, ssh_host="linux.example",
                                        ssh_port=22, ssh_username="backup", ssh_password="")
        with patch.object(sql.shutil, "which", return_value="/test/ssh"), \
                patch.object(sql.subprocess, "run", return_value=Mock(returncode=1, stderr="Permission denied")) as run:
            with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                sql.file_operation(settings, "delete", {"/backup/file.bak": [1, 2]})
            self.assertIn("BatchMode=yes", run.call_args.args[0])
        with patch.object(sql.shutil, "which", return_value="/test/ssh"), \
                patch.object(sql.subprocess, "run", return_value=Mock(returncode=0, stderr="", stdout="{}")):
            with self.assertRaisesRegex(ValueError, "every file"):
                sql.file_operation(settings, "delete", {"/backup/file.bak": [1, 2]})

    def test_remote_file_helper_refuses_symlinks_outside_paths_and_replaced_files(self):
        root = Path(self.directory.name)
        file = root / "file.bak"
        file.write_bytes(b"backup")
        link = root / "link.bak"
        link.symlink_to(file)
        for path in (str(link), "/outside/file.bak"):
            with self.assertRaises((OSError, ValueError)):
                sql._file_module.operate(dict(directory=str(root), operation="inspect", files={path: None}))
        with self.assertRaisesRegex(ValueError, "changed"):
            sql._file_module.operate(dict(directory=str(root), operation="delete", files={str(file): [0, 0]}))
        self.assertTrue(file.exists())

    def test_deleted_remote_history_file_is_hidden_with_ssh_access(self):
        settings = self.settings | dict(host="remote.example", backup_directory="/backup", ssh_enabled=True)
        record = dict(id=1, path="/backup/missing.bak", database_name="Example")
        with patch.object(sql, "query_json", side_effect=[[record], []]), \
                patch.object(sql, "file_operation", return_value={}) as operation:
            self.assertEqual(sql.backups(settings), [])
        operation.assert_called_once_with(settings, "inspect", {"/backup/missing.bak": None}, skip_missing=True)

    def test_drop_prevalidates_files_and_deletes_only_after_sql_success(self):
        data = dict(action="drop", database="Example", confirmation="Example", delete_backups=True)
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "backup_files_for_deletion", return_value={"file": (1, 2)}) as validate, \
                patch.object(sql, "execute", side_effect=RuntimeError("Drop failed")), \
                patch.object(sql, "exclusive_database_logins", return_value=["Example"]), \
                patch.object(sql, "delete_backup_files") as delete:
            with self.assertRaises(RuntimeError):
                sql.run_action(self.settings, data)
            validate.assert_called_once_with(self.settings, "Example")
            delete.assert_not_called()

    def test_exclusive_logins_keep_shared_and_offline_database_logins(self):
        with patch.object(sql, "query_json", side_effect=[
                [{"name": "Exclusive"}, {"name": "Shared"}],
                [{"name": "Other", "state": "ONLINE"}], [], [], [], [{"name": "SharedUser"}]]) as query:
            self.assertEqual(sql.exclusive_database_logins(self.settings, "Example"), ["Exclusive"])
            self.assertIn("sys.server_role_members", query.call_args_list[0].args[1])
            self.assertIn("sp.name<>N'sa'", query.call_args_list[0].args[1])
        with patch.object(sql, "query_json", side_effect=[
                [{"name": "Exclusive"}], [{"name": "Other", "state": "OFFLINE"}]]):
            self.assertEqual(sql.exclusive_database_logins(self.settings, "Example"), [])

    def test_database_drop_removes_exclusive_login_after_database(self):
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "exclusive_database_logins", return_value=["different_login"]), \
                patch.object(sql, "execute") as execute:
            sql.run_action(self.settings, dict(action="drop", database="Example", confirmation="Example"))
        self.assertIn("DROP DATABASE [Example]", execute.call_args_list[0].args[1])
        self.assertEqual(execute.call_args_list[1].args[1], "DROP LOGIN [different_login];")

    def test_database_drop_can_keep_associated_logins(self):
        with patch.object(sql, "databases", return_value=self.rows), \
                patch.object(sql, "exclusive_database_logins") as logins, patch.object(sql, "execute") as execute:
            sql.run_action(self.settings, dict(action="drop", database="Example", confirmation="Example",
                                               delete_login=False))
        logins.assert_not_called()
        self.assertEqual(execute.call_count, 1)

    def test_deleted_local_file_disappears_from_history_listing(self):
        path = Path(self.directory.name) / "deleted.bak"
        record = dict(id=1, database_name="Example", path=str(path))
        with patch.object(sql, "query_json", side_effect=[[record], []]):
            self.assertEqual(sql.backups(self.settings | {"backup_directory": self.directory.name}), [])


class ScopedLoginsTests(unittest.TestCase):
    def test_a_user_who_may_change_logins_sees_and_changes_only_their_own(self):
        """Users had no say over SQL logins: Logins is given per connection, and such a user sees only their databases,
        and only the logins that reach them or that they made, never sa or a sysadmin (1.0.70)"""
        spec = importlib.util.spec_from_file_location("access_for_logins", ROOT / "lib/access.py")
        access = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(access)
        self.assertIn("logins", access.SQL_ACTION_NAMES)
        nodes = [node for node in ast.parse((ROOT / "admin/app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "scoped_logins"]
        ns = {"access": access}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "scoped logins", "exec"), ns)
        found = {"databases": ["Shop", "Other"], "roles": [], "logins": [
            {"name": "shop", "protected": False, "sysadmin": False, "databases": [{"database": "Shop", "roles": []}], "unlinked": []},
            {"name": "both", "protected": False, "sysadmin": False, "databases": [{"database": "Shop", "roles": []}, {"database": "Other", "roles": []}], "unlinked": []},
            {"name": "boss", "protected": False, "sysadmin": True, "databases": [], "unlinked": []},
            {"name": "new", "protected": False, "sysadmin": False, "databases": [], "unlinked": []}]}
        user = {"id": "u", "role": "user", "sql": True, "connections": {"c": {"databases": ["Shop"], "actions": ["logins"]}}}
        scoped = ns["scoped_logins"](user, "c", found)
        self.assertEqual(scoped["databases"], ["Shop"])
        # Another's login, a sysadmin and one of no database they did not make are not shown at all (a test user saw sa)
        self.assertEqual([login["name"] for login in scoped["logins"]], ["shop"])
        user["made"] = {"logins": {"c": ["new"]}}
        self.assertEqual([login["name"] for login in ns["scoped_logins"](user, "c", found)["logins"]], ["shop", "new"])
        self.assertFalse(any(login["protected"] for login in ns["scoped_logins"](user, "c", found)["logins"]))
        self.assertIs(ns["scoped_logins"]({"id": "a", "role": "admin"}, "c", found), found)
        self.assertTrue(access.can_sql(user, "c", None, "logins"))
        self.assertFalse(access.can_sql(user | {"connections": {"c": {"databases": ["Shop"], "actions": ["query"]}}}, "c", None, "logins"))


class SqlApiTests(unittest.TestCase):
    def setUp(self):
        tool = patch.object(sql, "sqlcmd_path", return_value="/test/sqlcmd")
        tool.start()
        self.addCleanup(tool.stop)
        # A connection is tried before it is saved: here it logs in
        login = patch.object(sql, "databases", return_value=[])
        login.start()
        self.addCleanup(login.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = str(Path(self.directory.name) / "connections.json")
        self.app = Flask(__name__)
        self.ns = dict(app=self.app, request=request, jsonify=jsonify, abort=abort, subprocess=subprocess,
                       SQL_LOCK=threading.Lock(), SQL_CONNECTIONS=self.store, SQL_HELPER=str(ROOT / "lib/sql_admin.py"),
                       _sql_module=sql, sys=sys, json=json, os=os, socket=__import__("socket"), HARBOR_DIR=str(ROOT), LIB_DIR=str(ROOT / "lib"), HARBOR_COMMAND=str(ROOT / "bin/harbor"),
                       error=lambda message, status=400: (jsonify(error=message), status))
        source = ROOT / "admin/app.py"
        names = {"api_sql_connections", "api_sql_connection_test", "local_sa_password", "api_sql_status", "build_command"}
        nodes = [node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "sql API", "exec"), self.ns)
        add_target_helpers(self.ns)
        self.client = self.app.test_client()

    def test_missing_tools_blocks_connection_setup_and_allows_installer(self):
        with patch.object(sql, "sqlcmd_path", return_value=None):
            response = self.client.get("/api/sql/connections")
            self.assertFalse(response.json["tools_available"])
            for method in ("post", "put"):
                response = getattr(self.client, method)("/api/sql/connections", json={})
                self.assertEqual(response.status_code, 409)
                self.assertIn("Install SQL", response.json["error"])
            _, argv, cwd = self.ns["build_command"]({"kind": "install_sql_tools"})
            self.assertEqual(argv, ["/usr/bin/python3", str(ROOT / "bin/harbor"), "sql", "install-sql-tools"])
            self.assertEqual(cwd, "/")

    def test_tools_installer_is_client_only_and_shared_with_server_setup(self):
        installer = frozen(ROOT / "sql/install_tools.sh").read_text()
        shared = (ROOT / "tests/legacy/sql/lib/install_tools.sh").read_text()
        server = frozen(ROOT / "sql/install_server.sh").read_text()
        self.assertIn('source "${_sqlDir}/lib/install_tools.sh"', installer)
        self.assertIn('source "${_sqlDir}/lib/install_tools.sh"', server)
        self.assertNotIn("mssql-conf setup", installer)
        self.assertNotIn("apt-get install -y mssql-server", installer + shared)
        self.assertIn("ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev", shared)
        with patch.object(os.path, "isfile", return_value=False), self.assertRaises(InternalServerError):
            self.ns["build_command"]({"kind": "install_sql_tools"})

    def test_a_connection_is_tested_without_being_saved(self):
        """Test connection tries what is typed: whether it logs in, in plain words when not, and nothing is kept (1.0.70)"""
        typed = {"name": "Here", "host": "sql.example.com", "port": 1433, "username": "app", "password": "test-only", "backup_directory": "/b", "trust_certificate": True}
        self.ns["me"] = lambda: {"id": "root", "role": "admin"}
        self.ns["access"] = type("access", (), {"is_admin": staticmethod(lambda user: True)})
        response = self.client.post("/api/sql/connections/test", json=typed)
        self.assertEqual(response.json, {"ok": True, "databases": 0})
        with patch.object(sql, "databases", side_effect=RuntimeError("Login timeout expired. TCP Provider: Error code 0x2749.")):
            response = self.client.post("/api/sql/connections/test", json=typed)
        self.assertFalse(response.json["ok"])
        self.assertIn("No SQL Server answers on sql.example.com:1433", response.json["error"])
        self.assertEqual(sql.connections(self.store), [])

    def test_tables_of_sql_servers_own_databases_are_not_changed(self):
        """master, model, msdb and tempdb could be designed and dropped in like any other: they are read only (1.0.70)"""
        source = ROOT / "admin/app.py"
        tree = ast.parse(source.read_text())
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name == "api_sql_table")
                 or (isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "SQL_SYSTEM_DATABASES" for target in node.targets))]
        ran = []
        self.ns.update(require_sql=lambda *args: None, sql_settings=lambda connection: self.settings_for_test,
                       audit=lambda text: None, sql_failure=lambda exc: (jsonify(error=str(exc)), 422),
                       _sql_design=type("design", (), {"plan_drop": staticmethod(lambda schema, table: "DROP TABLE x"),
                                                        "apply": staticmethod(lambda *args: ran.append(args))}))
        self.settings_for_test = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "sql table API", "exec"), self.ns)
        for database in ("master", "MSDB", "tempdb", "model"):
            response = self.client.post("/api/sql/local/table", json={"database": database, "table": "spt_monitor", "action": "drop",
                                                                      "confirmation": "spt_monitor", "run": True})
            self.assertEqual(response.status_code, 403, database)
            self.assertIn("SQL Server's own databases", response.json["error"])
        self.assertEqual(ran, [])
        response = self.client.post("/api/sql/local/table", json={"database": "Shop", "table": "Orders", "action": "drop",
                                                                  "confirmation": "Orders", "run": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ran), 1)

    def test_connection_api_redacts_credentials_and_builds_allowlisted_task(self):
        response = self.client.post("/api/sql/connections", json={
            "name": "Local", "host": "localhost", "port": 1433, "username": "sa",
            "password": "test-only-secret", "backup_directory": "/backup", "trust_certificate": True})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-only-secret", response.get_data(as_text=True))
        connection = response.json["connections"][0]["id"]
        self.assertEqual(response.json["id"], connection)
        updated = self.client.put("/api/sql/connections", json={
            "id": connection, "name": "Edited", "host": "localhost", "port": 1433, "username": "sa",
            "password": "", "backup_directory": "/backup", "trust_certificate": True})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["id"], connection)
        self.assertNotIn("password", updated.json["connections"][0])
        self.assertEqual(sql.connection(self.store, connection)["password"], "test-only-secret")
        login_payload = dict(kind="sql_action", connection=connection, action="restore",
                             database="Uploaded", target="New", login="New", login_password="private-test-secret", backup_id=1)
        restore_argv = self.ns["build_command"](login_payload)[1]
        self.assertNotIn("private-test-secret", json.dumps(restore_argv))
        self.assertEqual(json.loads(restore_argv[-1])["login"], "New")
        argv = self.ns["build_command"]({"kind": "sql_action", "connection": connection, "action": "backup_all"})[1]
        self.assertNotIn("test-only-secret", json.dumps(argv))
        self.assertEqual(json.loads(argv[-1]), {"action": "backup_all"})
        with self.app.test_request_context(), self.assertRaises(BadRequest):
            self.ns["build_command"]({"kind": "sql_action", "connection": connection, "action": "arbitrary"})
        response = self.client.delete("/api/sql/connections", json={"id": connection})
        self.assertEqual(response.json["connections"], [])

    def test_sql_status_errors_are_reported_and_unknown_connections_rejected(self):
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.get("/api/sql/missing")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Unknown SQL connection", response.json["error"])

if __name__ == "__main__":
    unittest.main()
