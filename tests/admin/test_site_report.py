"""Keep-alive's log of what is wrong with the sites, and its report by mail (lib/site_report.py, 1.0.70)"""
from datetime import datetime
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("site_report", ROOT_DIR / "lib/site_report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

MAIL = {"host": "smtp.example.com", "port": 587, "security": "starttls", "user": "", "password": "", "sender": "harbor@example.com"}


class SiteReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        self.sent = []

    def send(self, settings, to, subject, body, html=None):
        self.sent.append((to, subject, body, html))

    def run_with(self, results, settings=None, at=None):
        at = at or datetime(2026, 11, 24, 10, 0).timestamp()
        return report.record(results, settings, self.state, now=at, mail=MAIL, sender=self.send, hostname="web-01")

    def test_a_problem_is_logged_when_it_starts_and_when_it_ends_once_each(self):
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": list(report.CHECKS)}
        self.run_with([{"name": "shop.example.com", "problems": {"down": "HTTP 502"}}], on)
        self.run_with([{"name": "shop.example.com", "problems": {"down": "HTTP 502"}}], on)
        self.run_with([{"name": "shop.example.com", "problems": {}}], on)
        self.assertEqual([(item["event"], item["check"]) for item in report.events(self.state)], [("failed", "down"), ("recovered", "down")])
        # A mail when it broke, one when it came back: not one each run
        self.assertEqual(len(self.sent), 2)
        self.assertIn("shop.example.com", self.sent[0][1])
        self.assertIn("Started: shop.example.com - Not answering: HTTP 502", self.sent[0][2])
        self.assertIn("Solved: shop.example.com", self.sent[1][2])

    def test_only_the_chosen_checks_are_mailed_but_all_are_logged(self):
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": ["certificate"]}
        self.run_with([{"name": "a.example.com", "problems": {"certificate_soon": "Expires in 5 days"}}], on)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(report.events(self.state)), 1)
        self.run_with([{"name": "a.example.com", "problems": {"certificate": "certificate has expired"}}], on)
        self.assertEqual(len(self.sent), 1)

    def test_what_was_wrong_before_the_report_took_it_in_is_told_once(self):
        """A site stopped before Site stopped was chosen was never mailed: the report told only of what changed (1.0.70)"""
        self.run_with([{"name": "a.example.com", "problems": {"stopped": "Visitors see the page that says it is updating"}}],
                      {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": ["down"]})
        self.assertEqual(self.sent, [])
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": ["down", "stopped"]}
        self.run_with([{"name": "a.example.com", "problems": {"stopped": "Visitors see the page that says it is updating"}}], on)
        self.run_with([{"name": "a.example.com", "problems": {"stopped": "Visitors see the page that says it is updating"}}], on)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Started: a.example.com - Site stopped", self.sent[0][2])
        self.run_with([{"name": "a.example.com", "problems": {}}], on)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Solved: a.example.com", self.sent[1][2])

    def test_a_site_not_checked_any_more_says_nothing(self):
        self.run_with([{"name": "gone.example.com", "problems": {"down": "refused"}}])
        self.run_with([])
        self.assertEqual([item["event"] for item in report.events(self.state)], ["failed"])

    def test_daily_once_a_day_after_seven_and_only_with_something_to_tell(self):
        daily = {"enabled": True, "emails": ["ops@example.com"], "when": "daily", "checks": list(report.CHECKS)}
        self.run_with([{"name": "a.example.com", "problems": {}}], daily, datetime(2026, 11, 24, 8).timestamp())
        self.assertEqual(self.sent, [])
        self.run_with([{"name": "a.example.com", "problems": {"down": "timed out"}}], daily, datetime(2026, 11, 24, 9).timestamp())
        self.assertEqual(self.sent, [])
        self.run_with([{"name": "a.example.com", "problems": {"down": "timed out"}}], daily, datetime(2026, 11, 25, 6).timestamp())
        self.assertEqual(self.sent, [], "Not before seven")
        self.run_with([{"name": "a.example.com", "problems": {"down": "timed out"}}], daily, datetime(2026, 11, 25, 7, 5).timestamp())
        self.run_with([{"name": "a.example.com", "problems": {"down": "timed out"}}], daily, datetime(2026, 11, 25, 9).timestamp())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Daily site report: 1 problem", self.sent[0][1])

    def test_a_mail_that_cannot_be_sent_is_kept_for_the_admin(self):
        def fail(*args, **kwargs):
            raise OSError("Connection refused")
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": list(report.CHECKS)}
        report.record([{"name": "a.example.com", "problems": {"down": "HTTP 500"}}], on, self.state, now=1000, mail=MAIL, sender=fail)
        self.assertEqual(report.read_json(self.state / "report.json", {})["mail_error"]["error"], "Connection refused")

    def test_a_test_run_mails_how_every_site_is_and_leaves_the_log(self):
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": ["down", "certificate"]}
        result = report.test_report([{"name": "a.example.com", "problems": {"down": "HTTP 502", "certificate_soon": "Expires in 3 days"}},
                                     {"name": "b.example.com", "problems": {}}], on, "web-01", mail=MAIL, sender=self.send)
        self.assertEqual(result["to"], ["ops@example.com"])
        self.assertIn("TEST: 1 problem on 2 sites", result["subject"])
        self.assertIn("a.example.com - Not answering: HTTP 502", result["body"])
        # Only the checks chosen; a site with only an unchosen problem is fine
        self.assertNotIn("Expires in 3 days", result["body"])
        self.assertEqual(self.sent[0][1], result["subject"])
        self.assertEqual(report.events(self.state), [])
        # Without recipients it is shown, not sent
        self.assertIn("No recipients", report.test_report([], {}, "web-01", mail=MAIL, sender=self.send)["error"])

    def test_the_mail_has_an_html_part_that_says_how_long_it_lasted(self):
        """The mails were plain text, hard to read at a glance: they have an HTML part beside it (1.0.92)"""
        on = {"enabled": True, "emails": ["ops@example.com"], "when": "change", "checks": list(report.CHECKS)}
        start = datetime(2026, 11, 24, 10, 0).timestamp()
        self.run_with([{"name": "<b>shop.example.com", "problems": {"down": "timed out"}}], on, start)
        self.run_with([{"name": "<b>shop.example.com", "problems": {}}], on, start + 9 * 3600 + 34 * 60)
        started, solved = self.sent[0][3], self.sent[1][3]
        self.assertIn("<!doctype html>", started)
        self.assertIn("&lt;b&gt;shop.example.com", started)
        self.assertNotIn("<b>shop", started)
        self.assertIn("Not answering", started)
        self.assertIn("Answering again", solved)
        self.assertIn("it lasted 9 h 34 min", solved)
        self.assertIn("(it lasted 9 h 34 min)", self.sent[1][2])
        self.assertIn("Nothing is wrong now", solved)

    def test_settings_are_checked(self):
        with self.assertRaises(ValueError):
            report.validate_report({"enabled": True, "emails": [], "when": "change", "checks": ["down"]})
        with self.assertRaises(ValueError):
            report.validate_report({"enabled": False, "emails": ["not an address"], "when": "change", "checks": ["down"]})
        with self.assertRaises(ValueError):
            report.validate_report({"enabled": False, "emails": [], "when": "weekly", "checks": ["down"]})
        # An empty password keeps the one saved; without a user there is none
        kept = report.validate_mail({**MAIL, "user": "harbor", "password": ""}, {"password": "secret"})
        self.assertEqual(kept["password"], "secret")
        with self.assertRaises(ValueError):
            report.validate_mail({**MAIL, "sender": "harbor"})


if __name__ == "__main__":
    unittest.main()
