"""The admin's API for the VNS.Harbor package (phase 9b): a site's token says which site and environment it is, and a
site may only sync where Sync setup lets it"""
import ast
import glob
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "admin/app.py"


def functions(*names, **namespace):
    tree = ast.parse(APP.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "harbor api", "exec"), namespace)
    return namespace


class TokenTests(unittest.TestCase):
    def test_a_token_names_its_site_and_environment(self):
        folder = Path(tempfile.mkdtemp())
        for site, environment, token in (("shop.dk", "main", "a" * 64), ("shop.dk", "staging", "b" * 64), ("blog.dk", "main", "c" * 64)):
            path = folder / site / "connection" / f"{environment}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"token": token, "login_key": "k"}))
        ns = functions("harbor_token_site", glob=glob, os=os, re=re, json=json, hmac=hmac, HARBOR_SECRETS=str(folder))
        check = ns["harbor_token_site"]
        self.assertEqual(check("Bearer " + "b" * 64), ("shop.dk", "staging"))
        self.assertEqual(check("Bearer " + "c" * 64), ("blog.dk", "main"))
        for header in ("", "Bearer " + "d" * 64, "Basic " + "a" * 64, "Bearer " + "a" * 63, "Bearer ../" + "a" * 61):
            self.assertIsNone(check(header), header)


class DestinationTests(unittest.TestCase):
    def test_only_what_sync_setup_allows(self):
        spec = importlib.util.spec_from_file_location("vns_data_sync", ROOT / "lib/data_sync.py")
        data_sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(data_sync)
        web = Path(tempfile.mkdtemp())
        (web / "shop.dk/scripts").mkdir(parents=True)
        (web / "shop.dk/scripts/data_sync.json").write_text(json.dumps({
            "main": {"enabled": True, "send": True, "content": True, "media": False},
            "test": {"enabled": True, "receive": True, "content": True},
            "staging": {"enabled": False, "settings_receive": False},
        }))
        settings = {"test": {"PROD_ENVIRONMENT": "Test", "PROD_BINDING": "test.shop.dk"},
                    "staging": {"PROD_ENVIRONMENT": "Staging", "PROD_BINDING": "staging.shop.dk"}}
        ns = functions("harbor_api_destinations", os=os, WEB_ROOT=str(web), _data_sync_module=data_sync,
                       target_ids=lambda root: ["staging", "test"], parse_prod=lambda root, target: settings.get(target),
                       conf_binding=lambda site: "shop.dk", https_status=lambda site: {"scheme": "https"})
        found = ns["harbor_api_destinations"]("shop.dk", "main")
        self.assertEqual(found, [{"id": "test", "environment": "Test", "binding": "test.shop.dk", "url": "https://test.shop.dk/", "settings": True,
                                  "data": ["content"]}])
        # From an environment: settings only ever come from the main site
        back = ns["harbor_api_destinations"]("shop.dk", "test")
        self.assertEqual([item["id"] for item in back], [])


if __name__ == "__main__":
    unittest.main()
