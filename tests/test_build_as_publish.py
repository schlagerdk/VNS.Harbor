import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import build_as  # noqa: E402


def write(root, *paths):
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)


def build(root, *paths):
    if root.exists():
        for directory, folders, files in os.walk(root, topdown=False):
            for name in files:
                os.unlink(os.path.join(directory, name))
            for name in folders:
                os.rmdir(os.path.join(directory, name))
    root.mkdir(parents=True, exist_ok=True)
    write(root, *paths)


class RemovedPackageTests(unittest.TestCase):
    """A package removed from the project and the site published again into the same folder: its DLL stayed there, and
    Umbraco, which loads every DLL in the folder, stopped with FileNotFoundException (dev.vns.dk without VNS.Harbor)"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.project = self.root / "site"
        self.project.mkdir()
        self.staging = self.root / "stage"
        self.target = self.project / "_publish"
        self.site = ["site.dll", "site.deps.json", "Umbraco.Core.dll", "wwwroot/App_Plugins/uSync/umbraco-package.json"]
        self.package = ["VNS.Harbor.dll", "wwwroot/App_Plugins/VNS.Harbor/umbraco-package.json",
                        "wwwroot/App_Plugins/VNS.Harbor/dashboard.js"]
        # The site's own, never in a build
        self.data = ["appsettings.Production.json", "wwwroot/media/1/a.jpg", "umbraco/Data/Umbraco.sqlite.db", "uSync/v18/x.config"]

    def publish(self, *paths):
        build(self.staging, *paths)
        build_as.copy_out(self.staging, self.target, self.project)

    def test_the_removed_package_is_deleted_and_the_site_keeps_its_data(self):
        self.publish(*self.site, *self.package)
        write(self.target, *self.data)
        self.publish(*self.site)
        for path in self.package:
            self.assertFalse((self.target / path).exists(), path)
        self.assertFalse((self.target / "wwwroot/App_Plugins/VNS.Harbor").exists())
        for path in self.site + self.data:
            self.assertTrue((self.target / path).is_file(), path)

    def test_a_folder_published_before_harbor_kept_a_record_loses_only_dlls_and_app_plugins(self):
        write(self.target, *self.site, *self.package, *self.data, "site.pdb")
        self.publish(*self.site)
        for path in self.package:
            self.assertFalse((self.target / path).exists(), path)
        for path in self.site + self.data + ["site.pdb"]:
            self.assertTrue((self.target / path).is_file(), path)
        self.assertTrue((self.target / build_as.PUBLISHED).is_file())

    def test_the_project_folder_as_publish_folder_is_never_deleted_from(self):
        self.target = self.project
        write(self.project, "Old.dll", "wwwroot/App_Plugins/Mine/umbraco-package.json")
        self.publish(*self.site)
        self.assertTrue((self.project / "Old.dll").is_file())
        self.assertTrue((self.project / "wwwroot/App_Plugins/Mine/umbraco-package.json").is_file())

    def test_a_record_cannot_reach_outside_the_publish_folder(self):
        outside = self.root / "outside.dll"
        outside.write_text("x")
        self.target.mkdir()
        (self.target / build_as.PUBLISHED).write_text("../../outside.dll\n/etc/passwd\n")
        self.publish(*self.site)
        self.assertTrue(outside.is_file())


if __name__ == "__main__":
    unittest.main()
