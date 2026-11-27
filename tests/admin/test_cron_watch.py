"""Email alerts for the scheduled jobs: how each run ended, and a mail when a watched job fails (lib/cron_watch.py, 1.0.92)"""
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cron_watch", ROOT_DIR / "lib/cron_watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

ON = {"enabled": True, "emails": ["ops@example.com"]}
BACKUP = "/usr/local/bin/harbor sql backup-everything >> /var/log/backup.log 2>&1"
TABLE = f"MAILTO=\"\"\n# Harbor label: Backup\n# Harbor watch: exit, output, missed, long=30\n15 */4 * * * {BACKUP}\n0 3 * * * /root/scripts/other.sh\n"


class CronWatchTests(unittest.TestCase):
	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.state = Path(folder.name)
		self.sent = []

	def send(self, settings, to, subject, body, html=None):
		self.sent.append((to, subject, body, html))

	def record(self, code, lines=(), at=None, seconds=12, written=()):
		at = at or datetime(2026, 11, 27, 8, 15).timestamp()
		watch.record(BACKUP, at, at + seconds, code, list(lines), list(written), state=self.state, table=TABLE, now=at + seconds,
					 settings=ON, sender=self.send)

	def test_the_watch_line_is_read_and_written(self):
		self.assertEqual(watch.parse_watch("exit, output, missed, long=30, nonsense, long=0"), {"exit": True, "output": True, "missed": True, "long": 30})
		self.assertEqual(watch.watch_text({"long": True, "exit": True, "other": True}), "exit, long=60")
		self.assertEqual(watch.watch_text({}), "")
		found = watch.jobs(TABLE)
		self.assertEqual(found[BACKUP], {"schedule": "15 */4 * * *", "label": "Backup", "watch": {"exit": True, "output": True, "missed": True, "long": 30}})
		self.assertEqual(found["/root/scripts/other.sh"]["watch"], {})

	def test_when_cron_should_have_run_a_schedule(self):
		at = lambda *args: datetime(*args).timestamp()
		self.assertEqual(watch.previous_run("0 3 * * *", at(2026, 11, 27, 10, 0)), at(2026, 11, 27, 3, 0))
		self.assertEqual(watch.previous_run("0 3 * * *", at(2026, 11, 27, 2, 59)), at(2026, 11, 26, 3, 0))
		self.assertEqual(watch.previous_run("15 */4 * * *", at(2026, 11, 27, 10, 0)), at(2026, 11, 27, 8, 15))
		# 27.11.2026 is a Friday: Sundays at 3, and either day field when both are limited
		self.assertEqual(watch.previous_run("0 3 * * 0", at(2026, 11, 27, 10, 0)), at(2026, 11, 22, 3, 0))
		self.assertEqual(watch.previous_run("0 3 1 * 5", at(2026, 11, 27, 10, 0)), at(2026, 11, 27, 3, 0))
		self.assertEqual(watch.previous_run("@daily", at(2026, 11, 27, 10, 0)), at(2026, 11, 27, 0, 0))
		self.assertIsNone(watch.previous_run("@reboot", at(2026, 11, 27, 10, 0)))

	def test_a_failed_run_is_mailed_once_and_once_when_it_works_again(self):
		self.record(1, ["Msg 3201: Cannot open backup device"])
		self.record(1, ["still broken"])
		self.assertEqual(len(self.sent), 1, "not again while it still fails")
		to, subject, body, html = self.sent[0]
		self.assertEqual(to, ["ops@example.com"])
		self.assertIn("Job failed: Backup", subject)
		self.assertIn("Ended with an error code: Exit code 1", body)
		self.assertIn("Errors in its output", body)
		self.assertIn("Cannot open backup device", html)
		self.record(0, ["Backed up 4 databases"])
		self.assertEqual(len(self.sent), 2)
		self.assertIn("Job works again: Backup", self.sent[1][1])
		self.assertIn("Every watched job works now", self.sent[1][2])
		runs = watch.runs(self.state)[watch.key_of(BACKUP)]["runs"]
		self.assertEqual([run["code"] for run in runs], [0, 1, 1])

	def test_output_counts_what_it_wrote_to_its_log_and_a_long_run(self):
		self.record(0, [], written=["Backup of shop failed: disk full"])
		self.assertIn("output", json.loads((self.state / "alerts.json").read_text())["open"][watch.key_of(BACKUP)]["problems"])
		self.record(0, [], seconds=31 * 60)
		self.assertEqual(list(json.loads((self.state / "alerts.json").read_text())["open"][watch.key_of(BACKUP)]["problems"]), ["long"])

	def test_a_job_not_watched_is_noted_and_never_mailed(self):
		watch.record("/root/scripts/other.sh", 1000, 1001, 2, ["error"], state=self.state, table=TABLE, now=1001, settings=ON, sender=self.send)
		self.assertEqual(self.sent, [])
		self.assertEqual(watch.runs(self.state)[watch.key_of("/root/scripts/other.sh")]["runs"][0]["code"], 2)

	def test_a_run_cron_did_not_make_is_missed_and_solved_by_the_next(self):
		start = datetime(2026, 11, 27, 7, 0).timestamp()
		check = lambda at: watch.check(self.state, TABLE, now=at, settings=ON, sender=self.send, alive=lambda pid: False)
		check(start)
		self.assertEqual(self.sent, [], "not before it was watched")
		check(datetime(2026, 11, 27, 8, 20).timestamp())
		self.assertEqual(self.sent, [], "within the grace")
		check(datetime(2026, 11, 27, 8, 30).timestamp())
		self.assertEqual(len(self.sent), 1)
		self.assertIn("It should have run 27.11.2026 08:15", self.sent[0][2])
		check(datetime(2026, 11, 27, 8, 35).timestamp())
		self.assertEqual(len(self.sent), 1, "told once")
		self.record(0, at=datetime(2026, 11, 27, 8, 40).timestamp())
		self.assertEqual(len(self.sent), 2)
		self.assertIn("works again", self.sent[1][1])
		check(datetime(2026, 11, 27, 9, 0).timestamp())
		self.assertEqual(len(self.sent), 2)

	def test_the_shell_goes_in_and_out_of_root_s_crontab(self):
		table = "SHELL=/bin/bash\nMAILTO=\"\"\n0 3 * * * /root/a.sh\n"
		on = watch.with_shell(table, True)
		self.assertEqual(on.splitlines()[:3], [watch.NOTE, f"SHELL={watch.SHELL}", "HARBOR_CRON_SHELL=/bin/bash"])
		self.assertEqual(watch.with_shell(on, True), on, "once")
		self.assertEqual(watch.with_shell(on, False), table)
		self.assertEqual(watch.with_shell("", True), f"{watch.NOTE}\nSHELL={watch.SHELL}\n")
		# The note in its older wording is not kept beside the new one
		older = "# Harbor: each job runs through harbor-cron-shell, which notes how it ended (Scheduled jobs, Email alerts)\n"
		self.assertEqual(watch.with_shell(on + older, True), on)

	def test_a_job_runs_as_before_and_its_end_is_noted(self):
		out = io.BytesIO()
		with mock.patch.object(watch, "crontab", return_value=TABLE), mock.patch.object(watch, "load_settings", return_value={"enabled": False, "emails": []}), \
				mock.patch.object(sys, "stdout", mock.Mock(buffer=out)):
			code = watch.run_job(["-c", "echo hello; echo oops >&2; exit 3"], state=self.state)
		self.assertEqual(code, 3)
		self.assertEqual(out.getvalue(), b"hello\noops\n")
		saved = watch.runs(self.state)[watch.key_of("echo hello; echo oops >&2; exit 3")]
		self.assertEqual(saved["runs"][0]["code"], 3)
		self.assertEqual(saved["tail"], ["hello", "oops"])
		self.assertEqual(list((self.state / "running").iterdir()), [])

	def test_a_test_mail_tells_how_each_watched_job_is(self):
		self.record(1)
		result = watch.test_mail(["ops@example.com"], state=self.state, table=TABLE, sender=self.send)
		self.assertIn("TEST: 1 watched job, 1 failing", result["subject"])
		self.assertIn("Failing: Backup", result["body"])
		with self.assertRaises(ValueError):
			watch.test_mail(["not an address"], state=self.state, table=TABLE)


if __name__ == "__main__":
	unittest.main()
