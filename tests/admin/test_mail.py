"""Mail in Harbor, from mailcow (phase 15): the server, its answers (a write that failed is answered 200 too), the DNS a
domain needs against what is there, and who keeps which domain and how many mailboxes"""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

spec = importlib.util.spec_from_file_location("mailcow_under_test", ROOT_DIR / "lib/mailcow.py")
mailcow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mailcow)

SETTINGS = {"host": "mail.example.com", "key": "ABCDEF-123456-ABCDEF-123456"}


class Answer(io.BytesIO):
	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False


def opener(answer, asked=None):
	def call(request, timeout=None):
		if asked is not None:
			asked.append((request.get_method(), request.full_url, request.get_header("X-api-key"), json.loads(request.data) if request.data else None))
		if isinstance(answer, Exception):
			raise answer
		return Answer(json.dumps(answer).encode())
	return call


class ServerTests(unittest.TestCase):
	def test_settings_are_checked_and_the_key_kept(self):
		self.assertEqual(mailcow.validate({"host": "https://Mail.Example.com/", "key": SETTINGS["key"]}), SETTINGS)
		# An empty key keeps the one saved, for the same server only
		self.assertEqual(mailcow.validate({"host": "mail.example.com", "key": ""}, SETTINGS)["key"], SETTINGS["key"])
		with self.assertRaises(ValueError):
			mailcow.validate({"host": "other.example.com", "key": ""}, SETTINGS)
		with self.assertRaises(ValueError):
			mailcow.validate({"host": "not a host", "key": SETTINGS["key"]})
		self.assertEqual(mailcow.shown(SETTINGS), {"configured": True, "host": "mail.example.com"})

	def test_the_key_is_sent_and_a_failed_write_is_told(self):
		asked = []
		mailcow.call(SETTINGS, "POST", "add/mailbox", {"local_part": "a"}, opener=opener([{"type": "success", "msg": ["mailbox_added", "a@x.dk"]}], asked))
		self.assertEqual(asked[0][:3], ("POST", "https://mail.example.com/api/v1/add/mailbox", SETTINGS["key"]))
		# mailcow answers 200 to one that failed: its messages say so
		with self.assertRaises(mailcow.MailcowError) as failed:
			mailcow.call(SETTINGS, "POST", "add/mailbox", {}, opener=opener([{"type": "danger", "msg": "max_mailbox_exceeded"}]))
		self.assertIn("as many mailboxes", str(failed.exception))
		with self.assertRaises(mailcow.MailcowError) as refused:
			mailcow.call(SETTINGS, "GET", "get/domain/all", opener=opener({"type": "error", "msg": "authentication failed"}))
		self.assertIn("API key", str(refused.exception))
		with self.assertRaises(mailcow.MailcowError):
			mailcow.call(SETTINGS, "GET", "get/domain/all", opener=opener(urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"{}"))))
		# A listing with nothing in it is {}
		self.assertEqual(mailcow.domains(SETTINGS, opener=opener({})), [])

	def test_a_mailbox_on_its_login_off_or_off(self):
		"""mailcow's 1 (on), 2 (its login off, its mail still delivered) and 0 (off)"""
		for sent, kept in ((True, 1), (2, 2), (False, 0), (0, 0)):
			asked = []
			mailcow.edit_mailbox(SETTINGS, "anna@shop.dk", {"active": sent}, opener=opener([{"type": "success", "msg": "mailbox_modified"}], asked))
			self.assertEqual(asked[0][3]["attr"]["active"], kept)
		with self.assertRaises(ValueError):
			mailcow.edit_mailbox(SETTINGS, "anna@shop.dk", {"active": 3})

	def test_a_new_domain_has_its_limits_and_dkim_key(self):
		asked = []
		mailcow.add_domain(SETTINGS, {"domain": "Shop.DK", "mailboxes": 5, "aliases": 20, "quota_mb": 5120, "mailbox_quota_mb": 1024},
		                   opener=opener([{"type": "success", "msg": "domain_added"}], asked))
		body = asked[0][3]
		self.assertEqual((body["domain"], body["mailboxes"], body["quota"], body["maxquota"], body["defquota"], body["key_size"]),
		                 ("shop.dk", 5, 5120, 1024, 1024, 2048))
		with self.assertRaises(ValueError):
			mailcow.domain_limits({"mailboxes": 5, "aliases": 0, "quota_mb": 100, "mailbox_quota_mb": 200})


class DnsTests(unittest.TestCase):
	def wanted(self):
		return mailcow.wanted_records("mail.example.com", "shop.dk", {"selector": "dkim", "txt": "v=DKIM1;k=rsa;t=s;s=email;p=ABC"})

	def test_each_record_against_what_is_there(self):
		current = [
			{"id": "1", "type": "MX", "name": "shop.dk", "content": "mail.example.com"},
			{"id": "2", "type": "A", "name": "autodiscover.shop.dk", "content": "192.0.2.1"},
			{"id": "3", "type": "TXT", "name": "shop.dk", "content": '"v=spf1 include:_spf.example.net -all"'},
			{"id": "4", "type": "TXT", "name": "dkim._domainkey.shop.dk", "content": '"v=DKIM1;k=rsa;" "t=s;s=email;p=ABC"'},
		]
		states = {item["kind"]: item["state"] for item in mailcow.check_records(self.wanted(), current)}
		# An SPF record that only includes another sender does not let this server send: it differs
		self.assertEqual(states, {"mx": "ok", "autodiscover": "differs", "autoconfig": "missing", "srv": "missing",
		                          "spf": "differs", "dmarc": "missing", "dkim": "ok"})
		# An SPF record without the mail server is fixed by adding mx, keeping its other senders
		spf = next(item for item in self.wanted() if item["kind"] == "spf")
		body = mailcow.record_body(spf, {"content": '"v=spf1 include:_spf.example.net -all"'})
		self.assertEqual(body["content"], '"v=spf1 mx include:_spf.example.net -all"')
		srv = mailcow.record_body(next(item for item in self.wanted() if item["kind"] == "srv"))
		self.assertEqual(srv["data"], {"priority": 0, "weight": 1, "port": 443, "target": "mail.example.com"})
		cname = mailcow.record_body(next(item for item in self.wanted() if item["kind"] == "autodiscover"))
		self.assertFalse(cname["proxied"])


class SpfTests(unittest.TestCase):
	def test_the_server_is_let_send_by_its_address_or_an_include(self):
		"""An SPF record lets the mailcow server send by its address in an ip4 range, or through an include that does
		(followed): not only by its name"""
		class Fake:
			def addresses(self, host):
				return {"192.0.2.46"}

			def txt(self, name):
				return {"_spf.shop.dk": ['"v=spf1 ip4:192.0.2.0/24 -all"'], "loop.shop.dk": ['"v=spf1 include:loop.shop.dk -all"']}.get(name, [])
		item = next(entry for entry in mailcow.wanted_records("mail.example.com", "shop.dk", relay="all") if entry["kind"] == "spf")
		allows = lambda text: mailcow.spf_allows(text, item, Fake())
		self.assertTrue(allows('"v=spf1 ip4:192.0.2.46 include:spf.protection.outlook.com -all"'))
		self.assertTrue(allows('"v=spf1 include:_spf.shop.dk" " ~all"'))
		self.assertTrue(allows("v=spf1 a:mail.example.com -all"))
		# Its mx is its own mail server (a relay domain), a range it is not in, one only refused (-), and an include
		# that leads back to itself (followed once)
		self.assertFalse(allows("v=spf1 mx ip4:198.51.100.0/24 -ip4:192.0.2.46 -all"))
		self.assertFalse(allows("v=spf1 include:loop.shop.dk -all"))


class DkimTests(unittest.TestCase):
	def test_the_same_key_with_other_tags_is_the_same(self):
		"""mailcow's v=DKIM1;k=rsa;t=s;s=email;p=… and a record of the same key without t and s, in parts with spaces, are
		the same; another key is not"""
		wanted = mailcow.wanted_records("mail.example.com", "shop.dk", {"selector": "dkim", "txt": "v=DKIM1;k=rsa;t=s;s=email;p=MIIBAQAB"}, relay="all")
		state = lambda content: {item["kind"]: item["state"] for item in mailcow.check_records(wanted, [
			{"id": "1", "type": "TXT", "name": "dkim._domainkey.shop.dk", "content": content}])}["dkim"]
		self.assertEqual(state('"v=DKIM1; k=rsa; p=MIIB" "AQAB"'), "ok")
		self.assertEqual(state('"v=DKIM1; p=MIIBAQAB"'), "ok")
		self.assertEqual(state('"v=DKIM1; k=rsa; p=MIIBOTHER"'), "differs")


class PublicDnsTests(unittest.TestCase):
	def test_a_domain_kept_elsewhere_is_checked_in_public_dns(self):
		"""Not in the Cloudflare account: what dig says now is checked as Cloudflare's records are"""
		from types import SimpleNamespace
		said = {("MX", "shop.dk"): "10 mail.example.com.\n", ("TXT", "shop.dk"): '"v=spf1 mx ~all"\n',
		        ("CNAME", "autodiscover.shop.dk"): "", ("A", "autodiscover.shop.dk"): "other.example.net.\n192.0.2.4\n",
		        ("SRV", "_autodiscover._tcp.shop.dk"): "0 1 443 mail.example.com.\n", ("TXT", "_dmarc.shop.dk"): ""}
		run = lambda argv, **kwargs: SimpleNamespace(stdout=said.get((argv[-2], argv[-1]), ""))
		wanted = [item for item in mailcow.wanted_records("mail.example.com", "shop.dk") if item["kind"] != "autoconfig"]
		states = {item["kind"]: item["state"] for item in mailcow.check_records(wanted, mailcow.public_records(wanted, run=run))}
		self.assertEqual(states, {"mx": "ok", "autodiscover": "differs", "srv": "ok", "spf": "ok", "dmarc": "missing"})
		# Without dig: nothing to check with
		def missing(argv, **kwargs):
			raise OSError("no dig")
		self.assertIsNone(mailcow.public_records(wanted, run=missing))


class MailFixTests(unittest.TestCase):
	def test_missing_records_are_made_and_one_of_another_type_replaced(self):
		"""Make missing makes those that are not there; Fix replaces one that differs, an A record where the CNAME goes
		taken out first (a CNAME stands alone). What Harbor made is kept, to take it out with the domain"""
		import ast
		import os
		import tempfile
		import threading
		from unittest import mock
		app = ROOT_DIR / "admin/app.py"
		nodes = [node for node in ast.parse(app.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in ("mail_dns", "mail_fix", "mail_dns_made", "mail_record_body", "mail_dns_undo", "mail_dns_kind")
		         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("MAIL_DNS_KEPT", "MAIL_DNS_MAILBOXES_ONLY")]
		current = [{"id": "a1", "type": "A", "name": "autodiscover.shop.dk", "content": "192.0.2.1"}]
		cloudflare = mock.Mock(records=lambda settings, zone: current, create=mock.Mock(side_effect=lambda s, z, body: {"id": "new-" + body["name"]}),
		                       delete=mock.Mock(), update=mock.Mock())
		with tempfile.TemporaryDirectory() as folder:
			ns = {"json": json, "os": os, "MAIL_LOCK": threading.Lock(), "MAIL_DNS": os.path.join(folder, "mail-dns.json"), "audit": lambda message: None,
			      "_mailcow": mock.Mock(dkim=lambda settings, name: None, domain=lambda settings, name: {"relay": ""},
			                            wanted_records=mailcow.wanted_records, check_records=mailcow.check_records,
			                            record_body=mailcow.record_body),
			      "_cloudflare": cloudflare, "mail_zone": lambda name: ({"token": "t"}, {"id": "z1", "name": "shop.dk"}),
			      "is_admin": lambda: True, "CLOUDFLARE_LOCKS": os.path.join(folder, "locks.json")}
			exec(compile(ast.Module(body=nodes, type_ignores=[]), str(app), "exec"), ns)
			done = ns["mail_fix"](SETTINGS, "shop.dk")
			# The differing one is left without replace: only the missing ones are made
			self.assertNotIn("Made CNAME autodiscover.shop.dk", done)
			self.assertIn("Made MX shop.dk", done)
			cloudflare.delete.assert_not_called()
			ns["mail_fix"](SETTINGS, "shop.dk", ["autodiscover"], replace=True)
			cloudflare.delete.assert_called_once_with({"token": "t"}, "z1", "a1")
			kept = {entry["id"]: entry for entry in ns["mail_dns_made"]("shop.dk")}
			# One it made: taken out with the domain; the A record it replaced: kept as it was, to be made again
			self.assertIsNone(kept["new-shop.dk"]["before"])
			self.assertEqual((kept["new-autodiscover.shop.dk"]["before"]["content"], kept["new-autodiscover.shop.dk"]["replaced"]), ("192.0.2.1", True))
			cloudflare.delete.reset_mock()
			out, back, undone = ns["mail_dns_undo"]({"token": "t"}, {"id": "z1"}, list(kept.values()))
			self.assertEqual(back, 1)
			# What each is for is kept with it: the domain's SPF stays when the domain goes, and a relay domain loses
			# only what mailboxes here need
			self.assertEqual(kept["new-autodiscover.shop.dk"]["kind"], "autodiscover")
			self.assertEqual(ns["mail_dns_kind"]({"id": "x", "before": {"content": '"v=spf1 mx -all"'}}), "spf")
			self.assertEqual(ns["mail_dns_kind"]({"id": "x", "before": None}), "")
			self.assertIn("spf", ns["MAIL_DNS_KEPT"])
			self.assertEqual(set(ns["MAIL_DNS_MAILBOXES_ONLY"]), {"mx", "autodiscover", "autoconfig", "srv"})
			cloudflare.create.assert_called_with({"token": "t"}, "z1", {"type": "A", "name": "autodiscover.shop.dk", "ttl": 1, "content": "192.0.2.1", "proxied": False})
			# A user who keeps the zone: a record an administrator locked is left, and said so
			cloudflare.reset_mock()
			cloudflare.is_locked.return_value = True
			ns["is_admin"] = lambda: False
			current[:] = [{"id": "a2", "type": "A", "name": "autoconfig.shop.dk", "content": "192.0.2.2"}]
			done = ns["mail_fix"](SETTINGS, "shop.dk", ["autoconfig"], replace=True)
			self.assertEqual(done, ["Left CNAME autoconfig.shop.dk: it is locked, an administrator changes it"])
			cloudflare.delete.assert_not_called()


class RspamdTests(unittest.TestCase):
	def test_its_counts_and_latest_messages(self):
		answers = {"rspamd-stats": {"version": "3.12.1", "uptime": 90000, "scanned": 300, "spam_count": 44, "ham_count": 256,
		                            "actions": {"reject": 38, "no action": 255, "greylist": 1}},
		           "rspamd-history/100": [{"unix_time": 1794334338, "action": "reject", "score": 17.5, "required_score": 15, "sender_mime": "spam@example.net",
		                                   "rcpt_mime": ["anna@shop.dk"], "subject": "Win", "ip": "192.0.2.9", "user": "unknown", "size": 2048,
		                                   "symbols": {"BAYES_SPAM": {"score": 5.1}, "DMARC_POLICY_ALLOW": {"score": -0.5}, "R_SPF_ALLOW": {"score": 0}, "URIBL": {"score": 7.5}}}]}

		def call(request, timeout=None):
			return Answer(json.dumps(next(value for key, value in answers.items() if request.full_url.endswith(key))).encode())
		found = mailcow.rspamd(SETTINGS, opener=call)
		self.assertEqual((found["scanned"], found["spam"], found["actions"]["reject"], found["actions"]["add header"]), (300, 44, 38, 0))
		message = found["history"][0]
		self.assertEqual((message["action"], message["score"], message["to"]), ("reject", 17.5, ["anna@shop.dk"]))
		# What weighed most first, either way; one that weighed nothing left out
		self.assertEqual([symbol["name"] for symbol in message["symbols"]], ["URIBL", "BAYES_SPAM", "DMARC_POLICY_ALLOW"])


class RelayTests(unittest.TestCase):
	def test_a_relay_domain_as_mailcow_says_it(self):
		"""mailcow's Relay, Relay All and Relay Non-Local: read as its badges are, and sent as its flags. A relay
		domain's MX stays with its own mail server: only SPF, DMARC and DKIM are wanted"""
		self.assertEqual(mailcow.relay_of({"backupmx": 1, "relay_all_recipients": 1}), "all")
		self.assertEqual(mailcow.relay_of({"backupmx_int": 1, "relay_all_recipients_int": 1, "relay_unknown_only_int": 1}), "nonlocal")
		self.assertEqual(mailcow.relay_of({"backupmx": 1}), "relay")
		self.assertEqual(mailcow.relay_of({"backupmx": 0, "relay_all_recipients": 1}), "")
		self.assertEqual(mailcow.relay_fields("all"), {"backupmx": 1, "relay_all_recipients": 1, "relay_unknown_only": 0})
		self.assertEqual(mailcow.relay_fields(""), {"backupmx": 0, "relay_all_recipients": 0, "relay_unknown_only": 0})
		with self.assertRaises(ValueError):
			mailcow.relay_fields("elsewhere")
		# Relay All has no mailboxes here: its limits are the least mailcow takes, whatever was sent
		self.assertEqual(mailcow.limits_for({"relay": "all", "mailboxes": 10}), {"mailboxes": 0, "aliases": 0, "quota": 1, "maxquota": 1, "defquota": 1})
		self.assertEqual(mailcow.limits_for({"relay": "relay", "mailboxes": 3, "aliases": 5, "quota_mb": 100, "mailbox_quota_mb": 50})["mailboxes"], 3)
		wanted = mailcow.wanted_records("mail.example.com", "shop.dk", {"selector": "dkim", "txt": "v=DKIM1;p=A"}, relay="all")
		self.assertEqual([item["kind"] for item in wanted], ["spf", "dmarc", "dkim"])
		# Its mx is its own mail server: SPF lets this one send by its name, and one there gets it added
		spf = wanted[0]
		self.assertEqual(spf["content"], "v=spf1 a:mail.example.com ~all")
		there = lambda content: {item["kind"]: item["state"] for item in mailcow.check_records(wanted, [{"id": "1", "type": "TXT", "name": "shop.dk", "content": content}])}["spf"]
		self.assertEqual(there('"v=spf1 mx a:mail.example.com -all"'), "ok")
		self.assertEqual(there('"v=spf1 mx include:spf.example.net -all"'), "differs")
		self.assertEqual(mailcow.record_body(spf, {"content": '"v=spf1 mx -all"'})["content"], '"v=spf1 a:mail.example.com mx -all"')


class SetupMailTests(unittest.TestCase):
	def test_the_setup_mailed_to_another_address(self):
		"""What a mailbox's user is sent: webmail at the server's own address, the servers and ports, and its password
		only when it is given"""
		report_spec = importlib.util.spec_from_file_location("site_report_under_test", ROOT_DIR / "lib/site_report.py")
		report = importlib.util.module_from_spec(report_spec)
		report_spec.loader.exec_module(report)
		text = report.mailbox_setup_text("anna@shop.dk", "mail.example.com")
		self.assertIn("Webmail: https://mail.example.com/\n", text)
		self.assertIn("port 993", text)
		self.assertNotIn("Password:", text)
		self.assertIn("sent to you on its own", text)
		with_secret = report.mailbox_setup_html("harbor.example.com", "anna@shop.dk", "mail.example.com", "S3cret<pw>")
		self.assertIn("S3cret&lt;pw&gt;", with_secret)
		self.assertIn('href="https://mail.example.com/"', with_secret)
		self.assertNotIn("SOGo", with_secret)
		# Without the domain's autodiscover and autoconfig records: set up by hand, never said to find the server itself
		by_hand = report.mailbox_setup_text("anna@shop.dk", "mail.example.com", automatic=False)
		self.assertNotIn("finds the server itself", by_hand)
		self.assertIn("by hand", by_hand)
		self.assertIn("by hand", report.mailbox_setup_html("harbor.example.com", "anna@shop.dk", "mail.example.com", automatic=False))


class MailMenuTests(unittest.TestCase):
	def test_a_user_with_mail_reaches_it(self):
		"""A user with Cloudflare and Mail, without Web Server or SQL Server, was sent to Cloudflare from Mail: the menu's
		Mail did nothing. Each part they have is reached; one they do not have leads to the first they have"""
		import subprocess
		source = (ROOT_DIR / "admin/static/app.js").read_text()
		start = source.index("	const wantsSql = location.hash.startsWith")
		block = source[start:source.index("	const hash = decodeURIComponent(location.hash.slice(1));", start)]
		harness = """
const assert = require("node:assert/strict");
const where = (user, hash) => {
	const location = { hash };
	const history = { replaceState: (state, title, to) => { location.hash = to; } };
	const currentUser = user, startView = "sites";
	%s
	return location.hash;
};
const both = { dns: true, mail: true };
assert.equal(where(both, "#mail"), "#mail");
assert.equal(where(both, "#mail/502.dk"), "#mail/502.dk");
assert.equal(where(both, "#dns"), "#dns");
assert.equal(where(both, "#sites"), "#dns");
assert.equal(where({ mail: true }, ""), "#mail");
assert.equal(where({ dns: true }, "#mail"), "#dns");
assert.equal(where({ web: true, mail: true }, "#mail"), "#mail");
assert.equal(where({ sql: true, mail: true }, "#mail"), "#mail");
assert.equal(where({ sql: true }, "#sites"), "#sql");
assert.equal(where({ web: true }, "#sql"), "#sites");
assert.equal(where({ admin: true }, "#mail"), "#mail");
""" % block
		result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)


class ReopenedTests(unittest.TestCase):
	def test_users_of_a_relay_all_domain_get_mailboxes_again(self):
		"""A Relay All domain is given with no mailbox; turned into one that takes mailboxes, its users may have the
		usual number (5), not none. One given a number keeps it"""
		import ast
		import threading
		app = ROOT_DIR / "admin/app.py"
		nodes = [node for node in ast.parse(app.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == "mail_grants_reopened"
		         or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "MAIL_USER_DEFAULT"]
		saved = []
		cfg = {"users": [{"id": "a", "mail_domains": {"schlager.dk": {"mailboxes": 0, "settings": True}}},
		                 {"id": "b", "mail_domains": {"schlager.dk": {"mailboxes": 3}}}, {"id": "c", "mail_domains": {"other.dk": {"mailboxes": 0}}}]}
		ns = {"cfg": cfg, "CONFIG_LOCK": threading.Lock(), "save_config": lambda: saved.append(True), "audit": lambda message: None}
		exec(compile(ast.Module(body=nodes, type_ignores=[]), str(app), "exec"), ns)
		self.assertEqual(ns["mail_grants_reopened"]("schlager.dk"), 1)
		self.assertEqual([user["mail_domains"] for user in cfg["users"]],
		                 [{"schlager.dk": {"mailboxes": 5, "settings": True}}, {"schlager.dk": {"mailboxes": 3}}, {"other.dk": {"mailboxes": 0}}])
		self.assertEqual(saved, [True])


class MailAccessTests(unittest.TestCase):
	def test_a_user_keeps_their_domains_with_a_limit(self):
		user = access.clean({"username": "anna", "role": "user", "mail": True,
		                     "mail_domains": {"Shop.DK": {"mailboxes": 3}, "other.dk": {}, "not a domain": {}}})
		self.assertEqual(user["mail_domains"], {"shop.dk": {"mailboxes": 3}, "other.dk": {}})
		self.assertTrue(access.mail_domain(user, "shop.dk"))
		self.assertFalse(access.mail_domain(user, "third.dk"))
		self.assertEqual(access.mail_limit(user, "shop.dk"), 3)
		self.assertIsNone(access.mail_limit(user, "other.dk"))
		# On with no domain is no access; an administrator keeps every domain, without a limit
		self.assertFalse(access.clean({"username": "bo", "role": "user", "mail": True, "mail_domains": {}})["mail"])
		self.assertTrue(access.mail_domain({"role": "admin"}, "any.dk"))
		self.assertIsNone(access.mail_limit({"role": "admin"}, "any.dk"))
		with self.assertRaises(ValueError):
			access.clean({"username": "c", "role": "user", "mail": True, "mail_domains": {"shop.dk": {"mailboxes": -1}}})
		# Its settings too, where given with the domain
		given = access.clean({"username": "d", "role": "user", "mail": True, "mail_domains": {"shop.dk": {"mailboxes": 2, "settings": True}, "other.dk": {"settings": "yes"}}})
		self.assertEqual(given["mail_domains"], {"shop.dk": {"mailboxes": 2, "settings": True}, "other.dk": {}})
		self.assertTrue(access.mail_settings(given, "shop.dk"))
		# Its settings theirs: the domain's own number of mailboxes holds, not one of theirs
		self.assertIsNone(access.mail_limit(given, "shop.dk"))
		self.assertFalse(access.mail_settings(given, "other.dk"))
		self.assertTrue(access.mail_settings({"role": "admin"}, "any.dk"))


if __name__ == "__main__":
	unittest.main()
