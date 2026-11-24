"""Tables and logins of a SQL Server designed in the admin: the SQL each change makes (lib/sql_design.py, 1.0.70)"""
import importlib.util
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sql_design", ROOT_DIR / "lib/sql_design.py")
design = importlib.util.module_from_spec(spec)
spec.loader.exec_module(design)

SETTINGS = {"username": "sa"}


def column(name, kind, **more):
    return {"name": name, "type": kind, **more}


class TableTests(unittest.TestCase):
    def test_create_with_types_defaults_identity_and_key(self):
        sql = design.plan_create("dbo", "Orders", [
            column("Id", "int", identity=True, primary=True),
            column("Title", "nvarchar", length=200),
            column("Notes", "nvarchar", length="max", nullable=True),
            column("Price", "decimal", precision=18, scale=2, default="0"),
            column("Created", "datetime2", scale=7, default="getdate()"),
            column("Status", "varchar", length=20, default="new"),
            column("Key", "uniqueidentifier", default="NEWID()")])
        self.assertIn("[Id] int IDENTITY(1,1) NOT NULL", sql)
        self.assertIn("[Title] nvarchar(200) NOT NULL", sql)
        self.assertIn("[Notes] nvarchar(max) NULL", sql)
        self.assertIn("[Price] decimal(18,2) NOT NULL CONSTRAINT [DF_Orders_Price] DEFAULT 0", sql)
        self.assertIn("DEFAULT GETDATE()", sql)
        self.assertIn("DEFAULT N'new'", sql)
        self.assertIn("CONSTRAINT [PK_Orders] PRIMARY KEY ([Id])", sql)
        self.assertTrue(sql.startswith("CREATE TABLE [dbo].[Orders] ("))

    def test_names_and_types_are_checked(self):
        for bad in ([column("A", "nvarchar", length=5000)], [column("A", "int(4)")], [column("A]", "int")],
                    [column("A", "int"), column("a", "int")], [column("A", "nvarchar", length=10, identity=True)], []):
            with self.assertRaises(ValueError, msg=bad):
                design.plan_create("dbo", "T", bad)
        # A text default with a quote stays one value
        self.assertIn("DEFAULT N'O''Neil'", design.plan_create("dbo", "T", [column("A", "nvarchar", length=10, default="O'Neil")]))

    def test_alter_renames_retypes_drops_adds_and_moves_the_key(self):
        current = {"schema": "dbo", "name": "Orders", "primary_key": "PK__Orders__1", "columns": [
            {"id": 1, "name": "Id", "type": "int", "nullable": False, "identity": True, "primary": True, "default": "", "default_name": None},
            {"id": 2, "name": "Title", "type": "nvarchar", "length": 100, "nullable": False, "identity": False, "primary": False, "default": "", "default_name": None},
            {"id": 3, "name": "Old", "type": "int", "nullable": True, "identity": False, "primary": False, "default": "0", "default_name": "DF__Orders__Old"},
            {"id": 4, "name": "Created", "type": "datetime", "nullable": False, "identity": False, "primary": False, "default": "GETDATE()", "default_name": "DF__Orders__Cr"}]}
        sql = design.plan_alter(current, [
            {"id": 1, "name": "Id", "type": "int", "identity": True, "primary": True},
            {"id": 2, "name": "Name", "type": "nvarchar", "length": 200, "nullable": True},
            {"id": 4, "name": "Created", "type": "datetime", "default": "SYSDATETIME()"},
            {"name": "Code", "type": "varchar", "length": 10, "primary": True}])
        lines = sql.splitlines()
        self.assertEqual(lines[:2], ["SET XACT_ABORT ON;", "BEGIN TRANSACTION;"])
        self.assertEqual(lines[-1], "COMMIT;")
        order = [lines.index(line) for line in (
            "ALTER TABLE [dbo].[Orders] DROP CONSTRAINT [PK__Orders__1];",
            "ALTER TABLE [dbo].[Orders] DROP CONSTRAINT [DF__Orders__Old];",
            "ALTER TABLE [dbo].[Orders] DROP COLUMN [Old];",
            "EXEC sp_rename N'dbo.Orders.Title', N'Name', 'COLUMN';",
            "ALTER TABLE [dbo].[Orders] ALTER COLUMN [Name] nvarchar(200) NULL;",
            "ALTER TABLE [dbo].[Orders] ADD [Code] varchar(10) NOT NULL;",
            "ALTER TABLE [dbo].[Orders] ADD CONSTRAINT [DF_Orders_Created] DEFAULT SYSDATETIME() FOR [Created];",
            "ALTER TABLE [dbo].[Orders] ADD CONSTRAINT [PK_Orders] PRIMARY KEY ([Id], [Code]);")]
        self.assertEqual(order, sorted(order))
        self.assertIn("ALTER TABLE [dbo].[Orders] DROP CONSTRAINT [DF__Orders__Cr];", lines)
        # Nothing changed: no SQL
        same = [{key: value for key, value in item.items() if key != "default_name"} for item in current["columns"]]
        self.assertEqual(design.plan_alter(current, same), "")
        with self.assertRaisesRegex(ValueError, "identity"):
            design.plan_alter(current, [dict(same[0], identity=False), *same[1:]])

    def test_defaults_read_back_as_typed(self):
        self.assertEqual(design.default_value("(getdate())"), "GETDATE()")
        self.assertEqual(design.default_value("((0))"), "0")
        self.assertEqual(design.default_value("(N'O''Neil')"), "O'Neil")

    def test_rename_and_drop(self):
        self.assertEqual(design.plan_rename("dbo", "Orders", "Sales"), "EXEC sp_rename N'dbo.Orders', N'Sales';")
        self.assertEqual(design.plan_drop("sales", "Orders"), "DROP TABLE [sales].[Orders];")


class SchemaTests(unittest.TestCase):
    def test_a_new_table_offers_the_databases_own_schemas(self):
        """Schema was a text field with dbo in it: the schemas there are offered instead (1.0.70)"""
        asked = []

        class Helper:
            identifier = staticmethod(design.helper().identifier)

            @staticmethod
            def query_json(settings, query, database="master", **more):
                asked.append((query, database))
                return [{"name": "dbo"}, {"name": "sales"}]

        original = design.helper
        design.helper = lambda: Helper
        try:
            self.assertEqual(design.schemas(SETTINGS, "Shop"), ["dbo", "sales"])
            self.assertEqual(asked[0][1], "Shop")
            self.assertIn("N'INFORMATION_SCHEMA'", asked[0][0])
            self.assertIn("schema_id < 16384", asked[0][0])
            Helper.query_json = staticmethod(lambda *args, **more: [])
            self.assertEqual(design.schemas(SETTINGS, "Empty"), ["dbo"])
        finally:
            design.helper = original


class LoginTests(unittest.TestCase):
    def test_create_login_runs_alone_first_and_errors_read_plainly(self):
        """A password SQL Server refused, and the user made after it for a login that was not there (1.0.70)"""
        ran = []
        design.use(type("sql", (), {"execute": staticmethod(lambda settings, text, **kw: ran.append(text)), "identifier": staticmethod(design.helper().identifier),
                                    "literal": staticmethod(design.helper().literal)}))
        try:
            design.apply(SETTINGS, design.plan_login("create", {"name": "shop", "password": "Secret-123", "databases": [{"database": "Shop", "roles": ["db_owner"]}]}, SETTINGS))
        finally:
            design.use(None)
        self.assertEqual(len(ran), 2)
        self.assertTrue(ran[0].startswith("CREATE LOGIN "))
        self.assertEqual(design.readable("Msg 33064, Level 16, State 2, Server WEB-01, Line 2\nPassword validation failed."), "Password validation failed.")

    def test_create_with_databases_and_roles(self):
        sql = design.plan_login("create", {"name": "shop", "password": "Secret-123", "default_database": "Shop",
                                           "databases": [{"database": "Shop", "roles": ["db_datareader", "db_datawriter"]}]}, SETTINGS)
        self.assertIn("CREATE LOGIN [shop] WITH PASSWORD=N'Secret-123', DEFAULT_DATABASE=[Shop], CHECK_POLICY=OFF", sql)
        # A simple password is let through: the login is not held to SQL Server's policy
        self.assertIn("PASSWORD=N'mindb1'", design.plan_login("create", {"name": "mindb", "password": "mindb1"}, SETTINGS))
        self.assertIn("USE [Shop];", sql)
        self.assertIn("CREATE USER [shop] FOR LOGIN [shop];", sql)
        self.assertIn("ALTER ROLE [db_datawriter] ADD MEMBER [shop];", sql)
        self.assertNotIn("Secret-123", design.masked(sql, "Secret-123"))

    def test_access_adds_takes_away_and_drops(self):
        current = {"databases": [{"database": "Shop", "roles": ["db_owner"]}, {"database": "Old", "roles": ["db_datareader"]}]}
        sql = design.plan_login("access", {"name": "shop", "databases": [{"database": "Shop", "roles": ["db_datareader"]},
                                                                        {"database": "New", "roles": ["db_owner"]}]}, SETTINGS, current)
        self.assertIn("ALTER ROLE [db_owner] DROP MEMBER [shop];", sql)
        self.assertIn("ALTER ROLE [db_datareader] ADD MEMBER [shop];", sql)
        self.assertIn("USE [New];", sql)
        self.assertIn("USE [Old];\nDROP USER IF EXISTS [shop];", sql)

    def test_password_lock_delete_and_what_is_refused(self):
        self.assertEqual(design.plan_login("disable", {"name": "shop"}, SETTINGS), "ALTER LOGIN [shop] DISABLE;")
        self.assertIn("ALTER LOGIN [shop] WITH PASSWORD=", design.plan_login("password", {"name": "shop", "password": "Secret-123"}, SETTINGS))
        sql = design.plan_login("delete", {"name": "shop"}, SETTINGS, {"databases": [{"database": "Shop", "roles": []}]})
        self.assertTrue(sql.endswith("DROP LOGIN [shop];"))
        for action, data in (("password", {"name": "shop", "password": "short"}), ("create", {"name": "mindb", "password": "abc12"}), ("delete", {"name": "sa"}),
                             ("create", {"name": "x", "password": "Secret-123", "databases": [{"database": "Shop", "roles": ["sysadmin"]}]})):
            with self.assertRaises(ValueError, msg=(action, data)):
                design.plan_login(action, data, SETTINGS)


    def test_sysadmins_and_users_left_without_their_login(self):
        """kiibee.dk showed No database: it was a sysadmin, and its user in kiibee.dk was another login's, left from the
        server the database came from. sa showed only the databases whose dbo was it (1.0.70)"""
        def query_json(settings, query, database="master", **more):
            if "sysadmin" in query:
                return [{"name": "sa"}, {"name": "kiibee"}]
            if "[Kiibee]" in query:
                return [{"user": "dbo", "login": "sa", "roles": "db_owner"}, {"user": "kiibee", "login": None, "roles": "db_owner"}]
            if "[Shop]" in query:
                return [{"user": "shop", "login": "shop", "roles": "db_datareader"}]
            return [{"name": "kiibee", "disabled": 0}, {"name": "sa", "disabled": 0}, {"name": "shop", "disabled": 0}]
        helper = type("sql", (), {"query_json": staticmethod(query_json), "identifier": staticmethod(design.helper().identifier),
                                  "literal": staticmethod(design.helper().literal),
                                  "databases": staticmethod(lambda settings: [{"name": "Kiibee", "id": 5, "state": "ONLINE"}, {"name": "Shop", "id": 6, "state": "ONLINE"}])})
        design.use(helper)
        try:
            found = {login["name"]: login for login in design.logins(SETTINGS)["logins"]}
        finally:
            design.use(None)
        self.assertTrue(found["kiibee"]["sysadmin"])
        self.assertEqual(found["kiibee"]["databases"], [])
        self.assertEqual(found["kiibee"]["unlinked"], [{"database": "Kiibee", "roles": ["db_owner"]}])
        self.assertFalse(found["shop"]["sysadmin"])
        self.assertEqual(found["shop"]["unlinked"], [])
        sql = design.plan_login("link", {"name": "kiibee"}, SETTINGS, found["kiibee"])
        self.assertEqual(sql, "USE [Kiibee];\nALTER USER [kiibee] WITH LOGIN = [kiibee];\nUSE [master];")
        self.assertEqual(design.plan_login("unsysadmin", {"name": "kiibee"}, SETTINGS, found["kiibee"]),
                         "ALTER SERVER ROLE [sysadmin] DROP MEMBER [kiibee];")
        for action in ("link", "unsysadmin"):
            with self.assertRaises(ValueError):
                design.plan_login(action, {"name": "shop"}, SETTINGS, found["shop"])


if __name__ == "__main__":
    unittest.main()
