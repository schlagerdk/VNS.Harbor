"""The query window on a table: INSERT, UPDATE and DELETE snippets with its own columns and key, and values that fit
their types; its headings sort its rows (1.0.82)"""
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[2] / "admin/static/app.js"
SCRIPT = r'''
const assert = require("node:assert/strict");
const src = require("fs").readFileSync(process.argv[1], "utf8");
const pick = (start, end) => src.slice(src.indexOf(start), src.indexOf(end, src.indexOf(start)));
var engine = "postgres";
var sqlPageEngine = () => engine;
var sqlQuote = (name, e) => e === "postgres" ? '"' + name.replaceAll('"', '""') + '"' : "[" + name.replaceAll("]", "]]") + "]";
eval(pick("const SNIPPET_RESERVED", "// PostgreSQL's: the same help").replace(/^const /gm, "var "));
// PostgreSQL: id numbered by it, created by its default: only name is given
let shape = snippetShape([{ name: "id", type: "integer", identity: true, primary: true, nullable: false, default: "" },
	{ name: "name", type: "varchar", nullable: false, default: "" }, { name: "created", type: "timestamptz", nullable: false, default: "now()" }], "postgres");
assert.equal(snippetInsert('"public"."user"', shape, "postgres"), 'INSERT INTO "public"."user" (name)\nVALUES (\'value\');');
assert.equal(snippetUpdate('"public"."user"', shape, "postgres"), 'UPDATE "public"."user"\nSET name = \'value\'\nWHERE id = 1;');
// Microsoft SQL Server: a uuid key with NEWID(), an nvarchar given as N'…'
engine = "mssql";
shape = snippetShape([{ name: "id", type: "uniqueidentifier", primary: true, nullable: false, default: "NEWID()" },
	{ name: "name", type: "nvarchar", nullable: false, default: "" }, { name: "created", type: "datetime2", nullable: false, default: "SYSUTCDATETIME()" }], "mssql");
assert.equal(snippetInsert("[dbo].[user]", shape, "mssql"), "INSERT INTO [dbo].[user] (name)\nVALUES (N'value');");
assert.equal(snippetDelete("[dbo].[user]", shape), "DELETE FROM [dbo].[user]\nWHERE id = '00000000-0000-0000-0000-000000000000';");
// Types: a number, true or false, a new uuid; a reserved word quoted
engine = "postgres";
shape = snippetShape([{ name: "order", type: "integer", nullable: false, default: "" }, { name: "done", type: "boolean", nullable: false, default: "" },
	{ name: "ref", type: "uuid", nullable: false, default: "" }], "postgres");
assert.equal(snippetInsert("t", shape, "postgres"), 'INSERT INTO t ("order", done, ref)\nVALUES (0, false, gen_random_uuid());');
// No table known: the placeholders stay
assert.equal(snippetShape(null), null);
// A heading sorts a table's rows: ASC, then DESC, another column ASC; a script of one's own is left alone
eval(pick("const VIEW_FORMS", "function showSqlResults").replace(/^const /gm, "var "));
engine = "postgres";
let sorted = sortedView(parseView('SELECT * FROM "public"."user" LIMIT 1000;'), "name");
assert.equal(sorted, 'SELECT * FROM "public"."user" ORDER BY "name" ASC LIMIT 1000;');
assert.equal(sortedView(parseView(sorted), "name"), 'SELECT * FROM "public"."user" ORDER BY "name" DESC LIMIT 1000;');
engine = "mssql";
sorted = sortedView(parseView("SELECT TOP 1000 * FROM [dbo].[user];"), "created");
assert.equal(sorted, "SELECT TOP 1000 * FROM [dbo].[user] ORDER BY [created] ASC;");
assert.equal(parseView("SELECT * FROM [dbo].[user] WHERE id = 1;"), null);
// A row edited or deleted by its key, its values as SQL for their types; a new one with what must be given
var sqlNumber = /^-?\d+(\.\d+)?$/;
eval(pick("function rowValue", "// Put a script in the window").replace(/^const /gm, "var "));
const columns = [{ name: "id", type: "uniqueidentifier", primary: true, nullable: false, default: "NEWID()" },
	{ name: "name", type: "nvarchar", nullable: false, default: "" }, { name: "created", type: "datetime2", nullable: false, default: "SYSUTCDATETIME()" }];
const rows = rowScripts("[dbo].[user]", columns, { columns: ["id", "name", "created"] }, "mssql");
const row = ["061B1767-9F28-4677-B0CB-8DE37CF32906", "Kaya's", "2026-11-24 14:05:09.713"];
assert.equal(rows.update(row), "UPDATE [dbo].[user]\nSET name = N'Kaya''s',\n    created = '2026-11-24 14:05:09.713'\nWHERE id = '061B1767-9F28-4677-B0CB-8DE37CF32906';");
assert.equal(rows.remove(row), "DELETE FROM [dbo].[user]\nWHERE id = '061B1767-9F28-4677-B0CB-8DE37CF32906';");
assert.equal(rows.insert(), "INSERT INTO [dbo].[user] (name)\nVALUES (N'value');");
engine = "postgres";
const pg = rowScripts('"public"."user"', [{ name: "id", type: "integer", identity: true, primary: true }, { name: "active", type: "boolean", nullable: true, default: "" },
	{ name: "note", type: "text", nullable: true, default: "" }], { columns: ["id", "active", "note"] }, "postgres");
assert.equal(pg.update(["7", "t", "NULL"]), 'UPDATE "public"."user"\nSET active = true,\n    note = NULL\nWHERE id = 7;');
'''


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SnippetTests(unittest.TestCase):
    def test_snippets_fit_the_table(self):
        subprocess.run(["node", "-e", SCRIPT, str(APP)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
