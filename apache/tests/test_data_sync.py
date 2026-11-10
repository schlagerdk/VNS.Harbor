import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apache.lib import data_sync

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "solutionfiles/admin/app"
ON = {"enabled": True, "send": True, "receive": True, "content": True, "media": True}


class DataSyncTests(unittest.TestCase):
    def test_both_sites_must_have_it_on_and_the_right_direction(self):
        config = {"staging": dict(ON, receive=False), "prod": dict(ON, send=False), "main": dict(ON, enabled=False)}
        self.assertEqual(data_sync.kinds(config, "staging", "prod"), ["content", "media"])
        for source, destination, reason in (("prod", "staging", "may not send"), ("main", "prod", "Data sync is off for main"),
                                            ("staging", "main", "Data sync is off for main"), ("prod", "prod", "the same"),
                                            ("staging", "test", "Data sync is off for test")):
            with self.subTest(source=source, destination=destination):
                with self.assertRaisesRegex(ValueError, reason):
                    data_sync.kinds(config, source, destination)

    def test_only_what_both_take_part_with_is_synced(self):
        config = {"a": dict(ON, media=False), "b": ON, "c": dict(ON, content=False)}
        self.assertEqual(data_sync.kinds(config, "b", "a"), ["content"])
        with self.assertRaisesRegex(ValueError, "in common"):
            data_sync.kinds(config, "a", "c")

    def test_settings_are_saved_per_site_and_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "scripts").mkdir()
            data_sync.save(folder, "main", {"enabled": True, "send": "yes", "content": True, "unknown": True})
            data_sync.save(folder, "prod", ON)
            saved = data_sync.load(folder)
            self.assertEqual(saved["main"], {"enabled": True, "send": False, "receive": False, "content": True, "media": False})
            self.assertEqual(saved["prod"], ON)
            with self.assertRaises(ValueError):
                data_sync.save(folder, "../x", ON)

    def test_appsettings_follow_the_settings_and_revert_when_turned_off(self):
        template = json.loads((ROOT / "solutionfiles/csharp/umbraco/project/appsettings.json").read_text())
        sender = data_sync.apply_appsettings(json.loads(json.dumps(template)), dict(ON, receive=False, media=False))
        usync = sender["uSync"]
        self.assertEqual(usync["Sets"]["Default"]["DisabledHandlers"], ["MediaHandler"])
        self.assertEqual(usync["Settings"]["ExportOnSave"], "All")
        self.assertEqual(usync["Settings"]["ImportAtStartup"], "Settings")
        self.assertEqual(usync["Settings"]["UiEnabledGroups"], "Settings,Content")
        self.assertTrue(usync["Settings"]["FailOnMissingParent"])
        receiver = data_sync.apply_appsettings(json.loads(json.dumps(template)), dict(ON, send=False))
        self.assertEqual(receiver["uSync"]["Sets"]["Default"]["DisabledHandlers"], [])
        self.assertEqual(receiver["uSync"]["Settings"]["ImportAtStartup"], "All")
        self.assertEqual(receiver["uSync"]["Settings"]["ExportOnSave"], "Settings")
        off = data_sync.apply_appsettings(receiver, {"enabled": False})
        self.assertEqual(off["uSync"]["Sets"]["Default"]["DisabledHandlers"], ["ContentHandler", "MediaHandler"])
        self.assertEqual(off["uSync"]["Settings"]["ImportAtStartup"], "Settings")
        self.assertEqual(off["uSync"]["Settings"]["ExportOnSave"], "All")
        self.assertEqual(off["uSync"]["Settings"]["UiEnabledGroups"], "Settings")

    def test_command_line_reports_why_a_sync_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "scripts").mkdir()
            data_sync.save(folder, "staging", ON)
            result = subprocess.run(["python3", str(ROOT / "lib/data_sync.py"), "kinds", folder, "staging", "prod"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Data sync is off for prod", result.stderr)
            data_sync.save(folder, "prod", dict(ON, media=False))
            result = subprocess.run(["python3", str(ROOT / "lib/data_sync.py"), "kinds", folder, "staging", "prod"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "content")

    def test_data_is_merged_and_settings_are_mirrored(self):
        script = (ROOT / "sync_env.sh").read_text()
        self.assertIn("_rcloneMode=copy", script)
        self.assertIn("_rsyncDelete=()", script)
        self.assertIn("_versionFolders+=(Content Dictionary)", script)
        self.assertIn("_versionFolders+=(Media)", script)
        self.assertNotIn("Domains", script.split("_versionFolders=(")[1].split("for _version")[0])
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "sync_env.sh")]).returncode, 0)
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "data_sync.sh")]).returncode, 0)

    def test_the_admin_offers_only_allowed_destinations(self):
        source = (APP / "static/app.js").read_text()
        start = source.index("const dataSyncKinds")
        code = source[start:source.index("function dataSyncSummary", start)]
        script = """
const isMissingEnv = (env) => env.missing;
const sortedEnvironments = (list) => list;
""" + code + """
const staging = {target: "staging", data_sync: {enabled: true, send: true, content: true, media: true}};
const prod = {target: "prod", data_sync: {enabled: true, receive: true, content: true}};
const test = {target: "test", data_sync: {enabled: false, receive: true, content: true}};
const gone = {target: "gone", missing: true, data_sync: {enabled: true, receive: true, content: true}};
const site = {environments: [staging, prod, test, gone]};
console.log(JSON.stringify([dataSyncDestinations(site, staging).map((env) => env.target), dataSyncKinds(staging, prod), dataSyncDestinations(site, prod)]));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [["prod"], ["content"], []])


if __name__ == "__main__":
    unittest.main()
