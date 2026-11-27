"""DNS in Harbor, from Cloudflare (phase 14): the account, its zones and records, and who may keep which"""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

spec = importlib.util.spec_from_file_location("cloudflare_under_test", ROOT_DIR / "lib/cloudflare.py")
cloudflare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloudflare)

ZONE = "0" * 31 + "1"
RECORD = "a" * 32


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeCloudflare:
    """Answers as Cloudflare does, and keeps what it was asked"""
    def __init__(self, pages=None):
        self.asked = []
        self.pages = pages or {}

    def __call__(self, request, timeout=None):
        self.asked.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
        path = request.full_url.removeprefix(cloudflare.API).split("?")[0]
        page = int(dict(item.split("=") for item in request.full_url.split("?")[1].split("&")).get("page", 1)) if "?" in request.full_url else 1
        result, total = self.pages.get(path, ([], 1)) if request.get_method() == "GET" else ({"id": RECORD}, 1)
        if isinstance(result, list) and total > 1:
            result = result[(page - 1) * 1:page * 1]
        return Answer(json.dumps({"success": True, "result": result, "result_info": {"total_pages": total}}).encode())


class CloudflareTests(unittest.TestCase):
    def test_the_account_is_checked_and_never_shown(self):
        with self.assertRaises(ValueError):
            cloudflare.validate({"auth": "key", "email": "not an email", "key": "x" * 37})
        kept = cloudflare.validate({"auth": "key", "email": "ops@example.com", "key": "k" * 37})
        self.assertEqual(cloudflare.shown(kept), {"configured": True, "auth": "key", "email": "ops@example.com"})
        # An empty key keeps the one saved
        self.assertEqual(cloudflare.validate({"auth": "key", "email": "ops@example.com", "key": ""}, kept)["key"], "k" * 37)
        token = cloudflare.validate({"auth": "token", "token": "t" * 40})
        self.assertEqual(cloudflare.shown(token)["auth"], "token")
        self.assertNotIn("t" * 40, json.dumps(cloudflare.shown(token)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cloudflare.json"
            cloudflare.save(token, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cloudflare.load(path), token)

    def test_calls_log_in_as_the_account_says(self):
        fake = FakeCloudflare({"/zones": ([{"id": ZONE, "name": "shop.dk", "status": "active"}], 1)})
        cloudflare.zones({"email": "ops@example.com", "key": "k" * 37}, opener=fake)
        headers = {key.lower(): value for key, value in fake.asked[-1][2].items()}
        self.assertEqual((headers["x-auth-email"], headers["x-auth-key"]), ("ops@example.com", "k" * 37))
        cloudflare.zones({"token": "t" * 40}, opener=fake)
        headers = {key.lower(): value for key, value in fake.asked[-1][2].items()}
        self.assertEqual(headers["authorization"], "Bearer " + "t" * 40)

    def test_every_page_of_records_sorted_by_type_and_name(self):
        records = [{"id": RECORD, "type": "TXT", "name": "shop.dk", "content": '"v=spf1"', "ttl": 1},
                   {"id": RECORD, "type": "A", "name": "www.shop.dk", "content": "203.0.113.10", "ttl": 300, "proxied": True}]
        fake = FakeCloudflare({f"/zones/{ZONE}/dns_records": (records, 2)})
        found = cloudflare.records({"token": "t" * 40}, ZONE, opener=fake)
        self.assertEqual([(item["type"], item["name"]) for item in found], [("A", "www.shop.dk"), ("TXT", "shop.dk")])
        self.assertEqual(len([item for item in fake.asked if "dns_records" in item[1]]), 2)

    def test_a_record_as_account_vns_dk_makes_it(self):
        body = cloudflare.record_body({"type": "TXT", "name": "@", "content": "v=spf1 -all", "ttl": 300}, "shop.dk", admin=False)
        self.assertEqual(body, {"type": "TXT", "name": "shop.dk", "content": '"v=spf1 -all"', "ttl": 300})
        body = cloudflare.record_body({"type": "MX", "name": "mail", "content": "mx.example.com", "ttl": 1, "priority": 10}, "shop.dk", admin=True)
        self.assertEqual((body["name"], body["priority"]), ("mail.shop.dk", 10))
        with self.assertRaises(ValueError):
            cloudflare.record_body({"type": "MX", "name": "mail", "content": "mx.example.com", "ttl": 1}, "shop.dk", admin=True)
        with self.assertRaises(ValueError):
            cloudflare.record_body({"type": "A", "name": "www", "content": "203.0.113.10", "ttl": 42}, "shop.dk", admin=True)
        # Proxied is an administrator's: a user's record keeps what it has
        self.assertTrue(cloudflare.record_body({"type": "A", "name": "www", "content": "203.0.113.10", "ttl": 1, "proxied": True}, "shop.dk", admin=True)["proxied"])
        self.assertFalse(cloudflare.record_body({"type": "A", "name": "www", "content": "203.0.113.10", "ttl": 1, "proxied": True}, "shop.dk", admin=False)["proxied"])
        # Its type stays as it was made
        self.assertEqual(cloudflare.record_body({"type": "TXT", "name": "www", "content": "203.0.113.11", "ttl": 1}, "shop.dk", False,
                                                {"type": "A", "proxied": False})["type"], "A")

    def test_srv_and_caa_as_their_parts(self):
        body = cloudflare.record_body({"type": "SRV", "name": "_sip._tcp", "ttl": 1, "priority": 10, "weight": 5, "port": 5060, "target": "sip.shop.dk"}, "shop.dk", admin=True)
        self.assertEqual(body, {"type": "SRV", "name": "_sip._tcp.shop.dk", "ttl": 1, "data": {"priority": 10, "weight": 5, "port": 5060, "target": "sip.shop.dk"}})
        with self.assertRaises(ValueError):
            cloudflare.record_body({"type": "SRV", "name": "sip", "ttl": 1, "priority": 1, "weight": 1, "port": 1, "target": "x.dk"}, "shop.dk", admin=True)
        body = cloudflare.record_body({"type": "CAA", "name": "@", "ttl": 1, "flags": 0, "tag": "issue", "value": "letsencrypt.org"}, "shop.dk", admin=True)
        self.assertEqual(body["data"], {"flags": 0, "tag": "issue", "value": "letsencrypt.org"})
        with self.assertRaises(ValueError):
            cloudflare.record_body({"type": "CAA", "name": "@", "ttl": 1, "tag": "nope", "value": "x"}, "shop.dk", admin=True)

    def test_cloudflare_saying_no_is_said(self):
        def refuse(request, timeout=None):
            return Answer(json.dumps({"success": False, "errors": [{"message": "Invalid request headers"}]}).encode())
        with self.assertRaises(cloudflare.CloudflareError) as said:
            cloudflare.zones({"token": "t" * 40}, opener=refuse)
        self.assertIn("Invalid request headers", str(said.exception))


class DnsAccessTests(unittest.TestCase):
    def test_a_user_keeps_only_the_domains_given(self):
        user = access.clean({"username": "anna", "role": "user", "dns": True, "dns_domains": ["Shop.DK.", "not a domain"]})
        self.assertEqual(user["dns_domains"], ["shop.dk"])
        self.assertTrue(access.has_dns(user))
        self.assertTrue(access.dns_zone(user, "shop.dk"))
        self.assertFalse(access.dns_zone(user, "other.dk"))
        # On with no domain is no access
        self.assertFalse(access.clean({"username": "bo", "role": "user", "dns": True, "dns_domains": []})["dns"])
        self.assertTrue(access.dns_zone({"role": "admin"}, "anything.dk"))
        # A template keeps DNS on or off; the domains are each user's
        self.assertTrue(access.template_rules({"dns": True})["dns"])
        self.assertFalse(access.template_rules({})["dns"])


if __name__ == "__main__":
    unittest.main()
