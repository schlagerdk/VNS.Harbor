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


if __name__ == "__main__":
    unittest.main()
