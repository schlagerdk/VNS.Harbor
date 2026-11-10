import ast
import logging
import os
import re
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/app.py"


def load(fake_socket, fake_ssl, root):
    nodes = [node for node in ast.parse(APP.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name == "windows_https"]
    ns = dict(WINDOWS_HTTPS={}, time=time, re=re, os=os, socket=fake_socket, ssl=fake_ssl, WEB_ROOT=root,
              target_folder=lambda site_root, target: os.path.join(site_root, "scripts", "targets", target),
              app=SimpleNamespace(logger=logging.getLogger(__name__)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "windows https", "exec"), ns)
    return ns


class WindowsHttpsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.script = Path(self.folder.name, "site", "scripts", "targets", "prod", "publish_prod.sh")
        self.script.parent.mkdir(parents=True)
        self.script.write_text("PROD_BINDING='x.test'\nPROD_HTTPS=n                   # n = http\n")

    def tearDown(self):
        self.folder.cleanup()

    def fake_socket(self, error=None):
        fake = MagicMock()
        if error:
            fake.create_connection.side_effect = error
        return fake

    def test_a_site_that_answers_on_https_is_saved_as_https(self):
        fake_ssl = MagicMock(SSLError=ssl.SSLError)
        ns = load(self.fake_socket(), fake_ssl, self.folder.name)
        self.assertTrue(ns["windows_https"]("site", "prod", "x.test", False))
        self.assertIn("PROD_HTTPS=y                   # n = http", self.script.read_text())
        fake_ssl.create_default_context.return_value.wrap_socket.assert_called_once()

    def test_a_refused_connection_or_bad_certificate_is_http(self):
        self.script.write_text(self.script.read_text().replace("PROD_HTTPS=n", "PROD_HTTPS=y"))
        for error in (ConnectionRefusedError(), ssl.SSLCertVerificationError()):
            with self.subTest(error=type(error).__name__):
                ns = load(self.fake_socket(error), ssl, self.folder.name)
                self.assertFalse(ns["windows_https"]("site", "prod", "x.test", True))
                self.assertIn("PROD_HTTPS=n", self.script.read_text())
                self.script.write_text(self.script.read_text().replace("PROD_HTTPS=n", "PROD_HTTPS=y"))

    def test_no_answer_keeps_what_is_saved(self):
        for error in (socket.timeout(), socket.gaierror()):
            with self.subTest(error=type(error).__name__):
                ns = load(self.fake_socket(error), ssl, self.folder.name)
                self.assertFalse(ns["windows_https"]("site", "prod", "x.test", False))
                self.assertIn("PROD_HTTPS=n", self.script.read_text())


if __name__ == "__main__":
    unittest.main()
