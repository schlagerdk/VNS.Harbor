"""Update Python packages is shown only when pip says a package in requirements.txt has a newer version"""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

APP = Path(__file__).resolve().parents[2] / "admin/app.py"


class PythonOutdatedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        site = self.root / "py.example"
        (site / "venv/bin").mkdir(parents=True)
        (site / "app").mkdir()
        (site / "app/requirements.txt").write_text("Flask==3.0  # web\nrequests>=2\n-r other.txt\n")
        self.calls = self.root / "calls"
        listed = [{"name": "flask", "version": "3.0.0", "latest_version": "3.1.0"},
                  {"name": "Werkzeug", "version": "3.0.0", "latest_version": "3.1.0"}]
        pip = site / "venv/bin/pip"
        pip.write_text(f"#!/bin/sh\necho x >> {self.calls}\necho '{json.dumps(listed)}'\n")
        pip.chmod(0o755)
        names = {"requirement_names", "python_outdated"}
        nodes = [node for node in ast.parse(APP.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names
                 or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("PIP_OUTDATED", "PIP_OUTDATED_FOR")]
        self.ns = dict(os=os, re=re, json=json, time=time, subprocess=subprocess, WEB_ROOT=str(self.root),
                       read_text=lambda path: Path(path).read_text() if Path(path).is_file() else "",
                       app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "python outdated", "exec"), self.ns)

    def test_only_the_packages_it_requires_and_asked_again_only_after_an_hour_or_an_update(self):
        found = self.ns["python_outdated"]("py.example")
        self.assertEqual(found, [{"name": "flask", "version": "3.0.0", "latest": "3.1.0"}], "Werkzeug is not in requirements.txt")
        self.ns["python_outdated"]("py.example")
        self.assertEqual(self.calls.read_text().count("x"), 1, "kept, not asked again")
        self.ns["PIP_OUTDATED"].pop("py.example")
        self.ns["python_outdated"]("py.example")
        self.assertEqual(self.calls.read_text().count("x"), 2, "after Update Python packages")
        self.assertEqual(self.ns["python_outdated"]("nowhere.example"), [], "no venv: nothing")


if __name__ == "__main__":
    unittest.main()
