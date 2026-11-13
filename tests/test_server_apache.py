"""harbor server set-servername against the script it replaces (tests/legacy/set_servername.sh)"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402


def legacy(name, root, hostname="web.example.test"):
    """Run a frozen script with / at root, and the programs it calls recorded. Returns (exit code, output, commands)."""
    bin_folder = root / "bin"
    bin_folder.mkdir(exist_ok=True)
    log = root / "commands.log"
    log.write_text("")
    for program, output in (("hostname", hostname), ("a2enconf", "")):
        script = bin_folder / program
        script.write_text(f'#!/bin/bash\necho "{program} $*" >>"{log}"\n' + (f'echo "{output}"\n' if output else ""))
        script.chmod(0o755)
    text = (ROOT / "tests/legacy" / name).read_text().replace("/etc/", f"{root}/etc/")
    result = subprocess.run(["bash", "-c", text], capture_output=True, text=True,
                            env=os.environ | {"PATH": f"{bin_folder}:{os.environ['PATH']}"})
    return result.returncode, result.stdout, log.read_text().splitlines()


class SetServernameTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Path(folder.name) / "old"
        self.new = Path(folder.name) / "new"
        for root in (self.old, self.new):
            (root / "etc/apache2/conf-available").mkdir(parents=True)
            (root / "etc/apache2/conf-enabled").mkdir()
            (root / "etc/apache2/apache2.conf").write_text("# Global configuration\nTimeout 300\n")

    def files(self, root):
        return {str(path.relative_to(root)): path.read_text() for path in sorted((root / "etc").rglob("*")) if path.is_file()}

    def harbor(self, hostname="web.example.test"):
        system = RecordingSystem(root=self.new, answers={"hostname -f": hostname, "hostname": hostname})
        self.assertEqual(main(["server", "set-servername"], system), 0)
        return system

    def test_same_files_commands_and_output_as_the_script(self):
        code, output, commands = legacy("set_servername.sh", self.old)
        system = self.harbor()
        self.assertEqual(code, 0)
        self.assertEqual(self.files(self.new), self.files(self.old))
        self.assertEqual(system.commands, commands)
        self.assertEqual(system.said, output.splitlines())
        self.assertIn("ServerName web.example.test\n", (self.new / "etc/apache2/conf-available/vns-servername.conf").read_text())

    def test_a_servername_in_apache2_conf_is_left_alone_as_by_the_script(self):
        for root in (self.old, self.new):
            (root / "etc/apache2/apache2.conf").write_text("ServerName own.example.test\n")
        code, output, commands = legacy("set_servername.sh", self.old)
        system = self.harbor()
        self.assertEqual((code, output), (0, ""))
        self.assertEqual(system.said, [])
        self.assertEqual(self.files(self.new), self.files(self.old))
        # The script asks for nothing at all here; harbor neither
        self.assertEqual(system.commands, [])

    def test_an_odd_host_name_falls_back_like_the_script(self):
        for name in ("bad name!", ""):
            for root in (self.old, self.new):
                for path in (root / "etc/apache2/conf-available").glob("*"):
                    path.unlink()
            legacy("set_servername.sh", self.old, hostname=name)
            self.harbor(hostname=name)
            self.assertEqual(self.files(self.new), self.files(self.old), name)
            self.assertIn("ServerName localhost\n", (self.new / "etc/apache2/conf-available/vns-servername.conf").read_text())

    def test_one_set_by_an_enabled_config_is_seen(self):
        """The one difference, on purpose: everything in conf-enabled is a link, which grep -r does not follow, so the
        script wrote the file again on every run. Its comment says nothing changes when one is set, as harbor does."""
        available = self.new / "etc/apache2/conf-available/vns-servername.conf"
        available.write_text("ServerName set.example.test\n")
        (self.new / "etc/apache2/conf-enabled/vns-servername.conf").symlink_to("../conf-available/vns-servername.conf")
        system = self.harbor()
        self.assertEqual(system.commands, [])
        self.assertEqual(available.read_text(), "ServerName set.example.test\n")


if __name__ == "__main__":
    unittest.main()
