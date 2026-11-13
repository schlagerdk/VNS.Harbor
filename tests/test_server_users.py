"""harbor users: a Linux user for each user of the admin, and nobody else's user touched (phase 8a)"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.server import users  # noqa: E402

PASSWD = "root:x:0:0:root:/root:/bin/bash\nwww-data:x:33:33::/var/www:/usr/sbin/nologin\nmhs:x:1000:1000::/home/mhs:/bin/bash\n"
GROUP = "root:x:0:\nwww-data:x:33:\nmhs:x:1000:\n"


class Server:
    """passwd and group as useradd, usermod, userdel and groupadd change them"""

    def __init__(self, root):
        self.root = root
        (root / "etc").mkdir(exist_ok=True)
        (root / "etc/passwd").write_text(PASSWD)
        (root / "etc/group").write_text(GROUP)
        self.uid = 2000

    def lines(self, name):
        return (self.root / "etc" / name).read_text().splitlines()

    def write(self, name, lines):
        (self.root / "etc" / name).write_text("".join(line + "\n" for line in lines))

    def groupadd(self, argv):
        self.write("group", self.lines("group") + [f"{argv[-1]}:x:999:"])
        return ""

    def useradd(self, argv):
        name, groups, shell = argv[-1], argv[argv.index("--groups") + 1], argv[argv.index("--shell") + 1]
        self.uid += 1
        self.write("passwd", self.lines("passwd") + [f"{name}:x:{self.uid}:{self.uid}::/home/{name}:{shell}"])
        group = [line + ("," if not line.endswith(":") else "") + name if line.split(":")[0] == groups else line for line in self.lines("group")]
        self.write("group", group + [f"{name}:x:{self.uid}:"])
        return ""

    def userdel(self, argv):
        name = argv[-1]
        self.write("passwd", [line for line in self.lines("passwd") if line.split(":")[0] != name])
        group = []
        for line in self.lines("group"):
            parts = line.split(":")
            if parts[0] == name:
                continue
            parts[3] = ",".join(member for member in parts[3].split(",") if member and member != name)
            group.append(":".join(parts))
        self.write("group", group)
        return ""

    def gpasswd(self, argv):
        group, names = argv[-1], argv[-2]
        self.write("group", [":".join(line.split(":")[:3] + [names]) if line.split(":")[0] == group else line for line in self.lines("group")])
        return ""

    def groupdel(self, argv):
        self.write("group", [line for line in self.lines("group") if line.split(":")[0] != argv[-1]])
        return ""

    def usermod(self, argv):
        name, shell = argv[-1], argv[argv.index("--shell") + 1]
        self.write("passwd", [":".join(line.split(":")[:6] + [shell]) if line.split(":")[0] == name else line for line in self.lines("passwd")])
        return ""


class UserTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.server = Server(self.root)

    def sync(self, admin_users):
        system = RecordingSystem(root=self.root, answers={name: getattr(self.server, name) for name in ("groupadd", "useradd", "userdel", "usermod", "gpasswd", "groupdel")})
        system.programs.update({"useradd", "setfacl"})
        with mock.patch("sys.stdin", io.StringIO(json.dumps(admin_users))):
            self.assertEqual(main(["users", "sync"], system), 0, system.said)
        return system

    def accounts(self):
        return users.passwd(RecordingSystem(root=self.root))

    def test_a_linux_user_for_each_admin_user(self):
        system = self.sync([{"id": "u_1", "username": "mhs@vns.dk", "role": "admin"}, {"id": "u_2", "username": "Anna.B", "role": "user"},
                            {"id": "root", "username": "martin"}])
        # mhs is taken by a user Harbor does not manage, so the admin user mhs@vns.dk gets mhs-2
        self.assertEqual(users.mapping(system)["u_1"]["linux"], "mhs-2")
        self.assertEqual(users.mapping(system)["u_2"]["linux"], "anna-b")
        self.assertNotIn("root", users.mapping(system))
        self.assertEqual(users.members(system, "harbor-users"), {"mhs-2", "anna-b"})
        self.assertIn("useradd --create-home --user-group --groups harbor-users --shell /bin/bash --comment Harbor Anna.B anna-b", system.commands)
        self.assertFalse(any("sudo" in command for command in system.commands))
        # Again: nothing to do
        again = self.sync([{"id": "u_1", "username": "mhs@vns.dk"}, {"id": "u_2", "username": "Anna.B"}])
        self.assertFalse(any(command.startswith(("useradd", "usermod", "userdel")) for command in again.commands), again.commands)

    def test_disabled_renamed_and_removed(self):
        self.sync([{"id": "u_1", "username": "anna"}, {"id": "u_2", "username": "bo"}])
        system = self.sync([{"id": "u_1", "username": "anna", "disabled": True}, {"id": "u_2", "username": "bo.new"}])
        self.assertIn("usermod --expiredate 1 --shell /usr/sbin/nologin anna", system.commands)
        self.assertEqual(self.accounts()["anna"][2], "/usr/sbin/nologin")
        # A new username keeps the Linux user
        self.assertEqual(users.mapping(system)["u_2"]["linux"], "bo")
        system = self.sync([{"id": "u_1", "username": "anna"}])
        self.assertIn("usermod --expiredate  --shell /bin/bash --comment Harbor anna anna", system.commands)
        self.assertIn("userdel --remove bo", system.commands)
        self.assertNotIn("bo", self.accounts())
        self.assertEqual(set(users.mapping(system)), {"u_1"})

    def test_a_user_harbor_did_not_make_is_never_removed(self):
        self.sync([{"id": "u_1", "username": "anna"}])
        # Someone made anna leave harbor-users by hand: Harbor no longer manages that user
        self.server.write("group", [line.replace(":anna", ":") if line.startswith("harbor-users") else line for line in self.server.lines("group")])
        system = self.sync([])
        self.assertNotIn("userdel --remove anna", system.commands)
        self.assertIn("anna", self.accounts())
        self.sync([{"id": "u_9", "username": "mhs"}])
        self.assertIn("mhs", self.accounts())
        self.assertEqual(self.accounts()["mhs"][1], "/home/mhs")

    def test_names(self):
        self.assertEqual(users.base_name("Martin.H.Schläger@vns.dk"), "martin-h-schl-ger")
        self.assertEqual(users.base_name("123"), "u123")
        self.assertEqual(users.base_name("_x_"), "x")
        self.assertEqual(len(users.base_name("a" * 64)), 28)
        self.assertEqual(users.free_name("root@x", set()), "root-2")



class SiteGroupTests(UserTests):
    def setUp(self):
        super().setUp()
        for folder in ("var/www/shop.dk/scripts", "var/www/shop.dk/.vns-publish", "var/www/blog.dk"):
            (self.root / folder).mkdir(parents=True)

    def users(self, anna_actions=("files",), bo_sites=None):
        return [{"id": "u_1", "username": "anna", "role": "user", "sites": {"shop.dk": {"envs": ["main"], "actions": list(anna_actions)},
                                                                          "gone.dk": {"envs": ["main"], "actions": ["files"]}}},
                {"id": "u_2", "username": "bo", "role": "user", "sites": bo_sites or {}},
                {"id": "u_3", "username": "boss", "role": "admin", "sites": {"blog.dk": {"actions": ["files"]}}}]

    def test_a_group_per_site_for_the_files_grant(self):
        system = self.sync(self.users(bo_sites={"shop.dk": {"actions": ["files", "logs"]}, "blog.dk": {"actions": ["logs"]}}))
        self.assertEqual(users.site_groups(system), {"shop.dk": "site-shop-dk"})
        self.assertEqual(users.members(system, "site-shop-dk"), {"anna", "bo"})
        self.assertIn("setfacl -R -P -m g:site-shop-dk:rwX,m::rwX /var/www/shop.dk", system.commands)
        self.assertIn("find -P /var/www/shop.dk -type d -exec setfacl -d -m g:site-shop-dk:rwX,m::rwX {} +", system.commands)
        # Never on the folders that hold passwords
        self.assertIn("setfacl -R -P -x g:site-shop-dk /var/www/shop.dk/scripts", system.commands)
        self.assertIn("setfacl -R -P -x g:site-shop-dk /var/www/shop.dk/.vns-publish", system.commands)
        # Bo loses the grant: only the members change, the ACLs stay
        system = self.sync(self.users())
        self.assertEqual(users.members(system, "site-shop-dk"), {"anna"})
        self.assertFalse(any(command.startswith("setfacl -R -P -m") for command in system.commands))
        # Nobody has it: the group and its ACLs go
        system = self.sync(self.users(anna_actions=("logs",)))
        self.assertEqual(users.site_groups(system), {})
        self.assertIn("setfacl -R -P -x g:site-shop-dk /var/www/shop.dk", system.commands)
        self.assertIn("groupdel site-shop-dk", system.commands)

    def test_a_disabled_user_leaves_the_group(self):
        self.sync(self.users())
        listed = self.users()
        listed[0]["disabled"] = True
        system = self.sync(listed)
        self.assertIn("groupdel site-shop-dk", system.commands)

    def test_site_folders_are_closed_to_others_while_there_are_users(self):
        shop = self.root / "var/www/shop.dk"
        shop.chmod(0o1775)
        (self.root / "var/www/blog.dk").chmod(0o750)
        admin = self.root / "var/www/admin.shop.dk/app"
        admin.mkdir(parents=True)
        (admin / "manage.py").write_text("")
        (admin / "config.json").write_text("{}")
        (self.root / "var/www/admin.shop.dk").chmod(0o755)
        system = self.sync(self.users())
        commands = system.commands
        self.assertLess(commands.index("setfacl -m u:www-data:rX /var/www/shop.dk"), commands.index("chmod o-rwx /var/www/shop.dk"))
        self.assertNotIn("chmod o-rwx /var/www/blog.dk", commands, "not open to others")
        self.assertNotIn("chmod o-rwx /var/www/admin.shop.dk", commands, "the admin's own")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/closed-sites.json").read_text()), {"shop.dk": "1775"})
        # The last user goes: the folder gets its mode back
        system = self.sync([])
        self.assertIn("chmod 1775 /var/www/shop.dk", system.commands)
        self.assertEqual(json.loads((self.root / "var/lib/harbor/closed-sites.json").read_text()), {})

    def test_long_site_names(self):
        name = users.site_group("a-very-long-subdomain.of.some-customer.example.com")
        self.assertLessEqual(len(name), 32)
        self.assertTrue(name.startswith("site-a-very-long"))
        self.assertNotEqual(name, users.site_group("a-very-long-subdomain.of.another-customer.example.com"))
        self.assertEqual(users.site_group("shop.dk"), "site-shop-dk")


if __name__ == "__main__":
    unittest.main()
