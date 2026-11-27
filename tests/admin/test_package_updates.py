import ast
from concurrent.futures import ThreadPoolExecutor
import glob
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request
from tests.admin.app_targets import add_target_helpers


ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin"
spec = importlib.util.spec_from_file_location("publish_updates", ROOT_DIR / "lib/publish_status.py")
tracking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracking)


def update_output(packages=None):
    return json.dumps({"version": 1, "projects": [{"frameworks": [
        {"framework": "net10.0", "topLevelPackages": packages or []}]}]})


PACKAGE_UPDATE = {"id": "Example", "resolvedVersion": "1.0", "latestVersion": "2.0"}


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "Example.csproj"
        self.project.write_text("<Project />")
        # NuGet is not asked: the newest version a project can use is the one dotnet named
        nuget = patch.object(tracking, "compatible_latest", side_effect=lambda name, current, frameworks: {"example": "2.0"}.get(name.lower()))
        nuget.start()
        # Nor is NuGet's cache cleared: its own test asks for it
        self.cache = patch.object(tracking, "clear_http_cache")
        self.cache.start()
        self.addCleanup(patch.stopall)
        self.addCleanup(nuget.stop)

    def state(self):
        return tracking.updates_status(self.root, tracking.fingerprint(self.root))

    def test_exact_command_and_no_updates_phrase(self):
        with patch.object(tracking.subprocess, "run", return_value=Mock(
                stdout=update_output())) as run:
            self.assertEqual(tracking.check_updates(self.root), "current")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["dotnet", "list", str(self.project), "package", "--outdated",
                                  "--format", "json", "--output-version", "1"])
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["env"]["DOTNET_CLI_UI_LANGUAGE"], "en-US")
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["timeout"], 180)
        self.assertEqual(self.state(), "current")

    def test_available_result_persists_and_project_changes_clear_flag(self):
        self.assertEqual(self.state(), "")
        with patch.object(tracking.subprocess, "run", return_value=Mock(stdout=update_output([PACKAGE_UPDATE]))):
            self.assertEqual(tracking.check_updates(self.root), "available")
        self.assertEqual(self.state(), "available")
        self.assertEqual(tracking.updates_details(self.root, tracking.fingerprint(self.root))["packages"],
                         [{"name": "Example", "current": "1.0", "latest": "2.0"}])
        self.assertEqual(json.loads((self.root / ".vns-publish/updates.json").read_text())["status"], "available")
        tracking.record(self.root, "test", tracking.fingerprint(self.root))
        self.project.write_text('<Project><PackageReference Include="Example" Version="2.0" /></Project>')
        self.assertEqual(self.state(), "")
        self.assertEqual(tracking.status(self.root, "test", tracking.fingerprint(self.root)), "pending")

    def test_a_check_asks_nuget_org_as_it_is_now(self):
        """A version just published was not seen: NuGet's http cache kept nuget.org's answer up to 30 minutes (VNS.Harbor 18.3.3, 1.0.92)"""
        self.cache.stop()
        with patch.object(tracking.subprocess, "run", return_value=Mock(stdout=update_output())) as run:
            tracking.check_updates(self.root)
        self.assertEqual(run.call_args_list[0].args[0][-4:], ["nuget", "locals", "http-cache", "--clear"])

    def test_a_development_version_is_offered_the_release_and_a_stable_one_no_prerelease(self):
        """A site on VNS.Harbor 18.3.3.1-dev.… (dev/nuget-dev.sh) was not offered 18.3.3.1: --outdated skips a prerelease (1.0.92)"""
        self.project.write_text('<Project><PackageReference Include="VNS.Harbor" Version="18.3.3.1-dev.1" /></Project>')
        output = update_output([{"id": "VNS.Harbor", "requestedVersion": "18.3.3.1-dev.1", "resolvedVersion": "18.3.3.1-dev.1", "latestVersion": "18.3.3.1"},
                                PACKAGE_UPDATE | {"latestVersion": "3.0-beta"}])
        with patch.object(tracking, "compatible_latest", side_effect=lambda name, current, frameworks: {"vns.harbor": "18.3.3.1"}.get(name.lower())), \
                patch.object(tracking.subprocess, "run", return_value=Mock(stdout=output)) as run:
            self.assertEqual(tracking.check_updates(self.root), "available")
        self.assertIn("--include-prerelease", run.call_args.args[0])
        self.assertEqual([item["name"] for item in tracking.updates_details(self.root, tracking.fingerprint(self.root))["packages"]], ["VNS.Harbor"])

    def test_all_projects_checked_and_any_update_flags_site(self):
        (self.root / "Other.csproj").write_text("<Project />")
        with patch.object(tracking.subprocess, "run", side_effect=[
                Mock(stdout=update_output()),
                Mock(stdout=update_output([PACKAGE_UPDATE]))]) as run:
            self.assertEqual(tracking.check_updates(self.root), "available")
        self.assertEqual(run.call_count, 2)

    def test_failed_checks_are_not_available_or_success(self):
        for error in (subprocess.CalledProcessError(1, ["dotnet"]),
                      subprocess.TimeoutExpired(["dotnet"], 180), OSError("not found")):
            with self.subTest(error=error), patch.object(tracking.subprocess, "run", side_effect=error):
                with self.assertRaises(RuntimeError):
                    tracking.check_updates(self.root)
                self.assertEqual(self.state(), "error")
        with patch.object(tracking.subprocess, "run", return_value=Mock(stdout="")):
            with self.assertRaises(RuntimeError):
                tracking.check_updates(self.root)
        self.assertEqual(self.state(), "error")

    def test_edits_during_check_invalidate_result(self):
        def edit(*args, **kwargs):
            self.project.write_text("<Project>Changed</Project>")
            return Mock(stdout=update_output([PACKAGE_UPDATE]))
        with patch.object(tracking.subprocess, "run", side_effect=edit):
            tracking.check_updates(self.root)
        self.assertEqual(self.state(), "")

    def test_invalid_status_is_explicit_error(self):
        tracking.record(self.root, "updates", tracking.fingerprint(self.root), status="invalid")
        with self.assertRaises(ValueError):
            self.state()

    def test_parser_rejects_invalid_and_conflicting_updates(self):
        for output in ("{}", '{"version": 1, "projects": []}', update_output([{"id": "Example"}])):
            with self.subTest(output=output), self.assertRaises(ValueError):
                tracking.parse_updates(output)
        output = update_output([PACKAGE_UPDATE, PACKAGE_UPDATE | {"latestVersion": "3.0"}])
        with self.assertRaises(ValueError):
            tracking.parse_updates(output)

    def test_parser_accepts_real_empty_dotnet_response_and_deduplicates_frameworks(self):
        self.assertEqual(tracking.parse_updates(json.dumps({
            "version": 1, "projects": [{"path": "/tmp/Empty.csproj"}]})), [])
        self.assertEqual(len(tracking.parse_updates(update_output([PACKAGE_UPDATE, PACKAGE_UPDATE]))), 1)

    def test_individual_update_uses_exact_checked_version_and_refreshes(self):
        tracking.record(self.root, "updates", tracking.fingerprint(self.root), status="available",
                        packages=[{"name": "Example", "current": "1.0", "latest": "2.0"},
                                  {"name": "Other", "current": "3.0", "latest": "4.0"}])
        with patch.object(tracking.subprocess, "run", return_value=Mock(returncode=0)) as run, \
                patch.object(tracking, "check_updates") as check:
            self.assertEqual(tracking.upgrade_packages(self.root, "Example"), 0)
        self.assertEqual(run.call_args.args[0],
                         ["dotnet", "add", str(self.project), "package", "Example", "--version", "2.0"])
        self.assertEqual(run.call_count, 1)
        check.assert_called_once_with(self.root, show_output=True, planned=True)

    def test_update_all_stops_at_failure_and_does_not_claim_success(self):
        tracking.record(self.root, "updates", tracking.fingerprint(self.root), status="available",
                        packages=[{"name": "Example", "current": "1.0", "latest": "2.0"},
                                  {"name": "Other", "current": "3.0", "latest": "4.0"}])
        with patch.object(tracking.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=7)]) as run, \
                patch.object(tracking, "check_updates") as check:
            self.assertEqual(tracking.upgrade_packages(self.root), 7)
        self.assertEqual(run.call_count, 2)
        check.assert_not_called()

    def test_update_all_snapshots_packages_before_fingerprint_changes(self):
        tracking.record(self.root, "updates", tracking.fingerprint(self.root), status="available",
                        packages=[{"name": "Example", "current": "1.0", "latest": "2.0"},
                                  {"name": "Other", "current": "3.0", "latest": "4.0"}])
        def upgrade(*args, **kwargs):
            self.project.write_text("<Project>Updated</Project>")
            return Mock(returncode=0)
        with patch.object(tracking.subprocess, "run", side_effect=upgrade) as run, \
                patch.object(tracking, "check_updates") as check:
            self.assertEqual(tracking.upgrade_packages(self.root), 0)
        self.assertEqual([call.args[0][-3:] for call in run.call_args_list],
                         [["Example", "--version", "2.0"], ["Other", "--version", "4.0"]])
        check.assert_called_once_with(self.root, show_output=True, planned=True)

    def test_upgrade_rejects_unknown_package_and_stale_check(self):
        tracking.record(self.root, "updates", tracking.fingerprint(self.root), status="available",
                        packages=[{"name": "Example", "current": "1.0", "latest": "2.0"}])
        with self.assertRaises(ValueError):
            tracking.upgrade_packages(self.root, "Unknown")
        self.project.write_text("<Project>Changed</Project>")
        with patch.object(tracking.subprocess, "run") as run, self.assertRaises(ValueError):
            tracking.upgrade_packages(self.root)
        run.assert_not_called()

    def test_frontend_flag_progress_and_error_handling(self):
        source = (APP / "static/app.js").read_text()
        monitor = source[source.index("let updateCheckTimer = null;"):source.index("async function loadSites()")]
        badges = source[source.index("function kindBadges("):source.index("function httpsLock(")]
        harness = r"""
const assert = require("node:assert/strict");
const document = {querySelectorAll: () => []};
const button = {replaceChildren(...children) {this.children = children;}};
const $ = () => button;
const el = (tag, properties, ...children) => Object.assign({children, append(child) {this.children.push(child);}}, properties);
const svgIcon = name => ({icon: name});
const kindLabel = {csharp: "C#"};
let callback, calls = [], notices = [], loads = 0, result, failed = false, job;
const setTimeout = action => {callback = action; return 1;};
const toast = (message, type) => notices.push({message, type});
const loadSites = async () => {loads++;};
const api = async url => {calls.push(url); if(failed) throw new Error("Offline"); return result;};
const run = action => {job = action().catch(error => toast(error.message, "error")); return job;};
(async () => {
    monitorUpdateCheck({running: true, completed: 1, total: 3});
    result = {running: false, total: 3, completed: 3, errors: []};
    callback(); await job;
    assert.equal(loads, 1);
    assert.equal(notices.at(-1).type, "ok");
    assert.deepEqual(calls, ["/api/package-updates"]);
    monitorUpdateCheck({running: true, completed: 0, total: 1});
    result = {running: false, total: 1, completed: 1, errors: ["broken.test"]};
    callback(); await job;
    assert.equal(notices.at(-1).type, "error");
    assert.ok(notices.at(-1).message.includes("broken.test"));
    monitorUpdateCheck({running: true, completed: 0, total: 1});
    failed = true; callback(); await job;
    assert.equal(notices.at(-1).message, "Offline");
    for (const [updates, label] of [["available", "Updates available"], ["error", "Update check failed"]]) {
        assert.ok(kindBadges({kind:"csharp", updates}).children.some(child => child.textContent === label));
    }
    assert.equal(kindBadges({kind:"csharp", updates:"current"}).children.length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(["node", "-e", monitor + badges + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_package_update_controls(self):
        source = (APP / "static/app.js").read_text()
        start = source.index("\t\tconst updates = new Map(")
        panel = source[start:source.index("\n\t}\n\tdecorate(head)", start)]
        harness = r"""
const assert = require("node:assert/strict");
const el = (tag, properties, ...children) => Object.assign({tag, children, setAttribute() {}, append(...items) {this.children.push(...items);}}, properties);
const svgIcon = name => ({icon: name});
const run = () => {}, openPackageDialog = () => {}, openTask = () => {}, updatePackages = () => {};
const unfoldedPackages = new Set();
const flatten = node => [node, ...(node.children || []).flatMap(flatten)];
const site = {name: "example", packages: [{name:"One",version:"1.0"},{name:"Two",version:"2.0"}],
    package_updates: [{name:"One",current:"1.0",latest:"3.0"},{name:"Two",current:"2.0",latest:"4.0"}], updates:"available"};
function render(site) {
    const packagesPanel = el("section", {classList: {toggle() {}, contains() {return false;}}});
    PANEL
    return flatten(packagesPanel);
}
let nodes = render(site);
assert.equal(nodes.filter(n => n.textContent === "Update").length, 2);
assert.equal(nodes.filter(n => n.textContent === "Update all").length, 1);
for (const version of ["1.0", "2.0", "3.0", "4.0"]) assert.ok(nodes.some(n => n.textContent === version));
for (const button of nodes.filter(n => n.tag === "button")) {
    assert.ok(!button.children.some(n => flatten(n).some(child => child.tag === "button")));
}
site.package_updates = site.package_updates.slice(0, 1);
assert.ok(!render(site).some(n => n.textContent === "Update all"));
site.package_updates = [];
assert.ok(render(site).some(n => n.textContent === "Run Check to load available package versions."));
"""
        result = subprocess.run(["node", "-e", harness.replace("PANEL", panel)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class UpdateCheckApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("source.test", "other.test", "admin.test"):
            (self.root / name).mkdir()
            (self.root / name / "Example.csproj").touch()
        for name in ("published.test", "python.test"):
            (self.root / name).mkdir()
        self.app = Flask(__name__)
        self.app.logger = logging.getLogger(__name__)
        self.state = {"running": False, "total": 0, "completed": 0, "errors": [], "pending": [], "results": {}}
        self.ns = dict(app=self.app, request=request, jsonify=jsonify, os=os, glob=glob,
                       WEB_ROOT=str(self.root), ADMIN_SITE="admin.test",
                       UPDATE_CHECK=self.state, UPDATE_CHECK_LOCK=threading.Lock(),
                       threading=SimpleNamespace(Thread=Mock()), ThreadPoolExecutor=ThreadPoolExecutor,
                       _publish_module=SimpleNamespace(check_updates=Mock()),
                       site_dirs=lambda: sorted(path.name for path in self.root.iterdir()),
                       error=lambda message, status: (jsonify(error=message), status))
        names = {"check_site_updates", "check_all_updates", "api_package_updates"}
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "update check API", "exec"), self.ns)
        add_target_helpers(self.ns)
        self.client = self.app.test_client()

    def test_start_filters_projects_and_duplicate_requests_do_not_restart(self):
        response = self.client.post("/api/package-updates")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["running"])
        self.assertEqual(response.json["total"], 2)
        self.assertEqual(response.json["pending"], ["other.test", "source.test"])
        thread = self.ns["threading"].Thread
        self.assertEqual(thread.call_args.kwargs["args"], (["other.test", "source.test"],))
        self.client.post("/api/package-updates")
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(self.client.get("/api/package-updates").json["completed"], 0)

    def test_start_checks_only_the_selected_sites_that_have_projects(self):
        response = self.client.post("/api/package-updates", json={"sites": ["source.test", "python.test", "unknown.test"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["pending"], ["source.test"])
        self.assertEqual(self.ns["threading"].Thread.call_args.kwargs["args"], (["source.test"],))

    def test_invalid_or_empty_selection_is_rejected_without_starting(self):
        for body in ({"sites": "source.test"}, {"sites": [1]}, {"sites": ["admin.test", "python.test"]}, {"sites": []}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/package-updates", json=body).status_code, 400)
        self.ns["threading"].Thread.assert_not_called()
        self.assertFalse(self.state["running"])

    def test_worker_progress_and_errors(self):
        self.state.update(running=True, total=2, pending=["source.test", "other.test"])
        self.ns["_publish_module"].check_updates.side_effect = ["current", RuntimeError("failed")]
        with self.assertLogs(__name__, level="ERROR"):
            self.ns["check_all_updates"](["source.test", "other.test"])
        self.assertFalse(self.state["running"])
        self.assertEqual(self.state["completed"], 2)
        self.assertEqual(len(self.state["errors"]), 1)
        self.assertEqual(self.state["pending"], [])
        self.assertEqual(list(self.state["results"].values()), ["current"])

    def test_four_sites_can_run_in_parallel(self):
        barrier = threading.Barrier(4, timeout=5)
        self.state.update(running=True, total=4)
        self.ns["_publish_module"].check_updates.side_effect = lambda root: barrier.wait()
        self.ns["check_all_updates"](["one", "two", "three", "four"])
        self.assertEqual(self.state["completed"], 4)
        self.assertEqual(self.state["errors"], [])
        self.assertFalse(self.state["running"])

    def test_thread_start_failure_is_reported(self):
        self.ns["threading"].Thread.return_value.start.side_effect = RuntimeError("failed")
        with self.assertLogs(__name__, level="ERROR"):
            response = self.client.post("/api/package-updates")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(self.state["running"])


if __name__ == "__main__":
    unittest.main()
