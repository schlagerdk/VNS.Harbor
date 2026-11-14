"""The VNS.Harbor package: its manifest follows the package's version"""
import json
from pathlib import Path
import re
import unittest

NUGET = Path(__file__).resolve().parents[1] / "nuget"


class PackageVersionTests(unittest.TestCase):
    def test_the_dashboard_url_changes_with_each_version(self):
        """Sites serve App_Plugins with a long cache (5 days): a new dashboard.js is only fetched under a new URL, so a
        browser that had 18.3.1 kept showing its buttons after 18.3.1.1"""
        version = re.search(r"<Version>([^<]+)</Version>", (NUGET / "VNS.Harbor.csproj").read_text())[1]
        manifest = json.loads((NUGET / "wwwroot/App_Plugins/VNS.Harbor/umbraco-package.json").read_text())
        self.assertEqual(manifest["version"], version)
        # Every script it loads: the dashboard (element) and the condition that hides it (api)
        scripts = [extension[key] for extension in manifest["extensions"] for key in ("element", "api", "js") if key in extension]
        self.assertTrue(scripts)
        for script in scripts:
            self.assertTrue(script.endswith(f".js?v={version}"), script)


if __name__ == "__main__":
    unittest.main()
