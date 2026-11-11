import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "lib/windows_files.py"

# Just enough of rclone, on a local folder in place of the share
FAKE_RCLONE = textwrap.dedent('''
    import datetime, json, os, shutil, sys
    args = [arg for arg in sys.argv[1:] if arg != "--no-mimetype"]
    command = args.pop(0)
    stat = "--stat" in args
    args = [arg for arg in args if arg != "--stat"]
    if command == "lsf":
        args = [arg for arg in args if arg not in ("--files-only", "--max-depth", "1")]
    def entry(path):
        info = os.stat(path)
        return {"Name": os.path.basename(path), "IsDir": os.path.isdir(path), "Size": -1 if os.path.isdir(path) else info.st_size,
                "ModTime": datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc).isoformat()}
    path = args[0]
    if command == "lsjson":
        if not os.path.exists(path):
            sys.exit("ERROR : directory not found")
        print(json.dumps(entry(path) if stat else [entry(os.path.join(path, name)) for name in os.listdir(path)]))
    elif command == "lsf":
        print(*(name for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))), sep=chr(10))
    elif command == "cat":
        sys.stdout.buffer.write(open(path, "rb").read())
    elif command == "rcat":
        open(path, "wb").write(sys.stdin.buffer.read())
    elif command == "mkdir":
        os.makedirs(path, exist_ok=True)
    elif command == "moveto":
        os.rename(path, args[1])
    elif command == "deletefile":
        os.remove(path)
    elif command == "purge":
        shutil.rmtree(path)
''')


class WindowsFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.root = base / "share"
        self.root.mkdir()
        fake = base / "rclone"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_RCLONE)
        fake.chmod(0o755)
        self.env = {**os.environ, "VNS_RCLONE": str(fake)}
        (self.root / "Views").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "web.config").write_text("\ufeff<configuration />")
        (self.root / "App.dll").write_bytes(b"MZ\0\0")
        (self.root / "appsettings.json").write_text("{}")

    def tearDown(self):
        self.folder.cleanup()

    def run_operation(self, **operation):
        result = subprocess.run([sys.executable, str(HELPER), str(self.root)], input=json.dumps(operation),
                                capture_output=True, text=True, env=self.env)
        return result.returncode, json.loads(result.stdout)

    def test_details_read_dotnet_and_database_from_the_share(self):
        (self.root / "App.runtimeconfig.json").write_text(json.dumps({"runtimeOptions": {"tfm": "net10.0",
            "frameworks": [{"name": "Microsoft.AspNetCore.App", "version": "10.0.0"}]}}))
        (self.root / "AppSettings.Production.json").write_text(json.dumps({"ConnectionStrings": {
            "umbracoDbDSN": "Server=sql.example.test;Database=Shop;User Id=shop;Password=x",
            "umbracoDbDSN_ProviderName": "Microsoft.Data.SqlClient"}}))
        code, data = self.run_operation(action="details", environment="Production")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["dotnet"], "10.0")
        self.assertEqual(data.get("database_name"), "Shop")
        self.assertNotEqual(data.get("database"), "Unknown")

    def test_folders_come_first_then_files_alphabetically_without_owner(self):
        code, data = self.run_operation(action="list", path="")
        self.assertEqual(code, 0)
        self.assertTrue(data["windows"])
        self.assertEqual([item["name"] for item in data["items"]], ["bin", "Views", "App.dll", "appsettings.json", "web.config"])
        files = {item["name"]: item for item in data["items"]}
        self.assertFalse(files["App.dll"]["editable"])
        self.assertTrue(files["web.config"]["editable"])
        self.assertNotIn("owner", files["web.config"])
        self.assertNotIn("permissions", files["web.config"])

    def test_read_save_create_rename_and_delete(self):
        self.assertEqual(self.run_operation(action="read", path="web.config")[1]["content"], "<configuration />")
        self.assertEqual(self.run_operation(action="save", path="web.config", content="<x />")[0], 0)
        self.assertEqual((self.root / "web.config").read_text(), "<x />")
        self.assertEqual(self.run_operation(action="newfile", path="Views", name="Home.cshtml")[0], 0)
        self.assertTrue((self.root / "Views/Home.cshtml").is_file())
        self.assertEqual(self.run_operation(action="mkdir", path="", name="logs")[0], 0)
        self.assertEqual(self.run_operation(action="rename", path="logs", name="Logs2")[0], 0)
        self.assertTrue((self.root / "Logs2").is_dir())
        code, data = self.run_operation(action="rename", path="Logs2", name="Views")
        self.assertEqual((code, data["error"]), (1, "It already exists"))
        self.assertEqual(self.run_operation(action="delete", path="Logs2")[0], 0)
        self.assertFalse((self.root / "Logs2").exists())
        self.assertEqual(self.run_operation(action="delete", path="App.dll")[0], 0)
        self.assertFalse((self.root / "App.dll").exists())

    def test_upload_and_download(self):
        payload = base64.b64encode(b"\x00binary").decode("ascii")
        self.assertEqual(self.run_operation(action="upload", path="bin", files=[{"name": "a.bin", "content": payload}])[1]["count"], 1)
        self.assertEqual(self.run_operation(action="download", path="bin/a.bin")[1]["content"], payload)
        self.assertEqual(self.run_operation(action="read", path="bin/a.bin")[1]["error"], "Binary file. Download it instead.")

    def test_nothing_leads_out_of_the_site(self):
        for operation in ({"action": "list", "path": "../other"}, {"action": "read", "path": "Views/../../x"},
                          {"action": "mkdir", "path": "", "name": ".."}, {"action": "rename", "path": "web.config", "name": "a/b"},
                          {"action": "read", "path": "C:\\\\x"}, {"action": "delete", "path": ""}):
            with self.subTest(operation=operation):
                code, data = self.run_operation(**operation)
                self.assertEqual(code, 1)
                self.assertIn("error", data)
        self.assertTrue((self.root / "web.config").exists())


if __name__ == "__main__":
    unittest.main()
