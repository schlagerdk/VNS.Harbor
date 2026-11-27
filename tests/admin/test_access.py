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

    def test_environments_on_a_member_only_for_one_who_may_add_them(self):
        # May create sites is another thing: alone, no server makes environments for them
        servers = {"two": {"web": True, "create_sites": True}, "three": {"web": True, "create_envs": True}}
        user = access.clean({"username": "dina@shop.dk", "role": "user", "servers": servers})
        self.assertNotIn("create_envs", user["servers"]["three"])
        # A site given that lets them add environments: their environments may go to three
        given = {"two": {"web": True, "sites": {"shop": {"envs": ["main"], "actions": ["create_env"]}}}, "three": {"create_envs": True}}
        self.assertTrue(access.clean({"username": "dina@shop.dk", "role": "user", "servers": given})["servers"]["three"]["create_envs"])
        # Neither: no server makes environments for them, and one with nothing else goes
        none = {"two": {"web": True, "sites": {"shop": {"envs": ["main"], "actions": ["service"]}}}, "three": {"create_envs": True}}
        user = access.clean({"username": "dina@shop.dk", "role": "user", "servers": none})
        self.assertNotIn("three", user["servers"])
        self.assertNotIn("create_envs", user["servers"]["two"])

    def test_terminal_gives_files(self):
        user = access.clean({"username": "anna", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["terminal", "logs"]}}},
                            sites={"shop.dk": ["main"]})
        self.assertEqual(user["sites"]["shop.dk"]["actions"], ["appsettings", "delete", "files", "logs", "packages", "packages_view", "terminal"])
        self.assertTrue(access.can(user, "shop.dk", "main", "terminal"))

    def test_files_gives_the_nuget_packages(self):
        """Files can edit the .csproj, and with it the packages: Change and See NuGet packages, also on a grant saved before"""
        self.assertEqual(access.site_actions(["files"]), ["appsettings", "delete", "files", "packages", "packages_view"])
        saved = {"id": "u1", "role": "user", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["files"]}}}
        self.assertTrue(access.can(saved, "shop.dk", "main", "packages"))
        self.assertTrue(access.can(saved, "shop.dk", "main", "packages_view"))

    def test_bindings_certificate_and_environment_are_three_actions(self):
        """One action gave all three before 1.0.82: those who had it keep them, once, and after that each is its own"""
        config = {"users": [{"id": "u1", "sites": {"shop.dk": {"envs": ["main"], "actions": ["bindings", "logs"]},
                                                   "blog.dk": {"envs": ["main"], "actions": ["logs"]}}}],
                  "user_templates": [{"name": "New", "access": {"site_rules": {"actions": ["bindings"]}}},
                                     {"name": "Old", "access": {"sites": {"shop.dk": {"actions": ["bindings"]}}}}]}
        self.assertTrue(access.split_bindings(config))
        self.assertEqual(config["users"][0]["sites"]["shop.dk"]["actions"], ["bindings", "certificate", "environment", "logs"])
        self.assertEqual(config["users"][0]["sites"]["blog.dk"]["actions"], ["logs"])
        self.assertEqual(config["user_templates"][0]["access"]["site_rules"]["actions"], ["bindings", "certificate", "environment"])
        self.assertEqual(config["user_templates"][1]["access"]["sites"]["shop.dk"]["actions"], ["bindings", "certificate", "environment"])
        # Once: a user given only Change bindings afterwards keeps only that
        config["users"][0]["sites"]["blog.dk"]["actions"] = ["bindings"]
        self.assertFalse(access.split_bindings(config))
        self.assertEqual(config["users"][0]["sites"]["blog.dk"]["actions"], ["bindings"])
        user = access.clean({"username": "anna", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["certificate"]}}},
                            sites={"shop.dk": ["main"]})
        self.assertTrue(access.can(user, "shop.dk", "main", "certificate"))
        self.assertFalse(access.can(user, "shop.dk", "main", "bindings"))
        self.assertFalse(access.can(user, "shop.dk", "main", "environment"))

    def test_nuget_packages_are_seen_or_changed(self):
        """Everyone who saw a main site saw its packages before 1.0.82: they keep seeing them, once; changing is seeing"""
        config = {"users": [{"id": "u1", "sites": {"shop.dk": {"envs": ["main", "prod"], "actions": ["logs"]},
                                                   "blog.dk": {"envs": ["prod"], "actions": ["logs"]}}}],
                  "user_templates": [{"name": "New", "access": {"site_rules": {"actions": ["logs"]}}}]}
        self.assertTrue(access.split_packages(config))
        self.assertEqual(config["users"][0]["sites"]["shop.dk"]["actions"], ["logs", "packages_view"])
        self.assertEqual(config["users"][0]["sites"]["blog.dk"]["actions"], ["logs"], "no main site: no packages")
        self.assertEqual(config["user_templates"][0]["access"]["site_rules"]["actions"], ["logs", "packages_view"])
        config["users"][0]["sites"]["shop.dk"]["actions"] = ["logs"]
        self.assertFalse(access.split_packages(config))
        self.assertEqual(config["users"][0]["sites"]["shop.dk"]["actions"], ["logs"])
        self.assertEqual(access.site_actions(["packages"]), ["packages", "packages_view"])
        user = access.clean({"username": "anna", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["packages_view"]}}},
                            sites={"shop.dk": ["main"]})
        self.assertTrue(access.can(user, "shop.dk", "main", "packages_view"))
        self.assertFalse(access.can(user, "shop.dk", "main", "packages"))

    def test_which_sql_servers_a_user_may_add_connections_to(self):
        """May add SQL connections, to Microsoft SQL Server, PostgreSQL or both; one given it before 1.0.82 has both"""
        user = access.clean({"username": "anna", "sql": True, "manage_connections": True, "connection_engines": ["postgres", "oracle"]})
        self.assertEqual(user["connection_engines"], ["postgres"])
        self.assertEqual(access.connection_engines(user), ["postgres"])
        self.assertEqual(access.connection_engines({**user, "manage_connections": False}), [])
        self.assertEqual(access.connection_engines({**user, "sql": False}), [])
        before = {"id": "u1", "role": "user", "sql": True, "manage_connections": True}
        self.assertEqual(access.connection_engines(before), ["mssql", "postgres"])
        self.assertEqual(access.connection_engines({"role": "admin"}), ["mssql", "postgres"])
        self.assertEqual(access.template_rules({"connection_engines": ["mssql"]})["connection_engines"], ["mssql"])

    def test_a_part_with_nothing_in_it_is_no_access(self):
        """SQL Server on, with no connection and nothing they may add, is not theirs; nor the Web Server with no site"""
        user = {"id": "u1", "role": "user", "sql": True, "manage_connections": False, "connections": {}, "web": True, "sites": {}}
        self.assertFalse(access.has_sql(user))
        self.assertFalse(access.has_web(user))
        self.assertTrue(access.has_sql({**user, "connections": {"c1": {"databases": ["*"]}}}))
        self.assertTrue(access.has_sql({**user, "manage_connections": True}))
        self.assertFalse(access.has_sql({**user, "manage_connections": True, "connection_engines": []}))
        self.assertTrue(access.has_web({**user, "create_sites": True}))
        self.assertTrue(access.has_web({**user, "sites": {"shop.dk": {"envs": ["main"]}}}))
        self.assertFalse(access.has_sql({**user, "sql": False, "connections": {"c1": {}}}))
        self.assertTrue(access.has_sql({"role": "admin"}))

    def test_a_part_with_nothing_in_it_is_turned_off_as_it_is_saved(self):
        user = access.clean({"username": "anna", "sql": True, "manage_connections": False, "web": True}, sites=SITES, connections=["c1"])
        self.assertFalse(user["sql"])
        self.assertFalse(user["web"])
        user = access.clean({"username": "anna", "sql": True, "manage_connections": True, "connection_engines": ["postgres"],
                             "web": True, "create_sites": True}, sites=SITES, connections=["c1"])
        self.assertTrue(user["sql"])
        self.assertTrue(user["web"])
        # To no SQL Server at all: nothing in it either
        user = access.clean({"username": "anna", "sql": True, "manage_connections": True, "connection_engines": []}, sites=SITES, connections=["c1"])
        self.assertFalse(user["sql"])
        user = access.clean({"username": "anna", "sql": True, "connections": {"c1": {"databases": ["*"], "actions": ["query"]}},
                             "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": ["logs"]}}}, sites=SITES, connections=["c1"])
        self.assertTrue(user["sql"])
        self.assertTrue(user["web"])

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
        # They may make sites: a site they make is theirs (on with nothing in it, the Web Server would be turned off)
        user = self.user(sql=True, create_sites=True, connections={"c1": {"databases": ["Shop"], "actions": ["create"]}})
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

    def test_a_member_with_nothing_on_it_is_no_access_there(self):
        """Web Server switched on for a member, with no site there nor sites or environments to make, kept the member
        as theirs (its dot lit): it is no access there, as SQL Server with no connection"""
        servers = access.clean_servers({"two": {"web": True, "sql": True}, "three": {"web": True, "sites": {"shop.dk": {"envs": ["main"]}}, "sql": True},
                                        "four": {"sql": True, "manage_connections": True}})
        self.assertEqual(set(servers), {"three", "four"})
        self.assertEqual((servers["three"]["web"], servers["three"]["sql"]), (True, False))
        self.assertTrue(servers["four"]["sql"])

    def test_a_template_on_a_master_gives_access_on_its_members(self):
        """A template on a master says what a user made from it has on each member: its parts and limits there, not
        its sites, and nothing on a member with neither Web Server, SQL Server nor environments"""
        kept = access.clean_template({"name": "Developer", "access": {"servers": {
            "linux-01": {"web": True, "create_sites": True, "limits": {"sites": 2}, "sites": {"shop.dk": {"envs": ["main"]}}},
            "linux-02": {"create_envs": True}, "linux-03": {"sftp": True}, "bad name!": {"web": True}}}})["access"]
        self.assertEqual(set(kept["servers"]), {"linux-01", "linux-02"})
        self.assertEqual((kept["servers"]["linux-01"]["web"], kept["servers"]["linux-01"]["limits"]), (True, {"sites": 2}))
        self.assertNotIn("sites", kept["servers"]["linux-01"])
        self.assertTrue(kept["servers"]["linux-02"]["create_envs"])
        self.assertNotIn("servers", access.clean_template({"name": "x", "access": {}})["access"])

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
        self.assertEqual(set(kept), set(access.TEMPLATE_KEYS) - {"servers"}, "no sites, connections or anything of the person (servers only on a master)")
        self.assertEqual(kept["site_rules"], {"actions": ["appsettings", "delete", "files", "logs", "packages", "packages_view", "terminal"], "new_envs": False},
                         "a terminal gives Files, and Files the NuGet packages")
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
