from pathlib import Path
import subprocess
import unittest


APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/static/app.js"


class ScriptMenuTests(unittest.TestCase):
    def test_custom_scripts_are_grouped_by_filename_prefix(self):
        source = APP.read_text()
        start = source.index("function customScriptGroups(files) {")
        end = source.index("\nfunction scriptSelect(site", start)
        helper = source[start:end]
        harness = r"""
const assert = require("node:assert/strict");
assert.deepEqual(customScriptGroups([
    "sync_to_windows.sh", "publish_website.sh", "sync_one.sh",
    "publish_all.sh", "sync_all.sh", "backup.sh"
]), [
    {label: "Publish", items: [
        {file: "publish_all.sh", label: "All"},
        {file: "publish_website.sh", label: "Website"}
    ]},
    {label: "Sync", items: [
        {file: "sync_all.sh", label: "All"},
        {file: "sync_one.sh", label: "One"},
        {file: "sync_to_windows.sh", label: "To Windows"}
    ]},
    {label: "Other", items: [{file: "backup.sh", label: "Backup"}]}
]);
"""
        subprocess.run(["node", "-e", helper + "\n" + harness], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
