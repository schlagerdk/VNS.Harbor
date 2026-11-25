"""PostgreSQL's admin (phase 10): psql's output read as the admin shows a script's results, and a script never read
by psql itself"""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pg_admin_for_tests", ROOT / "lib/pg_admin.py")
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)
sql_spec = importlib.util.spec_from_file_location("sql_admin_for_pg_tests", ROOT / "lib/sql_admin.py")
sql = importlib.util.module_from_spec(sql_spec)
sql_spec.loader.exec_module(sql)


class OutputTests(unittest.TestCase):
    def test_result_sets_follow_each_other_without_a_line_break(self):
        """As psql 18 wrote it on linux-01: the second set began right after the first one's footer"""
        output = ("id\x1ffull_name\x1e1\x1fBo\x1e2\x1fCy\x1e(2 rows)\n"
                  "x\x1fn\x1etwo\nlines\x1fNULL\x1e(1 row)\n"
                  "INSERT 0 1\n")
        results, messages = sql.parse_results(pg.readable_output(output, "psql:<stdin>:4: NOTICE:  hello\n"))
        self.assertEqual(results, [{"columns": ["id", "full_name"], "rows": [["1", "Bo"], ["2", "Cy"]]},
                                   {"columns": ["x", "n"], "rows": [["two\nlines", "NULL"]]}])
        self.assertEqual(messages, ["INSERT 0 1\nNOTICE:  hello"])

    def test_no_rows_and_text_before(self):
        output = "CREATE TABLE\n?column?\x1e(0 rows)\n"
        results, messages = sql.parse_results(pg.readable_output(output))
        self.assertEqual(results, [{"columns": ["?column?"], "rows": []}])
        self.assertEqual(messages, ["CREATE TABLE"])

    def test_a_script_goes_as_one_request_never_beginning_with_a_backslash(self):
        """psql runs a -c that begins with a backslash as its own command (\\! is a shell): a comment comes first"""
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        settings = {"host": "db.example.com", "port": 5432, "username": "postgres", "password": "x", "trust_certificate": True}
        with mock.patch.object(pg, "psql", return_value="psql"), mock.patch.object(pg.subprocess, "run", side_effect=run):
            pg.run_query(settings, "shop", "\\! touch /tmp/x")
        argv = calls[0]
        self.assertNotIn("-f", argv)
        script = argv[argv.index("-c") + 1]
        self.assertTrue(script.startswith("-- VNS Harbor\n"))
        self.assertNotIn("x", argv[:argv.index("-c")])


class DesignChoiceTests(unittest.TestCase):
    def test_the_designer_is_chosen_by_engine_as_the_admin_loads_it(self):
        """The admin loads sql_design.py under a name of its own, not in sys.modules: looking itself up there failed,
        and Logins and the table designer with it (1.0.82)"""
        spec = importlib.util.spec_from_file_location("vns_sql_design", ROOT / "lib/sql_design.py")
        design = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(design)
        self.assertIsNone(design.for_connection({"engine": "mssql"}))
        self.assertIsNone(design.for_connection({}))
        self.assertEqual(design.for_connection({"engine": "postgres"}).DEFAULT_SCHEMA, "public")


if __name__ == "__main__":
    unittest.main()


class TakenNameTests(unittest.TestCase):
    def test_a_user_is_not_told_a_database_is_there(self):
        """Create MinDB said Database already exists: a user learned of a database not theirs (1.0.82)"""
        rows = [{"name": "MinDB", "id": 5, "state": "ONLINE"}]
        settings = {"name": "Local", "host": "localhost", "port": 1433, "username": "sa", "password": "x", "trust_certificate": True}
        with mock.patch.object(sql, "databases", return_value=rows):
            with self.assertRaises(ValueError) as caught:
                sql.run_action(settings, {"action": "create", "database": "mindb", "private_names": True})
            self.assertEqual(str(caught.exception), "That name cannot be used. Choose another.")
            with self.assertRaises(ValueError) as caught:
                sql.run_action(settings, {"action": "create", "database": "mindb"})
            self.assertEqual(str(caught.exception), "Database already exists")
        with mock.patch.object(pg, "databases", return_value=[{"name": "shop", "id": 100, "state": "ONLINE", "owner": "shop"}]):
            with self.assertRaises(ValueError) as caught:
                pg.run_action(dict(settings, engine="postgres"), {"action": "create", "database": "shop", "private_names": True})
            self.assertEqual(str(caught.exception), "That name cannot be used. Choose another.")
