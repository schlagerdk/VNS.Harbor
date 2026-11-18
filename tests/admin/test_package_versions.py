"""A NuGet package added without a version gets the newest stable one from nuget.org, never Version="*\""""
import ast
import io
import json
from pathlib import Path
import re
import unittest
from unittest import mock
import urllib.parse
import urllib.request

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/app.py"


class BadRequest(Exception):
    pass


def abort(code, message):
    raise BadRequest(message)


class NewestVersionTests(unittest.TestCase):
    def setUp(self):
        source = APP.read_text()
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "newest_package_version"]
        version = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                       and any(getattr(target, "id", "") == "VERSION_RE" for target in node.targets))
        self.ns = {"urllib": urllib, "json": json, "re": re, "abort": abort, "app": mock.Mock()}
        exec(compile(ast.Module(body=[version, *nodes], type_ignores=[]), "app", "exec"), self.ns)
        self.source = source

    def answer(self, versions):
        return mock.patch.object(urllib.request, "urlopen", return_value=mock.MagicMock(
            __enter__=lambda self: io.BytesIO(json.dumps({"versions": versions}).encode())))

    def test_the_newest_stable_version(self):
        with self.answer(["17.0.6", "18.0.0", "18.3.1", "18.10.0", "19.0.0-rc1"]) as opened:
            self.assertEqual(self.ns["newest_package_version"]("VNS.Harbor"), "18.10.0")
        self.assertIn("/v3-flatcontainer/vns.harbor/index.json", opened.call_args[0][0].full_url)

    def test_none_found_asks_for_a_version(self):
        with self.answer([]), self.assertRaisesRegex(BadRequest, "Give the version"):
            self.ns["newest_package_version"]("Private.Package")
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("offline")), self.assertRaises(BadRequest):
            self.ns["newest_package_version"]("VNS.Harbor")

    def test_the_add_names_the_version(self):
        self.assertIn("version = version or newest_package_version(package)", self.source)



class DefaultUmbracoTests(unittest.TestCase):
    """A new Umbraco site is offered the newest Umbraco of the major version harbor site create is made for"""

    def setUp(self):
        import threading
        import time
        tree = ast.parse(APP.read_text())
        wanted = ("umbraco_version_key", "newest_umbraco_of", "ask_newest_umbraco", "default_umbraco_version")
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        version = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(getattr(target, "id", "") == "VERSION_RE" for target in node.targets))
        self.started = []
        thread = mock.Mock(side_effect=lambda target, daemon: self.started.append(target) or mock.Mock())
        self.ns = {"urllib": urllib, "json": json, "re": re, "os": __import__("os"), "time": time, "app": mock.Mock(),
                   "threading": mock.Mock(Thread=thread), "NEWEST_UMBRACO": {"versions": [], "checked": 0.0, "asking": False},
                   "NEWEST_UMBRACO_LOCK": threading.Lock(), "HARBOR_DIR": "/harbor",
                   "read_text": lambda path: 'DEFAULT_UMBRACO_VERSION = "18.3.1"\n'}
        exec(compile(ast.Module(body=[version, *nodes], type_ignores=[]), "app", "exec"), self.ns)

    def answer(self, versions):
        return mock.patch.object(urllib.request, "urlopen", return_value=mock.MagicMock(
            __enter__=lambda self: io.BytesIO(json.dumps({"versions": versions}).encode())))

    def test_the_newest_of_the_same_major_once_nuget_has_answered(self):
        # The first ask does not wait for nuget.org
        self.assertEqual(self.ns["default_umbraco_version"](), "18.3.1")
        self.assertEqual(len(self.started), 1)
        with self.answer(["17.9.0", "18.3.1", "18.3.2", "18.10.0-rc1", "19.0.0"]):
            self.started[0]()
        self.assertEqual(self.ns["default_umbraco_version"](), "18.3.2")
        # Kept an hour: not asked again
        self.assertEqual(len(self.started), 1)

    def test_nuget_out_of_reach_gives_the_known_version(self):
        self.ns["default_umbraco_version"]()
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("offline")):
            self.started[0]()
        self.assertEqual(self.ns["default_umbraco_version"](), "18.3.1")
        self.assertFalse(self.ns["NEWEST_UMBRACO"]["asking"])


if __name__ == "__main__":
    unittest.main()
