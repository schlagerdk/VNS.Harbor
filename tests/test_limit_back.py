"""A limit sent from the server that has a site as an environment, changed on the server it runs on: sent back (1.0.97)"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harbor.core.system import RecordingSystem, Result, Stop  # noqa: E402
from harbor.server import tools  # noqa: E402


class LimitBackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / "app"
        self.data.mkdir()
        (Path(folder.name) / "var/www/staging.dk").mkdir(parents=True)
        self.system = RecordingSystem(root=folder.name)
        self.usage = tools._usage_module()
        patcher = patch.object(tools, "_admin_data", return_value=self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_received_with_where_it_came_from_and_sent_back(self):
        with patch.dict("os.environ", {"SSH_CONNECTION": "10.0.0.1 51000 10.0.0.2 22"}):
            tools.usage_limit(self.system, "staging.dk", 2048, "LINUX-01", "shop.dk", "staging-2")
        # Checked at once there, so its admin warns without waiting for the hour
        self.assertTrue(any(line.endswith(f"usage.py --sync {self.data}") for line in self.system.commands))
        self.assertTrue(any(line.startswith("setsid -f env /usr/bin/python3") and line.endswith(f"--data {self.data}") for line in self.system.commands))
        source = self.usage.load_limit_sources(self.data)["site|staging.dk"]
        self.assertEqual(source, {"host": "LINUX-01", "address": "10.0.0.1", "site": "shop.dk", "target": "staging-2", "mails": True, "port": 22})
        # Harbor here does not know LINUX-01: nothing changes
        with patch("harbor.server.servers.servers", return_value={}):
            with self.assertRaises(Stop):
                tools.usage_limit_back(self.system, "staging.dk", None)
        self.assertEqual(self.usage.load_limits(self.data)["site|staging.dk"], 2048)
        # It knows it, by its address: sent there, then kept here
        known = {"linux-01": {"host": "10.0.0.1", "user": "root", "port": 22}}
        self.system.answers["ssh"] = Result(0, "", "")
        with patch("harbor.server.servers.servers", return_value=known):
            tools.usage_limit_back(self.system, "staging.dk", 1024)
        sent = [line for line in self.system.commands if line.startswith("ssh ")]
        self.assertIn("env set-limit shop.dk staging-2 1024", sent[-1])
        self.assertIn("root@10.0.0.1", sent[-1])
        self.assertEqual(self.usage.load_limits(self.data)["site|staging.dk"], 1024)
        self.assertEqual(self.usage.load_limit_origins(self.data), {"site|staging.dk": "LINUX-01"})
        with patch("harbor.server.servers.servers", return_value=known):
            tools.usage_limit_back(self.system, "staging.dk", None)
        self.assertNotIn("site|staging.dk", self.usage.load_limits(self.data))
        self.assertIn("env set-limit shop.dk staging-2 none", [line for line in self.system.commands if line.startswith("ssh ")][-1])
        # Taken away, it is still known whose environment it is: a limit set here later is sent there and shared
        self.assertEqual(self.usage.load_environment_of(self.data)["site|staging.dk"]["site"], "shop.dk")
        with patch("harbor.server.servers.servers", return_value=known):
            tools.usage_limit_back(self.system, "staging.dk", 2048)
        self.assertIn("env set-limit shop.dk staging-2 2048", [line for line in self.system.commands if line.startswith("ssh ")][-1])
        self.assertEqual(self.usage.load_limit_origins(self.data), {"site|staging.dk": "LINUX-01"})


    def test_a_share_sent_back_is_held_to_the_sites_limit_there(self):
        """On the server that manages the site, a share sent back from where it runs is refused when it does not fit"""
        self.usage.set_limit(self.data, "site", "shop.dk", limit_mb=2048)
        (self.data / "usage-sizes.json").write_text(json.dumps({"measured": 1, "sizes": {"sites": {"shop.dk": 967.0}, "databases": {}, "environments": [
            {"site": "shop.dk", "target": "staging-2", "in_site": False, "size_mb": 233.0}]}}))
        with self.assertRaises(Stop) as refused:
            tools.environment_limit(self.system, "shop.dk", "staging-2", 2048)
        self.assertIn("At most", str(refused.exception))
        room = self.usage.limit_room(self.data, "shop.dk", "staging-2", measure_environment=lambda site, target: None)
        self.assertEqual((room["min"], room["max"], room["budget"]), (233, 1081, 2048))
        # Grown since Usage measured it: what it uses now (measured again, or as the check found it) is the least
        room = self.usage.limit_room(self.data, "shop.dk", "staging-2", measure_environment=lambda site, target: 272.6)
        self.assertEqual(room["min"], 273)
        self.assertIn("273", self.usage.limit_conflict(self.data, "environment", "shop.dk", "staging-2", 268, measure_environment=lambda site, target: 272.6))
        tools.environment_limit(self.system, "shop.dk", "staging-2", 800)
        self.assertEqual(self.usage.load_limits(self.data)["environment|shop.dk|staging-2"], 800)
        # Asked to raise: the site's limit goes up by what the share does not fit in, then the share is set
        tools.environment_limit(self.system, "shop.dk", "staging-2", 1581, raise_site=True)
        limits = self.usage.load_limits(self.data)
        self.assertEqual((limits["environment|shop.dk|staging-2"], limits["site|shop.dk"]), (1581, 2548))


if __name__ == "__main__":
    unittest.main()
