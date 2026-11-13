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


if __name__ == "__main__":
    unittest.main()
