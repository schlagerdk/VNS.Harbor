"""Harbor updated by itself each day, when it is turned on (1.0.97)"""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harbor.core.system import RecordingSystem, Result, Stop  # noqa: E402
from harbor.install import autoupdate, update  # noqa: E402


class FakeReport:
    def __init__(self):
        self.sent = []

    esc = staticmethod(lambda text: str(text))

    def row(self, *args):
        return "row"

    def section(self, *args):
        return "section"

    def page(self, *args):
        return "<html>"

    def load_mail(self):
        return {"host": "mail.example.com"}

    def send(self, settings, to, subject, body, html=None):
        self.sent.append((to, subject))


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.system = RecordingSystem(root=self.root)
        self.lock = self.root / "lock"

    def path(self, absolute):
        return self.system.path(absolute)

    def test_turned_on_at_a_time_and_off_again(self):
        self.assertEqual(autoupdate.status(self.system), {"on": False, "time": "04:00", "last": None})
        autoupdate.turn_on(self.system, "03:30")
        cron = self.path(autoupdate.CRON).read_text()
        self.assertIn("30 3 * * * root /usr/bin/python3 ", cron)
        self.assertIn(" self auto-update run >> /var/log/harbor-auto-update.log 2>&1", cron)
        self.assertEqual(autoupdate.status(self.system)["time"], "03:30")
        self.assertTrue(autoupdate.status(self.system)["on"])
        with self.assertRaises(Stop):
            autoupdate.turn_on(self.system, "25:00")
        # As soon as a release is out: each hour, at a minute of the server's own
        with patch("socket.gethostname", return_value="linux-01"):
            autoupdate.turn_on(self.system, "soon")
            minute = autoupdate.own_minute()
        self.assertIn(f"\n{minute} * * * * root /usr/bin/python3 ", self.path(autoupdate.CRON).read_text())
        self.assertEqual(autoupdate.status(self.system)["time"], "soon")
        autoupdate.turn_off(self.system)
        self.assertFalse(self.path(autoupdate.CRON).exists())

    def run_with(self, versions, installed="1.0.95", result=Result(0, "", "")):
        self.system.answers["/usr/bin/python3"] = result
        with patch.object(update, "installed_version", return_value=installed), \
                patch.object(update, "installed_repository", return_value=update.REPOSITORY), \
                patch.object(update, "release_versions", return_value=versions):
            return autoupdate.run(self.system, lock=self.lock, mailer=self.report, now=1000)

    def test_the_daily_run_installs_the_newest_release_and_mails_it(self):
        self.report = FakeReport()
        self.path(autoupdate.HEALTH).parent.mkdir(parents=True, exist_ok=True)
        self.path(autoupdate.HEALTH).write_text(json.dumps({"enabled": True, "emails": ["ops@example.com"]}))
        self.assertEqual(self.run_with(["1.0.95"])["result"], "current")
        self.assertEqual(self.report.sent, [])
        done = self.run_with(["1.0.97", "1.0.96", "1.0.95"])
        self.assertEqual((done["result"], done["from"], done["to"]), ("updated", "1.0.95", "1.0.97"))
        self.assertTrue(self.system.commands[-1].endswith("self update --ref v1.0.97"))
        self.assertEqual(self.report.sent[-1][0], ["ops@example.com"])
        self.assertIn("updated to v1.0.97", self.report.sent[-1][1])
        failed = self.run_with(["1.0.97"], result=Result(1, "Fetch v1.0.97\nCould not reach GitHub", ""))
        self.assertEqual((failed["result"], failed["error"]), ("failed", "Could not reach GitHub"))
        self.assertIn("could not update", self.report.sent[-1][1])
        self.assertEqual(json.loads(self.path(autoupdate.STATE).read_text())["result"], "failed")

    def test_it_waits_while_another_update_runs(self):
        """One update at a time: the one started in the admin holds the lock"""
        self.report = FakeReport()
        with open(self.lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_with(["1.0.97"])["result"], "busy")
            self.assertFalse(any("self update" in line for line in self.system.commands))
            with patch.object(autoupdate, "LOCK", self.lock), patch.object(self.system, "path", lambda absolute: Path(absolute)):
                with self.assertRaises(Stop):
                    update.lock_update(self.system)


if __name__ == "__main__":
    unittest.main()
