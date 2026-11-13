import ast
import hmac
import io
import os
import grp
import pwd
import stat
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, request, send_file, session
from werkzeug.exceptions import BadRequest

from apache.lib.remote_files import contains_editable_files, editable_file, file_metadata
from apache.tests.app_targets import add_target_helpers


APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app"


class ServerFilesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.web = self.root / "www"
        self.web.mkdir()
        self.outside = self.root / "etc"
        self.outside.mkdir()
        self.file = self.outside / "example.conf"
        self.file.write_text("original\n")
        self.file.chmod(0o640)
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.config["TESTING"] = True
        self.ns = dict(app=self.app, os=os, Path=Path, io=io, shutil=shutil, re=re,
                       abort=abort, jsonify=jsonify, request=request, send_file=send_file,
                       session=session, hmac=hmac, urlparse=urlparse, redirect=redirect,
                       WEB_ROOT=str(self.web), MAX_EDIT_BYTES=2 * 1024 * 1024,
                       editable_file=editable_file, contains_editable_files=contains_editable_files,
                       file_metadata=file_metadata)
        names = {"protect", "error", "safe_path", "rel", "server_files", "fs_root", "fs_path", "fs_relative",
                 "is_site_root", "own_like_parent", "valid_name", "build_command",
                 "fs_list", "fs_read", "fs_save", "fs_create", "fs_rename", "fs_delete",
                 "fs_upload", "fs_download"}
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP / "app.py"), "exec"), self.ns)
        add_target_helpers(self.ns)
        self.client = self.app.test_client()
        with self.client.session_transaction() as state:
            state.update(user="test-admin", csrf="test-csrf")

    def post(self, action, **data):
        return self.client.post("/api/server-fs/" + action, json=data,
                                headers={"X-CSRF-Token": "test-csrf"})

    def test_user_does_not_see_the_folders_only_administrators_reach(self):
        site = self.web / "example.test"
        for folder in ("scripts", ".vns-publish", "wwwroot"):
            (site / folder).mkdir(parents=True)
        (site / "scripts/publish.sh").write_text("runs as root")
        names = {"hidden_from_user", "file_access", "file_env", "file_env_root"}
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names
                 or isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "USER_HIDDEN_FOLDERS"]
        self.ns.update(is_admin=lambda: False, site_dirs=lambda: ["example.test"], target_ids=lambda root: [])
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP / "app.py"), "exec"), self.ns)
        response = self.client.get("/api/site-fs/example.test/list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.json["items"]], ["wwwroot"])
        response = self.client.get("/api/site-fs/example.test/read", query_string={"path": "scripts/publish.sh"})
        self.assertEqual(response.status_code, 403)

    def test_site_browser_is_confined_to_its_root_for_all_operations(self):
        site = self.web / "example.test"
        site.mkdir()
        (site / "index.html").write_text("site")
        sibling = self.web / "other.test"
        sibling.mkdir()
        (sibling / "index.html").write_text("other")
        base = "/api/site-fs/example.test/"
        response = self.client.get(base + "list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["path"], "")
        self.assertEqual([item["name"] for item in response.json["items"]], ["index.html"])
        for action in ("list", "read", "download"):
            response = self.client.get(base + action, query_string={"path": "../other.test/index.html"})
            self.assertEqual(response.status_code, 400, action)
        for action in ("save", "delete", "rename", "mkdir", "newfile"):
            response = self.client.post(base + action,
                                       json={"path": "../other.test/index.html", "name": "renamed", "content": "changed"},
                                       headers={"X-CSRF-Token": "test-csrf"})
            self.assertEqual(response.status_code, 400, action)
        self.assertEqual((sibling / "index.html").read_text(), "other")
        for action in ("rename", "delete"):
            response = self.client.post(base + action, json={"path": "", "name": "renamed"},
                                       headers={"X-CSRF-Token": "test-csrf"})
            self.assertEqual(response.status_code, 400)
        self.assertTrue(site.is_dir())

    def test_site_browser_blocks_symlink_escape_including_upload(self):
        site = self.web / "example.test"
        site.mkdir()
        (site / "linked.conf").symlink_to(self.file)
        base = "/api/site-fs/example.test/"
        for action in ("read", "download"):
            response = self.client.get(base + action, query_string={"path": "linked.conf"})
            self.assertEqual(response.status_code, 400)
        response = self.client.post(base + "upload",
                                   data={"path": "", "files": (io.BytesIO(b"overwrite"), "linked.conf")},
                                   headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.file.read_text(), "original\n")

    def test_local_production_root_is_relative_and_cannot_be_removed(self):
        site = self.web / "example.test"
        production = site / "_prod"
        production.mkdir(parents=True)
        (site / "private.txt").write_text("source")
        (production / "index.html").write_text("production")
        base = "/api/site-fs/example.test/_prod/"
        response = self.client.get(base + "list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["path"], "")
        response = self.client.get(base + "read", query_string={"path": "../private.txt"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(base + "delete", json={"path": ""},
                                   headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(base + "save", json={"path": "index.html", "content": "updated"},
                                   headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((production / "index.html").read_text(), "updated")

    def test_editor_preserves_danish_characters_as_utf8(self):
        content = '<meta charset="utf-8">\nHello world! - æøå'
        for prefix, path in (("/api/server-fs/", self.file),
                             ("/api/fs/", self.web / "index.html")):
            relative = str(path) if prefix == "/api/server-fs/" else "index.html"
            response = self.client.post(prefix + "save", json={"path": relative, "content": content},
                                        headers={"X-CSRF-Token": "test-csrf"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(path.read_bytes(), content.encode("utf-8"))
            response = self.client.get(prefix + "read", query_string={"path": relative})
            self.assertEqual(response.json["content"], content)

    def test_root_listing_and_no_recursive_system_scan(self):
        with patch("os.scandir", return_value=[]) as scan:
            response = self.client.get("/api/server-fs/list")
        self.assertEqual(response.json["path"], "")
        scan.assert_called_once_with("/")
        empty = self.outside / "empty"
        empty.mkdir()
        with patch.dict(self.ns, contains_editable_files=Mock(side_effect=AssertionError("recursive scan"))):
            response = self.client.get("/api/server-fs/list", query_string={"path": str(self.outside)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["path"], str(self.outside).lstrip("/"))
        entries = {item["name"]: item for item in response.json["items"]}
        self.assertTrue(entries["empty"]["has_editable_files"])
        self.assertTrue(entries["example.conf"]["editable"])

    def test_read_save_download_outside_web_root(self):
        response = self.client.get("/api/server-fs/read", query_string={"path": str(self.file)})
        self.assertEqual(response.json["content"], "original\n")
        response = self.post("save", path=str(self.file), content="updated\n")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.file.read_text(), "updated\n")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)
        response = self.client.get("/api/server-fs/download", query_string={"path": str(self.file)})
        self.assertEqual(response.data, b"updated\n")
        response.close()

    def test_local_and_server_listings_sort_names_without_folders_first(self):
        for root, endpoint in ((self.outside, "/api/server-fs/list"), (self.web, "/api/fs/list")):
            (root / "a-file.txt").write_text("text")
            (root / "Z-folder").mkdir()
            path = str(root) if root == self.outside else ""
            response = self.client.get(endpoint, query_string={"path": path})
            self.assertEqual(response.status_code, 200)
            names = [item["name"] for item in response.json["items"]]
            self.assertEqual(names, sorted(names, key=lambda name: (name.lower(), name)))
            self.assertLess(names.index("a-file.txt"), names.index("Z-folder"))

    def test_remote_listing_sorts_names_without_folders_first(self):
        from apache.lib.remote_files import handle
        (self.outside / "a-file.txt").write_text("text")
        (self.outside / "Z-folder").mkdir()
        result = handle(self.outside, {"action": "list"})
        names = [item["name"] for item in result["items"]]
        self.assertEqual(names, sorted(names, key=lambda name: (name.lower(), name)))
        self.assertLess(names.index("a-file.txt"), names.index("Z-folder"))

    def test_local_server_and_remote_listings_include_unix_metadata(self):
        from apache.lib.remote_files import handle
        for root, endpoint in ((self.outside, "/api/server-fs/list"), (self.web, "/api/fs/list")):
            file = root / "permissions.txt"
            file.write_text("text")
            file.chmod(0o640)
            folder = root / "restricted"
            folder.mkdir(mode=0o750)
            link = root / "linked"
            link.symlink_to(file)
            path = str(root) if root == self.outside else ""
            listings = [self.client.get(endpoint, query_string={"path": path}).json,
                        handle(root, {"action": "list"})]
            for listing in listings:
                items = {item["name"]: item for item in listing["items"]}
                self.assertEqual(items[file.name]["permissions"], "-rw-r-----")
                self.assertEqual(items[folder.name]["permissions"], "drwxr-x---")
                self.assertTrue(items[link.name]["permissions"].startswith("l"))
                for entry in (file, folder, link):
                    info = entry.lstat()
                    item = items[entry.name]
                    self.assertEqual(item["permissions"], stat.filemode(info.st_mode))
                    self.assertEqual(item["uid"], info.st_uid)
                    self.assertEqual(item["gid"], info.st_gid)
                    self.assertEqual(item["owner"], pwd.getpwuid(info.st_uid).pw_name)
                    self.assertEqual(item["group"], grp.getgrgid(info.st_gid).gr_name)

    def test_metadata_preserves_special_bits_and_unnamed_account_ids(self):
        info = Mock(st_mode=stat.S_IFREG | 0o6750, st_uid=123456, st_gid=234567)
        with patch("apache.lib.remote_files.pwd.getpwuid", side_effect=KeyError), \
                patch("apache.lib.remote_files.grp.getgrgid", side_effect=KeyError):
            self.assertEqual(file_metadata(info), {
                "permissions": "-rwsr-s---", "owner": "123456", "group": "234567",
                "uid": 123456, "gid": 234567})
        self.assertEqual(file_metadata(Mock(st_mode=stat.S_IFDIR | 0o1777, st_uid=os.getuid(),
                                            st_gid=os.getgid()))["permissions"], "drwxrwxrwt")

    def test_create_upload_rename_delete_outside_web_root(self):
        self.assertEqual(self.post("mkdir", path=str(self.outside), name="new").status_code, 200)
        folder = self.outside / "new"
        self.assertEqual(self.post("newfile", path=str(folder), name="file.txt").status_code, 200)
        file = folder / "file.txt"
        self.assertEqual(file.stat().st_uid, folder.stat().st_uid)
        response = self.client.post("/api/server-fs/upload",
                                    data={"path": str(folder), "files": (io.BytesIO(b"upload"), "upload.txt")},
                                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.json["count"], 1)
        self.assertEqual(self.post("rename", path=str(file), name="renamed.txt").status_code, 200)
        self.assertEqual(self.post("delete", path=str(folder / "renamed.txt")).status_code, 200)
        self.assertEqual(self.post("delete", path=str(folder)).status_code, 200)
        self.assertFalse(folder.exists())

    def test_web_root_confinement_and_protected_roots_are_preserved(self):
        for action in ("list", "read", "download"):
            response = self.client.get("/api/fs/" + action, query_string={"path": "../etc/example.conf"})
            self.assertEqual(response.status_code, 400)
        link = self.web / "link"
        link.symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(self.client.get("/api/fs/list?path=link").status_code, 400)
        for action in ("rename", "delete"):
            with self.subTest(action=action):
                self.assertEqual(self.post(action, path="/", name="renamed").status_code, 400)
                self.assertEqual(self.post(action, path=str(self.web), name="renamed").status_code, 400)

    def test_server_file_routes_require_login_and_csrf(self):
        self.assertEqual(self.client.post("/api/server-fs/save", json={
            "path": str(self.file), "content": "bad"}).status_code, 403)
        with self.client.session_transaction() as state:
            state.clear()
        self.assertEqual(self.client.get("/api/server-fs/list").status_code, 401)
        self.assertEqual(self.file.read_text(), "original\n")

    def test_server_directory_links_are_browsable_and_mutations_change_link_only(self):
        link = self.outside / "linked-directory"
        target = self.outside / "target"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link.symlink_to(target, target_is_directory=True)
        listing = self.client.get("/api/server-fs/list", query_string={"path": str(self.outside)})
        item = next(item for item in listing.json["items"] if item["name"] == link.name)
        self.assertTrue(item["dir"])
        response = self.client.get("/api/server-fs/list", query_string={"path": str(link)})
        self.assertEqual(response.json["path"], str(target).lstrip("/"))
        self.assertEqual(self.post("rename", path=str(link), name="renamed-link").status_code, 200)
        renamed = self.outside / "renamed-link"
        self.assertTrue(renamed.is_symlink())
        self.assertEqual(self.post("delete", path=str(renamed)).status_code, 200)
        self.assertTrue((target / "keep.txt").is_file())

    def test_server_shell_starts_at_root_and_site_shell_is_unchanged(self):
        self.ns["site_arg"] = lambda data: data["site"]
        self.assertEqual(self.ns["build_command"]({"kind": "shell"})[2], "/")
        self.assertEqual(self.ns["build_command"]({"kind": "shell", "site": "example.test"})[2],
                         str(self.web / "example.test"))

    def test_settings_update_runs_scripts_in_order_and_stops_after_failure(self):
        apache = self.root / "vns/apache"
        apache.mkdir(parents=True)
        calls = self.root / "update-calls"
        updater = apache.parent / "update.sh"
        admin_updater = apache / "update_admin_site.sh"
        admin_updater.write_text(f'printf "admin:%s\\n" "$1" >> "{calls}"\n')
        self.ns.update(APACHE_DIR=str(apache), ADMIN_SITE="admin.example.test", update_target=lambda: "")
        for code in (0, 7):
            with self.subTest(code=code):
                updater.write_text(f'echo vns >> "{calls}"\nexit {code}\n')
                if calls.exists():
                    calls.unlink()
                title, argv, cwd = self.ns["build_command"]({
                    "kind": "update_system", "site": "ignored.test",
                })
                self.assertEqual(cwd, "/")
                result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(calls.read_text().splitlines(),
                                 ["vns", "admin:admin.example.test"] if code == 0 else ["vns"])
        updater.unlink()
        with self.app.test_request_context(), self.assertRaises(BadRequest):
            self.ns["build_command"]({"kind": "update_system"})


if __name__ == "__main__":
    unittest.main()
