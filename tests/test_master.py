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
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


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
        self.assertEqual(answer, {"status": 200, "body": {"sites": []}})
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
