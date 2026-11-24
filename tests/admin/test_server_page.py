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
