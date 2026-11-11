import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import publish_status  # noqa: E402


class PackageFrameworkTests(unittest.TestCase):
    def test_framework_support(self):
        self.assertTrue(publish_status.framework_supports(".NETStandard2.0", "net10.0"))
        self.assertTrue(publish_status.framework_supports("net8.0", "net10.0"))
        self.assertTrue(publish_status.framework_supports("net10.0", "net10.0"))
        self.assertFalse(publish_status.framework_supports("net11.0", "net10.0"))
        self.assertFalse(publish_status.framework_supports("net8.0-windows7.0", "net10.0"))
        self.assertFalse(publish_status.framework_supports(".NETFramework4.6.2", "net10.0"))

    def test_versions_sort_releases_after_previews(self):
        self.assertLess(publish_status.version_key("11.0.0-rc.2"), publish_status.version_key("11.0.0"))
        self.assertLess(publish_status.version_key("10.0.14"), publish_status.version_key("10.0.15"))

    def test_a_version_for_a_newer_dotnet_is_replaced_or_left_out(self):
        registration = {"items": [{"items": [
            {"catalogEntry": {"version": "10.0.15", "dependencyGroups": [{"targetFramework": "net10.0"}]}},
            {"catalogEntry": {"version": "11.0.0", "dependencyGroups": [{"targetFramework": "net11.0"}]}},
        ]}]}
        found = [{"name": "Microsoft.AspNetCore.Authentication.JwtBearer", "current": "10.0.14", "latest": "11.0.0"},
                 {"name": "Other", "current": "10.0.15", "latest": "11.0.0"}]
        with patch.object(publish_status, "_nuget_json", return_value=registration):
            fitted = publish_status.fit_to_frameworks(found, ["net10.0"])
        self.assertEqual(fitted, [{"name": "Microsoft.AspNetCore.Authentication.JwtBearer", "current": "10.0.14", "latest": "10.0.15"}])

    def test_nuget_errors_keep_what_dotnet_found(self):
        found = [{"name": "A", "current": "1.0.0", "latest": "2.0.0"}]
        with patch.object(publish_status, "_nuget_json", side_effect=OSError("offline")):
            self.assertEqual(publish_status.fit_to_frameworks(found, ["net10.0"]), found)


if __name__ == "__main__":
    unittest.main()
