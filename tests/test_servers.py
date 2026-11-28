"""harbor servers: other servers, reached with Harbor's own key; the password once, never kept (phase 5)"""
import json
import os
import stat
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

    def test_update_all_goes_on_past_one_that_fails(self):
        """Update all: each server one after another; one that fails is said, the rest are still updated"""
        from harbor.core.system import Stop
        from harbor.server import servers
        done = []

        def install(system, name):
            if name == "two":
                raise Stop("did not answer")
            done.append(name)
        system = RecordingSystem(root=self.root)
        with mock.patch.object(servers, "install", side_effect=install):
            self.assertNotEqual(main(["servers", "install", "one", "two", "three"], system), 0)
        self.assertEqual(done, ["one", "three"])
        self.assertIn("Not updated: two", " ".join(system.said))

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
        self.assertTrue(system.commands[-2].endswith("python3 /opt/harbor/releases/1.2.0-abc/bin/harbor self use 1.2.0-abc"))
        # Then brought up to it there, as an update there does (kikayo kept the old installation without it)
        self.assertTrue(system.commands[-1].endswith("python3 /opt/harbor/current/bin/harbor self after-update"), system.commands[-1])
        # There already: only made current
        system = self.system(ssh=lambda argv: json.dumps(STATUS) if argv[-1] == "python3 -" else "")
        main(["servers", "install", "two"], system)
        self.assertFalse(any(command.startswith("rsync") for command in system.commands))

    def test_rollback_runs_there_only_with_an_earlier_release(self):
        self.add(self.system(ssh=lambda argv: ""))
        earlier = {**STATUS, "rollback": {"name": "1.0.0-abc", "version": "1.0.0"}}
        system = self.system(ssh=lambda argv: json.dumps(earlier) if argv[-1] == "python3 -" else "")
        self.assertEqual(main(["servers", "rollback", "two"], system), 0, system.said)
        self.assertTrue(system.commands[-1].endswith("python3 /opt/harbor/current/bin/harbor self rollback"), system.commands[-1])
        system = self.system(ssh=lambda argv: json.dumps({**STATUS, "rollback": None}) if argv[-1] == "python3 -" else "")
        self.assertEqual(main(["servers", "rollback", "two"], system), 1)
        self.assertIn("no earlier release", system.said[-1])
        self.assertFalse(any("self rollback" in command for command in system.commands))

    def test_the_public_address_is_kept_with_the_server(self):
        """A server behind a router knows only its internal address: DNS for a site there is checked against this one"""
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=lambda argv: json.dumps({**STATUS, "public": "203.0.113.9"}) if argv[-1] == "python3 -" else "")
        main(["servers", "status", "two"], system)
        self.assertEqual(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["public"], "203.0.113.9")

    def test_a_new_address_follows_into_every_environment_on_the_server(self):
        """The server got another IP: Servers and each environment on the old address change, nothing else"""
        self.add(self.system(ssh=lambda argv: ""))
        from harbor.environments import settings
        system = self.system(ssh=lambda argv: "")
        with mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"}):
            for site, target, ip in (("shop.dk", "prod2", "10.0.0.2"), ("shop.dk", "staging", "10.0.0.7"), ("blog.dk", "prod3", "10.0.0.2")):
                (self.root / f"var/www/{site}").mkdir(parents=True, exist_ok=True)
                settings.write_copy(system, f"/var/www/{site}", target, {"IP": ip, "USER": "root", "FOLDER": site, "BINDING": site, "SSH_PORT": "22"})
            self.assertEqual(main(["servers", "set-host", "two", "10.0.0.20"], system), 0, system.said)
            ips = {(site, target): settings.load(system, f"/var/www/{site}", target).IP
                   for site, target in (("shop.dk", "prod2"), ("shop.dk", "staging"), ("blog.dk", "prod3"))}
        self.assertEqual(ips, {("shop.dk", "prod2"): "10.0.0.20", ("shop.dk", "staging"): "10.0.0.7", ("blog.dk", "prod3"): "10.0.0.20"})
        self.assertEqual(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["host"], "10.0.0.20")
        # Not reachable at the new address: nothing is changed
        system = self.system(ssh=lambda argv: Result(255, "", "No route to host") if argv[-1] == "true" else "")
        self.assertEqual(main(["servers", "set-host", "two", "10.0.0.30"], system), 1)
        self.assertEqual(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["host"], "10.0.0.20")

    def test_a_server_is_renamed_with_its_password_and_the_copies_that_name_it(self):
        """Nothing changes on the server: its entry, a Windows password and Copy from Windows' links follow the name"""
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system()
        with mock.patch("harbor.sites.windows_import.share"), mock.patch.dict(os.environ, {"VNS_SMB_PASSWORD": "pw"}):
            main(["servers", "add-windows", "win", "//10.0.0.7/d$/sites", "--user", "Administrator"], system)
        links = self.root / "var/lib/harbor/sites/test.shop.dk/windows-move.json"
        links.parent.mkdir(parents=True)
        links.write_text(json.dumps({"prod2": {"windows": "production", "server": "two", "alias": ""}}))
        self.assertEqual(main(["servers", "rename", "two", "prod-01"], system), 0, system.said)
        self.assertEqual(main(["servers", "edit", "win", "--name", "prod-01-win"], system), 0, system.said)
        kept = json.loads((self.root / "var/lib/harbor/servers.json").read_text())
        self.assertEqual(sorted(kept), ["prod-01", "prod-01-win"])
        self.assertEqual(kept["prod-01"]["host"], "10.0.0.2")
        from harbor.server import servers
        self.assertEqual(servers.windows_login(system, "prod-01-win")[2], "pw", "the password went with it")
        self.assertEqual(json.loads(links.read_text())["prod2"]["server"], "prod-01")
        # Not onto another server's name, nor a name with capitals
        self.assertEqual(main(["servers", "rename", "prod-01", "prod-01-win"], system), 1)
        self.assertEqual(main(["servers", "rename", "prod-01", "Prod-01"], system), 1)

    def test_the_full_name_is_kept_for_a_temporary_alias(self):
        """{binding}.<its full name> is the alias suggested for a site moved there; a name without a domain is not"""
        self.add(self.system(ssh=lambda argv: ""))
        for fqdn, kept in (("web-02.example.com", "web-02.example.com"), ("web-02", "")):
            system = self.system(ssh=lambda argv: json.dumps({**STATUS, "fqdn": fqdn}) if argv[-1] == "python3 -" else "")
            main(["servers", "status", "two"], system)
            self.assertEqual(json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["fqdn"], kept)

    def test_a_windows_server_keeps_its_share_and_login(self):
        """For Import from Windows: nothing is run there, and its password is kept for root only"""
        from harbor.server import servers
        with mock.patch("harbor.sites.windows_import.share") as opened, mock.patch.dict(os.environ, {"VNS_SMB_PASSWORD": "pw"}):
            system = self.system()
            self.assertEqual(main(["servers", "add-windows", "windows-01", "//10.0.0.7/d$/sites/", "--user", "Administrator"], system), 0, system.said)
        opened.assert_called_once_with(system, "//10.0.0.7/d$/sites", "Administrator", "pw", "")
        kept = json.loads((self.root / "var/lib/harbor/servers.json").read_text())["windows-01"]
        self.assertEqual((kept["kind"], kept["share"], kept["user"]), ("windows", "//10.0.0.7/d$/sites", "Administrator"))
        self.assertNotIn("pw", json.dumps(kept))
        secret = self.root / "etc/harbor/secrets/servers/windows-01.json"
        self.assertEqual(json.loads(secret.read_text())["password"], "pw")
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        system = self.system()
        self.assertEqual(servers.windows_login(system, "windows-01"), ("//10.0.0.7/d$/sites", "Administrator", "pw", ""))
        # Not an SSH server: never installed on, nor listed where SSH is used
        self.assertEqual(servers.servers(system, "linux"), {})
        self.assertEqual(main(["servers", "install", "windows-01"], system), 1)
        with mock.patch("harbor.sites.windows_import.share"):
            self.assertTrue(servers.status(system, "windows-01")["reachable"])
        self.assertEqual(main(["servers", "remove", "windows-01"], system), 0, system.said)
        self.assertFalse(secret.exists())

    def test_remove_forgets_and_can_uninstall_and_take_the_key(self):
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two"], system), 0, system.said)
        # Its key goes always: this server cannot log in there unseen afterwards
        self.assertTrue(any("grep -vxF" in command for command in system.commands))
        self.assertEqual(system.said[-1], "two is removed here. Harbor stays installed there.")
        # Not answering: forgotten all the same, its key left there, and said
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=Result(255))
        self.assertEqual(main(["servers", "remove", "two"], system), 0, system.said)
        self.assertIn("Harbor's key stays", system.said[-1])
        self.assertIn("it does not answer", system.said[-1])
        self.add(self.system(ssh=lambda argv: ""))
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two", "--uninstall"], system), 0, system.said)
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

    def test_the_key_stays_while_an_environment_here_runs_there(self):
        self.add(self.system(ssh=lambda argv: ""))
        host = json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["host"]
        environments = self.root / "var/lib/harbor/sites/shop.dk/environments"
        environments.mkdir(parents=True)
        (environments / "production.json").write_text(json.dumps({"IP": host, "USER": "root"}))
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(main(["servers", "remove", "two"], system), 1)
        self.assertIn("shop.dk (production)", system.said[-1])
        self.assertFalse(any("grep -vxF" in command for command in system.commands))
        self.assertIn("two", json.loads((self.root / "var/lib/harbor/servers.json").read_text()))

    def test_a_windows_server_stays_while_an_environment_runs_on_it(self):
        with mock.patch("harbor.sites.windows_import.share"), mock.patch.dict(os.environ, {"VNS_SMB_PASSWORD": "pw"}):
            self.assertEqual(main(["servers", "add-windows", "windows-01", "//10.0.0.7/d$/sites/", "--user", "Administrator"], self.system()), 0)
        environments = self.root / "var/lib/harbor/sites/shop.dk/environments"
        environments.mkdir(parents=True)
        (environments / "production.json").write_text(json.dumps({"TARGET": "windows", "WINDOWS_SHARE": "\\\\10.0.0.7\\D$\\sites\\shop"}))
        system = self.system()
        self.assertEqual(main(["servers", "remove", "windows-01"], system), 1)
        self.assertIn("shop.dk (production)", system.said[-1])
        (environments / "production.json").unlink()
        self.assertEqual(main(["servers", "remove", "windows-01"], self.system()), 0)

    def test_the_servers_environments_run_on_are_put_in_servers(self):
        environments = self.root / "var/lib/harbor/sites/shop.dk/environments"
        environments.mkdir(parents=True)
        (environments / "production.json").write_text(json.dumps({"IP": "10.0.0.20", "USER": "root", "SSH_PORT": "22"}))
        (environments / "test.json").write_text(json.dumps({"IP": "", "LOCAL_PATH": "/var/www/test.shop.dk"}))
        (environments / "staging.json").write_text(json.dumps({"TARGET": "windows", "WINDOWS_SHARE": "//10.0.0.7/d$/sites/shop", "SMB_USER": "Administrator"}))
        secrets = self.root / "etc/harbor/secrets/sites/shop.dk"
        secrets.mkdir(parents=True)
        (secrets / "staging.json").write_text(json.dumps({"SMB_PASSWORD": "pw"}))
        system = self.system(ssh=lambda argv: "prod-01\n")
        self.assertEqual(main(["servers", "adopt"], system), 0, system.said)
        kept = json.loads((self.root / "var/lib/harbor/servers.json").read_text())
        self.assertEqual(kept["prod-01"]["host"], "10.0.0.20")
        self.assertEqual(kept["windows-10-0-0-7"]["share"], "//10.0.0.7/d$/sites")
        self.assertEqual(json.loads((self.root / "etc/harbor/secrets/servers/windows-10-0-0-7.json").read_text()), {"password": "pw"})
        self.assertEqual(len(kept), 2, "the local one is not a server")
        # Again: nothing more, and an unreachable one is named by its address
        system = self.system(ssh=Result(255))
        main(["servers", "adopt"], system)
        self.assertEqual(len(json.loads((self.root / "var/lib/harbor/servers.json").read_text())), 2)
        (environments / "dev.json").write_text(json.dumps({"IP": "10.0.0.30"}))
        main(["servers", "adopt"], self.system(ssh=Result(255)))
        self.assertIn("server-10-0-0-30", json.loads((self.root / "var/lib/harbor/servers.json").read_text()))

    def test_harbor_stays_where_it_is_used(self):
        self.add(self.system(ssh=lambda argv: ""))
        host = json.loads((self.root / "var/lib/harbor/servers.json").read_text())["two"]["host"]
        # Sites of its own there
        system = self.system(ssh=lambda argv: json.dumps({"own_sites": ["shop.dk"]}))
        self.assertEqual(main(["servers", "remove", "two", "--uninstall"], system), 1)
        self.assertIn("it has sites of its own: shop.dk", system.said[-1])
        self.assertFalse(any(command.endswith("sh -s") for command in system.commands), "nothing uninstalled")
        # Only environments managed from elsewhere there, but an SQL connection here reaches it
        connections = self.root / "var/www/admin.test/app/sql-connections.json"
        connections.parent.mkdir(parents=True)
        connections.write_text(json.dumps([{"id": "1", "name": "Shop SQL", "host": f"{host},1433"}]))
        system = self.system(ssh=lambda argv: json.dumps({"own_sites": []}))
        self.assertEqual(main(["servers", "remove", "two", "--uninstall"], system), 1)
        self.assertIn("SQL connections here use it: Shop SQL", system.said[-1])
        # Neither: it goes
        connections.write_text("[]")
        system = self.system(ssh=lambda argv: json.dumps({"own_sites": []}) if "python3" in " ".join(argv) else "")
        self.assertEqual(main(["servers", "remove", "two", "--uninstall"], system), 0, system.said)

    def test_a_servers_host_key_is_forgotten_with_it_and_for_a_new_address(self):
        """A server set up again has a new host key: Harbor refused to log in to linux-02 after it was reinstalled"""
        known = self.root / "etc/harbor/ssh/known_hosts"
        known.parent.mkdir(parents=True)
        known.write_text("10.0.0.2 ssh-ed25519 AAAAold\n")
        (self.root / "root/.ssh").mkdir(parents=True)
        (self.root / "root/.ssh/known_hosts").write_text("10.0.0.2 ecdsa-sha2-nistp256 AAAAold\n")
        forget = "ssh-keygen -f /etc/harbor/ssh/known_hosts -R 10.0.0.2"
        system = self.system(ssh=lambda argv: "")
        self.assertEqual(self.add(system), 0, system.said)
        self.assertIn(forget, system.commands, "a new address: its old key goes before the first login")
        self.assertIn("ssh-keygen -f /root/.ssh/known_hosts -R 10.0.0.2", system.commands,
                      "and root's, which the environments on it use: a copy to a server set up again failed on its old key")
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
