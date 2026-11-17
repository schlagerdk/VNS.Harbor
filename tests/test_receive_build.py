"""harbor site receive-build: a C# site published from a developer's machine, set up the first time, then updated"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402


def real_rsync(argv, stdin=None):
    done = subprocess.run(argv, capture_output=True, text=True)
    return Result(done.returncode, done.stdout, done.stderr)


@unittest.skipUnless(shutil.which("rsync") and subprocess.run(["rsync", "--version"], capture_output=True, text=True).stdout.startswith("rsync  version"),
                     "needs rsync with filter rules, as on the servers")
class ReceiveBuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.incoming = self.root / "var/lib/harbor/incoming/shop.dk"
        patcher = mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, files):
        if self.incoming.exists():
            shutil.rmtree(self.incoming)
        for name, text in files.items():
            (self.incoming / name).parent.mkdir(parents=True, exist_ok=True)
            (self.incoming / name).write_text(text)

    def harbor(self, *argv):
        system = RecordingSystem(root=self.root, answers={"rsync": real_rsync})
        return main(["site", "receive-build", "shop.dk", *argv], system), system

    def test_nothing_sent_is_said(self):
        code, system = self.harbor()
        self.assertEqual(code, 1)
        self.assertIn("Make the site in Harbor first", system.said[-1])

    def test_the_first_build_sets_the_site_up_and_later_ones_keep_what_it_has(self):
        self.build({"Shop.dll": "v1", "Shop.runtimeconfig.json": "{}", "appsettings.json": '{"local": true}',
                    "Views/Home.cshtml": "<h1>1</h1>", "App_Plugins/Shop/a.js": "a"})
        with mock.patch("harbor.sites.setup.setup_site") as setup:
            code, system = self.harbor("--binding", "shop.dk", "--https", "y", "--environment", "Production")
        self.assertEqual(code, 0, system.said)
        setup.assert_called_once_with(system, "shop.dk", "shop.dk", "y", "Production")
        site = self.root / "var/www/shop.dk"
        self.assertEqual((site / "appsettings.json").read_text(), '{"local": true}', "the first time, its settings come along")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").read_text())["binding"], "shop.dk")
        # Running there: its own settings, data and packages
        (site / "appsettings.json").write_text('{"server": true}')
        for name, text in (("umbraco/Data/Umbraco.sqlite.db", "live"), ("wwwroot/media/1/a.png", "png"),
                           ("App_Plugins/Extra/x.js", "added there"), ("Old.dll", "gone"), ("vns-harbor.json", "{}")):
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_text(text)
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}", "appsettings.json": '{"local": true}',
                    "Views/Home.cshtml": "<h1>2</h1>", "App_Plugins/Shop/a.js": "a2"})
        code, system = self.harbor()
        self.assertEqual(code, 0, system.said)
        self.assertEqual((site / "Shop.dll").read_text(), "v2")
        self.assertEqual((site / "Views/Home.cshtml").read_text(), "<h1>2</h1>")
        self.assertEqual((site / "App_Plugins/Shop/a.js").read_text(), "a2")
        self.assertFalse((site / "Old.dll").exists(), "the build is the build: what it no longer has goes")
        for name, text in (("appsettings.json", '{"server": true}'), ("umbraco/Data/Umbraco.sqlite.db", "live"),
                           ("wwwroot/media/1/a.png", "png"), ("App_Plugins/Extra/x.js", "added there"), ("vns-harbor.json", "{}")):
            self.assertEqual((site / name).read_text(), text, name)
        self.assertIn("systemctl restart shop.dk", system.commands)

    def test_create_external_makes_a_running_site_with_only_the_build(self):
        """Published from an external source: an empty project's build starts it, and only the build is kept"""
        root = self.root / "var/www/shop.dk"

        def build(argv, stdin=None):
            publish = root / "_publish"
            publish.mkdir(parents=True)
            for name in ("Shop.dll", "Shop.runtimeconfig.json", "appsettings.json"):
                (publish / name).write_text(name)
            return Result(0)

        def new(argv, stdin=None):
            (root / "Shop.csproj").write_text("<Project/>")
            (root / "Program.cs").write_text("app")
            return Result(0)
        system = RecordingSystem(root=self.root, answers={"rsync": real_rsync, "python3": build, "dotnet new": new, "uname -m": "x86_64"})
        with mock.patch("harbor.sites.setup.setup_site") as setup:
            code = main(["site", "create-external", "shop.dk", "--project", "Shop", "--binding", "shop.dk", "--environment", "Test"], system)
        self.assertEqual(code, 0, system.said)
        setup.assert_called_once_with(system, "shop.dk", "shop.dk", "n", "Test")
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["Shop.dll", "Shop.runtimeconfig.json", "appsettings.json"], "no project")
        self.assertTrue((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").is_file(), "published from a machine from now on")
        build_command = next(command for command in system.commands if "build_as.py" in command)
        self.assertNotIn("sudo", build_command)
        code = main(["site", "create-external", "shop.dk", "--project", "Shop"], RecordingSystem(root=self.root))
        self.assertEqual(code, 1, "not over a site that is there")

    def running_site(self, port="4020"):
        """shop.dk as a publish from a machine left it: its service runs Shop.dll on a port"""
        site = self.root / "var/www/shop.dk"
        (site / "wwwroot").mkdir(parents=True)
        (site / "wwwroot/upload.png").write_text("made there")
        (site / "Shop.dll").write_text("v1")
        (site / "Shop.runtimeconfig.json").write_text("{}")
        (site / "appsettings.json").write_text('{"server": true}')
        (site / "appsettings.Production.json").write_text('{"secret": true}')
        os.chmod(site, 0o1775)
        (self.root / "etc/systemd/system").mkdir(parents=True, exist_ok=True)
        (self.root / "etc/systemd/system/shop.dk.service").write_text(
            f'ExecStart=/usr/lib/dotnet/dotnet /var/www/shop.dk/Shop.dll --urls "http://localhost:{port}"\n')
        return site

    def system(self, code="200"):
        return RecordingSystem(root=self.root, answers={"rsync": real_rsync, "curl": code})

    def test_kept_paths_own_scripts_and_the_check_that_it_answers(self):
        """What its own publish script did: /wwwroot/ kept, scripts/ along as root's, and the site checked"""
        site = self.running_site()
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}", "wwwroot/site.css": "from the build"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/wwwroot/"], system), 1, "a build with what the site keeps")
        self.assertIn("The build has /wwwroot/", system.said[-1])
        self.assertEqual((site / "Shop.dll").read_text(), "v1", "nothing changed")
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}"})
        scripts = self.root / "var/lib/harbor/incoming/shop.dk.scripts"
        scripts.mkdir(parents=True)
        (scripts / "warm.sh").write_text("echo warm")
        (scripts / "notes.txt").write_text("not a script")
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/wwwroot/"], system), 0, system.said)
        self.assertEqual((site / "Shop.dll").read_text(), "v2")
        self.assertEqual((site / "wwwroot/upload.png").read_text(), "made there")
        owned = self.root / "var/lib/harbor/sites/shop.dk/scripts"
        self.assertEqual((owned / "warm.sh").read_text(), "echo warm", "where only root can write, not in the site's folder")
        self.assertFalse((site / "scripts").exists())
        self.assertFalse((owned / "notes.txt").exists(), "only scripts")
        self.assertIn(f"chown -R root:root {owned}", system.commands, "root's, as they run as root")
        # One deleted on the machine goes on the server too; one written on the server stays
        (owned / "cleanup.sh").write_text("written on the server")
        (site / "scripts").mkdir()
        (site / "scripts/maintain.sh").write_text("in the site's own scripts folder")
        (scripts / "warm.sh").unlink()
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 0)
        self.assertFalse((owned / "warm.sh").exists())
        self.assertEqual((owned / "cleanup.sh").read_text(), "written on the server")
        self.assertEqual((site / "scripts/maintain.sh").read_text(), "in the site's own scripts folder")
        self.assertIn(f"chown -R root:root {site}/scripts", system.commands, "root's, as the admin runs it as root")
        self.assertTrue(any("curl" in command and "127.0.0.1:4020" in command for command in system.commands))
        self.assertEqual(oct(os.stat(site).st_mode & 0o7777), "0o1775", "the folder's own rights stay")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").read_text())["keep"], ["/wwwroot/"])
        # Kept from then on, without saying so again
        self.build({"Shop.dll": "v3", "Shop.runtimeconfig.json": "{}", "wwwroot/site.css": "from the build"})
        self.assertEqual(main(["site", "receive-build", "shop.dk"], self.system()), 1)
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/../etc/"], RecordingSystem(root=self.root)), 1, "never out of the site")

    def test_a_build_without_the_dll_the_service_runs_changes_nothing(self):
        site = self.running_site()
        self.build({"Other.dll": "x", "Other.runtimeconfig.json": "{}"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 1)
        self.assertIn("The build has no Shop.dll", system.said[-1])
        self.assertTrue((site / "Shop.dll").is_file(), "the site still runs what it ran")
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}", "App_Data/seed.json": "{}"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 1, "App_Data is the server's")
        self.assertIn("The build has /App_Data/", system.said[-1])

    def test_a_site_that_does_not_answer_fails_and_rollback_puts_the_build_before_back(self):
        site = self.running_site()
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}"})
        system = self.system("502")
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/wwwroot/"], system), 1)
        self.assertIn("journalctl -u shop.dk -n 30 --no-pager", system.commands)
        self.assertIn("does not answer on port 4020 (502)", system.said[-1])
        self.assertIn("harbor site rollback shop.dk", system.said[-1])
        self.assertEqual((site / "Shop.dll").read_text(), "v2")
        system = self.system()
        self.assertEqual(main(["site", "rollback", "shop.dk"], system), 0, system.said)
        self.assertEqual((site / "Shop.dll").read_text(), "v1", "the build before")
        self.assertEqual((site / "appsettings.json").read_text(), '{"server": true}', "its settings were never part of a build")
        self.assertEqual((site / "wwwroot/upload.png").read_text(), "made there")

    def test_appsettings_from_the_build_when_the_site_takes_them(self):
        site = self.running_site()
        self.build({"Shop.dll": "v2", "Shop.runtimeconfig.json": "{}", "appsettings.json": '{"code": true}',
                    "appsettings.Production.json": '{"from code": true}'})
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/wwwroot/"], self.system()), 0)
        self.assertEqual((site / "appsettings.json").read_text(), '{"server": true}', "the server's, by default")
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--appsettings", "build"], self.system()), 0)
        self.assertEqual((site / "appsettings.json").read_text(), '{"code": true}')
        self.assertEqual((site / "appsettings.Production.json").read_text(), '{"secret": true}', "the environment's stays")

    def tar(self, link=False):
        import io
        import tarfile
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name, text in (("./build/Shop.dll", "v2"), ("./build/Shop.runtimeconfig.json", "{}"), ("./scripts/warm.sh", "echo")):
                info = tarfile.TarInfo(name)
                info.size = len(text)
                archive.addfile(info, io.BytesIO(text.encode()))
            if link:
                escape = tarfile.TarInfo("./build/escape")
                escape.type, escape.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(escape)
        data.seek(0)
        return data

    def test_the_tar_on_stdin_one_publish_at_a_time_and_who_published(self):
        from harbor.core.system import Stop
        from harbor.sites.receive_build import receive_build
        site = self.running_site()
        with self.assertRaises(Stop) as refused:
            receive_build(self.system(), "shop.dk", stream=self.tar(link=True))
        self.assertIn("points out of it", str(refused.exception))
        self.assertEqual((site / "Shop.dll").read_text(), "v1", "nothing changed")
        receive_build(self.system(), "shop.dk", stream=self.tar(), by="mhs@mac", commit="abc1234", dirty="y", keep=["/wwwroot/"])
        self.assertEqual((site / "Shop.dll").read_text(), "v2")
        self.assertEqual((self.root / "var/lib/harbor/sites/shop.dk/scripts/warm.sh").read_text(), "echo")
        last = json.loads((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").read_text())["publishes"][0]
        self.assertEqual((last["by"], last["commit"], last["dirty"]), ("mhs@mac", "abc1234", True))
        # A second publish while one runs is refused
        import fcntl
        lock_path = self.root / "var/lib/harbor/incoming/shop.dk.lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            system = self.system()
            self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 1)
            self.assertIn("being published already", system.said[-1])

    def test_a_python_site_gets_its_app_its_packages_when_they_changed_and_is_checked(self):
        site = self.root / "var/www/shop.dk"
        for name, text in (("app/app.py", "v1"), ("app/requirements.txt", "flask==3.0"), ("app/.env", "SECRET=1"), ("venv/bin/pip", "")):
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_text(text)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        (self.root / "etc/systemd/system/shop.dk.service").write_text("ExecStart=/var/www/shop.dk/venv/bin/gunicorn --bind 127.0.0.1:4100 app:app\n")
        self.build({"app.py": "v2", "requirements.txt": "flask==3.0"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/.env"], system), 0, system.said)
        self.assertEqual((site / "app/app.py").read_text(), "v2")
        self.assertEqual((site / "app/.env").read_text(), "SECRET=1", "kept")
        self.assertFalse(any("pip" in command for command in system.commands), "the same requirements: no install")
        self.assertTrue(any("127.0.0.1:4100" in command for command in system.commands), "gunicorn's port, checked")
        self.build({"app.py": "v3", "requirements.txt": "flask==3.1"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 0, system.said)
        self.assertIn("sudo -u www-data ./venv/bin/pip install --upgrade -r app/requirements.txt", system.commands)
        self.build({"main.py": "x"})
        self.assertEqual(main(["site", "receive-build", "shop.dk"], self.system()), 1, "no app.py: not what the site runs")

    def test_an_html_site_gets_its_files_and_keeps_its_scripts_root(self):
        site = self.root / "var/www/shop.dk"
        (site / "scripts").mkdir(parents=True)
        (site / "scripts/own.sh").write_text("echo")
        (site / "index.html").write_text("old")
        (site / "old.html").write_text("gone")
        self.build({"index.html": "new", "css/site.css": "body{}"})
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk"], system), 1, "it would delete old.html: a choice first")
        self.assertIn("would delete 1 file(s)", system.said[-1])
        self.assertIn("old.html", system.said[-1])
        self.assertEqual((site / "old.html").read_text(), "gone", "nothing changed")
        system = self.system()
        self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "none"], system), 0, system.said)
        self.assertEqual((site / "index.html").read_text(), "new")
        self.assertFalse((site / "old.html").exists())
        self.assertEqual((site / "scripts/own.sh").read_text(), "echo", "its scripts are not the build's")
        self.assertFalse(any("systemctl restart" in command for command in system.commands), "no service to restart")
        self.assertEqual(main(["site", "rollback", "shop.dk"], self.system()), 0)
        self.assertEqual((site / "index.html").read_text(), "old")

    def test_two_failed_publishes_in_a_row_still_roll_back_to_the_last_that_ran(self):
        site = self.running_site()
        for version in ("v2", "v3"):
            self.build({"Shop.dll": version, "Shop.runtimeconfig.json": "{}"})
            self.assertEqual(main(["site", "receive-build", "shop.dk", "--keep", "/wwwroot/"], self.system("502")), 1)
        marker = json.loads((self.root / "var/lib/harbor/sites/shop.dk/local-publish.json").read_text())
        self.assertFalse(marker["ok"])
        self.assertTrue(marker["publishes"][0]["failed"])
        self.assertEqual(main(["site", "rollback", "shop.dk"], self.system()), 0)
        self.assertEqual((site / "Shop.dll").read_text(), "v1", "not v2, which never ran")

    def test_a_site_with_a_project_here_is_not_replaced(self):
        site = self.root / "var/www/shop.dk"
        site.mkdir(parents=True)
        (site / "Shop.csproj").write_text("<Project/>")
        self.build({"Shop.dll": "v1", "Shop.runtimeconfig.json": "{}"})
        code, system = self.harbor()
        self.assertEqual(code, 1)
        self.assertIn("built here", system.said[-1])


if __name__ == "__main__":
    unittest.main()
