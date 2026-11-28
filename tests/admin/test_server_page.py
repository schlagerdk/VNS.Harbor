"""Server tools, Scheduled jobs and Servers are tabs of a page (#server/tools, #server/jobs, #server/servers), not
dialogs on top of each other (1.0.69)"""
from pathlib import Path
import re
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
PAGE = (ROOT_DIR / "admin/templates/index.html").read_text()
SOURCE = (ROOT_DIR / "admin/static/app.js").read_text()


class ServerPageTests(unittest.TestCase):
    def test_the_three_are_tabs_of_a_page(self):
        for gone in ('id="dlg-server-tools"', 'id="dlg-servers"', 'id="dlg-crontab"', 'id="dlg-users"', 'id="settings-server-tools"', 'id="settings-servers"'):
            self.assertNotIn(gone, PAGE)
        self.assertIn('<section id="view-server"', PAGE)
        # A part each in the side menu: every tool, then the server's own parts
        for tab in ("sql", "dotnet", "node", "access", "jobs", "settings", "servers", "users", "usage"):
            self.assertIn(f'id="server-tab-{tab}"', PAGE)
        for pane in ("tools", "jobs", "settings", "servers", "users"):
            self.assertIn(f'id="server-pane-{pane}"', PAGE)
        # What the tabs show keeps its ids, inside the page
        page = PAGE[PAGE.index('<section id="view-server"'):PAGE.index('<section id="view-sql"')]
        for ident in ("server-tools-list", "crontab-list", "crontab-add", "servers-list", "users-list",
                      "server-settings-list"):
            self.assertIn(f'id="{ident}"', page)
        # A job is added or edited in a dialog of its own
        self.assertIn('<dialog id="dlg-crontab-form"', PAGE)
        self.assertIn('<form id="crontab-form"', PAGE[PAGE.index('<dialog id="dlg-crontab-form"'):])

    def test_the_menu_and_the_switch_lead_to_the_page(self):
        # Users is on the page, no longer in the menu
        self.assertNotIn('id="menu-users"', PAGE)
        self.assertNotIn('"#menu-users"', SOURCE)
        # Preferences keeps the user's own start page; the server's settings are on the page
        preferences = PAGE[PAGE.index('<dialog id="dlg-preferences"'):]
        preferences = preferences[:preferences.index("</dialog>")]
        self.assertIn('name="start_view"', preferences)
        self.assertNotIn("public_address", preferences)
        self.assertNotIn("keep_alive", preferences)
        self.assertIn('const views = ["sites", "site", "sql", "server", "dns", "mail"];', SOURCE)
        self.assertIn('$("#switch-server").hidden = !currentUser.admin;', SOURCE)
        # No code waits on the dialogs that went
        for gone in ("serverToolsDialog", "serversDialog", "crontabDialog.", "showModal(); // server"):
            self.assertNotIn(gone, SOURCE)
        self.assertFalse(re.search(r'\$\("#(server-tools|servers|crontab)-(close|refresh)"\)', SOURCE))


if __name__ == "__main__":
    unittest.main()


class SiteToolsTests(unittest.TestCase):
    def test_convert_and_export_only_on_a_server_with_sites(self):
        """Without sites there is nothing to convert or export: the Sites group is not shown. Export Site stays while
        exports are kept here (one of a site deleted with its export is restored from there)"""
        from jinja2 import Environment, FileSystemLoader
        template = Environment(loader=FileSystemLoader(str(ROOT_DIR / "admin/templates"))).get_template("index.html")
        values = dict(csrf="x", admin_site="admin.example.com", hostname="web-01", user="Admin", start_view="sites", role="admin",
                      header={"search": True, "addresses": True, "files": True, "terminal": True}, asset_version="1")
        def shown(tools):
            page = template.render(**values, site_tools=tools)
            hidden = lambda ident: re.search(rf'id="{ident}"[^>]*\shidden[\s>]', page) is not None
            group = re.search(r'<div class="server-nav-group" id="server-group-sites"( hidden)?>Sites</div>', page)
            return not group[1], not hidden("server-tab-convert"), not hidden("server-tab-export")
        self.assertEqual(shown({"convert": True, "export": True}), (True, True, True))
        self.assertEqual(shown({"convert": False, "export": True}), (True, False, True))
        self.assertEqual(shown({"convert": False, "export": False}), (False, False, False))
        # An address to a tab that is not shown leads to General
        self.assertIn('SERVER_TABS.includes(arg) && !$("#server-tab-" + arg)?.hidden ? arg', SOURCE)


class TopMenuTests(unittest.TestCase):
    """The top menu is in the page as the user has it from the start: a reload never shows Web Server and SQL Server to
    one who has neither, before the page knows them and takes them away (1.0.114)"""

    def render(self, menu):
        from jinja2 import Environment, FileSystemLoader
        template = Environment(loader=FileSystemLoader(str(ROOT_DIR / "admin/templates"))).get_template("index.html")
        page = template.render(csrf="x", admin_site="admin.example.com", hostname="web-01", user="Anna", start_view="sites", role="user",
                               header={"search": True}, asset_version="1", site_tools={"convert": False, "export": False}, menu=menu)
        hidden = lambda ident: re.search(rf'id="{ident}"[^>]*\shidden[\s>]', page) is not None
        nav = re.search(r'<nav class="view-switch"[^>]*>', page)[0]
        return " hidden" in nav, {part: not hidden(f"switch-{part}") for part in ("sites", "sql", "dns", "mail", "server")}

    def test_a_mail_users_page_has_no_web_or_sql_server(self):
        nav_hidden, shown = self.render({"sites": False, "sql": False, "dns": False, "mail": True, "server": False})
        self.assertTrue(nav_hidden)
        self.assertEqual(shown, {"sites": False, "sql": False, "dns": False, "mail": True, "server": False})

    def test_an_administrators_menu_is_there_at_once(self):
        nav_hidden, shown = self.render({"sites": True, "sql": True, "dns": True, "mail": True, "server": True})
        self.assertFalse(nav_hidden)
        self.assertTrue(all(shown.values()))
        _, shown = self.render({"sites": True, "sql": False, "dns": False, "mail": True, "server": False})
        self.assertEqual(shown, {"sites": True, "sql": False, "dns": False, "mail": True, "server": False})

    def test_the_page_is_given_what_the_user_has(self):
        app = (ROOT_DIR / "admin/app.py").read_text()
        self.assertIn("menu=top_menu(user))", app)
        # /api/me says the same of DNS and Mail as the page had from the start
        self.assertIn('dns=top_menu(user)["dns"], mail=top_menu(user)["mail"]', app)


class TopMenuOfAUserTests(unittest.TestCase):
    """What the page is given: a master's user with Web Server on a member has it in the menu from the start, not
    after the member has answered (1.0.116)"""

    def top_menu(self, user, members=("web-02",)):
        import ast
        import sys
        sys.path.insert(0, str(ROOT_DIR / "lib"))
        import access
        tree = ast.parse((ROOT_DIR / "admin/app.py").read_text())
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "top_menu")
        ns = {"access": access, "member_names": lambda: list(members), "menu_shown": lambda: {"web": True, "sql": False}, "cfg": {},
              "_cloudflare": type("C", (), {"load": staticmethod(lambda: {"token": "x"})}),
              "_mailcow": type("M", (), {"load": staticmethod(lambda: {"key": "x"})})}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "top_menu", "exec"), ns)
        return ns["top_menu"](user)

    def test_web_server_on_a_member(self):
        user = {"id": "u_1", "role": "user", "web": False, "sql": False, "dns": True, "dns_domains": ["example.com"],
                "mail": True, "mail_domains": {"example.com": {}},
                "servers": {"web-02": {"web": True, "sites": {"shop.example.com": {}}}}}
        self.assertEqual(self.top_menu(user), {"sites": True, "sql": False, "dns": True, "mail": True, "server": False})
        # A server that is no longer a member gives nothing
        self.assertFalse(self.top_menu(user, members=())["sites"])
        # Nothing in it there is no Web Server
        user["servers"]["web-02"]["sites"] = {}
        self.assertFalse(self.top_menu(user)["sites"])

    def test_an_administrator_as_set(self):
        self.assertEqual(self.top_menu({"id": "root", "role": "admin"}),
                         {"sites": True, "sql": False, "dns": True, "mail": True, "server": True})
