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

    def test_zone_settings_only_as_cloudflare_takes_them(self):
        fake = FakeCloudflare({f"/zones/{ZONE}/settings": ([{"id": "ssl", "value": "full", "editable": True}, {"id": "always_use_https", "value": "off"},
                                                            {"id": "brotli", "value": "on"}], 1)})
        found = cloudflare.zone_settings({"token": "t" * 40}, ZONE, "ssl", opener=fake)
        self.assertEqual(set(found), {"ssl", "always_use_https"})
        cloudflare.change_setting({"token": "t" * 40}, ZONE, "ssl", "ssl", "strict", opener=fake)
        self.assertEqual((fake.asked[-1][0], fake.asked[-1][1].split("/")[-1], json.loads(fake.asked[-1][3])), ("PATCH", "ssl", {"value": "strict"}))
        with self.assertRaises(ValueError):
            cloudflare.change_setting({"token": "t" * 40}, ZONE, "ssl", "ssl", "everything", opener=fake)
        with self.assertRaises(ValueError):
            cloudflare.change_setting({"token": "t" * 40}, ZONE, "cache", "ssl", "strict", opener=fake)
        cloudflare.change_setting({"token": "t" * 40}, ZONE, "cache", "browser_cache_ttl", "14400", opener=fake)
        self.assertEqual(json.loads(fake.asked[-1][3]), {"value": 14400})

    def test_purge_of_addresses_and_redirects(self):
        fake = FakeCloudflare()
        cloudflare.purge_urls({"token": "t" * 40}, ZONE, ["https://shop.dk/a.css", " "], opener=fake)
        self.assertEqual(json.loads(fake.asked[-1][3]), {"files": ["https://shop.dk/a.css"]})
        with self.assertRaises(ValueError):
            cloudflare.purge_urls({"token": "t" * 40}, ZONE, ["shop.dk/a.css"], opener=fake)
        self.assertEqual(cloudflare.redirect_expression("*.old.dk", "/blog"), 'ends_with(http.host, ".old.dk") and starts_with(http.request.uri.path, "/blog")')
        with self.assertRaises(ValueError):
            cloudflare.redirect_expression("not a host")
        cloudflare.save_redirects({"token": "t" * 40}, ZONE, [{"description": "Old", "source_host": "old.shop.dk", "target": "https://shop.dk", "status": 301, "keep_query": True}], opener=fake)
        sent = json.loads(fake.asked[-1][3])["rules"][0]
        self.assertEqual((sent["expression"], sent["action_parameters"]["from_value"]["target_url"]["value"], sent["action_parameters"]["from_value"]["preserve_query_string"]),
                         ('http.host eq "old.shop.dk"', "https://shop.dk", True))
        self.assertEqual(fake.asked[-1][0], "PUT")

    def test_page_rules_read_and_a_forward_made(self):
        rule = {"id": "b" * 32, "status": "active", "priority": 1,
                "targets": [{"target": "url", "constraint": {"operator": "matches", "value": "www.aogtillon.dk/*"}}],
                "actions": [{"id": "forwarding_url", "value": {"url": "https://aogtillon.dk/$1", "status_code": 301}}]}
        fake = FakeCloudflare({f"/zones/{ZONE}/pagerules": ([rule], 1)})
        found = cloudflare.page_rules({"token": "t" * 40}, ZONE, opener=fake)
        self.assertEqual((found[0]["pattern"], found[0]["forward"], found[0]["enabled"]), ("www.aogtillon.dk/*", {"url": "https://aogtillon.dk/$1", "status": 301}, True))
        cloudflare.save_page_rule({"token": "t" * 40}, ZONE, None, {"pattern": "old.shop.dk/*", "url": "https://shop.dk/$1", "status": 302}, opener=fake)
        sent = json.loads(fake.asked[-1][3])
        self.assertEqual((fake.asked[-1][0], sent["actions"][0]["value"]["status_code"], sent["status"]), ("POST", 302, "active"))
        with self.assertRaises(ValueError):
            cloudflare.save_page_rule({"token": "t" * 40}, ZONE, None, {"pattern": "x", "url": "https://shop.dk"}, opener=fake)
        cloudflare.page_rule_state({"token": "t" * 40}, ZONE, "b" * 32, False, opener=fake)
        self.assertEqual((fake.asked[-1][0], json.loads(fake.asked[-1][3])), ("PATCH", {"status": "disabled"}))

    def test_cache_and_configuration_rules_read_and_saved_with_what_harbor_does_not_know(self):
        cache = {"rules": [{"description": "Images", "enabled": True, "action": "set_cache_settings", "expression": 'http.request.full_uri wildcard r"http*://shop.dk/img/*"',
                            "action_parameters": {"cache": True, "edge_ttl": {"mode": "override_origin", "default": 86400}, "cache_key": {"cache_deception_armor": True, "cache_by_device_type": True},
                                                  "serve_stale": {"disable_stale_while_updating": True}}}]}
        fake = FakeCloudflare({f"/zones/{ZONE}/rulesets/phases/http_request_cache_settings/entrypoint": (cache, 1)})
        found = cloudflare.settings_rules({"token": "t" * 40}, ZONE, "cache", opener=fake)[0]
        self.assertEqual((found["mode"], found["pattern"]), ("wildcard", "http*://shop.dk/img/*"))
        self.assertEqual(found["actions"], [{"id": "cache_level", "value": "cache_everything"}, {"id": "edge_cache_ttl", "value": 86400}, {"id": "cache_deception_armor", "value": "on"}])
        self.assertEqual(found["extra"], {"cache_key": {"cache_by_device_type": True}, "serve_stale": {"disable_stale_while_updating": True}})
        # Saved as it was read: the same parameters, the unknown kept
        cloudflare.save_settings_rules({"token": "t" * 40}, ZONE, "cache", [found], opener=fake)
        sent = json.loads(fake.asked[-1][3])["rules"][0]
        self.assertEqual(sent["action_parameters"], cache["rules"][0]["action_parameters"])
        self.assertEqual((sent["action"], sent["expression"]), ("set_cache_settings", cache["rules"][0]["expression"]))
        config = cloudflare.settings_build("config", {"mode": "all", "actions": [{"id": "ssl", "value": "strict"}, {"id": "browser_check", "value": "off"}, {"id": "disable_zaraz", "value": None}]})
        self.assertEqual((config["expression"], config["action"], config["action_parameters"]), ("true", "set_config", {"ssl": "strict", "bic": False, "disable_zaraz": True}))
        with self.assertRaises(ValueError):
            cloudflare.settings_build("cache", {"pattern": "http*://shop.dk/*", "actions": [{"id": "ssl", "value": "full"}]})
        with self.assertRaises(ValueError):
            cloudflare.settings_build("cache", {"pattern": "http*://shop.dk/*", "actions": [{"id": "cache_level", "value": "basic"}]})

    def test_a_page_rule_converted_as_cloudflare_guides(self):
        forward = cloudflare.page_rule_plan({"pattern": "www.shop.dk/*", "enabled": True, "actions": [{"id": "forwarding_url", "value": {"url": "https://shop.dk/$1", "status_code": 301}}]})
        # http*:// before it is the first *, so $1 is ${2}
        self.assertEqual((forward["redirect"]["pattern"], forward["redirect"]["target"], forward["redirect"]["keep_query"]), ("http*://www.shop.dk/*", "https://shop.dk/${2}", True))
        https = cloudflare.page_rule_plan({"pattern": "*shop.dk/*", "enabled": True, "actions": [{"id": "always_use_https"}]})
        self.assertEqual((https["redirect"]["pattern"], https["redirect"]["target"]), ("http://*shop.dk/*", "https://${1}shop.dk/${2}"))
        mixed = cloudflare.page_rule_plan({"pattern": "shop.dk/img", "enabled": False, "actions": [{"id": "cache_level", "value": "cache_everything"}, {"id": "ssl", "value": "full"},
                                                                                                   {"id": "rocket_loader", "value": "off"}]})
        # An address without * at its end is that address, with or without a query string, not every one after it
        self.assertEqual((mixed["cache"]["mode"], mixed["cache"]["expression"], mixed["cache"]["enabled"]),
                         ("custom", '(http.request.full_uri wildcard r"http*://shop.dk/img" or http.request.full_uri wildcard r"http*://shop.dk/img?*")', False))
        exact = cloudflare.page_rule_plan({"pattern": "*.shop.dk/", "actions": [{"id": "forwarding_url", "value": {"url": "https://shop.dk/$1", "status_code": 301}}]})["redirect"]
        self.assertEqual((exact["mode"], exact["target"], exact["target_expression"]),
                         ("custom", "", 'wildcard_replace(http.request.full_uri, r"http*://*.shop.dk/*", r"https://shop.dk/${2}")'))
        cloudflare.redirect_build(exact)
        self.assertEqual([action["id"] for action in mixed["config"]["actions"]], ["ssl", "rocket_loader"])
        self.assertEqual(cloudflare.page_rule_plan({"pattern": "shop.dk/*", "actions": [{"id": "cache_level", "value": "aggressive"}]})["dropped"][0]["id"], "cache_level")
        self.assertEqual([item["id"] for item in cloudflare.page_rule_plan({"pattern": "shop.dk/*", "actions": [{"id": "cache_level", "value": "basic"}, {"id": "ip_geolocation", "value": "on"}]})["left"]],
                         ["cache_level", "ip_geolocation"])

    def test_converting_puts_the_new_rules_in_order_and_turns_the_page_rule_off(self):
        rule = {"id": "b" * 32, "status": "active", "priority": 1, "targets": [{"target": "url", "constraint": {"operator": "matches", "value": "shop.dk/img/*"}}],
                "actions": [{"id": "cache_level", "value": "cache_everything"}, {"id": "security_level", "value": "high"}]}
        there = {"rules": [{"description": "Old", "enabled": True, "action": "set_cache_settings", "expression": "true", "action_parameters": {"cache": False}}]}
        fake = FakeCloudflare({f"/zones/{ZONE}/pagerules": ([rule], 1), f"/zones/{ZONE}/rulesets/phases/http_request_cache_settings/entrypoint": (there, 1),
                               f"/zones/{ZONE}/rulesets/phases/http_config_settings/entrypoint": ({"rules": []}, 1)})
        cloudflare.convert_page_rule({"token": "t" * 40}, ZONE, "b" * 32, opener=fake)
        changed = [(method, url.split("/zones/")[1], json.loads(data) if data else None) for method, url, _, data in fake.asked if method != "GET"]
        cache = next(body for method, url, body in changed if "cache_settings" in url)["rules"]
        # First of the cache rules, where the last that matches wins: page rules converted from the top keep their order
        self.assertEqual([item["action_parameters"] for item in cache], [{"cache": True}, {"cache": False}])
        config = next(body for method, url, body in changed if "config_settings" in url)["rules"]
        self.assertEqual(config[0]["action_parameters"], {"security_level": "high"})
        self.assertEqual(changed[-1], ("PATCH", f"{ZONE}/pagerules/{'b' * 32}", {"status": "disabled"}))
        # Nothing is sent for one with a setting that has no newer rule
        rule["actions"] = [{"id": "cache_level", "value": "simplified"}]
        before = len(fake.asked)
        with self.assertRaises(ValueError):
            cloudflare.convert_page_rule({"token": "t" * 40}, ZONE, "b" * 32, opener=fake)
        self.assertTrue(all(method == "GET" for method, *_ in fake.asked[before:]))

    def test_analytics_a_day_with_the_empty_ones(self):
        import datetime
        def answer(request, timeout=None):
            sent = json.loads(request.data)
            self.assertEqual((sent["variables"]["since"], sent["variables"]["until"]), ("2026-11-21", "2026-11-27"))
            return Answer(json.dumps({"data": {"viewer": {"zones": [{"httpRequests1dGroups": [
                {"dimensions": {"date": "2026-11-26"}, "sum": {"requests": 120, "cachedRequests": 60, "bytes": 5000, "cachedBytes": 2500, "threats": 2, "pageViews": 40}, "uniq": {"uniques": 9}}]}]}}}).encode())
        days = cloudflare.analytics({"token": "t" * 40}, ZONE, 7, today=datetime.date(2026, 11, 27), opener=answer)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[5], {"date": "2026-11-26", "requests": 120, "cached": 60, "bytes": 5000, "cached_bytes": 2500, "threats": 2, "views": 40, "visitors": 9})
        self.assertEqual(days[0]["requests"], 0)
        def refuse(request, timeout=None):
            return Answer(json.dumps({"errors": [{"message": "not authorized for that account"}]}).encode())
        with self.assertRaises(cloudflare.CloudflareError):
            cloudflare.analytics({"token": "t" * 40}, ZONE, 7, opener=refuse)

    def test_what_the_traffic_was_made_of_summed_over_the_days(self):
        import datetime
        def answer(request, timeout=None):
            day = {"encryptedRequests": 90, "countryMap": [{"clientCountryName": "DK", "requests": 80, "threats": 1, "bytes": 4000}, {"clientCountryName": "US", "requests": 20, "threats": 0, "bytes": 900}],
                   "responseStatusMap": [{"edgeResponseStatus": 200, "requests": 95}, {"edgeResponseStatus": 404, "requests": 5}],
                   "contentTypeMap": [{"edgeResponseContentTypeName": "html", "requests": 60, "bytes": 3000}, {"edgeResponseContentTypeName": "", "requests": 1, "bytes": 0}],
                   "browserMap": [{"uaBrowserFamily": "Chrome", "pageViews": 30}], "clientHTTPVersionMap": [{"clientHTTPProtocol": "HTTP/2", "requests": 100}],
                   "clientSSLMap": [{"clientSSLProtocol": "TLSv1.3", "requests": 90}]}
            other = dict(day, countryMap=[{"clientCountryName": "US", "requests": 70, "threats": 3, "bytes": 100}])
            return Answer(json.dumps({"data": {"viewer": {"zones": [{"httpRequests1dGroups": [{"sum": day}, {"sum": other}]}]}}}).encode())
        found = cloudflare.breakdown({"token": "t" * 40}, ZONE, 7, today=datetime.date(2026, 11, 27), opener=answer)
        self.assertEqual(found["encrypted"], 180)
        # Summed over the days, largest first
        self.assertEqual(found["countries"], [{"name": "US", "requests": 90, "threats": 3, "bytes": 1000}, {"name": "DK", "requests": 80, "threats": 1, "bytes": 4000}])
        self.assertEqual(found["statuses"][0], {"name": "200", "requests": 190})
        self.assertEqual(found["browsers"], [{"name": "Chrome", "views": 60}])
        # A name Cloudflare left empty is said as Unknown
        self.assertIn({"name": "Unknown", "requests": 2, "bytes": 0}, found["content"])
        with self.assertRaises(ValueError):
            cloudflare.breakdown({"token": "t" * 40}, ZONE, 14, opener=answer)

    def test_how_many_rules_a_domain_may_have(self):
        zone = {"plan": {"legacy_id": "free"}, "meta": {"page_rule_quota": 3}}
        # The entitlements when they are given, exact
        fake = FakeCloudflare({f"/zones/{ZONE}": (zone, 1), f"/zones/{ZONE}/entitlements": ([{"id": "rulesets.dynamic_redirect_rule_max", "allocation": {"value": 12}},
                                                                                               {"id": "page_rules", "allocation": {"value": 5}}], 1)})
        self.assertEqual(cloudflare.rule_limits({"token": "t" * 40}, ZONE, opener=fake), {"redirects": 12, "page_rules": 5, "cache": 10, "config": 10})
        # A token that may not read them: the domain's page rules and its plan's redirects
        def refused(request, timeout=None):
            if request.full_url.endswith("/entitlements"):
                return Answer(json.dumps({"success": False, "errors": [{"message": "Unauthorized"}]}).encode())
            return Answer(json.dumps({"success": True, "result": zone}).encode())
        self.assertEqual(cloudflare.rule_limits({"token": "t" * 40}, ZONE, opener=refused), {"redirects": 10, "page_rules": 3, "cache": 10, "config": 10})

    def test_managed_transforms_read_and_one_turned_on(self):
        current = {"managed_request_headers": [{"id": "remove_visitor_ip_headers", "enabled": False, "has_conflict": True, "conflicts_with": ["add_true_client_ip_headers"]}],
                   "managed_response_headers": [{"id": "add_security_headers", "enabled": False, "has_conflict": False}]}
        fake = FakeCloudflare({f"/zones/{ZONE}/managed_headers": (current, 1)})
        found = cloudflare.managed_headers({"token": "t" * 40}, ZONE, opener=fake)
        self.assertEqual(found["request"][0]["conflicts"], ["add_true_client_ip_headers"])
        self.assertEqual(found["response"][0]["title"], "Add security headers")
        cloudflare.set_managed_header({"token": "t" * 40}, ZONE, "add_security_headers", True, opener=fake)
        sent = [json.loads(data) for method, url, headers, data in fake.asked if method == "PATCH"]
        self.assertEqual(sent, [{"managed_request_headers": [], "managed_response_headers": [{"id": "add_security_headers", "enabled": True}]}])
        # Only one Cloudflare has for the domain
        with self.assertRaises(ValueError):
            cloudflare.set_managed_header({"token": "t" * 40}, ZONE, "add_everything", True, opener=fake)

    def test_a_redirect_by_wildcard_pattern_and_its_parts(self):
        built = cloudflare.redirect_build({"mode": "wildcard", "pattern": "http://*", "target": "https://${1}", "status": 308, "keep_query": True})
        self.assertEqual(built["expression"], 'http.request.full_uri wildcard r"http://*"')
        # An address with no path is every page of it: Cloudflare's address always has one, so it matched nothing
        whole = cloudflare.redirect_build({"mode": "wildcard", "pattern": "http*://account.example.com", "target": "https://harbor.example.com", "status": 302})
        self.assertEqual(whole["expression"], 'http.request.full_uri wildcard r"http*://account.example.com/*"')
        self.assertEqual(cloudflare.whole_address("https://old.example.com/x"), "https://old.example.com/x")
        self.assertEqual(built["action_parameters"]["from_value"]["target_url"], {"expression": 'wildcard_replace(http.request.full_uri, r"http://*", r"https://${1}")'})
        # Read back as it was given
        read = cloudflare.redirect_read(built)
        self.assertEqual((read["mode"], read["pattern"], read["target"], read["status"]), ("wildcard", "http://*", "https://${1}", 308))
        # Without ${n} the target is a plain address; every request is "true"
        self.assertEqual(cloudflare.redirect_build({"mode": "all", "target": "https://shop.dk"})["action_parameters"]["from_value"]["target_url"], {"value": "https://shop.dk"})
        self.assertEqual(cloudflare.redirect_build({"mode": "all", "target": "https://shop.dk"})["expression"], "true")
        # Cloudflare's own expression and target expression are kept as they are
        own = {"expression": 'http.host eq "old.shop.dk"', "action_parameters": {"from_value": {"target_url": {"expression": 'concat("https://shop.dk", http.request.uri.path)'}}}}
        read = cloudflare.redirect_read(own)
        self.assertEqual((read["mode"], read["target_expression"]), ("custom", 'concat("https://shop.dk", http.request.uri.path)'))
        self.assertEqual(cloudflare.redirect_build(read)["action_parameters"]["from_value"]["target_url"], {"expression": 'concat("https://shop.dk", http.request.uri.path)'})
        for wrong in ({"mode": "wildcard", "pattern": "http://*", "target": "https://${2}"}, {"mode": "all", "target": "https://shop.dk/${1}"},
                      {"mode": "wildcard", "pattern": 'x"y', "target": "https://shop.dk"}, {"mode": "all", "target": "https://shop.dk", "status": 303}):
            with self.assertRaises(ValueError):
                cloudflare.redirect_build(wrong)

    def test_a_page_rule_with_the_settings_chosen(self):
        body = cloudflare.page_rule_body({"pattern": "shop.dk/img/*", "actions": [{"id": "cache_level", "value": "cache_everything"}, {"id": "edge_cache_ttl", "value": 86400}, {"id": "disable_zaraz"}]})
        self.assertEqual(body["actions"], [{"id": "cache_level", "value": "cache_everything"}, {"id": "edge_cache_ttl", "value": 86400}, {"id": "disable_zaraz"}])
        # Harbor's first form still forwards; $n no further than the pattern's *
        self.assertEqual(cloudflare.page_rule_body({"pattern": "www.shop.dk/*", "url": "https://shop.dk/$1"})["actions"][0]["value"], {"url": "https://shop.dk/$1", "status_code": 301})
        for wrong in ([{"id": "forwarding_url", "value": {"url": "https://shop.dk/$2"}}], [{"id": "always_use_https"}, {"id": "ssl", "value": "full"}],
                      [{"id": "ssl", "value": "full"}, {"id": "ssl", "value": "strict"}], [{"id": "cache_level", "value": "everything"}], []):
            with self.assertRaises(ValueError):
                cloudflare.page_rule_body({"pattern": "www.shop.dk/*", "actions": wrong})

    def test_the_domains_kept_until_refreshed_or_another_account(self):
        fake = FakeCloudflare({"/zones": ([{"id": ZONE, "name": "shop.dk", "status": "active"}], 1)})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "zones.json"
            account = {"auth": "token", "token": "t" * 40}
            self.assertEqual(cloudflare.kept_zones(account, path, opener=fake)["zones"][0]["name"], "shop.dk")
            # Kept: Cloudflare is not asked again, and the key or token is not in the file
            cloudflare.kept_zones(account, path, opener=fake)
            self.assertEqual(len(fake.asked), 1)
            self.assertNotIn("t" * 40, path.read_text())
            # Refreshed, or another account: asked again
            cloudflare.kept_zones(account, path, fresh=True, opener=fake)
            cloudflare.kept_zones({"auth": "token", "token": "u" * 40}, path, opener=fake)
            self.assertEqual(len(fake.asked), 3)
            cloudflare.forget_zones(path)
            self.assertFalse(path.exists())

    def test_a_record_locked_for_users_as_chosen(self):
        """A proxied record is locked until an administrator opens it; any record can be locked; a deleted one forgotten"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "locks.json"
            locks = cloudflare.load_locks(path)
            self.assertTrue(cloudflare.is_locked(locks, ZONE, {"id": RECORD, "proxied": True}))
            self.assertFalse(cloudflare.is_locked(locks, ZONE, {"id": RECORD, "proxied": False}))
            cloudflare.set_lock(path, ZONE, RECORD, False)
            self.assertFalse(cloudflare.is_locked(cloudflare.load_locks(path), ZONE, {"id": RECORD, "proxied": True}))
            cloudflare.set_lock(path, ZONE, RECORD, True)
            self.assertTrue(cloudflare.is_locked(cloudflare.load_locks(path), ZONE, {"id": RECORD, "proxied": False}))
            cloudflare.set_lock(path, ZONE, RECORD, None)
            self.assertEqual(cloudflare.load_locks(path), {})

    def test_a_user_who_proxies_a_record_is_not_locked_out(self):
        """A user turns Proxied on for a record open to them: it stays open, so they can turn it off again"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "locks.json"
            cloudflare.keep_open(path, ZONE, RECORD, admin=False)
            self.assertFalse(cloudflare.is_locked(cloudflare.load_locks(path), ZONE, {"id": RECORD, "proxied": True}))
            # An administrator's lock is not undone by it, and an administrator's own change leaves no choice behind
            cloudflare.set_lock(path, ZONE, RECORD, True)
            cloudflare.keep_open(path, ZONE, RECORD, admin=False)
            self.assertTrue(cloudflare.is_locked(cloudflare.load_locks(path), ZONE, {"id": RECORD, "proxied": False}))
            cloudflare.set_lock(path, ZONE, RECORD, None)
            cloudflare.keep_open(path, ZONE, RECORD, admin=True)
            self.assertEqual(cloudflare.load_locks(path), {})

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
        # A domain with æ, ø or å is kept as it is named
        both = access.clean({"username": "d", "role": "user", "dns": True, "dns_domains": ["ledernelon.dk", "ledernøn.dk"]})
        self.assertEqual(both["dns_domains"], ["ledernelon.dk", "ledernøn.dk"])
        self.assertTrue(access.dns_zone(both, "ledernøn.dk"))
        self.assertTrue(access.has_dns(user))
        self.assertTrue(access.dns_zone(user, "shop.dk"))
        self.assertFalse(access.dns_zone(user, "other.dk"))
        # On with no domain is no access
        self.assertFalse(access.clean({"username": "bo", "role": "user", "dns": True, "dns_domains": []})["dns"])
        self.assertTrue(access.dns_zone({"role": "admin"}, "anything.dk"))
        # A template keeps DNS on or off; the domains are each user's
        self.assertTrue(access.template_rules({"dns": True})["dns"])
        self.assertFalse(access.template_rules({})["dns"])
        # Which parts of Cloudflare: DNS for one given before there was a choice
        parts = access.clean({"username": "c", "role": "user", "dns": True, "dns_domains": ["shop.dk"], "cloudflare_parts": ["cache", "dns"]})
        self.assertEqual(parts["cloudflare_parts"], ["dns", "cache"])
        self.assertTrue(access.cloudflare_part(parts, "cache"))
        self.assertFalse(access.cloudflare_part(parts, "ssl"))
        self.assertEqual(user["cloudflare_parts"], ["dns"])
        self.assertTrue(access.cloudflare_part({"role": "admin"}, "security"))


if __name__ == "__main__":
    unittest.main()
