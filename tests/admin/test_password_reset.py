"""Forgot password on the sign-in page: a link mailed to the user, good once within its minutes, found by username or
email, never for root or a locked user"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

spec = importlib.util.spec_from_file_location("password_reset_under_test", ROOT_DIR / "lib/password_reset.py")
password_reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(password_reset)

CONFIG = {"user": "root", "password_hash": "x", "users": [
	{"id": "u_anna", "username": "anna", "email": "Anna@Example.com"},
	{"id": "u_bo", "username": "bo@example.com"},
	{"id": "u_carl", "username": "carl"},
	{"id": "u_dina", "username": "dina", "email": "dina@example.com", "disabled": True}]}


class PasswordResetTests(unittest.TestCase):
	def test_a_link_works_once_and_only_its_hash_is_kept(self):
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "resets.json"
			token = password_reset.make(path, "u_anna", now=1000)
			self.assertNotIn(token, path.read_text())
			self.assertEqual(path.stat().st_mode & 0o777, 0o600)
			self.assertEqual(password_reset.peek(path, token, now=1001), "u_anna")
			self.assertEqual(password_reset.use(path, token, now=1002), "u_anna")
			self.assertIsNone(password_reset.use(path, token, now=1003))
			self.assertIsNone(password_reset.peek(path, "not a token at all, but long enough", now=1003))

	def test_a_link_runs_out_and_a_newer_one_replaces_it(self):
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "resets.json"
			old = password_reset.make(path, "u_anna", now=1000)
			self.assertIsNone(password_reset.peek(path, old, now=1000 + password_reset.TRY_MINUTES * 60 + 1))
			old = password_reset.make(path, "u_anna", now=2000)
			other = password_reset.make(path, "u_bo", now=2000)
			new = password_reset.make(path, "u_anna", now=2001)
			self.assertIsNone(password_reset.peek(path, old, now=2002))
			self.assertEqual(password_reset.peek(path, new, now=2002), "u_anna")
			self.assertEqual(password_reset.peek(path, other, now=2002), "u_bo")
			self.assertEqual(len(json.loads(path.read_text())), 2)

	def test_the_user_is_found_by_username_or_email_never_root_or_a_locked_one(self):
		self.assertEqual(access.for_reset(CONFIG, "ANNA")["id"], "u_anna")
		self.assertEqual(access.for_reset(CONFIG, " anna@example.com ")["id"], "u_anna")
		self.assertIsNone(access.for_reset(CONFIG, "root"))
		self.assertIsNone(access.for_reset(CONFIG, "dina@example.com"))
		self.assertIsNone(access.for_reset(CONFIG, ""))

	def test_the_mail_goes_to_the_email_or_a_username_that_is_one(self):
		self.assertEqual(access.mail_of(CONFIG["users"][0]), "Anna@Example.com")
		self.assertEqual(access.mail_of(CONFIG["users"][1]), "bo@example.com")
		self.assertEqual(access.mail_of(CONFIG["users"][2]), "")

	def test_a_users_email_is_checked_and_optional(self):
		self.assertEqual(access.clean({"username": "anna", "email": " anna@example.com "})["email"], "anna@example.com")
		self.assertEqual(access.clean({"username": "anna"})["email"], "")
		with self.assertRaises(ValueError):
			access.clean({"username": "anna", "email": "not an email"})


if __name__ == "__main__":
	unittest.main()
