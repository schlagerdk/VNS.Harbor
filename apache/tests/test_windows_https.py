import ast
import logging
import os
import re
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/app.py"


def load(answers, root):
    """windows_probe from app.py, with each address answering as given"""
    nodes = [node for node in ast.parse(APP.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name == "windows_probe"]
    asked = []

    def probe_url(url):
        asked.append(url)
        return answers[url.split(":")[0]]
    ns = dict(WINDOWS_HTTPS={}, time=time, re=re, os=os, WEB_ROOT=root, probe_url=probe_url, site_migrated=lambda site: False,
              target_folder=lambda site_root, target: os.path.join(site_root, "scripts", "targets", target),
              app=SimpleNamespace(logger=logging.getLogger(__name__)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "windows probe", "exec"), ns)
    return ns["windows_probe"], asked


class WindowsProbeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.script = Path(self.folder.name, "site", "scripts", "targets", "prod", "publish_prod.sh")
        self.script.parent.mkdir(parents=True)
        self.script.write_text("PROD_BINDING='x.test'\nPROD_HTTPS=n                   # n = http\n")

    def tearDown(self):
        self.folder.cleanup()

    def test_a_site_that_answers_on_https_is_active_and_saved_as_https(self):
        probe, asked = load({"https": ("answered", "")}, self.folder.name)
        result = probe("site", "prod", "x.test", False)
        self.assertEqual((result["https"], result["state"]), (True, "active"))
        self.assertEqual(asked, ["https://x.test/"])
        self.assertIn("PROD_HTTPS=y                   # n = http", self.script.read_text())

    def test_an_error_page_on_https_is_failed_but_still_https(self):
        probe, _ = load({"https": ("http-error", "HTTP 502")}, self.folder.name)
        self.assertEqual(probe("site", "prod", "x.test", True), {"https": True, "state": "failed", "error": "HTTP 502",
                                                                  "checked": unittest.mock.ANY})

    def test_without_https_the_site_is_asked_on_http(self):
        for https, http, state in ((("refused", ""), ("answered", ""), "active"),
                                   (("certificate", "expired"), ("refused", "Connection refused"), "failed")):
            with self.subTest(https=https[0], http=http[0]):
                self.script.write_text(self.script.read_text().replace("PROD_HTTPS=n", "PROD_HTTPS=y"))
                probe, asked = load({"https": https, "http": http}, self.folder.name)
                result = probe("site", "prod", "x.test", True)
                self.assertEqual((result["https"], result["state"]), (False, state))
                self.assertEqual(asked, ["https://x.test/", "http://x.test/"])
                self.assertIn("PROD_HTTPS=n", self.script.read_text())

    def test_no_answer_keeps_what_is_saved_and_leaves_the_state_to_keep_alive(self):
        probe, asked = load({"https": ("unreachable", "timed out")}, self.folder.name)
        result = probe("site", "prod", "x.test", False)
        self.assertEqual((result["https"], result["state"]), (False, ""))
        self.assertEqual(asked, ["https://x.test/"])
        self.assertIn("PROD_HTTPS=n", self.script.read_text())

    def test_the_answer_is_kept_for_a_minute(self):
        probe, asked = load({"https": ("answered", "")}, self.folder.name)
        probe("site", "prod", "x.test", True)
        probe("site", "prod", "x.test", True)
        self.assertEqual(len(asked), 1)


class WebConfigHttpsTests(unittest.TestCase):
    def test_the_redirect_is_added_first_and_removed_again_keeping_the_rest(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("windows_prod", Path(__file__).resolve().parents[1] / "lib/windows_prod.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "web.config")
            path.write_text(helper.web_config("Site", "Production", https=False, umbraco=True).replace(
                "<handlers>", "<!-- kept --><handlers>"))
            self.assertTrue(helper.set_https(str(path), True))
            self.assertFalse(helper.set_https(str(path), True))
            text = path.read_text()
            self.assertLess(text.index("Rewrite HTTP to HTTPS"), text.index("Rewrite to Sitemap.xml"))
            self.assertIn("<!-- kept -->", text)
            self.assertTrue(helper.set_https(str(path), False))
            self.assertNotIn("Rewrite HTTP to HTTPS", path.read_text())
            self.assertIn("Rewrite to Robots.txt", path.read_text())
            plain = Path(folder, "plain.config")
            plain.write_text(helper.web_config("Site", "Production"))
            self.assertTrue(helper.set_https(str(plain), True))
            self.assertIn("<rewrite>", plain.read_text())


class OfflinePageTests(unittest.TestCase):
    def test_publish_shows_the_offline_page_which_needs_nothing_from_outside(self):
        lib = Path(__file__).resolve().parents[1] / "lib"
        page = (lib / "app_offline.htm").read_text()
        self.assertIn('_design="$(dirname "${BASH_SOURCE[0]}")/app_offline.htm"', (lib / "windows_prod.sh").read_text())
        # The site is down while it is shown, so nothing may be loaded from elsewhere
        self.assertNotRegex(page, r"(?i)(src|href)=[\"']?(https?:)?//")
        self.assertNotIn("@import", page)
        self.assertIn('<noscript><meta http-equiv="refresh" content="5"></noscript>', page)
        self.assertIn("response.status !== 503", page)
        # IIS shows a page under 512 bytes as a browser's own error page in some browsers
        self.assertGreater(len(page.encode()), 512)
