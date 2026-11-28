"""Web Server and SQL Server in the top menu, as Settings sets them for administrators (1.0.70)"""
import ast
from pathlib import Path
import unittest

APP = Path(__file__).resolve().parents[2] / "admin/app.py"


def lifted(**names):
    tree = ast.parse(APP.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("menu_shown", "menu_part_shown")]
    namespace = dict(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "top menu", "exec"), namespace)
    return namespace["menu_shown"]


class TopMenuTests(unittest.TestCase):
    def shown(self, web="auto", sql="auto", sites=True, sql_here=True):
        # Not a master (a master's auto shows both, for its members)
        return lifted(cfg={"web_menu": web, "sql_menu": sql}, has_sites=lambda: sites, sql_in_use=lambda: sql_here, has_members=lambda: False, os=__import__("os"), NO_WEB="/nonexistent/no-web")()

    def test_automatic_follows_what_the_server_has(self):
        self.assertEqual(self.shown(), {"web": True, "sql": True})
        self.assertEqual(self.shown(sites=False), {"web": False, "sql": True})
        self.assertEqual(self.shown(sql_here=False), {"web": True, "sql": False})

    def test_always_and_never_are_as_set(self):
        self.assertEqual(self.shown(web="hide", sql="show", sql_here=False), {"web": False, "sql": True})
        self.assertEqual(self.shown(web="show", sql="hide", sites=False), {"web": True, "sql": False})

    def test_one_of_them_is_always_shown(self):
        # Nothing here at all: Web Server
        self.assertEqual(self.shown(sites=False, sql_here=False), {"web": True, "sql": False})
        # Web Server set to Never: SQL Server stays, even with nothing to work with
        self.assertEqual(self.shown(web="hide", sql_here=False), {"web": False, "sql": True})
        # Both set to Never: Web Server
        self.assertEqual(self.shown(web="hide", sql="hide"), {"web": True, "sql": False})


if __name__ == "__main__":
    unittest.main()


class UserMenuIconTests(unittest.TestCase):
    def test_every_item_of_the_user_menu_has_an_icon(self):
        """Usage was added to the user menu without the icon the others have (1.0.70)"""
        import re
        root = APP.parent
        template = (root / "templates/index.html").read_text()
        menu = template[template.index('<div id="user-menu"'):template.index("</div>", template.index('<div id="user-menu"') + 200)]
        ids = re.findall(r'<button id="(menu-[\w-]+)"', menu)
        self.assertIn("menu-usage", ids)
        script = (root / "static/app.js").read_text()
        listed = script[script.index('for (const [id, icon, tone] of [["menu-audit"'):]
        listed = listed[:listed.index("]]) {")]
        for item in ids:
            self.assertIn(f'["{item}", ', listed, f"{item} has no icon")


class HeaderPartsTests(unittest.TestCase):
    def test_each_administrator_chooses_what_the_header_shows(self):
        """The server's addresses, Files and Terminal in the header: each on unless an administrator took it out, root's
        kept as the server's and every other's on themselves (1.0.71)"""
        tree = ast.parse(APP.read_text())
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name == "header_shown")
                 or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "HEADER_PARTS")]
        cfg = {"header": {"files": False}}
        ns = {"cfg": cfg, "access": type("access", (), {"ROOT": "root"})}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "header", "exec"), ns)
        self.assertEqual(ns["header_shown"]({"id": "root"}), {"search": True, "addresses": True, "files": False, "terminal": True})
        self.assertEqual(ns["header_shown"]({"id": "u1", "header": {"terminal": False}}), {"search": True, "addresses": True, "files": True, "terminal": False})
        self.assertEqual(ns["header_shown"]({"id": "u2"}), {"search": True, "addresses": True, "files": True, "terminal": True})
        page = (APP.parent / "templates/index.html").read_text()
        self.assertIn('id="btn-dock" type="button"{% if role != "admin" or not header.terminal %} hidden{% endif %}', page)
