"""Settings, Export Site: the exports kept on other servers are listed with the site here they belong to (1.0.85)"""
import ast
import json
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/app.py"


class OtherServersTests(unittest.TestCase):
    def lifted(self, answers):
        tree = ast.parse(APP.read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "managed_export_items"
                 or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "REMOTE_HARBOR"]
        asked = []
        def run(argv, **kwargs):
            asked.append((argv[0], argv[-1]))
            return mock.Mock(stdout=json.dumps({"exports": answers[argv[0]]}) + "\n")
        from concurrent.futures import ThreadPoolExecutor
        from werkzeug.exceptions import HTTPException
        ns = {"json": json, "shlex": shlex, "subprocess": mock.Mock(run=run, SubprocessError=subprocess.SubprocessError), "HTTPException": HTTPException,
              "ThreadPoolExecutor": ThreadPoolExecutor, "manager_entry": lambda server: ({}, [server], "")}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), ns)
        return ns["managed_export_items"], asked

    def test_made_on_the_server_it_is_managed_from_or_on_an_environments_server(self):
        answers = {"web-01": [{"id": "a" * 16, "site": "test.example.com", "environments": ["example.com"]}],
                   "web-02": [{"id": "b" * 16, "site": "shop.example.com", "environments": ["shop.example.com"]}]}
        items, asked = self.lifted(answers)
        sites = [{"site": "example.com", "manager": "web-01", "environments": [{"target": "main", "unit": "example.com", "mode": "local", "where": ""}]},
                 {"site": "dev.shop.example.com", "manager": "", "environments": [
                     {"target": "main", "unit": "dev.shop.example.com", "mode": "local", "where": ""},
                     {"target": "prod", "unit": "shop.example.com", "mode": "remote", "where": "web-02"},
                     {"target": "test", "unit": "test.shop.example.com", "mode": "remote", "where": "10.0.0.9"}]}]
        with mock.patch("harbor.server.servers.servers", return_value={"web-01": {}, "web-02": {}}):
            found = sorted(items(sites), key=lambda item: item["id"])
        # Each under its site here, named as it is there, with the server it is kept on
        self.assertEqual([(item["site"], item["label"], item["server"]) for item in found],
                         [("example.com", "example.com", "web-01"), ("dev.shop.example.com", "shop.example.com", "web-02")])
        # A server not in Servers is not asked
        self.assertEqual(sorted(server for server, _command in asked), ["web-01", "web-02"])
        self.assertTrue(all("export-list --units" in command for _server, command in asked))


if __name__ == "__main__":
    unittest.main()
