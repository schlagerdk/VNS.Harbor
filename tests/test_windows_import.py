"""harbor windows: sites fetched from a live Windows server, kept the same, then taken over"""
import fnmatch
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.sites import windows_import  # noqa: E402

UNC = "//windows.example/d$/sites"


class FakeShare:
    """rclone, as far as harbor uses it, over a folder that stands for the Windows share"""

    def __init__(self, folder):
        self.folder = folder
        self.calls = []

    def path(self, target):
        assert target.startswith(":smb:d$/sites"), target
        return self.folder / target[len(":smb:d$/sites"):].lstrip("/")

    @staticmethod
    def matches(relative, patterns):
        return any(fnmatch.fnmatch("/" + relative, pattern.replace("**", "*")) or fnmatch.fnmatch(Path(relative).name, pattern) for pattern in patterns)

    def __call__(self, argv, stdin=None):
        args = list(argv[1:])
        self.calls.append(list(args))
        command = args.pop(0)
        if command == "obscure":
            return Result(0, "obscured\n")
        options = {name: [args[i + 1] for i, item in enumerate(args[:-1]) if item == name] for name in ("--include", "--exclude")}
        plain = [item for i, item in enumerate(args) if not item.startswith("--") and (i == 0 or args[i - 1] not in ("--include", "--exclude", "--max-depth", "--stats", "--modify-window"))]
        if command == "lsf":
            folder = self.path(plain[0])
            if not folder.is_dir():
                return Result(3, "", "directory not found")
            names = []
            for item in sorted(folder.iterdir()):
                if "--dirs-only" in args and not item.is_dir() or "--files-only" in args and not item.is_file():
                    continue
                if options["--include"] and not self.matches(item.name, options["--include"]):
                    continue
                names.append(item.name + ("/" if item.is_dir() else ""))
            return Result(0, "".join(name + "\n" for name in names))
        if command == "cat":
            return Result(0, self.path(plain[0]).read_text())
        if command == "copy":
            source, target = self.path(plain[0]), Path(plain[1])
            for item in sorted(source.rglob("*")):
                relative = str(item.relative_to(source))
                if options["--exclude"] and self.matches(relative, options["--exclude"]):
                    continue
                if options["--include"] and not self.matches(relative, options["--include"]):
                    continue
                if item.is_file():
                    (target / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target / relative)
            return Result(0)
        if command == "copyto":
            target = self.path(plain[1])
            target.write_text(Path(plain[0]).read_text())
            return Result(0)
        if command == "deletefile":
            self.path(plain[0]).unlink()
            return Result(0)
        return Result(1, "", f"unknown {command}")


class WindowsImportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "server"
        self.windows = Path(folder.name) / "windows"
        for name, text in (("shop.dk/Shop.csproj", '<Project><ItemGroup><PackageReference Include="Umbraco.Cms" Version="17.4.0" /></ItemGroup></Project>'),
                           ("shop.dk/Program.cs", "app"), ("shop.dk/Shop.dll", "build"), ("shop.dk/bin/Release/x.dll", "build"),
                           ("shop.dk/web.config", "<iis/>"), ("shop.dk/umbraco/Data/Umbraco.sqlite.db", "live data"),
                           ("shop.dk/umbraco/Data/Umbraco.sqlite.db-wal", "journal"), ("shop.dk/umbraco/Logs/x.json", "log"),
                           ("shop.dk/wwwroot/media/a1/logo.png", "png"), ("shop.dk/uSync/v17/Content/home.config", "<home/>"),
                           ("old.dk/Old.dll", "umbraco 8"), ("old.dk/web.config", "<iis/>"),
                           ("classic.dk/Global.asax", "<%@ Application %>"), ("classic.dk/web.config", "<iis/>"), ("classic.dk/bin/umbraco.dll", "7"),
                           ("backup/notes.txt", "old"),
                           ("built.dk/Built.dll", "build"), ("built.dk/Built.runtimeconfig.json", "{}"), ("built.dk/web.config", "<iis/>"),
                           ("built.dk/Built.deps.json", '{"runtimeTarget": {"name": ".NETCoreApp,Version=v10.0/win-x64"}, "libraries": {"Umbraco.Cms/17.4.0": {}}}'),
                           ("built.dk/appsettings.json", "{}"), ("built.dk/Views/Home.cshtml", "<h1/>"), ("built.dk/umbraco/Data/Umbraco.sqlite.db", "built data"),
                           ("built.dk/umbraco/Logs/log.json", "log"),
                           ("cdn.dk/index.html", "<h1>cdn</h1>"), ("cdn.dk/img/a.png", "png"), ("cdn.dk/web.config", "<iis/>")):
            (self.windows / name).parent.mkdir(parents=True, exist_ok=True)
            (self.windows / name).write_text(text)
        self.share = FakeShare(self.windows)
        patcher = mock.patch.dict(os.environ, {"VNS_WEB_ROOT": "/var/www", "VNS_SMB_PASSWORD": "pw"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setups = []

        def setup(system, name, binding, certificate, environment):
            self.setups.append((name, binding, certificate, environment))
            if (system.path(f"/var/www/{name}") / "index.html").exists():
                return
            if any(system.path(f"/var/www/{name}").glob("*.runtimeconfig.json")):
                # A build runs as it is, from its own folder
                system.path(f"/etc/systemd/system/{name}.service").parent.mkdir(parents=True, exist_ok=True)
                system.path(f"/etc/systemd/system/{name}.service").write_text(f"WorkingDirectory=/var/www/{name}\n")
                return
            # As harbor site setup would: a service that runs from _publish
            system.path(f"/var/www/{name}/_publish").mkdir(parents=True, exist_ok=True)
            system.path(f"/etc/systemd/system/{name}.service").parent.mkdir(parents=True, exist_ok=True)
            system.path(f"/etc/systemd/system/{name}.service").write_text(f"WorkingDirectory=/var/www/{name}/_publish\n")
        patcher = mock.patch("harbor.sites.setup.setup_site", side_effect=setup)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rsync(self, argv, stdin=None):
        """rsync -a with --exclude, between two folders on this server, as copy_project uses it"""
        args = list(argv[1:])
        excludes = [args[i + 1] for i, item in enumerate(args[:-1]) if item == "--exclude"]
        source, target = Path(args[-2]), Path(args[-1])
        for item in sorted(source.rglob("*")):
            relative = str(item.relative_to(source))
            parts = relative.split("/")
            if any(FakeShare.matches(relative, [pattern.rstrip("/")]) or any(fnmatch.fnmatch("/" + part + "/", pattern) and index == 0 for index, part in enumerate(parts[:-1] if item.is_file() else parts))
                   or "/".join(parts[:2]) + "/" in [pattern.lstrip("/") for pattern in excludes] for pattern in excludes):
                continue
            if item.is_file():
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target / relative)
        return Result(0)

    def harbor(self, *argv):
        system = RecordingSystem(root=self.root, answers={"rclone": self.share, "rsync": self.rsync})
        code = main(["windows", *argv], system)
        return code, system

    def test_list_tells_what_each_folder_holds(self):
        system = RecordingSystem(root=self.root, answers={"rclone": self.share})
        found = {item["folder"]: item for item in windows_import.list_folders(system, UNC, "admin", "pw")}
        self.assertEqual((found["shop.dk"]["kind"], found["shop.dk"]["project"], found["shop.dk"]["umbraco"]), ("project", "Shop", "17.4.0"))
        self.assertEqual(found["old.dk"]["kind"], "framework", "dlls without a runtimeconfig: .NET Framework")
        self.assertEqual((found["built.dk"]["kind"], found["built.dk"]["umbraco"], found["built.dk"]["windows_only"]), ("build", "17.4.0", "win-x64"))
        self.assertEqual(found["cdn.dk"]["kind"], "html")
        # Umbraco 7 has its dlls in bin, not at the top: it is not static files
        self.assertEqual(found["classic.dk"]["kind"], "framework")
        self.assertEqual(found["backup"]["kind"], "other", "no start page: not a site")

    def test_a_project_is_imported_without_its_builds_and_with_its_data(self):
        with mock.patch("harbor.sites.bindings.change_binding") as binding:
            code, system = self.harbor("import", UNC, "shop.dk", "shop.dk", "--user", "admin", "--aliases", "shop.dk.new.example")
        self.assertEqual(code, 0, system.said)
        self.assertEqual(binding.call_args[0][1:4], ("shop.dk", "shop.dk", "shop.dk.new.example"), "the temporary alias, as text")
        site = self.root / "var/www/shop.dk"
        self.assertTrue((site / "Program.cs").is_file())
        for left_out in ("Shop.dll", "bin", "web.config", "umbraco/Logs"):
            self.assertFalse((site / left_out).exists(), left_out)
        self.assertEqual(self.setups, [("shop.dk", "shop.dk", "n", "Production")], "HTTP, as Production")
        # The data where the site runs
        self.assertEqual((site / "_publish/umbraco/Data/Umbraco.sqlite.db").read_text(), "live data")
        self.assertTrue((site / "_publish/wwwroot/media/a1/logo.png").is_file())
        self.assertTrue((site / "_publish/uSync/v17/Content/home.config").is_file())
        # Where it came from, the password apart
        source = json.loads((self.root / "var/lib/harbor/sites/shop.dk/windows-source.json").read_text())
        self.assertEqual((source["unc"], source["folder"], source["kind"]), (UNC, "shop.dk", "project"))
        self.assertNotIn("pw", json.dumps(source))
        self.assertEqual(json.loads((self.root / "etc/harbor/secrets/sites/shop.dk/windows-source.json").read_text())["password"], "pw")

    def test_a_classic_aspnet_site_is_not_imported(self):
        code, system = self.harbor("import", UNC, "old.dk", "old.dk", "--user", "admin")
        self.assertEqual(code, 1)
        self.assertIn("Windows only", system.said[-1])
        self.assertFalse((self.root / "var/www/old.dk").exists())

    def test_a_build_is_built_here_from_its_project_without_windows_dlls(self):
        # The project lives in a site here (test.built.dk), with its own build, environments and data
        project = self.root / "var/www/test.built.dk"
        for name, text in (("Built.csproj", "<Project/>"), ("Program.cs", "app"), ("bin/Debug/Built.dll", "linux"), ("_publish/Built.dll", "dev"),
                           ("_production/x", "env"), ("umbraco/Data/Umbraco.sqlite.db", "dev data"), ("appsettings.json", '{"dev": true}')):
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_text(text)
        code, system = self.harbor("import", UNC, "built.dk", "built.dk", "--user", "admin")
        self.assertEqual(code, 1, "a build needs the project it is built from")
        self.assertIn("Build from", system.said[-1])
        code, system = self.harbor("import", UNC, "built.dk", "built.dk", "--user", "admin", "--build-from", "test.built.dk")
        self.assertEqual(code, 0, system.said)
        site = self.root / "var/www/built.dk"
        # The project's source, Windows' settings and files on top
        for kept in ("Built.csproj", "Program.cs", "Views/Home.cshtml"):
            self.assertTrue((site / kept).is_file(), kept)
        self.assertEqual((site / "appsettings.json").read_text(), "{}", "the live site's settings")
        # No binaries from Windows, nor the project's builds, environments and data
        for left_out in ("Built.dll", "Built.runtimeconfig.json", "Built.deps.json", "web.config", "umbraco/Logs", "bin", "_production"):
            self.assertFalse((site / left_out).exists(), left_out)
        self.assertEqual(self.setups[-1], ("built.dk", "built.dk", "n", "Production"))
        # The live data where it runs
        self.assertEqual((site / "_publish/umbraco/Data/Umbraco.sqlite.db").read_text(), "built data")
        self.assertEqual(json.loads((self.root / "var/lib/harbor/sites/built.dk/windows-source.json").read_text())["kind"], "build")

    def test_an_html_site_is_copied_as_it_is(self):
        code, system = self.harbor("import", UNC, "cdn.dk", "cdn.dk", "--user", "admin")
        self.assertEqual(code, 0, system.said)
        site = self.root / "var/www/cdn.dk"
        self.assertTrue((site / "index.html").is_file() and (site / "img/a.png").is_file())
        self.assertFalse((site / "web.config").exists())
        (self.windows / "cdn.dk/img/b.png").write_text("new")
        code, system = self.harbor("refresh", "cdn.dk")
        self.assertEqual(code, 0, system.said)
        self.assertTrue((site / "img/b.png").is_file())

    def test_refresh_keeps_the_data_the_same_as_the_live_site(self):
        self.harbor("import", UNC, "shop.dk", "shop.dk", "--user", "admin")
        published = self.root / "var/www/shop.dk/_publish"
        (self.windows / "shop.dk/umbraco/Data/Umbraco.sqlite.db").write_text("newer data")
        (self.windows / "shop.dk/umbraco/Data/Umbraco.sqlite.db-wal").unlink()
        code, system = self.harbor("refresh", "shop.dk")
        self.assertEqual(code, 0, system.said)
        self.assertEqual((published / "umbraco/Data/Umbraco.sqlite.db").read_text(), "newer data")
        self.assertFalse((published / "umbraco/Data/Umbraco.sqlite.db-wal").exists(), "a journal gone on Windows is not applied here")
        # The site here is stopped while its database is replaced; the one on Windows is not touched
        self.assertLess(system.commands.index("systemctl stop shop.dk"), system.commands.index("systemctl start shop.dk"))
        self.assertFalse(any(call[0] == "copyto" for call in self.share.calls))

    def test_refresh_with_pause_takes_the_live_site_offline_while_copying(self):
        self.harbor("import", UNC, "shop.dk", "shop.dk", "--user", "admin")
        self.share.calls.clear()
        code, system = self.harbor("refresh", "shop.dk", "--pause")
        self.assertEqual(code, 0, system.said)
        offline = next(index for index, call in enumerate(self.share.calls) if call[0] == "copyto" and call[-1].endswith("app_offline.htm"))
        online = next(index for index, call in enumerate(self.share.calls) if call[0] == "deletefile")
        database = next(index for index, call in enumerate(self.share.calls) if call[0] == "copy" and "umbraco/Data" in call[1])
        self.assertLess(offline, database)
        self.assertLess(database, online)
        self.assertFalse((self.windows / "shop.dk/app_offline.htm").exists(), "online again")

    def test_take_over_removes_the_aliases_issues_a_certificate_and_forgets_windows(self):
        self.harbor("import", UNC, "cdn.dk", "cdn.dk", "--user", "admin")
        with mock.patch("harbor.sites.bindings.change_binding") as binding, mock.patch("harbor.sites.actions.issue_certificate") as certificate:
            code, system = self.harbor("take-over", "cdn.dk")
        self.assertEqual(code, 0, system.said)
        binding.assert_called_once()
        self.assertEqual(binding.call_args[0][3], "", "no aliases")
        certificate.assert_called_once()
        self.assertFalse((self.root / "var/lib/harbor/sites/cdn.dk/windows-source.json").exists())
        self.assertFalse((self.root / "etc/harbor/secrets/sites/cdn.dk/windows-source.json").exists())
        code, system = self.harbor("refresh", "cdn.dk")
        self.assertEqual(code, 1, "taken over: nothing more is fetched")


if __name__ == "__main__":
    unittest.main()
