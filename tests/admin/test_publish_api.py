"""Publish from a machine without SSH: a user's publish token, and POST /api/publish/<site> streaming the publish"""
import ast
import glob
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask, Response, abort, g, jsonify, request, stream_with_context

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/app.py"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT_DIR / "lib" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tokens = load("publish_tokens")
access = load("access")
# The harbor command, played: what it was given, the build it read, and the exit code asked for
FAKE_HARBOR = '''import sys
data = sys.stdin.buffer.read()
print("args " + " ".join(sys.argv[1:]))
print("read %d bytes" % len(data))
sys.exit(int(open(sys.argv[0] + ".exit").read()))
'''


class PublishTokenTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "secrets/publish-tokens.json"

    def test_a_token_is_shown_once_kept_as_a_hash_and_ends_the_one_before(self):
        first = tokens.make("u1", self.path)
        self.assertTrue(first.startswith("hpt_"))
        self.assertNotIn(first, self.path.read_text(), "only its fingerprint")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(tokens.user_for(f"Bearer {first}", self.path), "u1")
        self.assertTrue(tokens.info("u1", self.path)["used"], "when it was last used")
        second = tokens.make("u1", self.path)
        self.assertIsNone(tokens.user_for(f"Bearer {first}", self.path), "a new one ends the one before")
        self.assertEqual(tokens.user_for(f"Bearer {second}", self.path), "u1")
        for header in ("", "Bearer ", f"Basic {second}", "Bearer hpt_wrong", second):
            self.assertIsNone(tokens.user_for(header, self.path), header)
        tokens.remove("u1", self.path)
        self.assertEqual(tokens.info("u1", self.path), {"exists": False})
        self.assertIsNone(tokens.user_for(f"Bearer {second}", self.path))


class PublishApiTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "publish-tokens.json"
        (self.root / "www/shop.dk").mkdir(parents=True)
        (self.root / "www/built.dk").mkdir(parents=True)
        (self.root / "www/built.dk/Built.csproj").write_text("<Project/>")
        self.harbor = self.root / "harbor.py"
        self.harbor.write_text(FAKE_HARBOR)
        self.exit(0)
        config = {"user": "root", "users": [
            {"id": "u1", "username": "dev", "name": "Dev", "role": "user", "web": True,
             "sites": {"shop.dk": {"envs": ["main"], "actions": ["publish"]}}},
            {"id": "u2", "username": "viewer", "role": "user", "web": True, "sites": {"shop.dk": {"envs": ["main"], "actions": []}}}]}
        source = APP.read_text()
        wanted = {"protect", "publish_target", "publish_api_info", "publish_api", "api_publish_token"}
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        app = Flask(__name__)
        bound = SimpleNamespace(**{name: (lambda function: lambda *args: function(*args, self.path))(getattr(tokens, name))
                                   for name in ("make", "info", "remove", "user_for")})
        self.ns = dict(app=app, Response=Response, stream_with_context=stream_with_context, request=request, jsonify=jsonify,
                       abort=abort, g=g, os=os, re=re, glob=glob, tempfile=tempfile, subprocess=subprocess, access=access,
                       cfg=config, _publish_tokens=bound, WEB_ROOT=str(self.root / "www"), ADMIN_SITE="admin.test",
                       NAME_RE=re.compile(r"^[a-z0-9.-]+$"), site_arg=lambda data: data["site"], managed_by=lambda site: None,
                       HARBOR_COMMAND=str(self.harbor), me=lambda: access.find(config, "u1"), PUBLISH_UPLOADS=str(self.root / "incoming"),
                       # The rest of protect(): a logged-in session, its CSRF token "t"
                       hmac=__import__("hmac"), session={"csrf": "t"}, urlparse=__import__("urllib.parse").parse.urlparse,
                       redirect=lambda path: ("", 302), session_user=lambda: access.find(config, "u1"), harbor_token_site=lambda header: None)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "publish api", "exec"), self.ns)
        self.client = app.test_client()

    def exit(self, code):
        Path(str(self.harbor) + ".exit").write_text(str(code))

    def test_the_token_endpoint_makes_shows_once_and_removes(self):
        csrf = {"X-CSRF-Token": "t"}
        made = self.client.post("/api/publish-token", headers=csrf).json
        self.assertTrue(made["token"].startswith("hpt_") and made["exists"])
        self.assertNotIn("token", self.client.get("/api/publish-token").json, "shown only when it is made")
        self.assertEqual(self.client.post("/api/publish-token").status_code, 403, "a session's own CSRF token, as everywhere")
        self.assertEqual(self.client.delete("/api/publish-token", headers=csrf).json, {"exists": False})

    def test_a_publish_with_a_token_streams_the_output_and_ends_with_its_result(self):
        token = tokens.make("u1", self.path)
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/publish/shop.dk/info", headers=headers).json, {"architecture": os.uname().machine})
        self.assertEqual(self.client.get("/api/publish/shop.dk/info").status_code, 401, "no token")
        self.assertEqual(self.client.get("/api/publish/shop.dk/info", headers={"Authorization": "Bearer hpt_x"}).status_code, 401)
        self.assertEqual(self.client.get("/api/publish/built.dk/info", headers=headers).status_code, 403, "not a site of theirs")
        self.assertEqual(self.client.get("/api/publish/nowhere.dk/info", headers=headers).status_code, 404, "made in Harbor first")
        response = self.client.post("/api/publish/shop.dk?commit=abc1234&dirty=y&keep=/wwwroot/", data=b"tar bytes", headers=headers)
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200, text)
        self.assertIn("args site receive-build shop.dk --stdin --by Dev (publish token) --commit abc1234 --dirty y --keep=/wwwroot/", text)
        self.assertIn("read 9 bytes", text)
        self.assertEqual(list((self.root / "incoming").glob(".shop.dk-upload-*")), [], "the upload is gone afterwards")
        self.assertEqual(text.splitlines()[-1], "Harbor: OK")
        self.exit(3)
        text = self.client.post("/api/publish/shop.dk", data=b"x", headers=headers).get_data(as_text=True)
        self.assertEqual(text.splitlines()[-1], "Harbor: FAILED (exit 3)", "the script on the machine fails on it")
        self.assertEqual(self.client.post("/api/publish/shop.dk?commit=$(id)", data=b"x", headers=headers).status_code, 400)
        viewer = tokens.make("u2", self.path)
        self.assertEqual(self.client.post("/api/publish/shop.dk", data=b"x", headers={"Authorization": f"Bearer {viewer}"}).status_code, 403,
                         "a user who may not publish it")


if __name__ == "__main__":
    unittest.main()
