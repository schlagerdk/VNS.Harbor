"""harbor master: a master Harbor over many servers (phase 13). A member's admin is asked with a token only root has,
and the master asks it over SSH, keeping nothing of it"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Stop  # noqa: E402
from harbor.server import master  # noqa: E402


class Answer:
    def __init__(self, status, body, kind="application/json", disposition=""):
        self.status, self.body = status, body
        self.headers = {"Content-Type": kind, **({"Content-Disposition": disposition} if disposition else {})}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


class MasterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def system(self, **answers):
        return RecordingSystem(root=self.root, answers=answers)

    def servers(self, found):
        path = self.root / "var/lib/harbor/servers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found))

    def test_a_member_asks_its_own_admin_with_a_token_only_root_reads(self):
        system = self.system()
        asked = []

        def opener(request, timeout=None):
            asked.append(request)
            return Answer(200, {"sites": []})
        with mock.patch.object(master, "admin_port", return_value="8123"):
            answer = master.call(system, "get", "/api/sites", opener=opener)
        self.assertEqual((answer["status"], answer["body"]), (200, {"sites": []}))
        self.assertEqual(asked[0].full_url, "http://127.0.0.1:8123/api/sites")
        token = (self.root / "etc/harbor/secrets/master-token")
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(asked[0].get_header("X-harbor-master"), token.read_text().strip())
        # The same token the next time
        with mock.patch.object(master, "admin_port", return_value="8123"):
            master.call(system, "GET", "/api/sites", opener=opener)
        self.assertEqual(asked[1].get_header("X-harbor-master"), asked[0].get_header("X-harbor-master"))
        # Only the admin's API, nothing that leaves it
        for path in ("/login", "/api/../etc/passwd x", "http://elsewhere/api/sites"):
            with self.assertRaises(Stop):
                master.call(system, "GET", path, opener=opener)
        with self.assertRaises(Stop):
            master.call(system, "TRACE", "/api/sites", opener=opener)

    def test_the_member_is_told_where_the_one_on_the_masters_page_is(self):
        """A member's Your address now said 127.0.0.1 (the master's call, from the member itself): the master says the
        address its page is used from, and only a plain IP address goes"""
        asked = []

        def opener(request, timeout=None):
            asked.append(request)
            return Answer(200, {"address": "x"})
        with mock.patch.object(master, "admin_port", return_value="8123"):
            master.call(self.system(), "GET", "/api/your-address", opener=opener, client="198.51.100.7")
            master.call(self.system(), "GET", "/api/your-address", opener=opener, client="198.51.100.7\r\nX-Evil: 1")
        self.assertEqual(asked[0].get_header("X-harbor-client"), "198.51.100.7")
        self.assertIsNone(asked[1].get_header("X-harbor-client"))
        system = self.system(ssh=json.dumps({"status": 200, "body": {}}))
        master.ask(system, "two", {"host": "10.0.0.2", "user": "root", "port": "22"}, "GET", "/api/your-address", client="2001:db8::7")
        self.assertIn("master call GET /api/your-address --client 2001:db8::7", " ".join(system.commands))
        master.ask(system, "two", {"host": "10.0.0.2", "user": "root", "port": "22"}, "GET", "/api/sites", client="; rm -rf /")
        self.assertNotIn("rm -rf", " ".join(system.commands))

    def test_an_upload_and_a_download_go_through_as_they_are(self):
        import base64
        system = self.system()
        asked = []

        def opener(request, timeout=None):
            asked.append(request)
            return Answer(200, b"\x00zip bytes", "application/zip", 'attachment; filename="shop.zip"')
        upload = b"--x\r\nContent-Disposition: form-data; name=\"files\"\r\n\r\n\x00\xff\r\n--x--"
        with mock.patch.object(master, "admin_port", return_value="8123"):
            answer = master.call(system, "POST", "/api/site-fs/shop/upload", raw={"type": "multipart/form-data; boundary=x", "data": base64.b64encode(upload).decode()}, opener=opener)
        self.assertEqual(asked[0].data, upload)
        self.assertEqual(asked[0].get_header("Content-type"), "multipart/form-data; boundary=x")
        self.assertEqual((base64.b64decode(answer["data"]), answer["type"], answer["disposition"]), (b"\x00zip bytes", "application/zip", 'attachment; filename="shop.zip"'))
        self.assertNotIn("body", answer)
        # A content type that would break the header is refused
        with self.assertRaises(Stop):
            master.call(system, "POST", "/api/sites", raw={"type": "text/plain\r\nX-Evil: 1", "data": ""}, opener=opener)

    def test_the_master_lists_each_members_sites_and_says_which_did_not_answer(self):
        self.servers({"two": {"host": "10.0.0.2", "user": "root", "port": "22", "member": True, "names": ["two.example.com"]},
                      "three": {"host": "10.0.0.3", "user": "root", "port": "22", "member": True},
                      "four": {"host": "10.0.0.4", "user": "root", "port": "22"}})

        def ssh(argv):
            if "10.0.0.2" in " ".join(argv):
                return json.dumps({"status": 200, "body": {"sites": [{"name": "shop", "environments": []}]}})
            raise AssertionError("asked a server that did not answer")
        system = self.system(ssh=lambda argv: ssh(argv) if "root@10.0.0.2" in argv else "")
        # three does not answer: its ssh gives nothing back
        found = {item["server"]: item for item in master.sites(system)}
        self.assertEqual(set(found), {"two", "three"})
        self.assertEqual((found["two"]["reachable"], [site["name"] for site in found["two"]["sites"]], found["two"]["admin"]),
                         (True, ["shop"], "https://admin.two.example.com"))
        self.assertFalse(found["three"]["reachable"])
        command = next(line for line in system.commands if "root@10.0.0.2" in line)
        self.assertIn("python3 /opt/harbor/current/bin/harbor master call GET /api/sites", command)

    def test_each_members_own_sql_server_without_system_databases_or_passwords(self):
        self.servers({"two": {"host": "10.0.0.2", "user": "root", "port": "22", "member": True},
                      "three": {"host": "10.0.0.3", "user": "root", "port": "22", "member": True}})
        connections = [{"id": "c1", "name": "Local", "engine": "mssql", "host": "localhost", "username": "sa", "password": "secret"},
                       {"id": "c2", "name": "three", "engine": "mssql", "host": "10.0.0.3", "username": "sa"}]
        databases = [{"name": "master", "id": 1}, {"name": "shop", "id": 7, "size_mb": 12.5, "state": "ONLINE", "owner": "shop"}]

        def ssh(argv):
            line = " ".join(argv)
            if "/api/sql/connections" in line:
                return json.dumps({"status": 200, "body": {"connections": connections}})
            return json.dumps({"status": 200, "body": {"databases": databases}})
        with mock.patch.object(master, "addresses", side_effect=lambda entry: {entry["host"]}):
            found = {item["server"]: item for item in master.connections(self.system(ssh=ssh))}
        # Only two's own SQL Server: its connection to three's is three's
        self.assertEqual([item["name"] for item in found["two"]["connections"]], ["Local"])
        self.assertEqual(found["two"]["connections"][0]["databases"], [{"name": "shop", "size_mb": 12.5, "state": "ONLINE", "owner": "shop"}])
        self.assertNotIn("secret", json.dumps(found))

    def test_a_member_gets_the_users_with_access_there_and_only_that(self):
        users = [{"id": "u_aaaaaa", "username": "anna@example.com", "role": "user", "password_hash": "secret", "version": 2,
                  "web": True, "sites": {"master-site": {}}, "servers": {"two": {"web": True, "sites": {"shop": {"envs": ["main"]}}}}},
                 {"id": "u_bbbbbb", "username": "bo@example.com", "role": "user", "password_hash": "secret", "servers": {"three": {"sql": True}}},
                 {"id": "u_cccccc", "username": "admin@example.com", "role": "admin", "password_hash": "hash"}]
        sent = {user["id"]: user for user in master.users_for(users, "two")}
        # Anna with what she may on two (not the master's own sites), Bo not at all, the administrator with the password
        self.assertEqual(set(sent), {"u_aaaaaa", "u_cccccc"})
        self.assertEqual((sent["u_aaaaaa"]["sites"], sent["u_aaaaaa"]["version"]), ({"shop": {"envs": ["main"]}}, 2))
        self.assertNotIn("password_hash", sent["u_aaaaaa"])
        self.assertNotIn("servers", sent["u_aaaaaa"])
        self.assertEqual(sent["u_cccccc"]["password_hash"], "hash")
        # Main sites on one member, every environment on another: two knows where their environments go
        mixed = [{"id": "u_dddddd", "username": "dina@example.com", "role": "user",
                  "servers": {"two": {"web": True, "create_sites": True, "sites": {"shop": {"envs": ["main"], "actions": ["create_env"]}}},
                              "three": {"web": True, "create_envs": True}}}]
        to_two = master.users_for(mixed, "two")[0]
        self.assertEqual((to_two["create_on"], to_two["envs_on"], to_two.get("create_envs")), ([], ["three"], None))
        to_three = master.users_for(mixed, "three")[0]
        self.assertEqual((to_three["create_envs"], to_three["create_on"]), (True, ["two"]))
        # No site of theirs lets them add environments (on any server): no server makes environments for them
        del mixed[0]["servers"]["two"]["sites"]
        self.assertEqual(master.users_for(mixed, "two")[0]["envs_on"], [])
        self.assertFalse(master.users_for(mixed, "three")[0]["create_envs"])

    def test_a_member_makes_no_members_and_a_master_joins_no_one(self):
        self.servers({"two": {"host": "10.0.0.2", "user": "root", "port": "22"}})
        # A member of a master: it makes no members of its own
        membership = self.root / "var/lib/harbor/master.json"
        membership.write_text(json.dumps({"name": "Harbor Master", "address": "https://harbor.example.com"}))
        system = self.system(ssh=json.dumps({"status": 200, "body": {"id": "root"}}))
        self.assertNotEqual(main(["servers", "join", "two"], system), 0)
        self.assertIn("only the master makes members", " ".join(system.said))
        membership.unlink()
        # A server that is a master itself is no one's member
        system = self.system(ssh=json.dumps({"status": 200, "body": {"id": "root", "master": True}}))
        self.assertNotEqual(main(["servers", "join", "two"], system), 0)
        self.assertIn("is a master itself", " ".join(system.said))
        self.assertNotIn("member", json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"])

    def test_a_member_that_hosts_no_sites(self):
        """A SQL Server's member hosts no sites: it is told so itself, the other members mark it so (no environment
        there), and no user has a Web Server there or makes sites or environments there"""
        self.servers({"sql": {"host": "10.0.0.5", "user": "root", "port": "22", "member": True},
                      "web": {"host": "10.0.0.6", "user": "root", "port": "22", "member": True}})
        system = self.system()
        with mock.patch.object(master, "call", return_value={"status": 200, "body": {}}) as told:
            self.assertEqual(main(["servers", "web", "sql", "off"], system), 0, system.said)
        commands = " ".join(system.commands)
        self.assertIn("root@10.0.0.5 python3 /opt/harbor/current/bin/harbor server web off", commands)
        self.assertIn("root@10.0.0.6 python3 /opt/harbor/current/bin/harbor servers web sql off --here", commands)
        told.assert_called_with(system, "POST", "/api/master/push-users", body={})
        self.assertTrue(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["sql"]["no_web"])
        users = [{"id": "u_aaaaaa", "username": "anna", "role": "user",
                  "servers": {"sql": {"web": True, "create_sites": True, "sites": {"x.dk": {}}, "sql": True}, "web": {"web": True, "create_envs": True}}}]
        to_sql = master.users_for(users, "sql", {"sql"})[0]
        self.assertEqual((to_sql["web"], to_sql["create_sites"], to_sql["sites"], to_sql["sql"]), (False, False, {}, True))
        self.assertEqual(master.users_for(users, "web", {"sql"})[0]["create_on"], [])
        # Again on: the flag goes
        with mock.patch.object(master, "call", return_value={"status": 200, "body": {}}):
            main(["servers", "web", "sql", "on"], self.system())
        self.assertNotIn("no_web", json.loads((self.root / "var/lib/harbor/servers.json").read_text())["sql"])

    def test_a_server_marked_so_makes_no_sites(self):
        from harbor.server import servers
        system = self.system()
        self.assertTrue(servers.hosts_sites(system))
        self.assertEqual(main(["server", "web", "off"], system), 0)
        self.assertFalse(servers.hosts_sites(system))
        main(["server", "web", "on"], system)
        self.assertTrue(servers.hosts_sites(system))

    def test_a_member_removed_leaves_first(self):
        """Removing a member told it nothing: the master's users stayed on it. It leaves first, while it can be asked"""
        self.servers({"two": {"host": "10.0.0.2", "user": "root", "port": "22", "member": True}})
        key = self.root / "etc/harbor/ssh/id_ed25519.pub"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("ssh-ed25519 AAAA harbor\n")
        system = self.system(ssh=json.dumps({"status": 200, "body": {}}))
        with mock.patch.object(master, "call", return_value={"status": 200, "body": {"changed": 1}}) as told:
            main(["servers", "remove", "two"], system)
        self.assertIn("master call DELETE /api/membership", " ".join(system.commands))
        told.assert_called_with(system, "POST", "/api/master/forget-member", body={"member": "two"})
        self.assertNotIn("two", json.loads((self.root / "var/lib/harbor/servers.json").read_text()))

    def test_a_server_joins_only_when_its_harbor_answers_the_master(self):
        self.servers({"two": {"host": "10.0.0.2", "user": "root", "port": "22"}})
        system = self.system(ssh=json.dumps({"status": 200, "body": {"id": "root"}}))
        self.assertEqual(main(["servers", "join", "two"], system), 0, system.said)
        self.assertTrue(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["member"])
        self.assertEqual(main(["servers", "leave", "two"], system), 0)
        self.assertNotIn("member", json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"])
        # One whose Harbor is older does not know the master's call: it is told to update
        with mock.patch.object(master, "ask", side_effect=Stop("two runs a Harbor without the master's calls: update it (Servers, Update).")):
            self.assertNotEqual(main(["servers", "join", "two"], self.system()), 0)
        self.assertNotIn("member", json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"])


if __name__ == "__main__":
    unittest.main()
