"""harbor.sites.service: a service's state for the Check service window, from what systemctl and journalctl print"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.environments.settings import Environment, SettingsError  # noqa: E402
from harbor.sites import service  # noqa: E402

RUNNING = """ActiveState=active
SubState=running
LoadState=loaded
UnitFileState=enabled
Result=success
Description=Running .NET Core [shop.dk]
MainPID=349152
ActiveEnterTimestamp=@1794566059
InactiveEnterTimestamp=@1794560000
MemoryCurrent=179204096
CPUUsageNSec=312000000
TasksCurrent=40
NRestarts=0
ExecMainStatus=0
ExecStart={ path=/usr/lib/dotnet/dotnet ; argv[]=/usr/lib/dotnet/dotnet /var/www/shop.dk/_publish/Shop.dll --urls http://127.0.0.1:4010 ; }
--- harbor log ---
2026-11-13T11:34:19+01:00 LINUX-01 shop.dk[349152]: info: Application started.
2026-11-13T11:34:20+01:00 LINUX-01 shop.dk[349152]: warn: Slow start
"""


class ServiceTests(unittest.TestCase):
    def test_a_running_service(self):
        state = service.read(RUNNING)
        self.assertEqual((state["active"], state["sub"], state["enabled"], state["pid"], state["port"]), ("active", "running", "enabled", 349152, 4010))
        self.assertEqual((state["since"], state["memory"], state["cpu_seconds"], state["tasks"]), (1794566059, 179204096, 0.3, 40))
        self.assertEqual(len(state["log"]), 2)
        self.assertTrue(state["log_problem"])

    def test_a_failed_one_is_since_it_stopped_and_has_no_numbers(self):
        text = RUNNING.replace("ActiveState=active", "ActiveState=failed").replace("MemoryCurrent=179204096", "MemoryCurrent=[not set]") \
            .replace("MainPID=349152", "MainPID=0").replace("warn: Slow start", "Application started")
        state = service.read(text)
        self.assertEqual((state["active"], state["since"], state["memory"], state["pid"], state["log_problem"]), ("failed", 1794560000, None, None, False))

    def test_here_or_where_the_environment_is(self):
        system = RecordingSystem(root="/", answers={"bash -c": RUNNING})
        self.assertEqual(service.status(system, "shop.dk")["port"], 4010)
        self.assertTrue(system.commands[0].startswith("bash -c systemctl show shop.dk.service --no-pager --timestamp=unix -p ActiveState,"))
        system = RecordingSystem(root="/", answers={"ssh": RUNNING})
        environment = Environment(IP="10.0.0.5", USER="root", FOLDER="shop.dk", BINDING="shop.dk")
        self.assertEqual(service.status(system, "shop.dk", environment)["active"], "active")
        self.assertIn("root@10.0.0.5", system.commands[0])

    def test_refused_and_unreadable(self):
        with self.assertRaises(SettingsError):
            service.status(RecordingSystem(root="/"), "shop.dk; rm -rf /")
        with self.assertRaisesRegex(SettingsError, "Connection refused"):
            service.status(RecordingSystem(root="/", answers={"ssh": Result(255, "", "ssh: connect to host: Connection refused")}), "shop.dk",
                           Environment(IP="10.0.0.5", USER="root", FOLDER="shop.dk", BINDING="shop.dk"))

    def test_the_admin_shows_it_in_a_window(self):
        app = (ROOT / "apache/solutionfiles/admin/app/static/app.js").read_text()
        self.assertIn("? openServiceStatus(site, env)", app)
        self.assertIn('@app.route("/api/sites/<name>/service")', (ROOT / "apache/solutionfiles/admin/app/app.py").read_text())


if __name__ == "__main__":
    unittest.main()
