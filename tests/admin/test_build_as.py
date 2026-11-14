"""lib/build_as.py: the build user may write what a build writes in the project folder"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))

import build_as  # noqa: E402


@unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "Needs the ACL tools, as on the servers")
class GroupMayWriteTests(unittest.TestCase):
    def test_the_groups_own_entry_when_the_folder_has_an_acl(self):
        """As test-01 had it: a site folder closed to others before it was built (group::r-x, mask rwx)"""
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["chmod", "750", folder], check=True)
            subprocess.run(["setfacl", "-m", f"u:{subprocess.check_output(['id', '-un'], text=True).strip()}:rwx", folder], check=True)
            subprocess.run(["chmod", "g+w", folder], check=True)
            entry = lambda: next(line for line in subprocess.check_output(["getfacl", "-p", folder], text=True).splitlines()  # noqa: E731
                                 if line.startswith("group::"))
            self.assertEqual(entry(), "group::r-x", "chmod changed only the mask")
            build_as.group_may_write(Path(folder))
            self.assertEqual(entry(), "group::rwx")

    def test_prepare_does_it(self):
        with mock.patch.object(build_as, "group_may_write") as allowed:
            source = Path(build_as.__file__).read_text()
            self.assertIn("    group_may_write(project)\n", source[source.index("def prepare("):])
            self.assertFalse(allowed.called)


class FreshPackageListsTests(unittest.TestCase):
    """A version uploaded to nuget.org minutes ago is not in NuGet's cached lists (NU1102 on test-01)"""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.project = self.root / "site"
        self.home = self.root / "home"
        self.project.mkdir()
        (self.project / "site.csproj").write_text("""<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>
            <PackageReference Include="Umbraco.Cms" Version="18.3.1" />
            <PackageReference Include="VNS.Harbor" Version="18.3.1.4" />
            <PackageReference Include="Floating" Version="18.*" />
            <PackageReference Include="Ranged" Version="[18.3.1, 19.0.0)" />
        </ItemGroup></Project>""")
        (self.home / ".nuget/packages/umbraco.cms/18.3.1").mkdir(parents=True)
        self.cache = self.home / ".local/share/NuGet/http-cache"
        self.cache.mkdir(parents=True)

    def test_a_version_not_there_yet_reads_the_lists_anew(self):
        self.assertEqual(build_as.fresh_package_lists(self.project, self.home), ["VNS.Harbor 18.3.1.4"])
        self.assertFalse(self.cache.exists())

    def test_the_lists_stay_when_every_version_is_there(self):
        (self.home / ".nuget/packages/vns.harbor/18.3.1.4").mkdir(parents=True)
        self.assertEqual(build_as.fresh_package_lists(self.project, self.home), [])
        self.assertTrue(self.cache.is_dir())


if __name__ == "__main__":
    unittest.main()
