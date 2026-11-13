import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock

from flask import Flask, abort, jsonify, request
from tests.admin.app_targets import add_target_helpers


ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT = ROOT_DIR
APP = ROOT / "admin"


class SystemUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.marker = self.root / ".vns-version"
        self.installed = "a" * 40
        self.latest = "b" * 40
        self.app = Flask(__name__)
        self.run = Mock(return_value=Mock(stdout=self.latest + "\tHEAD\n"))
        self.clock = Mock(return_value=1000)
        self.ns = dict(app=self.app, jsonify=jsonify, request=request, Path=Path, BASE=str(self.root), DATA=str(self.root), HARBOR_DIR=str(self.root), LIB_DIR=str(self.root / "lib"), json=json, os=os,
                       read_text=lambda path: Path(path).read_text() if Path(path).is_file() else "", re=re,
                       subprocess=Mock(run=self.run, SubprocessError=subprocess.SubprocessError),
                       SYSTEM_UPDATE_LOCK=threading.Lock(), SYSTEM_UPDATE_CACHE={},
                       time=Mock(monotonic=self.clock), GITHUB_REPO="schlagerdk/another", INSTALLED_FROM="schlagerdk/another",
                       installed_version=lambda: self.version, github_releases=lambda: self.releases,
                       release_versions=lambda: [release["version"] for release in self.releases or []] if self.tags is None else self.tags,
                       commit_details=lambda installed, latest: {"compare_url": "compare", "latest_commit": {"subject": "fix: x"}},
                       version_key=lambda value: tuple(int(part) for part in value.split(".")))
        self.version = ""
        self.releases = None
        self.tags = None
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("api_system_update", "installed_revision")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "system updates", "exec"), self.ns)
        add_target_helpers(self.ns)
        self.client = self.app.test_client()

    def test_changed_and_matching_revisions_and_cache(self):
        self.marker.write_text(self.installed)
        response = self.client.get("/api/system-update")
        self.assertEqual(response.json["status"], "available")
        self.assertEqual(response.json["installed"], self.installed)
        self.assertEqual(response.json["latest"], self.latest)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 15)
        self.client.get("/api/system-update")
        self.assertEqual(self.run.call_count, 1)
        self.clock.return_value = 1061
        self.run.return_value.stdout = self.installed + "\tHEAD\n"
        self.assertEqual(self.client.get("/api/system-update").json["status"], "current")
        self.assertEqual(self.run.call_count, 2)

    def test_releases_decide_once_there_are_some(self):
        self.marker.write_text(self.installed)
        self.version = "1.0.0"
        self.releases = [{"version": "1.2.0", "url": "r12", "notes": "b"}, {"version": "1.1.0", "url": "r11", "notes": "a"},
                         {"version": "1.0.0", "url": "r10", "notes": ""}]
        result = self.client.get("/api/system-update?refresh=1").json
        self.assertEqual((result["status"], result["mode"], result["latest_version"]), ("available", "release", "1.2.0"))
        self.assertEqual([release["version"] for release in result["releases"]], ["1.2.0", "1.1.0"])
        self.assertIn("compare/v1.0.0...v1.2.0", result["compare_url"])
        self.version = "1.2.0"
        self.assertEqual(self.client.get("/api/system-update?refresh=1").json["status"], "current")

    def test_the_version_is_known_when_the_notes_cannot_be_read(self):
        # GitHub's API allows few calls without a login; the tags come from git
        self.marker.write_text(self.installed)
        self.version = "1.0.2"
        self.tags = ["1.0.3", "1.0.2"]
        self.releases = None
        result = self.client.get("/api/system-update?refresh=1").json
        self.assertEqual((result["mode"], result["latest_version"], result["status"]), ("release", "1.0.3", "available"))
        self.assertTrue(result["notes_unavailable"])
        self.assertEqual(result["releases"][0]["notes"], "")

    def test_from_vns_linux_to_vns_harbor(self):
        """Installed from another repository, updated from VNS.Harbor: any release there is newer, as Harbor starts its own versions,
        and before its first release there is nothing to update to, never its branch"""
        self.ns.update(GITHUB_REPO="schlagerdk/VNS.Harbor", INSTALLED_FROM="schlagerdk/another")
        self.marker.write_text(self.installed)
        self.version = "1.3.0"
        self.tags = []
        result = self.client.get("/api/system-update?refresh=1").json
        self.assertEqual(result["status"], "current")
        self.assertEqual(self.run.call_count, 0, "not its HEAD")
        self.tags = ["1.0.0"]
        result = self.client.get("/api/system-update?refresh=1").json
        self.assertEqual((result["status"], result["latest_version"]), ("available", "1.0.0"))

    def test_before_the_first_release_the_latest_commit_is_shown(self):
        self.marker.write_text(self.installed)
        result = self.client.get("/api/system-update?refresh=1").json
        self.assertEqual((result["status"], result["mode"]), ("available", "commit"))
        self.assertEqual(result["latest_commit"]["subject"], "fix: x")

    def test_missing_and_empty_markers_are_unknown(self):
        for marker in (None, ""):
            with self.subTest(marker=marker):
                self.ns["SYSTEM_UPDATE_CACHE"].clear()
                if marker is not None:
                    self.marker.write_text(marker)
                self.assertEqual(self.client.get("/api/system-update").json["status"], "unknown")

    def test_explicit_refresh_bypasses_cached_revision(self):
        self.marker.write_text(self.installed)
        self.run.return_value.stdout = self.installed + "\tHEAD\n"
        self.assertEqual(self.client.get("/api/system-update").json["status"], "current")
        self.run.return_value.stdout = self.latest + "\tHEAD\n"
        self.assertEqual(self.client.get("/api/system-update").json["status"], "current")
        self.assertEqual(self.client.get("/api/system-update?refresh=1").json["status"], "available")
        self.assertEqual(self.run.call_count, 2)

    def test_malformed_markers_remote_responses_and_network_errors_are_explicit(self):
        for marker, output, failure in (
                ("invalid", self.latest + "\tHEAD", None),
                (self.installed, "", None),
                (self.installed, self.latest + "\trefs/heads/main", None),
                (self.installed, "", subprocess.TimeoutExpired("git", 15)),
                (self.installed, "", subprocess.CalledProcessError(128, "git"))):
            with self.subTest(marker=marker, output=output, failure=failure):
                self.ns["SYSTEM_UPDATE_CACHE"].clear()
                self.marker.write_text(marker)
                self.run.side_effect = failure
                self.run.return_value.stdout = output
                with self.assertLogs(self.app.logger, level="ERROR"):
                    response = self.client.get("/api/system-update")
                self.assertEqual(response.json["status"], "error")
                self.assertIn("error", response.json)

    def test_update_job_survives_new_app_and_reports_failures(self):
        helper = ROOT / "lib/system_update.py"
        self.ns.update(abort=abort, json=json, uuid=uuid, os=os, sys=__import__("sys"),
                       HARBOR_DIR=str(ROOT), LIB_DIR=str(ROOT / "lib"), time=time,
                       build_command=Mock(return_value=("Update", [sys.executable, "-c", "pass"], "/")),
                       update_target=lambda: "v1.1.0")
        self.ns["subprocess"] = subprocess
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "api_run_system_update"]
        self.app = Flask("update jobs")
        self.ns["app"] = self.app
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "update jobs", "exec"), self.ns)
        add_target_helpers(self.ns)
        client = self.app.test_client()
        self.assertEqual(client.get("/api/system-update/run").json["status"], "idle")
        from unittest.mock import patch
        with patch.object(subprocess, "Popen") as launch:
            response = client.post("/api/system-update/run")
        job = response.json["id"]
        self.assertEqual(response.json["status"], "running")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        status = self.root / "system-update.json"
        self.assertEqual(status.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "system-update.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual(client.post("/api/system-update/run").status_code, 409)
        for code, expected in ((0, "success"), (1, "error")):
            subprocess.run([sys.executable, str(helper), str(status), job,
                            sys.executable, "-c", f"raise SystemExit({code})"], check=True)
            state = client.get("/api/system-update/run").json
            self.assertEqual(state["id"], job)
            self.assertEqual(state["status"], expected)
        self.assertIn("code 1", state["error"])
        # The job's state is kept with the admin's data, which an update never replaces (harbor/install/admin.py)
        app = (APP / "app.py").read_text()
        for name in ("system-update.json", "system-update.log"):
            self.assertIn(f'Path(DATA) / "{name}"', app)

if __name__ == "__main__":
    unittest.main()
