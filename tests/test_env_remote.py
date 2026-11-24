"""harbor env remote against remote_site.sh (frozen in tests/legacy). The script quoted the values it sent with bash's
printf %q and harbor quotes them for any POSIX shell, so what is compared is the words the server's shell gets."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_actions import legacy_script  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake, split_call  # noqa: E402

ACTIONS = ("status", "start", "stop", "restart", "logs", "check_service", "edit_service", "edit_conf", "shell", "files", "config")


def words(command):
    """The words a shell makes of a command line, quotes removed"""
    result = subprocess.run(["/bin/bash", "-c", f"for word in {command}; do printf '%s\\0' \"$word\"; done"],
                            capture_output=True, text=True, check=True)
    return result.stdout.split("\0")[:-1]


@NEEDS_BASH_4
class RemoteSiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.web = self.folder / "var/www"
        self.target = self.web / "shop.dk/scripts/targets/production"
        self.target.mkdir(parents=True)
        self.fake = Fake(self.folder, variables=("SSHPASS",))
        self.sent = self.folder / "sent"
        self.fake.program("ssh", f'cat >"{self.sent}"; exit "${{FAIL_SSH:-0}}"\n')
        self.fake.program("sshpass", 'shift; exec "$@"\n')
        self.fake.program("hostname", 'echo admin.here.dk\n')

    def settings(self, text=None):
        (self.target / "publish_prod.sh").write_text(text or (
            "PROD_LOCAL_PATH=''\nPROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='s3 cret'\nPROD_SSH_PORT='2222'\n"
            "PROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n"))

    def run_legacy(self, action, env=None):
        self.fake.log.write_text("")
        self.sent.unlink(missing_ok=True)
        # The script reads its helpers in lib/ from the folder it runs in: the real apache folder
        text = legacy_script("remote_site.sh", self.folder, self.web).replace("cat lib/", f"cat {ROOT}/lib/")
        result = subprocess.run(["/bin/bash", "-c", text, "remote_site.sh", "shop.dk", action], capture_output=True, text=True, input="",
                                env=self.fake.env() | {"VNS_TARGET": "production"} | (env or {}), cwd=ROOT)
        calls = [split_call(line) for line in self.fake.calls() if line.startswith("ssh ")]
        return result, calls, self.sent.read_text() if self.sent.exists() else None

    def run_harbor(self, action, code=0):
        os.environ["VNS_TARGET"] = "production"
        try:
            system = RecordingSystem(root=self.folder, answers={"sshpass": Result(code), "hostname -f": "admin.here.dk"})
            result = main(["env", "remote", "shop.dk", action], system)
        finally:
            del os.environ["VNS_TARGET"]
        return result, system

    def test_every_action_sends_the_same(self):
        self.settings()
        for action in ACTIONS:
            with self.subTest(action=action):
                result, calls, sent = self.run_legacy(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                code, system = self.run_harbor(action)
                self.assertEqual(code, 0, system.said)
                self.assertEqual(len(calls), 1)
                name, argv, variables = calls[0]
                # sshpass -e ssh <options> user@host, then the command the server runs, compared word by word
                index = next(i for i, line in enumerate(system.commands) if line.startswith("sshpass "))
                ssh, remote = system.commands[index].split(" deploy@10.0.0.5 ", 1)
                self.assertEqual(ssh.split(" "), ["sshpass", "-e", name, *argv[:-2]])
                self.assertEqual(argv[-2], "deploy@10.0.0.5")
                expected = words(argv[-1])
                if action in ("files", "config"):
                    # Unlike the script, the helper carries the names it needs: it has no file beside it there
                    from harbor.core.names import standalone
                    legacy = [word for word in expected if word.lstrip().startswith('"""')]
                    self.assertEqual(len(legacy), 1, expected)
                    expected = [standalone(word).rstrip("\n") if word in legacy else word for word in expected]
                self.assertEqual(words(remote), expected)
                self.assertEqual(system.environments[index], variables)
                # A script on stdin, or none (the fake ssh got an empty stdin from the script)
                self.assertEqual(system.inputs[index] or "", sent)

    def test_a_failure_where_it_is_keeps_its_code(self):
        self.settings()
        result, _, _ = self.run_legacy("restart", env={"FAIL_SSH": "4"})
        code, system = self.run_harbor("restart", 4)
        self.assertEqual((code, result.returncode), (4, 4))

    def test_what_is_refused(self):
        for text, action in ((None, "dance"), ("PROD_LOCAL_PATH='/var/www/x'\nPROD_FOLDER='x.dk'\nPROD_BINDING='x.dk'\n", "status"),
                             ("PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//h/s'\nPROD_FOLDER='x'\nPROD_BINDING='x.dk'\n", "status"),
                             ("PROD_IP='bad host'\nPROD_USER='u'\nPROD_FOLDER='x.dk'\nPROD_BINDING='x.dk'\n", "status"),
                             ("PROD_IP='h'\nPROD_USER='9u'\nPROD_FOLDER='x.dk'\nPROD_BINDING='x.dk'\n", "status"),
                             ("PROD_IP='h'\nPROD_USER='u'\nPROD_SSH_PORT='70000'\nPROD_FOLDER='x.dk'\nPROD_BINDING='x.dk'\n", "status")):
            with self.subTest(text=text, action=action):
                self.settings(text)
                result, calls, _ = self.run_legacy(action)
                code, system = self.run_harbor(action)
                self.assertEqual((code, system.said[-1]), (result.returncode, result.stderr.strip()))
                self.assertEqual(calls, [])
                self.assertEqual(system.commands, [])


if __name__ == "__main__":
    unittest.main()


class CustomScriptNameTests(unittest.TestCase):
    def test_a_script_is_a_file_in_the_scripts_folder(self):
        """An environment's own custom script is named, never a path or a hidden file (1.0.77)"""
        from harbor.environments.remote import SCRIPT_NAME, STATUS
        for name in ("cleanup.sh", "delete_empty_folders.sh", "a-b.1.sh"):
            self.assertTrue(SCRIPT_NAME.fullmatch(name), name)
        for name in ("../x.sh", "x/y.sh", ".hidden.sh", "x.py", "x.sh; rm -rf /", "", "a b.sh"):
            self.assertFalse(SCRIPT_NAME.fullmatch(name), name)
        self.assertIn('"custom_scripts"', STATUS, "the status lists its scripts")
