"""The admin's Server tools: each action becomes a harbor command, the sa password only in its environment (phase 4)"""
import ast
from pathlib import Path
import re
import unittest

from flask import abort
from werkzeug.exceptions import BadRequest

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin"


class ServerToolCommandTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((APP / "app.py").read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("server_tool_command", "servers_command")]
        self.ns = {"abort": abort, "re": re, "HARBOR_COMMAND": "/vns/bin/harbor",
                   "SQL_EDITIONS": ("Developer", "Express", "Evaluation", "Standard", "Enterprise")}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "server tools", "exec"), self.ns)

    def command(self, **data):
        return self.ns["server_tool_command"](data)

    def test_install_sql_server(self):
        title, argv, env = self.command(tool="sql_install", edition="Express", sa_password="Secret-123", with_tools=True)
        self.assertEqual(argv[2:], ["sql", "install-server", "--edition", "Express", "--with-tools"])
        self.assertEqual(env["HARBOR_SA_PASSWORD"], "Secret-123")
        self.assertNotIn("Secret-123", " ".join(argv))
        for bad in ({"edition": "Free", "sa_password": "Secret-123"}, {"edition": "Express", "sa_password": "short"}, {"edition": "Express"}):
            with self.assertRaises(BadRequest):
                self.command(tool="sql_install", **bad)

    def test_remove_sql_server_deletes_data_only_when_typed(self):
        self.assertEqual(self.command(tool="sql_uninstall")[1][2:], ["sql", "uninstall-server", "--yes", "--keep-data", "--without-tools"])
        with self.assertRaises(BadRequest):
            self.command(tool="sql_uninstall", delete_data=True, confirmation="delete")
        self.assertIn("--delete-data", self.command(tool="sql_uninstall", delete_data=True, confirmation="DELETE", with_tools=True)[1])

    def test_the_other_tools(self):
        self.assertEqual(self.command(tool="cron_on")[1][2:], ["server", "cron", "on"])
        self.assertEqual(self.command(tool="dotnet_update", channel="10.0")[1][2:], ["server", "dotnet", "update", "10.0"])
        self.assertEqual(self.command(tool="node_install", major="22")[1][2:], ["server", "node", "install", "22"])
        self.assertEqual(self.command(tool="sa_password", sa_password="Secret-123")[1][2:], ["sql", "set-sa-password"])
        for data in ({"tool": "dotnet_install", "channel": "7.0"}, {"tool": "dotnet_install", "channel": "10.0 x"},
                     {"tool": "node_install", "major": "16"}, {"tool": "node_install", "major": "22;id"}, {"tool": "rm"}):
            with self.assertRaises(BadRequest):
                self.command(**data)

    def test_servers(self):
        title, argv, env = self.ns["servers_command"]({"action": "add", "name": "linux-02", "host": "172.23.3.16", "password": "pw 1"})
        self.assertEqual(argv[2:], ["servers", "add", "linux-02", "172.23.3.16", "--user", "root", "--port", "22"])
        self.assertEqual(env, {"HARBOR_SSH_PASSWORD": "pw 1"})
        self.assertEqual(self.ns["servers_command"]({"action": "install", "name": "linux-02"})[1][2:], ["servers", "install", "linux-02"])
        self.assertEqual(self.ns["servers_command"]({"action": "remove", "name": "linux-02", "uninstall": True, "revoke_key": True})[1][2:],
                         ["servers", "remove", "linux-02", "--uninstall", "--revoke-key"])
        self.assertEqual(self.ns["servers_command"]({"action": "remove", "name": "linux-02", "uninstall": "yes"})[1][2:], ["servers", "remove", "linux-02"])
        for data in ({"action": "add", "name": "Linux 02", "host": "x"}, {"action": "add", "name": "a", "host": "x;id"},
                     {"action": "add", "name": "a", "host": "x", "port": "22; id"}, {"action": "reboot", "name": "a"}):
            with self.assertRaises(BadRequest):
                self.ns["servers_command"](data)

    def test_only_administrators_start_them(self):
        source = (APP / "app.py").read_text()
        self.assertRegex(source, r'ADMIN_TASKS = \{"servers", "server_tool"')
        self.assertIn('require_admin()\n    if not os.path.isfile(HARBOR_COMMAND)', source)


if __name__ == "__main__":
    unittest.main()
