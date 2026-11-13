import ast
import os
import re
import glob
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "solutionfiles/admin/app/app.py"


def load(*names, **extra):
    nodes = [node for node in ast.parse(APP.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in names]
    ns = dict(os=os, glob=glob, re=re, read_text=lambda path: Path(path).read_text() if Path(path).is_file() else "", **extra)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "setup site", "exec"), ns)
    return ns


class SetupSiteTests(unittest.TestCase):
    def test_what_a_folder_holds(self):
        setup_kind = load("setup_kind")["setup_kind"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(setup_kind(str(root)), "html")
            (root / "_publish").mkdir()
            (root / "_publish/Account.dll").touch()
            (root / "_publish/Account.runtimeconfig.json").write_text("{}")
            self.assertEqual(setup_kind(str(root)), "C# build")
            (root / "_publish/Account.deps.json").write_text('{"libraries":{"Umbraco.Cms/18.3.1":{}}}')
            self.assertEqual(setup_kind(str(root)), "umbraco build")
            (root / "Account.csproj").write_text('<Project><PackageReference Include="Umbraco.Cms" /></Project>')
            self.assertEqual(setup_kind(str(root)), "umbraco project", "A project comes before its build")

    def test_the_script_is_syntactically_valid_and_never_overwrites_scripts(self):
        script = (ROOT.parent / "tests/legacy/setup_site.sh").read_text()  # as it was; harbor is compared with it in tests/
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "setup_site.sh")]).returncode, 0)
        self.assertIn("[[ -f ${_file} && ! -e ${_siteRoot}/scripts/$(basename \"${_file}\") ]] || continue", script)
        self.assertIn("is already set up: it has a service", script)
        self.assertIn("has an Apache config but no service: the setup is finished", script)
        self.assertIn("already holds a build: it is used as it is", script)

    def test_the_admin_marks_folders_that_are_not_set_up(self):
        source = APP.read_text()
        self.assertIn('"not_set_up": not_set_up,', source)
        self.assertIn('[bash, os.path.join(APACHE_DIR, "setup_site.sh"), site, binding, certificate, environment]', source)
        page = (ROOT / "solutionfiles/admin/app/templates/index.html").read_text()
        self.assertIn('id="dlg-setup-site"', page)


if __name__ == "__main__":
    unittest.main()
