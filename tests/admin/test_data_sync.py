import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib import data_sync

ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT = ROOT_DIR
APP = ROOT / "admin"
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

    def test_by_default_only_the_main_site_sends_settings(self):
        config = {"t2": {"settings_receive": False}, "t3": {"settings_send": True}}
        data_sync.settings_allowed(config, "main", "t1")
        data_sync.settings_allowed(config, "t3", "t1")
        for source, destination, reason in (("t1", "t3", "t1 may not send settings"), ("main", "t2", "t2 may not receive settings"),
                                            ("t3", "main", "does not receive settings")):
            with self.subTest(source=source, destination=destination), self.assertRaisesRegex(ValueError, reason):
                data_sync.settings_allowed(config, source, destination)
        self.assertTrue(data_sync.clean({}, "main")["settings_send"])
        self.assertFalse(data_sync.clean({}, "staging")["settings_send"])
        self.assertTrue(data_sync.clean({}, "staging")["settings_receive"])

    def test_settings_are_saved_per_site_and_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "scripts").mkdir()
            data_sync.save(folder, "main", {"enabled": True, "send": "yes", "content": True, "unknown": True})
            data_sync.save(folder, "prod", ON)
            saved = data_sync.load(folder)
            self.assertEqual(saved["main"], {"enabled": True, "send": False, "receive": False, "content": True, "media": False,
                                             "settings_send": True, "settings_receive": True, "settings_import": None})
            self.assertEqual(saved["prod"], ON | {"settings_send": False, "settings_receive": True, "settings_import": None})
            with self.assertRaises(ValueError):
                data_sync.save(folder, "../x", ON)

    def test_appsettings_follow_the_settings_and_revert_when_turned_off(self):
        template = json.loads((ROOT / "templates/sites/csharp/umbraco/project/appsettings.json").read_text())
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

    def test_import_at_startup_is_set_by_sync_setup(self):
        template = json.loads((ROOT / "templates/sites/csharp/umbraco/project/appsettings.json").read_text())
        copy = lambda: json.loads(json.dumps(template))
        startup = lambda data: data["uSync"]["Settings"].get("ImportAtStartup")
        # Settings only: on imports the settings, off imports nothing, unset leaves the file as it is
        self.assertEqual(startup(data_sync.apply_appsettings(copy(), {"settings_import": True})), "Settings")
        self.assertEqual(startup(data_sync.apply_appsettings(copy(), {"settings_import": False})), "None")
        self.assertEqual(startup(data_sync.apply_appsettings(copy(), {})), startup(template))
        # A site that receives data imports all, whatever is chosen; a sender follows the choice
        self.assertEqual(startup(data_sync.apply_appsettings(copy(), dict(ON, settings_import=False))), "All")
        self.assertEqual(startup(data_sync.apply_appsettings(copy(), dict(ON, receive=False, settings_import=False))), "None")
        self.assertIs(data_sync.clean({"settings_import": "yes"})["settings_import"], None)

    def test_a_sender_writes_everything_at_startup(self):
        """Export on save only writes what is saved after: content made before has to be written once, at startup"""
        template = json.loads((ROOT / "templates/sites/csharp/umbraco/project/appsettings.json").read_text())
        sender = data_sync.apply_appsettings(json.loads(json.dumps(template)), dict(ON, receive=False))
        self.assertEqual(sender["uSync"]["Settings"]["ExportAtStartup"], "All")
        receiver = data_sync.apply_appsettings(json.loads(json.dumps(sender)), dict(ON, send=False))
        self.assertEqual(receiver["uSync"]["Settings"]["ExportAtStartup"], "None")
        off = data_sync.apply_appsettings(json.loads(json.dumps(sender)), {"enabled": False})
        self.assertEqual(off["uSync"]["Settings"]["ExportAtStartup"], "None")
        untouched = data_sync.apply_appsettings(json.loads(json.dumps(template)), dict(ON, send=False))
        self.assertNotIn("ExportAtStartup", untouched["uSync"]["Settings"])

    def test_a_sync_without_written_content_stops_and_says_why(self):
        script = (ROOT / "tests/legacy/data_sync.sh").read_text()
        self.assertIn('if ((_wantsContent)) && ! has_exported_content "${_from}"; then', script)
        self.assertLess(script.index("has_exported_content \"${_from}\""), script.index("sync_one \"${_id}\""))

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
        script = (ROOT / "tests/legacy/sync_env.sh").read_text()
        self.assertIn("_rcloneMode=copy", script)
        self.assertIn("_rsyncDelete=()", script)
        self.assertIn("_versionFolders+=(Content Dictionary)", script)
        self.assertIn("_versionFolders+=(Media)", script)
        self.assertNotIn("Domains", script.split("_versionFolders=(")[1].split("for _version")[0])

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
