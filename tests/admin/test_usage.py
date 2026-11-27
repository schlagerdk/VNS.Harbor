"""Usage limits of the admin's users: how many sites and databases they may make, and how big each may grow, with a
mail when one grows past it (lib/usage.py, lib/access.py, 1.0.70)"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

spec = importlib.util.spec_from_file_location("usage_under_test", ROOT_DIR / "lib/usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


def user(**more):
    return {"id": "u1", "username": "anna", "role": "user", **more}


class LimitTests(unittest.TestCase):
    def test_limits_are_whole_numbers_or_none(self):
        self.assertEqual(access.clean_limits({"sites": 3, "site_mb": 2048, "databases": 2}), {"sites": 3, "site_mb": 2048})
        self.assertEqual(access.clean_limits({"databases": 2, "database_mb": "", "sites": 1}, ("databases", "database_mb")), {"databases": 2})
        self.assertEqual(access.clean_limits(None), {})
        for bad in ({"sites": -1}, {"sites": 1.5}, {"site_mb": True}, {"sites": "3"}, "x"):
            with self.assertRaises(ValueError, msg=bad):
                access.clean_limits(bad)

    def test_a_connections_database_limits_come_with_create_or_copy(self):
        """The database limits were the user's, for every connection: they are each connection's, and only where the
        user may make databases (1.0.70)"""
        cleaned = access.clean({"username": "anna", "role": "user", "sql": True, "connections": {
            "c1": {"databases": [], "own": True, "actions": ["create"], "limits": {"databases": 1, "database_mb": 500}},
            "c2": {"databases": ["*"], "actions": ["query"], "limits": {"databases": 3}}}}, connections=["c1", "c2"])
        self.assertEqual(cleaned["connections"]["c1"]["limits"], {"databases": 1, "database_mb": 500})
        self.assertNotIn("limits", cleaned["connections"]["c2"], "no Create or Copy: no limits")

    def test_a_user_keeps_limits_and_what_they_made_and_an_admin_has_none(self):
        saved = access.clean({"username": "anna", "role": "user", "limits": {"sites": 2}})
        saved["made"] = {"sites": ["a.dk"]}
        again = access.clean({"role": "user", "made": {"sites": ["forged.dk"]}}, existing=saved)
        self.assertEqual(again["limits"], {"sites": 2})
        self.assertEqual(again["made"], {"sites": ["a.dk"]}, "what they made is never taken from a form")
        self.assertEqual(access.clean({"role": "admin"}, existing=again)["limits"], {})

    def test_how_many_they_may_make(self):
        anna = user(limits={"sites": 2}, connections={"c1": {"databases": [], "own": True, "actions": ["create"], "limits": {"databases": 1}}})
        self.assertIsNone(access.may_make(anna, "sites"))
        self.assertTrue(access.record_made(anna, "sites", "a.dk"))
        self.assertFalse(access.record_made(anna, "sites", "a.dk"), "counted once")
        access.record_made(anna, "sites", "b.dk")
        self.assertIn("2 sites of the 2", access.may_make(anna, "sites"))
        # A site that is gone no longer counts
        self.assertIsNone(access.may_make(anna, "sites", lambda site: site == "a.dk"))
        self.assertTrue(access.forget_made(anna, "sites", "b.dk"))
        self.assertIsNone(access.may_make(anna, "sites"))
        # Databases: each connection its own limit, which create, copy and restore to a new one count against
        access.record_made(anna, "databases", "Shop", "c1")
        self.assertIn("1 database of the 1", access.may_make(anna, "databases", connection="c1"))
        self.assertIsNone(access.may_make(anna, "databases", connection="c2"), "another connection has none")
        self.assertIsNone(access.may_make(user(), "databases", connection="c1"), "no limit")
        self.assertIsNone(access.may_make({"role": "admin", "limits": {"sites": 0}}, "sites"))


class UsageCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        users = [user(name="Anna", limits={"site_mb": 100}, connections={"c1": {"databases": [], "actions": ["create"], "limits": {"database_mb": 1024}}},
                      made={"sites": ["big.dk", "small.dk"], "databases": {"c1": ["Shop", "Tiny"], "c2": ["Shop"]}}),
                 user(id="u2", username="bo", limits={}, made={"sites": ["free.dk"]}),
                 {"id": "a", "username": "root", "role": "admin", "made": {"sites": ["big.dk"]}, "limits": {"site_mb": 1}}]
        (self.data / "config.json").write_text(json.dumps({"users": users}))
        self.sites = {"big.dk": 250.0, "small.dk": 20.0, "free.dk": 9999.0}
        # The other server is never told from a test
        teller = patch.object(usage, "tell_others")
        self.told = teller.start()
        self.addCleanup(teller.stop)
        self.databases = {"Shop": 2048.0, "Tiny": 3.0}

    def measure(self):
        return {"measure_site": lambda site: self.sites.get(site), "measure_databases": lambda data, connection: self.databases,
                "measure_environments": lambda: []}

    def test_what_has_grown_past_its_limit(self):
        over = usage.check(self.data, **self.measure())
        self.assertEqual([(item["kind"], item["name"], item["user"]) for item in over], [("site", "big.dk", "Anna"), ("database", "Shop", "Anna")])
        self.assertEqual(over[0]["limit_mb"], 100)

    def test_mailed_once_when_over_and_once_when_under_again(self):
        (self.data / "usage.json").write_text(json.dumps({"enabled": True, "emails": ["ops@example.com"]}))
        sent = []
        with patch.object(usage.report, "send", side_effect=lambda settings, to, subject, body, html=None: sent.append((to, subject, body, html))), \
                patch.object(usage.report, "load_mail", return_value={"host": "smtp.example.com"}):
            usage.run(self.data, now=1, **self.measure())
            usage.run(self.data, now=2, **self.measure())
            self.sites["big.dk"] = 50.0
            usage.run(self.data, now=3, **self.measure())
        self.assertEqual(len(sent), 2, "nothing new the second time")
        self.assertIn("2 over", sent[0][1])
        self.assertIn("Site big.dk (Anna): 250 MB of 100 MB", sent[0][2])
        self.assertIn("Database Shop (Anna): 2.0 GB of 1.0 GB", sent[0][2])
        self.assertIn("1 under again", sent[1][1])
        # Under what changed, everything the users made, databases with no size limit too
        self.assertIn("Everything the users made, now:", sent[0][2])
        self.assertIn("- Database Tiny: 3 MB of 1.0 GB", sent[0][2])
        self.assertIn("- Database Shop: 2.0 GB of 1.0 GB (over)", sent[0][2])
        self.assertIn("- Site free.dk: 9.8 GB, no limit", sent[0][2])
        state = json.loads((self.data / "usage-state.json").read_text())
        self.assertEqual(list(state["open"]), ["database|c1|Shop"])

    def test_a_test_mail_goes_to_the_addresses_typed_and_keeps_nothing(self):
        """The alerts' dialog had no way to see their mail: Send test mails it to the addresses typed (1.0.92)"""
        sent = []
        result = usage.test_report(self.data, ["ops@example.com"], mail={"host": "smtp.example.com"}, now=1,
                                   sender=lambda settings, to, subject, body, html=None: sent.append((to, subject, body, html)), **self.measure())
        self.assertEqual(result["to"], ["ops@example.com"])
        self.assertIn("TEST: Usage limits: 2 over", sent[0][1])
        self.assertIn("Site big.dk (Anna): 250 MB of 100 MB", sent[0][2])
        self.assertIn("Test: 2 over their limit", sent[0][3])
        self.assertFalse((self.data / "usage-state.json").exists(), "nothing kept")
        with self.assertRaises(ValueError):
            usage.test_report(self.data, ["not an address"], **self.measure())

    def test_a_site_given_with_a_size_of_its_own_is_theirs_while_they_have_it(self):
        """An administrator's site lent a user with a size (1.0.94): theirs in Usage, warned and mailed past it; without
        one, or once taken from them, the administrator's again; their default never falls on a site they were given"""
        config = json.loads((self.data / "config.json").read_text())
        anna = config["users"][0]
        anna["sites"] = {"lent.dk": {"envs": ["main"], "actions": ["logs"], "size_mb": 50}, "other.dk": {"envs": ["main"], "actions": ["logs"]}}
        (self.data / "config.json").write_text(json.dumps(config))
        self.sites.update({"lent.dk": 80.0, "other.dk": 500.0})
        over = usage.check(self.data, **self.measure())
        self.assertIn(("site", "lent.dk", "Anna", 50), [(item["kind"], item["name"], item["user"], item["limit_mb"]) for item in over])
        self.assertNotIn("other.dk", [item["name"] for item in over], "her 100 MB for every site of hers is not put on it")
        self.assertTrue(usage.needed(self.data))
        shown = usage.overview(self.data, ["lent.dk", "other.dk"], {}, measure_site=lambda site: self.sites.get(site), measure_databases=lambda *a, **k: {})
        self.assertEqual(sorted((item["name"], item["user"] or "") for item in shown["sites"]), [("lent.dk", "Anna"), ("other.dk", "")])
        del anna["sites"]["lent.dk"]
        (self.data / "config.json").write_text(json.dumps(config))
        self.assertNotIn("lent.dk", [item["name"] for item in usage.check(self.data, **self.measure())])

    def test_an_administrator_limits_any_site_or_database_whoever_s_it_is(self):
        """The server's own limits (1.0.94): past one, it is over and mailed with the usage alerts; kept when the alerts'
        recipients are saved; taken away, nothing is over"""
        usage.set_limit(self.data, "site", "free.dk", limit_mb=1000)
        usage.set_limit(self.data, "database", "Tiny", "c1", 2)
        over = {(item["kind"], item["name"]): item for item in usage.check(self.data, **self.measure()) if item["key"].startswith("limit|")}
        self.assertEqual(set(over), {("site", "free.dk"), ("database", "Tiny")})
        self.assertEqual(over[("site", "free.dk")]["limit_mb"], 1000)
        self.assertTrue(usage.needed(self.data))
        shown = usage.overview(self.data, ["free.dk"], {}, measure_site=lambda site: self.sites.get(site), measure_databases=lambda *a, **k: {})
        self.assertEqual(shown["sites"][0]["server_limit_mb"], 1000)
        html = usage.mail_html([over[("site", "free.dk")]], [], host="web-01", now=1)
        self.assertIn("The server&#x27;s own limit", html)
        # Raised or taken away: not over any more at once, before the next check (1.0.94)
        (self.data / "usage-state.json").write_text(json.dumps({"open": {"limit|site|free.dk": {**over[("site", "free.dk")]}}}))
        usage.set_limit(self.data, "site", "free.dk", limit_mb=20000)
        self.assertEqual(json.loads((self.data / "usage-state.json").read_text())["open"], {})
        usage.set_limit(self.data, "site", "free.dk", limit_mb=None)
        self.assertNotIn(("site", "free.dk"), {(item["kind"], item["name"]) for item in usage.check(self.data, **self.measure())})
        with self.assertRaises(ValueError):
            usage.set_limit(self.data, "site", "../x", limit_mb=5)

    def test_an_environment_has_a_limit_measured_where_it_runs(self):
        """A limit on an environment (1.0.94): measured where it runs, here or on its server; every environment shows in
        Usage with its size and the limit on it"""
        usage.set_limit(self.data, "environment", "shop.dk", "production", 200)
        over = usage.check(self.data, **self.measure(), measure_environment=lambda site, target: 250.0)
        found = [item for item in over if item["kind"] == "environment"]
        self.assertEqual([(item["site"], item["target"], item["limit_mb"]) for item in found], [("shop.dk", "production", 200)])
        self.assertEqual(usage.check(self.data, **self.measure(), measure_environment=lambda site, target: None)
                         and [item for item in usage.check(self.data, **self.measure(), measure_environment=lambda site, target: None) if item["kind"] == "environment"], [],
                         "not reached: not over")
        shown = usage.overview(self.data, [], {}, measure_site=lambda site: None, measure_databases=lambda *a, **k: {},
                               measure_environments=lambda: [{"site": "shop.dk", "target": "production", "size_mb": 250.0},
                                                             {"site": "shop.dk", "target": "test", "size_mb": 10.0}])
        self.assertEqual([(item["target"], item["server_limit_mb"]) for item in shown["environments"]], [("production", 200), ("test", None)])
        with self.assertRaises(ValueError):
            usage.set_limit(self.data, "environment", "shop.dk", "../x", 5)

    def test_a_mail_that_fails_is_tried_again_and_off_mails_nothing(self):
        (self.data / "usage.json").write_text(json.dumps({"enabled": True, "emails": ["ops@example.com"]}))
        with patch.object(usage.report, "send", side_effect=OSError("refused")), patch.object(usage.report, "load_mail", return_value={}):
            result = usage.run(self.data, now=1, **self.measure())
        self.assertEqual(result["mail_error"], "refused")
        # Still shown at once (Needs attention), and mailed next time, as if it were new (1.0.97)
        state = json.loads((self.data / "usage-state.json").read_text())
        self.assertEqual(len(state["open"]), 2)
        self.assertEqual(sorted(state["unmailed"]), sorted(state["open"]))
        sent = []
        with patch.object(usage.report, "send", lambda *args, **kwargs: sent.append(args)), patch.object(usage.report, "load_mail", return_value={}):
            usage.run(self.data, now=2, **self.measure())
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads((self.data / "usage-state.json").read_text())["unmailed"], [])
        (self.data / "usage.json").write_text(json.dumps({"enabled": False, "emails": ["ops@example.com"]}))
        with patch.object(usage.report, "send") as send:
            usage.run(self.data, now=2, **self.measure())
        send.assert_not_called()
        # Check now in the admin: measured and shown, never mailed or kept
        with patch.object(usage.report, "send") as send:
            result = usage.run(self.data, mail=False, now=3, **self.measure())
        send.assert_not_called()
        self.assertEqual(len(result["over"]), 2)
        # and with everything a user made beside it, also what has no limit (a database of 16 MB was not shown)
        shown = {(item["user"], item["name"], item["limit_mb"]) for item in result["all"]}
        self.assertIn(("Anna", "Tiny", 1024), shown)
        self.assertIn(("bo", "free.dk", None), shown)
        self.assertEqual(len(result["all"]), 6)

    def test_a_users_own_usage_beside_their_limits(self):
        """What a user has made and how big each is, for them to see (1.0.70)"""
        anna = json.loads((self.data / "config.json").read_text())["users"][0]
        anna["create_sites"] = True
        shown = usage.of_user(self.data, anna, {"c1": "Local SQL Server"}, measure_site=lambda site: self.sites.get(site), measure_databases=lambda data, connection: self.databases)
        self.assertEqual(shown["sites"], {"may_make": True, "count_limit": None, "size_limit_mb": 100,
                                          "items": [{"name": "big.dk", "size_mb": 250.0, "limit_mb": 100}, {"name": "small.dk", "size_mb": 20.0, "limit_mb": 100}]})
        self.assertEqual([(item["name"], item["size_limit_mb"], [db["name"] for db in item["items"]]) for item in shown["connections"]],
                         [("Local SQL Server", 1024, ["Shop", "Tiny"])])
        # A site that is gone is not shown; a SQL Server that does not answer says so
        self.sites.pop("small.dk")
        shown = usage.of_user(self.data, anna, measure_site=lambda site: self.sites.get(site), measure_databases=lambda data, connection: {})
        self.assertEqual([item["name"] for item in shown["sites"]["items"]], ["big.dk"])
        self.assertTrue(shown["connections"][0]["unreachable"])
        self.assertTrue(usage.shows_usage(anna))
        self.assertFalse(usage.shows_usage(user()))
        self.assertFalse(usage.shows_usage({"role": "admin", "create_sites": True}))

    def test_the_hourly_job_runs_while_a_size_limit_or_the_alerts_are_there(self):
        """A user past a limit is warned in the admin from the last check: it runs while a user has a size limit, also
        with the alerts off, and each finding says whose it is (1.0.70)"""
        self.assertTrue(usage.needed(self.data), "Anna has a site size limit")
        over = usage.check(self.data, **self.measure())
        self.assertEqual({item["user_id"] for item in over}, {"u1"})
        (self.data / "config.json").write_text(json.dumps({"users": [user(limits={"sites": 3}), {"role": "admin", "limits": {"site_mb": 1}}]}))
        self.assertFalse(usage.needed(self.data), "a count limit alone needs no measuring")
        (self.data / "usage.json").write_text(json.dumps({"enabled": True, "emails": ["ops@example.com"]}))
        self.assertTrue(usage.needed(self.data))

    def test_an_overview_of_every_site_and_database_for_an_administrator(self):
        """Usage under Settings: everything here, biggest first, who made it and its limit (1.0.70)"""
        self.sites["admin.example.com"] = 5.0
        sizes = {"Shop": 2048.0, "Tiny": 3.0, "Legacy": 500.0}
        shown = usage.overview(self.data, ["admin.example.com", "big.dk", "free.dk", "gone.dk"], {"c1": "Local SQL Server"},
                               measure_site=lambda site: self.sites.get(site), measure_databases=lambda data, connection, user_only=False: sizes)
        self.assertEqual([(item["name"], item["user"], item["limit_mb"]) for item in shown["sites"]],
                         [("free.dk", "bo", None), ("big.dk", "Anna", 100), ("admin.example.com", None, None)])
        self.assertEqual([(item["name"], item["user"], item["limit_mb"]) for item in shown["connections"][0]["databases"]],
                         [("Shop", "Anna", 1024), ("Legacy", None, None), ("Tiny", "Anna", 1024)])
        self.assertEqual(shown["connections"][0]["name"], "Local SQL Server")
        self.assertFalse(shown["connections"][0]["unreachable"])

    def test_usage_shows_the_sizes_kept_at_once_and_limits_as_they_are_now(self):
        """Usage is slow to measure (environments on other servers over SSH): the sizes measured last come at once,
        stale after ten minutes, while the limits are always as they are now (1.0.95)"""
        measured = []
        def site(name):
            measured.append(name)
            return self.sites.get(name)
        measure = {"measure_site": site, "measure_databases": lambda data, connection, user_only=False: {}, "measure_environments": lambda: []}
        first = usage.kept_overview(self.data, ["big.dk"], {}, now=1000, **measure)
        self.assertEqual((first["measured"], first["stale"], measured), (1000, False, ["big.dk"]))
        self.sites["big.dk"] = 999.0
        usage.set_limit(self.data, "site", "big.dk", limit_mb=300)
        kept = usage.kept_overview(self.data, ["big.dk"], {}, now=1500, **measure)
        self.assertEqual(measured, ["big.dk"])
        self.assertEqual((kept["sites"][0]["size_mb"], kept["sites"][0]["server_limit_mb"], kept["stale"]), (250.0, 300, False))
        self.assertTrue(usage.kept_overview(self.data, ["big.dk"], {}, now=1700, **measure)["stale"])
        # A site made since makes it stale at once; one gone is not shown
        self.assertTrue(usage.kept_overview(self.data, ["big.dk", "new.dk"], {}, now=1100, **measure)["stale"])
        self.assertEqual(usage.kept_overview(self.data, [], {}, now=1100, **measure)["sites"], [])
        fresh = usage.kept_overview(self.data, ["big.dk"], {}, fresh=True, now=1800, **measure)
        self.assertEqual((fresh["sites"][0]["size_mb"], fresh["stale"], fresh["measured"]), (999.0, False, 1800))

    def test_a_limit_sent_from_the_server_that_manages_the_site(self):
        """An environment's limit set on the server that manages it reaches the server it runs on: kept there as the
        site's limit, marked as from there, shown, and mailed only from where it was set (1.0.97)"""
        usage.set_limit(self.data, "site", "free.dk", limit_mb=100, origin="test-01")
        self.assertEqual(usage.load_limit_origins(self.data), {"site|free.dk": "test-01"})
        shown = usage.overview(self.data, ["free.dk"], {}, measure_site=lambda site: self.sites.get(site),
                               measure_databases=lambda *a, **k: {}, measure_environments=lambda: [])
        self.assertEqual((shown["sites"][0]["server_limit_mb"], shown["sites"][0]["server_limit_from"]), (100, "test-01"))
        over = [item for item in usage.check(self.data, **self.measure()) if item["key"] == "limit|site|free.dk"]
        self.assertEqual((over[0]["from"], over[0]["user"]), ("test-01", "the limit set on test-01"))
        sent = []
        with patch.object(usage, "load_settings", return_value={"enabled": True, "emails": ["ops@example.com"]}), \
                patch.object(usage.report, "send", lambda *args, **kwargs: sent.append(args)):
            usage.run(self.data, **self.measure())
        self.assertFalse(any("free.dk" in str(args) and "limit set on" in str(args) for args in sent))
        self.assertIn("limit|site|free.dk", json.loads((self.data / "usage-state.json").read_text())["open"])
        # The server it was set on does not mail (its usage alerts off): this one does, so it is mailed once (1.0.97)
        (self.data / "usage-state.json").unlink()
        usage.set_limit(self.data, "site", "free.dk", limit_mb=100, origin={"host": "test-01", "mails": False})
        with patch.object(usage, "load_settings", return_value={"enabled": True, "emails": ["ops@example.com"]}), \
                patch.object(usage.report, "send", lambda *args, **kwargs: sent.append(args)):
            usage.run(self.data, **self.measure())
        self.assertTrue(any("limit set on test-01" in str(args) for args in sent))
        # Set here again, it is this server's own
        usage.set_limit(self.data, "site", "free.dk", limit_mb=200)
        self.assertEqual(usage.load_limit_origins(self.data), {})

    def test_a_limit_from_another_server_is_changed_there_too(self):
        """Changed on the server it was sent to, it is sent back to the server it was set on (harbor server
        usage-limit-back), so the two never tell different limits; when that cannot be done, nothing changes (1.0.97)"""
        import ast
        from types import SimpleNamespace
        from flask import Flask, jsonify, request
        usage.set_limit(self.data, "site", "free.dk", limit_mb=100, origin="LINUX-01")
        source = (ROOT_DIR / "admin/app.py").read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "api_usage_limit")
        app = Flask(__name__)
        ran = []
        answer = {"code": 1, "err": "Harbor here cannot reach LINUX-01: add it under Settings, Servers, or change the limit on LINUX-01."}
        def run(argv, **kwargs):
            ran.append(argv)
            return SimpleNamespace(returncode=answer["code"], stdout="", stderr=answer["err"])
        ns = {"app": app, "request": request, "jsonify": jsonify, "require_admin": lambda: None, "_usage": usage, "DATA": self.data,
              "error": lambda message, status=400: (jsonify(error=message), status), "audit": lambda message: None,
              "sync_usage_job": lambda check_now=False: None, "push_environment_limit": lambda data: None,
              "subprocess": SimpleNamespace(run=run), "HARBOR_COMMAND": "/opt/harbor/current/bin/harbor"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "usage limit api", "exec"), ns)
        client = app.test_client()
        refused = client.put("/api/usage/limit", json={"kind": "site", "name": "free.dk", "limit_mb": None})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("cannot reach LINUX-01", refused.json["error"])
        self.assertEqual(ran[-1][-3:], ["usage-limit-back", "free.dk", "none"])
        answer.update(code=0, err="")
        sent = client.put("/api/usage/limit", json={"kind": "site", "name": "free.dk", "limit_mb": 2048})
        self.assertEqual((sent.status_code, sent.json["remote"]["to"]), (200, "LINUX-01"))
        self.assertEqual(ran[-1][-1], "2048")
        # A limit of this server's own is kept here, as before
        self.assertEqual(client.put("/api/usage/limit", json={"kind": "site", "name": "big.dk", "limit_mb": 50}).status_code, 200)
        self.assertEqual(usage.load_limits(self.data)["site|big.dk"], 50)

    def test_a_sites_limit_is_the_budget_of_its_solution(self):
        """A site's limit holds the site itself and every environment of it, wherever it runs; the limits given to its
        parts are shares of it, never more together than it (1.0.97)"""
        usage.set_limit(self.data, "site", "shop.dk", limit_mb=2048)
        self.assertEqual(usage.limit_conflict(self.data, "environment", "shop.dk", "test", 2048), "")
        self.assertIn("At most 2.0 GB", usage.limit_conflict(self.data, "environment", "shop.dk", "test", 4096))
        usage.set_limit(self.data, "environment", "shop.dk", "production", limit_mb=1024)
        usage.set_limit(self.data, "environment", "shop.dk", "main", limit_mb=512)
        found = usage.limit_conflict(self.data, "environment", "shop.dk", "test", 1024)
        self.assertIn("Only 512 MB is left of shop.dk's 2.0 GB: 1.5 GB is given to its other parts", found)
        self.assertEqual(usage.limit_conflict(self.data, "environment", "shop.dk", "test", 512), "")
        # The same part changed: its own share is not counted twice
        self.assertEqual(usage.limit_conflict(self.data, "environment", "shop.dk", "production", 1536), "")
        self.assertIn("At least 1.5 GB", usage.limit_conflict(self.data, "site", "shop.dk", limit_mb=1024))
        self.assertEqual(usage.limit_conflict(self.data, "site", "shop.dk", limit_mb=1536), "")

    def test_a_limit_past_at_once_is_not_set(self):
        """A limit below what is used now, measured last, would be past at once: refused, for the site and its parts (1.0.97)"""
        (self.data / "usage-sizes.json").write_text(json.dumps({"measured": 1, "sizes": {"sites": {"shop.dk": 967.0}, "databases": {}, "environments": [
            {"site": "shop.dk", "target": "production", "in_site": True, "size_mb": 235.0},
            {"site": "shop.dk", "target": "staging", "in_site": True, "size_mb": 233.0},
            {"site": "shop.dk", "target": "staging-2", "in_site": False, "size_mb": 233.0},
            {"site": "shop.dk", "target": "test", "in_site": True, "size_mb": 232.0}]}}))
        # The solution uses 1200 MB: below it, refused
        self.assertIn("At least 1.18 GB", usage.limit_conflict(self.data, "site", "shop.dk", limit_mb=1000))
        self.assertEqual(usage.limit_conflict(self.data, "site", "shop.dk", limit_mb=1200), "")
        # staging-2 given 5 GB: the site needs that and what the rest (967 MB) uses
        usage.set_limit(self.data, "environment", "shop.dk", "staging-2", limit_mb=5120)
        self.assertIn("At least 5.95 GB", usage.limit_conflict(self.data, "site", "shop.dk", limit_mb=5120))
        usage.set_limit(self.data, "environment", "shop.dk", "staging-2", limit_mb=None)
        # A part below what it uses, or leaving the rest no room
        usage.set_limit(self.data, "site", "shop.dk", limit_mb=2048)
        self.assertIn("At least 233 MB", usage.limit_conflict(self.data, "environment", "shop.dk", "staging-2", 200))
        self.assertIn("At most 1.05 GB", usage.limit_conflict(self.data, "environment", "shop.dk", "staging-2", 1500))
        self.assertEqual(usage.limit_conflict(self.data, "environment", "shop.dk", "staging-2", 1000), "")

    def test_the_other_server_is_told_of_a_shared_limit_that_changed(self):
        """What goes past a shared limit or under it again: the other server is told to check too (1.0.97)"""
        usage.set_limit(self.data, "environment", "big.dk", "staging-2", limit_mb=50)
        common = {"measure_site": lambda site: self.sites.get(site), "measure_databases": lambda data, connection: {},
                  "measure_environments": lambda: [{"site": "big.dk", "target": "staging-2", "in_site": False, "size_mb": 60.0}],
                  "measure_environment": lambda site, target: 60.0}
        usage.run(self.data, now=1, **common)
        self.assertIn("limit|environment|big.dk|staging-2", self.told.call_args.args[0])
        usage.run(self.data, now=2, **common)
        self.assertEqual(self.told.call_args.args[0], set(), "nothing changed")

    def test_a_part_not_measured_keeps_what_was_over(self):
        """A server not reached now is not known, not fine: what was over stays over, and no mail says it is under again (1.0.97)"""
        usage.set_limit(self.data, "site", "big.dk", limit_mb=260)
        usage.set_limit(self.data, "environment", "big.dk", "staging-2", limit_mb=50)
        measured = [{"site": "big.dk", "target": "staging-2", "in_site": False, "size_mb": 60.0}]
        common = {"measure_site": lambda site: 250.0 if site == "big.dk" else self.sites.get(site), "measure_databases": lambda data, connection: {}}
        usage.run(self.data, mail=True, now=1, measure_environments=lambda: measured, measure_environment=lambda site, target: 60.0, **common)
        opened = json.loads((self.data / "usage-state.json").read_text())["open"]
        self.assertIn("limit|site|big.dk", opened)
        self.assertIn("limit|environment|big.dk|staging-2", opened)
        sent = []
        with patch.object(usage, "load_settings", return_value={"enabled": True, "emails": ["ops@example.com"]}), \
                patch.object(usage.report, "send", lambda *args, **kwargs: sent.append(args)), patch.object(usage.report, "load_mail", return_value={}):
            usage.run(self.data, now=2, measure_environments=lambda: None, measure_environment=lambda site, target: None, **common)
        opened = json.loads((self.data / "usage-state.json").read_text())["open"]
        self.assertIn("limit|site|big.dk", opened)
        self.assertIn("limit|environment|big.dk|staging-2", opened)
        self.assertFalse(any("under again" in str(args) for args in sent))

    def test_a_sites_size_is_its_solution(self):
        """The site itself (its folder without the environments in it) and every environment, also on another server"""
        environments = [{"site": "big.dk", "target": "production", "in_site": True, "size_mb": 100.0},
                        {"site": "big.dk", "target": "staging-2", "in_site": False, "size_mb": 60.0},
                        {"site": "big.dk", "target": "iis", "in_site": False, "windows": True, "size_mb": None}]
        self.assertEqual(usage.solution_of("big.dk", 250.0, environments), {"solution_mb": 310.0, "main_mb": 150.0})
        shown = usage.overview(self.data, ["big.dk"], {}, measure_site=lambda site: 250.0, measure_databases=lambda *a, **k: {},
                               measure_environments=lambda: environments)
        self.assertEqual((shown["sites"][0]["size_mb"], shown["sites"][0]["folder_mb"], shown["sites"][0]["main_mb"]), (310.0, 250.0, 150.0))
        # Anna's site may be 100 MB: its solution is checked, and the site itself as a part of its own
        usage.set_limit(self.data, "environment", "big.dk", "main", limit_mb=140)
        over = usage.check(self.data, measure_site=lambda site: 250.0 if site == "big.dk" else self.sites.get(site),
                           measure_databases=lambda data, connection: {}, measure_environments=lambda: environments)
        self.assertEqual({(item["key"], item["size_mb"]) for item in over if "big.dk" in item["key"]},
                         {("site|big.dk", 310.0), ("limit|environment|big.dk|main", 150.0)})

    def test_an_environment_deleted_goes_from_usage_at_once_with_its_limit(self):
        """Usage shows the sizes kept, but not of an environment that is gone: it goes at once, its share with it, and the
        rest is measured again behind it (1.0.97)"""
        environments = [{"site": "big.dk", "target": "staging-2", "in_site": False, "size_mb": 305.0},
                        {"site": "big.dk", "target": "test", "in_site": True, "size_mb": 20.0}]
        ids = {"big.dk": {"staging-2", "test"}}
        measure = {"measure_site": lambda site: self.sites.get(site), "measure_databases": lambda data, connection, user_only=False: {},
                   "measure_environments": lambda: environments, "ids": lambda site, web_root: ids.get(site, set())}
        usage.kept_overview(self.data, ["big.dk"], {}, now=1000, **measure)
        usage.set_limit(self.data, "environment", "big.dk", "staging-2", limit_mb=366)
        ids["big.dk"] = {"test"}
        shown = usage.kept_overview(self.data, ["big.dk"], {}, now=1010, **measure)
        self.assertEqual([item["target"] for item in shown["environments"]], ["test"])
        self.assertTrue(shown["stale"])
        self.assertNotIn("environment|big.dk|staging-2", usage.load_limits(self.data))

    def test_the_limits_a_site_is_held_to_for_add_environment(self):
        """Add environment tells whether a new environment counts in the site's limits: the server's own and its user's,
        with the site's size now (1.0.95)"""
        usage.set_limit(self.data, "site", "big.dk", limit_mb=2048)
        shown = usage.site_limits(self.data, "big.dk", measure_site=lambda site: self.sites.get(site))
        self.assertEqual(shown, {"size_mb": 250.0, "server_limit_mb": 2048, "given_mb": 0, "limit_mb": 100, "user": "Anna"})
        self.assertEqual(usage.site_limits(self.data, "admin.example.com", measure_site=lambda site: 5.0),
                         {"size_mb": 5.0, "server_limit_mb": None, "given_mb": 0, "limit_mb": None, "user": None})

    def test_a_site_can_have_its_own_size_limit(self):
        """Each site a user made takes the size their limits give every site, unless it has its own, bigger or smaller
        (1.0.71)"""
        config = json.loads((self.data / "config.json").read_text())
        config["users"][0]["sites"] = {"big.dk": {"envs": ["main"], "actions": [], "size_mb": 500}, "small.dk": {"envs": ["main"], "actions": [], "size_mb": 10}}
        (self.data / "config.json").write_text(json.dumps(config))
        over = usage.check(self.data, **self.measure())
        self.assertEqual([(item["name"], item["limit_mb"]) for item in over if item["kind"] == "site"], [("small.dk", 10)])
        self.assertEqual(usage.site_limit(config["users"][0], "big.dk"), 500)
        # Every site gets the default only when they made it: one they were only given has none unless it is set (1.0.94)
        self.assertIsNone(usage.site_limit(config["users"][0], "other.dk"))
        self.assertEqual(usage.site_limit({**config["users"][0], "made": {"sites": ["other.dk"]}}, "other.dk"), 100)
        cleaned = access.clean({"username": "anna", "role": "user", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": [], "size_mb": 2048}}},
                               sites={"shop.dk": ["main"]})
        self.assertEqual(cleaned["sites"]["shop.dk"]["size_mb"], 2048)
        self.assertNotIn("size_mb", access.clean({"username": "anna", "role": "user", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": []}}},
                                                 sites={"shop.dk": ["main"]})["sites"]["shop.dk"])

    def test_a_database_can_have_its_own_size(self):
        """A database a user made takes the size its connection gives every database of theirs, unless it has its own
        (1.0.71)"""
        config = json.loads((self.data / "config.json").read_text())
        config["users"][0]["connections"]["c1"]["sizes"] = {"Shop": 4096, "Tiny": 1}
        (self.data / "config.json").write_text(json.dumps(config))
        over = usage.check(self.data, **self.measure())
        self.assertEqual([(item["name"], item["limit_mb"]) for item in over if item["kind"] == "database"], [("Tiny", 1)])
        self.assertEqual(usage.database_limit(config["users"][0], "c1", "Other"), 1024)
        cleaned = access.clean({"username": "anna", "role": "user", "sql": True, "connections": {
            "c1": {"databases": [], "own": True, "actions": ["create"], "sizes": {"Shop": 2048, "Free": ""}}}}, connections=["c1"])
        self.assertEqual(cleaned["connections"]["c1"]["sizes"], {"Shop": 2048})

    def test_settings_and_the_hourly_job(self):
        self.assertEqual(usage.validate_settings({"enabled": True, "emails": ["ops@example.com"]}), {"enabled": True, "emails": ["ops@example.com"]})
        for bad in ({"enabled": True, "emails": []}, {"enabled": "yes"}, {"emails": ["not-an-address"]}):
            with self.assertRaises(ValueError, msg=bad):
                usage.validate_settings(bad)
        cron = self.data / "vns-usage"
        usage.install(str(self.data), cron=cron)
        line = cron.read_text().splitlines()[-1]
        self.assertTrue(line.startswith("17 * * * * root /usr/bin/python3 "))
        self.assertIn(f'--data "{self.data}"', line)
        with self.assertRaises(ValueError):
            usage.install("/var/www/a b/app", cron=cron)


if __name__ == "__main__":
    unittest.main()


class TellOthersTests(unittest.TestCase):
    def test_told_with_the_shared_keys_and_never_back(self):
        """Only shared limits are told, and a check the other server asked for tells nothing back"""
        with patch.object(usage.subprocess, "Popen") as popen, patch.dict("os.environ", {"HARBOR_USAGE_TOLD": ""}):
            usage.tell_others({"limit|environment|a.dk|staging-2", "site|b.dk", "limit|database|c1|Shop"})
        self.assertEqual(popen.call_args.args[0][-3:], ["server", "usage-tell", "limit|environment|a.dk|staging-2"])
        with patch.object(usage.subprocess, "Popen") as popen, patch.dict("os.environ", {"HARBOR_USAGE_TOLD": "1"}):
            usage.tell_others({"limit|environment|a.dk|staging-2"})
        popen.assert_not_called()
