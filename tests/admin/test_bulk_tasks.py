import ast
import glob
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from flask import abort
from werkzeug.exceptions import BadRequest
from tests.admin.app_targets import add_target_helpers


ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin"
spec = importlib.util.spec_from_file_location("bulk_tasks", ROOT_DIR / "lib/bulk_tasks.py")
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("one", "two"):
            scripts = self.root / name / "scripts"
            scripts.mkdir(parents=True)
            (scripts.parent / "Project.csproj").write_text("<Project />")
            (scripts / "publish.sh").write_text("exit 0\n")
        (self.root / "one/scripts/publish_prod.sh").write_text("exit 0\n")
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "build_command"]
        self.ns = dict(os=os, glob=glob, sys=sys, json=json, abort=abort,
                       WEB_ROOT=str(self.root), HARBOR_DIR=str(ROOT_DIR), LIB_DIR=str(ROOT_DIR / "lib"),
                       SCRIPT_RE=re.compile(r"^[A-Za-z0-9_.-]+\.sh$"),
                       PACKAGE_RE=re.compile(r"^[A-Za-z0-9_.-]+$"),
                       VERSION_RE=re.compile(r"^[A-Za-z0-9_.-]+$"),
                       site_arg=lambda data: data["site"], site_info=self.info, newest_package_version=lambda package: "9.9.9")
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "bulk API", "exec"), self.ns)
        add_target_helpers(self.ns)
    def info(self, name, _):
        return {"kind": "csharp", "scripts": [
            path.name for path in (self.root / name / "scripts").glob("*.sh")]}

    def command(self, **kwargs):
        return self.ns["build_command"]({"kind": "bulk", "sites": ["one", "two"], **kwargs})

    def test_scoped_publish_and_missing_production(self):
        for action in ("local", "prod", "both"):
            _, argv, cwd = self.command(action=action)
            tasks = json.loads(argv[-1])
            self.assertEqual(len(tasks), 2)
            self.assertEqual(bool(tasks[1].get("skip")), action != "local")
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Multi action summary", result.stdout)
            self.assertIn("one: OK", result.stdout)
            if action != "local":
                self.assertIn("two: SKIPPED", result.stdout)

    def test_clear_logs_of_main_sites_environments_or_all(self):
        # one is an Umbraco site with an environment, two is not Umbraco
        self.ns["site_info"] = lambda name, _: dict(self.info(name, _), kind="umbraco" if name == "one" else "csharp")
        self.ns["remote_status"] = lambda site, target="prod": {"exists": True}
        for script in ("clear_logs.sh", "clear_logs_prod.sh"):
            (self.root / "one/scripts" / script).write_text("exit 0\n")
        expected = {"clear_logs": ["one"], "clear_logs_prod": ["one · prod"], "clear_logs_all": ["one", "one · prod"]}
        for action, names in expected.items():
            with self.subTest(action=action):
                _, argv, cwd = self.command(action=action)
                tasks = json.loads(argv[-1])
                self.assertEqual([task["site"] for task in tasks if not task.get("skip")], names)
                self.assertEqual([task["site"] for task in tasks if task.get("skip")], ["two"])
                result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("two: SKIPPED: Not an Umbraco site", result.stdout)

    def test_sync_goes_from_each_main_site_to_the_environments_its_setup_allows(self):
        """Sync in Multi action: settings, content and media, or both, the settings first in the same task"""
        import shlex
        spec = importlib.util.spec_from_file_location("data_sync", ROOT_DIR / "lib/data_sync.py")
        data_sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(data_sync)
        data_sync.HARBOR_STATE = self.root / "state"
        (self.root / "one/scripts/data_sync.json").write_text(json.dumps({
            "main": {"enabled": True, "send": True, "content": True, "media": True},
            "prod": {"enabled": True, "receive": True, "content": True, "media": True},
            "staging": {"enabled": True, "receive": False}}))
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "multi_sync_task"]
        made = []

        def build_command(data):
            made.append(data)
            return "", ["harbor", data["kind"], data["destination"], *(["--settings-first"] if data.get("settings_first") else [])], ""
        ns = dict(os=os, shlex=shlex, WEB_ROOT=str(self.root), HARBOR_DIR="/opt/harbor", _data_sync_module=data_sync,
                  build_command=build_command, target_ids=lambda root: ["prod", "staging"], environment_order=lambda root, targets: targets,
                  allowed_targets=lambda site, targets, verb: targets)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "multi sync", "exec"), ns)
        task = ns["multi_sync_task"]
        umbraco = {"kind": "umbraco"}
        self.assertEqual(task("one", umbraco, "sync_settings")["argv"], ["harbor", "sync_env", "prod,staging"], "every environment that takes settings")
        self.assertEqual(task("one", umbraco, "sync_data")["argv"], ["harbor", "data_sync", "prod"], "only the one that takes content and media")
        both = task("one", umbraco, "sync_all")["argv"]
        self.assertEqual(both[:2], ["/bin/bash", "-c"])
        self.assertLess(both[2].index("data_sync prod --settings-first"), both[2].index("sync_env staging"), "one after the other, in one task")
        self.assertIn("exit ${failed:-0}", both[2])
        self.assertEqual(task("two", {"kind": "csharp"}, "sync_all"), {"skip": "Not an Umbraco site"})
        ns["target_ids"] = lambda root: []
        self.assertEqual(task("one", umbraco, "sync_data"), {"skip": "No environments"})

    def test_multi_sync_names_who_started_it_as_a_sync_on_a_card(self):
        """The sync history names the user of a multi sync from the overview, as of one from an environment's card"""
        app = (APP / "app.py").read_text()
        start = app.index('if payload.get("kind") in ("sync_env", "data_sync")')
        block = app[start:app.index('"HARBOR_SYNC_WHO_ID"', start)]
        self.assertIn('payload.get("kind") == "bulk" and str(payload.get("action") or "").startswith("sync_")', block)

    def test_package_commands_reuse_validation(self):
        for action in ("add", "remove"):
            _, argv, _ = self.command(action=action, package="Example.Package", version="1.2.3")
            tasks = json.loads(argv[-1])
            for task in tasks:
                self.assertEqual(task["argv"][:4], ["dotnet", action, "package", "Example.Package"])
                self.assertEqual("--version" in task["argv"], action == "add")
        with self.assertRaises(BadRequest):
            self.command(action="add", package="bad;command")

    def test_all_bulk_actions_require_project_file_in_site_root(self):
        project = self.root / "two/Project.csproj"
        project.unlink()
        nested = project.parent / "nested"
        nested.mkdir()
        (nested / "Project.csproj").touch()
        project.mkdir()
        for action in ("check", "add", "remove", "local", "prod", "both", "outdated"):
            with self.subTest(action=action), self.assertRaises(BadRequest):
                self.command(action=action, package="Example")

    def test_checked_package_update_commands_and_validation(self):
        self.ns["site_info"] = lambda *_: {
            "kind": "csharp", "updates": "available",
            "package_updates": [{"name": "Example", "current": "1.0", "latest": "2.0"}],
        }
        for package in (None, "Example"):
            payload = {"kind": "update_package", "site": "one"}
            if package:
                payload["package"] = package
            _, argv, _ = self.ns["build_command"](payload)
            self.assertEqual(argv[2:4], ["upgrade", str(self.root / "one")])
            self.assertEqual(argv[4:], [package] if package else [])
        with self.assertRaises(BadRequest):
            self.ns["build_command"]({"kind": "update_package", "site": "one", "package": "Unknown"})

    def test_outdated_upgrades_each_project_without_interactive_script(self):
        _, argv, _ = self.command(action="outdated")
        for task in json.loads(argv[-1]):
            self.assertEqual(task["argv"], ["dotnet", "outdated", "--upgrade"])
            self.assertTrue(Path(task["cwd"], "Project.csproj").is_file())

    def test_check_lists_outdated_packages_without_upgrading(self):
        _, argv, _ = self.command(action="check")
        for task in json.loads(argv[-1]):
            self.assertEqual(task["argv"], [sys.executable, str(ROOT_DIR / "lib/publish_status.py"),
                                            "check", task["cwd"]])
            self.assertTrue(Path(task["cwd"], "Project.csproj").is_file())

    def test_bulk_check_persists_update_badges_and_clears_them(self):
        tools = self.root / "tools"
        tools.mkdir()
        dotnet = tools / "dotnet"
        dotnet.write_text('#!/bin/bash\nprintf "%s\\n" "$CHECK_OUTPUT"\nexit "${CHECK_EXIT:-0}"\n')
        dotnet.chmod(0o700)
        _, argv, cwd = self.command(action="check")
        available = json.dumps({"version": 1, "projects": [{"frameworks": [{"topLevelPackages": [
            {"id": "Example", "resolvedVersion": "1.0", "latestVersion": "2.0"}]}]}]})
        current = json.dumps({"version": 1, "projects": [{"frameworks": []}]})
        for output, code, expected in (
            (available, "0", "available"),
            (current, "0", "current"),
            ("error: feed unavailable", "1", "error"),
        ):
            with self.subTest(expected=expected):
                result = subprocess.run(
                    argv, cwd=cwd, capture_output=True, text=True,
                    env=os.environ | {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                      "CHECK_OUTPUT": output, "CHECK_EXIT": code})
                self.assertEqual(result.returncode, int(code != "0"), result.stderr)
                self.assertIn("Example: 1.0 -> 2.0" if expected == "available" else
                              "No updates available" if expected == "current" else output, result.stdout)
                for site in ("one", "two"):
                    state = json.loads((self.root / site / ".vns-publish/updates.json").read_text())
                    self.assertEqual(state["status"], expected)

    def test_sites_start_concurrently_and_output_is_labelled(self):
        script = (
            "import pathlib, sys, time\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "name, other = sys.argv[2:]\n"
            "(root / name).touch()\n"
            "deadline = time.monotonic() + 5\n"
            "while not (root / other).exists():\n"
            "    if time.monotonic() >= deadline: sys.exit(9)\n"
            "    time.sleep(0.01)\n"
            "print('concurrent')\n"
        )
        tasks = [
            {"site": name, "argv": [sys.executable, "-c", script, str(self.root), name, other],
             "cwd": str(self.root)}
            for name, other in (("started-one", "started-two"), ("started-two", "started-one"))
        ]
        result = subprocess.run(
            [sys.executable, str(ROOT_DIR / "lib/bulk_tasks.py"), json.dumps(tasks)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for task in tasks:
            self.assertIn(f'[{task["site"]}] concurrent', result.stdout)
            self.assertIn(f'{task["site"]}: OK', result.stdout)

    def test_no_more_than_four_sites_run_at_once(self):
        """17 syncs at once to one server: its sshd reset the logins over its limit, and 7 sites failed"""
        script = (
            "import fcntl, os, pathlib, sys, time\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "with open(root / 'lock', 'a+') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    running = int((root / 'running').read_text() or 0) + 1 if (root / 'running').exists() else 1\n"
            "    (root / 'running').write_text(str(running))\n"
            "    most = max(running, int((root / 'most').read_text()) if (root / 'most').exists() else 0)\n"
            "    (root / 'most').write_text(str(most))\n"
            "time.sleep(0.3)\n"
            "with open(root / 'lock', 'a+') as lock:\n"
            "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
            "    (root / 'running').write_text(str(int((root / 'running').read_text()) - 1))\n"
        )
        tasks = [{"site": f"site-{number}", "argv": [sys.executable, "-c", script, str(self.root)], "cwd": str(self.root)}
                 for number in range(10)]
        result = subprocess.run([sys.executable, str(ROOT_DIR / "lib/bulk_tasks.py"), json.dumps(tasks)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(int((self.root / "most").read_text()), 4, "four at a time, and four did run together")
        self.assertEqual(result.stdout.count(": OK"), 10, "and every site ran")

    def test_steps_are_named_with_their_site_and_each_site_starts_and_ends(self):
        """The window shows each running site's newest step as active: several sites run at once"""
        step = 'import sys; sys.stdout.write("\\x1b]7777;harbor;{\\"type\\": \\"step\\", \\"text\\": \\"Sync settings\\"}\\x07Sync settings\\n"); sys.exit(int(sys.argv[1]))'
        tasks = [{"site": "one", "argv": [sys.executable, "-c", step, "0"], "cwd": str(self.root)},
                 {"site": "two", "argv": [sys.executable, "-c", step, "1"], "cwd": str(self.root)}]
        result = subprocess.run([sys.executable, str(ROOT_DIR / "lib/bulk_tasks.py"), json.dumps(tasks)], capture_output=True, text=True,
                                timeout=30, env={**os.environ, "HARBOR_PROGRESS": "1"})
        events = [json.loads(match) for match in re.findall(r"\x1b\]7777;harbor;(\{.*?\})\x07", result.stdout)]
        for site, end in (("one", "done"), ("two", "failed")):
            mine = [event for event in events if event.get("site") == site]
            self.assertEqual(mine, [{"type": "site", "site": site, "state": "running"}, {"type": "step", "text": "Sync settings", "site": site},
                                    {"type": "site", "site": site, "state": end}])
        result = subprocess.run([sys.executable, str(ROOT_DIR / "lib/bulk_tasks.py"), json.dumps(tasks)], capture_output=True, text=True,
                                timeout=30, env={key: value for key, value in os.environ.items() if key != "HARBOR_PROGRESS"})
        self.assertNotIn('"type": "site"', result.stdout, "no events where they are not wanted")

    def test_invalid_selection_and_non_csharp_are_rejected(self):
        for sites in ([], ["one", "one"], ["one"] * 101, [1]):
            with self.assertRaises(BadRequest):
                self.command(action="local", sites=sites)
        self.ns["site_info"] = lambda *_: {"kind": "python"}
        with self.assertRaises(BadRequest):
            self.command(action="local")

    def test_continues_after_failure_and_reports_all_sites(self):
        (self.root / "one/scripts/publish.sh").write_text("exit 7\n")
        _, argv, cwd = self.command(action="local")
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("one: FAILED: exit 7", result.stdout)
        self.assertIn("two: OK", result.stdout)

    def test_spawn_failure_does_not_stop_next_site(self):
        tasks = [
            {"site": "one", "argv": ["/nonexistent/vns-command"], "cwd": str(self.root)},
            {"site": "two", "argv": [sys.executable, "-c", "pass"], "cwd": str(self.root)},
        ]
        self.assertEqual(bulk.run(tasks), 1)

    def test_both_skips_failed_sites_production_but_continues_next_site(self):
        (self.root / "one/scripts/publish.sh").write_text("exit 7\n")
        (self.root / "one/scripts/publish_prod.sh").write_text("touch should-not-run\n")
        (self.root / "two/scripts/publish_prod.sh").write_text("exit 0\n")
        _, argv, cwd = self.command(action="both")
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "one/should-not-run").exists())
        self.assertIn("two: OK", result.stdout)

    def test_script_symlink_outside_site_is_rejected(self):
        (self.root / "one/scripts/publish.sh").unlink()
        outside = self.root / "outside.sh"
        outside.write_text("exit 0\n")
        (self.root / "one/scripts/publish.sh").symlink_to(outside)
        with self.assertRaises(BadRequest):
            self.command(action="local")


if __name__ == "__main__":
    unittest.main()
