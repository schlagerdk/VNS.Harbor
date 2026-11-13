"""harbor servers: other servers, reached with Harbor's own key; the password once, never kept (phase 5)"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402

STATUS = {"python": "3.12.3", "os": "Ubuntu 24.04 LTS", "os_id": "ubuntu", "harbor": None, "vns": "1.1.16", "admin": ""}


class ServerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def keygen(self, argv):
        if "-R" in argv:
            # A host key forgotten (known_hosts)
            return ""
        key = self.root / argv[argv.index("-f") + 1].lstrip("/")
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("private")
        key.with_suffix(".pub").write_text("ssh-ed25519 AAAAharbor harbor@one\n")
        return ""

    def system(self, **answers):
        return RecordingSystem(root=self.root, answers={"ssh-keygen": self.keygen, "hostname": "one", **answers})

    def add(self, system):
        with mock.patch.dict(os.environ, {"HARBOR_SSH_PASSWORD": "pw 1"}):
            return main(["servers", "add", "two", "10.0.0.2"], system)

    def test_add_puts_the_key_there_and_keeps_no_password(self):
        system = self.system(ssh=lambda argv: json.dumps(STATUS) if argv[-1] == "python3 -" else "")
        self.assertEqual(self.add(system), 0, system.said)
        put = next(index for index, command in enumerate(system.commands) if command.startswith("sshpass -e ssh"))
        self.assertEqual(system.environments[put], {"SSHPASS": "pw 1"})
        self.assertIn("ssh-ed25519 AAAAharbor harbor@one", system.commands[put])
        self.assertNotIn("pw 1", " ".join(system.commands))
        saved = (self.root / "var/lib/harbor/servers.json").read_text()
        self.assertNotIn("pw 1", saved)
        self.assertEqual(json.loads(saved)["two"]["host"], "10.0.0.2")
        login = system.commands[-1]
        self.assertIn("-i /etc/harbor/ssh/id_ed25519", login)
        self.assertIn("BatchMode=yes", login)
        # The handshake
        system = self.system(ssh=lambda argv: json.dumps(STATUS))
        with mock.patch("builtins.print") as printed:
            self.assertEqual(main(["servers", "status", "two"], system), 0)
        found = json.loads(printed.call_args[0][0])[0]
        self.assertEqual((found["reachable"], found["compatible"], found["vns"]), (True, True, "1.1.16"))

    def test_a_password_that_is_refused(self):
        system = self.system(sshpass=Result(5, "", "Permission denied"))
        self.assertEqual(self.add(system), 1)
        self.assertIn("Could not log in to root@10.0.0.2", system.said[-1])
        self.assertFalse((self.root / "var/lib/harbor/servers.json").exists())

    def test_a_server_that_cannot_run_harbor(self):
        self.add(self.system(ssh=lambda argv: ""))
        old = dict(STATUS, python="3.8.10", os="Ubuntu 20.04 LTS")
        from harbor.server import servers
        found = servers.status(self.system(ssh=lambda argv: json.dumps(old)), "two")
        self.assertFalse(found["compatible"])
        self.assertIn("Python 3.10 or newer is needed (it has 3.8.10)", found["problems"])
        system = self.system(ssh=Result(255, "", "ssh: connect to host 10.0.0.2 port 22: No route to host"))
        self.assertFalse(servers.status(system, "two")["reachable"])

    def test_install_copies_the_release_and_uses_it(self):
        self.add(self.system(ssh=lambda argv: ""))
        release = self.root / "opt/harbor/releases/1.2.0-abc"
        (release / "bin").mkdir(parents=True)
        (release / "bin/harbor").write_text("")
        (self.root / "opt/harbor/current").symlink_to("releases/1.2.0-abc")

        def remote(argv):
            command = argv[-1]
            if command == "python3 -":
                return json.dumps(STATUS)
            if command.startswith("test -d"):
                return Result(1)
            return ""
        system = self.system(ssh=remote)
        self.assertEqual(main(["servers", "install", "two"], system), 0, system.said)
        rsync = next(command for command in system.commands if command.startswith("rsync"))
        self.assertIn(f"{release}/ root@10.0.0.2:/opt/harbor/releases/.1.2.0-abc.new/", rsync)
        self.assertTrue(system.commands[-1].endswith("python3 /opt/harbor/releases/1.2.0-abc/bin/harbor self use 1.2.0-abc"))
        # There already: only made current
        system = self.system(ssh=lambda argv: json.dumps(STATUS) if argv[-1] == "python3 -" else "")
        main(["servers", "install", "two"], system)
        self.assertFalse(any(command.startswith("rsync") for command in system.commands))

    def test_remove_forgets_and_can_uninstall_and_take_the_key(self):
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two"], system), 0, system.said)
        self.assertFalse(any(command.startswith("ssh") for command in system.commands), "only forgotten here")
        self.assertIn("Harbor stays installed there", system.said[-1])
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two", "--uninstall", "--revoke-key"], system), 0, system.said)
        uninstall = next(index for index, command in enumerate(system.commands) if command.endswith("sh -s"))
        self.assertIn("rm -rf /opt/harbor", system.inputs[uninstall])
        self.assertNotIn("/vns", system.inputs[uninstall])
        revoke = next(command for command in system.commands if "grep -vxF" in command)
        self.assertIn("ssh-ed25519 AAAAharbor harbor@one", revoke)
        self.assertLess(uninstall, system.commands.index(revoke), "the key goes last")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/servers.json").read_text()), {})
        # Not answering: nothing is forgotten, as asked for more than that
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=Result(255))
        self.assertEqual(main(["servers", "remove", "two", "--uninstall"], system), 1)
        self.assertIn("two", json.loads((self.root / "var/lib/harbor/servers.json").read_text()))

    def test_a_servers_host_key_is_forgotten_with_it_and_for_a_new_address(self):
        """A server set up again has a new host key: Harbor refused to log in to linux-02 after it was reinstalled"""
        known = self.root / "etc/harbor/ssh/known_hosts"
        known.parent.mkdir(parents=True)
        known.write_text("10.0.0.2 ssh-ed25519 AAAAold\n")
        forget = "ssh-keygen -f /etc/harbor/ssh/known_hosts -R 10.0.0.2"
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(self.add(system), 0, system.said)
        self.assertIn(forget, system.commands, "a new address: its old key goes before the first login")
        system = self.system(ssh=lambda argv: "")
        with mock.patch.dict(os.environ, {"HARBOR_SSH_PASSWORD": "pw 1"}):
            self.assertEqual(main(["servers", "add", "three", "10.0.0.2"], system), 0, system.said)
        self.assertNotIn(forget, system.commands, "a server here is at it: its key is kept")
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "three"], system), 0, system.said)
        self.assertNotIn(forget, system.commands, "two is still at it")
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two"], system), 0, system.said)
        self.assertIn(forget, system.commands)

    def test_self_use(self):
        release = self.root / "opt/harbor/releases/1.2.0-abc"
        (release / "bin").mkdir(parents=True)
        (release / "bin/harbor").write_text("")
        system = RecordingSystem(root=self.root)
        self.assertEqual(main(["self", "use", "1.2.0-abc"], system), 0, system.said)
        self.assertEqual(os.readlink(self.root / "opt/harbor/current"), "releases/1.2.0-abc")
        self.assertEqual(main(["self", "use", "../etc"], RecordingSystem(root=self.root)), 1)


if __name__ == "__main__":
    unittest.main()
