import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import access  # noqa: E402

CONFIG = {"user": "root-name", "password_hash": "x", "users": []}
SITES = {"shop.dk": ["main", "prod", "staging"], "blog.dk": ["main"]}


class AccessTests(unittest.TestCase):
    def test_a_name_beside_the_username(self):
        """Shown in the sync history instead of the username, as it is now"""
        user = access.clean({"username": "anna@shop.dk", "name": "  Anna   Hansen ", "role": "user"})
        self.assertEqual(user["name"], "Anna Hansen")
        self.assertEqual(access.clean({"role": "user"}, existing=user)["name"], "Anna Hansen", "kept when not sent")
        with self.assertRaises(ValueError):
            access.clean({"username": "anna@shop.dk", "name": "x" * 81, "role": "user"})
        config = {"user": "root@shop.dk", "name": "", "users": [user]}
        self.assertEqual(access.label(config, user["id"]), "Anna Hansen")
        self.assertEqual(access.label(config, access.ROOT), "root@shop.dk", "the username without a name")
        config["name"] = "Martin"
        self.assertEqual(access.label(config, access.ROOT), "Martin")
        self.assertEqual(access.label(config, "u_gone"), "")

    def test_terminal_gives_files(self):
        user = access.clean({"username": "anna", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["terminal", "logs"]}}},
                            sites={"shop.dk": ["main"]})
        self.assertEqual(user["sites"]["shop.dk"]["actions"], ["files", "logs", "terminal"])
        self.assertTrue(access.can(user, "shop.dk", "main", "terminal"))

    def user(self, **data):
        base = {"username": "anna", "role": "user", "web": True}
        return access.clean({**base, **data}, sites=SITES, connections=["c1"])

    def test_root_is_found_but_never_listed(self):
        config = dict(CONFIG, users=[self.user()])
        self.assertTrue(access.is_admin(access.find(config, access.ROOT)))
        admin = dict(self.user(username="bo", role="admin"))
        listed = access.visible_users(config, admin)
        self.assertEqual([user["username"] for user in listed], ["anna"])
        self.assertNotIn("password_hash", listed[0])
        self.assertTrue(access.taken(config, "root-name"))

    def test_a_main_site_without_one_environment(self):
        user = self.user(sites={"shop.dk": {"envs": ["main", "prod"], "actions": ["publish", "logs"]}})
        self.assertTrue(access.can(user, "shop.dk", "main", "publish"))
        self.assertTrue(access.can(user, "shop.dk", "prod", "logs"))
        self.assertFalse(access.sees_env(user, "shop.dk", "staging"))
        self.assertFalse(access.can(user, "shop.dk", "staging", "publish"))
        self.assertFalse(access.can(user, "shop.dk", "main", "delete"))
        self.assertFalse(access.can(user, "blog.dk", "main", "logs"))

    def test_only_an_environment_hides_the_main_site(self):
        user = self.user(sites={"shop.dk": {"envs": ["staging"], "actions": ["publish"]}})
        self.assertFalse(access.sees_env(user, "shop.dk", "main"))
        self.assertTrue(access.can(user, "shop.dk", "staging", "publish"))

    def test_new_environments_follow_only_when_chosen(self):
        whole = self.user(sites={"shop.dk": {"envs": SITES["shop.dk"], "new_envs": True, "actions": ["logs"]}})
        part = self.user(sites={"shop.dk": {"envs": ["main"], "new_envs": False, "actions": ["logs"]}})
        self.assertTrue(access.sees_env(whole, "shop.dk", "later"))
        self.assertFalse(access.sees_env(part, "shop.dk", "later"))
        # An environment that was there and left out stays out, also with new environments on
        partial = self.user(sites={"shop.dk": {"envs": ["main", "prod"], "new_envs": True, "actions": ["logs"]}})
        self.assertFalse(access.sees_env(partial, "shop.dk", "staging"))

    def test_without_the_web_server_no_site_counts(self):
        user = self.user(web=False, sites={"shop.dk": {"envs": ["main"], "actions": ["logs"]}})
        self.assertFalse(access.can(user, "shop.dk", "main", "logs"))

    def test_unknown_sites_environments_and_actions_are_dropped(self):
        user = self.user(sites={"gone.dk": {"envs": ["main"], "actions": ["logs"]},
                                "shop.dk": {"envs": ["main", "nope"], "actions": ["logs", "root_shell"]}})
        self.assertEqual(list(user["sites"]), ["shop.dk"])
        self.assertEqual(user["sites"]["shop.dk"]["envs"], ["main"])
        self.assertEqual(user["sites"]["shop.dk"]["actions"], ["logs"])

    def test_sql_by_connection_and_database(self):
        user = self.user(sql=True, connections={"c1": {"databases": ["Shop"], "actions": ["query", "backup"]}, "c9": {"databases": ["*"]}})
        self.assertEqual(list(user["connections"]), ["c1"])
        self.assertTrue(access.can_sql(user, "c1", "Shop", "query"))
        self.assertTrue(access.can_sql(user, "c1", "Shop", "view"))
        self.assertFalse(access.can_sql(user, "c1", "Other", "view"))
        self.assertFalse(access.can_sql(user, "c1", "Shop", "restore"))
        self.assertFalse(access.can_sql(dict(user, sql=False), "c1", "Shop", "query"))

    def test_what_a_user_makes_becomes_theirs(self):
        user = self.user(sql=True, connections={"c1": {"databases": ["Shop"], "actions": ["create"]}})
        self.assertTrue(access.grant_database(user, "c1", "NewDb"))
        self.assertTrue(access.can_sql(user, "c1", "NewDb", "create"))
        self.assertTrue(access.grant_site(user, "new.dk"))
        self.assertTrue(access.can(user, "new.dk", "main", "delete"))
        self.assertTrue(access.grant_environment(user, "shop.dk", "qa"))
        self.assertTrue(access.sees_env(user, "shop.dk", "qa"))

    def test_only_their_own_databases_before_they_have_made_one(self):
        """A connection given with no database was dropped, so a user could not be given only what they make (1.0.70)"""
        user = self.user(sql=True, connections={"c1": {"databases": [], "own": True, "actions": ["create", "query"]}})
        self.assertEqual(user["connections"]["c1"], {"databases": [], "actions": ["create", "query"], "own": True})
        self.assertTrue(access.can_sql(user, "c1", None, "create"))
        self.assertFalse(access.can_sql(user, "c1", "Shop", "query"))
        self.assertTrue(access.grant_database(user, "c1", "Mine"))
        self.assertTrue(access.can_sql(user, "c1", "Mine", "query"))
        self.assertFalse(access.can_sql(user, "c1", "Shop", "view"))
        # Kept as their own when saved again, and dropped as before without it
        again = access.clean({"role": "user", "connections": user["connections"]}, existing=user, connections=["c1"])
        self.assertEqual(again["connections"]["c1"]["databases"], ["Mine"])
        self.assertTrue(again["connections"]["c1"]["own"])
        self.assertEqual(self.user(sql=True, connections={"c1": {"databases": [], "actions": ["create"]}})["connections"], {})
        self.assertNotIn("own", self.user(sql=True, connections={"c1": {"databases": ["*"], "own": True}})["connections"]["c1"])

    def test_a_template_keeps_rules_and_nothing_of_the_person(self):
        """Rules to start new users from: what they may do on each site and SQL connection they are given, never which
        ones, nor their name, password, lock or what they made (1.0.77)"""
        template = access.clean_template({"name": "  Web   developer ", "access": {
            "web": True, "create_sites": True, "sftp": True, "limits": {"sites": 3, "site_mb": 2048},
            "site_rules": {"actions": ["logs", "terminal", "nonsense"], "new_envs": False},
            "sql_rules": {"own": True, "actions": ["create", "query"], "limits": {"databases": 2}},
            "sites": {"shop.dk": {"envs": ["main"], "actions": ["logs"]}}, "connections": {"c1": {"databases": ["*"]}},
            "username": "anna", "password_hash": "x", "disabled": True, "made": {"sites": ["shop.dk"]}}})
        self.assertEqual(template["name"], "Web developer")
        self.assertTrue(template["id"].startswith("t_"))
        kept = template["access"]
        self.assertEqual(set(kept), set(access.TEMPLATE_KEYS), "no sites, connections or anything of the person")
        self.assertEqual(kept["site_rules"], {"actions": ["files", "logs", "terminal"], "new_envs": False}, "a terminal gives Files")
        self.assertEqual(kept["sql_rules"], {"own": True, "actions": ["create", "query"], "limits": {"databases": 2}})
        self.assertEqual(kept["limits"], {"sites": 3, "site_mb": 2048})
        self.assertTrue(kept["create_sites"])
        again = access.clean_template({"name": "Web developer", "access": kept}, existing=template)
        self.assertEqual(again, template, "saved again, the same template")
        no_making = access.clean_template({"name": "x", "access": {"sql_rules": {"actions": ["query"], "limits": {"databases": 2}}}})
        self.assertNotIn("limits", no_making["access"]["sql_rules"], "limits only with Create or Copy databases")
        for bad in ({"name": ""}, {"name": "x" * 61}, "x"):
            with self.assertRaises(ValueError):
                access.clean_template(bad)

    def test_a_template_saved_before_keeps_what_its_sites_gave_as_rules(self):
        """Templates from 1.0.76 kept a user's sites and connections: what they gave becomes the rules"""
        old = {"id": "t_1", "name": "Old", "access": {"role": "user", "web": True, "sql": True,
               "sites": {"shop.dk": {"envs": ["main"], "new_envs": True, "actions": ["logs"]},
                         "blog.dk": {"envs": ["main"], "new_envs": True, "actions": ["publish"]}},
               "connections": {"c1": {"databases": [], "own": True, "actions": ["create"], "limits": {"databases": 1}}}}}
        [template] = access.templates({"user_templates": [old]})
        self.assertEqual(template["id"], "t_1")
        self.assertEqual(template["access"]["site_rules"], {"actions": ["logs", "publish"], "new_envs": True})
        self.assertEqual(template["access"]["sql_rules"], {"own": True, "actions": ["create"], "limits": {"databases": 1}})
        self.assertNotIn("sites", template["access"])
        [empty] = access.templates({"user_templates": [{"id": "t_2", "name": "Empty", "access": {}}]})
        self.assertEqual(empty["access"]["site_rules"]["actions"], access.DEFAULT_SITE_ACTIONS, "nothing given: the defaults")

    def test_an_admin_has_no_grants_and_can_do_everything(self):
        admin = self.user(role="admin", sites={"shop.dk": {"envs": ["main"], "actions": ["logs"]}})
        self.assertEqual(admin["sites"], {})
        self.assertTrue(access.can(admin, "blog.dk", "main", "delete"))
        self.assertTrue(access.can_sql(admin, "c1", "Any", "delete"))


if __name__ == "__main__":
    unittest.main()
