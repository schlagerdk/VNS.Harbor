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
        self.assertIn('const views = ["sites", "site", "sql", "server"];', SOURCE)
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
            group = re.search(r'<div class="server-nav-group"( hidden)?>Sites</div>', page)
            return not group[1], not hidden("server-tab-convert"), not hidden("server-tab-export")
        self.assertEqual(shown({"convert": True, "export": True}), (True, True, True))
        self.assertEqual(shown({"convert": False, "export": True}), (True, False, True))
        self.assertEqual(shown({"convert": False, "export": False}), (False, False, False))
        # An address to a tab that is not shown leads to General
        self.assertIn('SERVER_TABS.includes(arg) && !$("#server-tab-" + arg)?.hidden ? arg', SOURCE)
