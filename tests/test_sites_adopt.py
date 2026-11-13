"""harbor site adopt / site release / env carry-snippets against adopt_site.sh and carry_snippets.sh (frozen in tests/legacy)"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.paths import BIN  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_actions import legacy_script  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake, split_call  # noqa: E402

SHOWN = ("SSHPASS", "R_APACHE_DIR", "VNS_PROD_MODAL", "VNS_PROD_NO_PUBLISH", "VNS_PROD_TARGET", "VNS_PROD_LOCAL_PATH", "VNS_PROD_FOLDER",
         "VNS_PROD_BINDING", "VNS_PROD_ENVIRONMENT", "VNS_PROD_HTTPS")
SNIPPET = '# Redirect to slash\n# vns {"key":"slash","values":{}}\n'


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.web = self.folder / "var/www"
        for path in ("var/www/shop.dk/scripts", "etc/apache2/sites-available", "etc/systemd/system"):
            (self.folder / path).mkdir(parents=True)
        (self.web / "shop.dk/Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n')
        self.conf("shop.dk", "test.shop.dk")
        (self.folder / "etc/systemd/system/shop.dk.service").write_text("[Service]\nWorkingDirectory=/var/www/shop.dk/_publish\n")
        self.fake = Fake(self.folder, variables=SHOWN)
        self.created = self.folder / "created.sh"
        self.sent = self.folder / "sent"
        self.fake.program("bash", f'cp "$1" "{self.created}"; exit "${{FAIL_CREATE:-0}}"\n')
        self.fake.program("ssh", f'cat >"{self.sent}"; exit "${{FAIL_SSH:-0}}"\n')
        self.fake.program("sshpass", 'shift; exec "$@"\n')
        self.fake.program("sudo", 'exec "$@"\n')

    def conf(self, name, binding, extra=""):
        (self.folder / f"etc/apache2/sites-available/{name}.conf").write_text(f"<VirtualHost *:80>\n  ServerName {binding}\n{extra}</VirtualHost>\n")

    def legacy(self, script, *args, env=None):
        self.fake.log.write_text("")
        for path in (self.created, self.sent):
            path.unlink(missing_ok=True)
        text = legacy_script(script, self.folder, self.web).replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
        # The main site's config here (what is sent to the server keeps its own paths)
        text = text.replace('_mainConf="/etc/', f'_mainConf="{self.folder}/etc/')
        # The script sees the sites under the test's folder: the paths in services and settings point there while it runs
        fixtures = [*self.folder.glob("etc/systemd/system/*.service"), *self.web.rglob("publish_prod.sh")]
        saved = {path: path.read_text() for path in fixtures}
        for path, content in saved.items():
            path.write_text(content.replace("=/var/www", f"={self.web}").replace("'/var/www", f"'{self.web}"))
        try:
            result = subprocess.run(["/bin/bash", "-c", text, script, *args], capture_output=True, text=True, input="",
                                    env=self.fake.env() | (env or {}), cwd=ROOT)
        finally:
            for path, content in saved.items():
                if path.exists():
                    path.write_text(content)
        result.stdout = result.stdout.replace(str(self.folder), "")
        result.stderr = result.stderr.replace(str(self.folder), "")
        created = self.created.read_text().replace(str(self.folder), "") if self.created.exists() else None
        calls = [split_call(line) for line in self.fake.calls() if line.startswith(("ssh ", "bash "))]
        return result, created, calls, self.sent.read_text() if self.sent.exists() else None

    def harbor(self, argv, answers=None, target=None):
        created = {}

        def create_environment(argv):
            created["argv"] = argv
            return Result(int(os.environ.get("FAIL_CREATE", "0")))
        saved = dict(os.environ)
        if target:
            os.environ["VNS_TARGET"] = target
        try:
            system = RecordingSystem(root=self.folder, answers={f"python3 {BIN} env create": create_environment, **(answers or {})})
            code = main(argv, system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, system, created.get("argv")

    def files(self):
        return {str(path.relative_to(self.folder)): path.read_bytes() for path in sorted(self.web.rglob("*")) if path.is_file()}


@NEEDS_BASH_4
class AdoptTests(Base):
    def published(self, name="shop-prod.dk", dll="Shop", working=None, environment="Staging", https=True):
        (self.web / name).mkdir(exist_ok=True)
        (self.folder / f"etc/systemd/system/{name}.service").write_text(
            f"[Service]\nWorkingDirectory={working or '/var/www/' + name}\nExecStart=/usr/lib/dotnet/dotnet /var/www/{name}/{dll}.dll\n"
            f"Environment=ASPNETCORE_ENVIRONMENT={environment}\n")
        self.conf(name, "shop.dk", "  SSLEngine On\n" if https else "")

    def compare(self, *args):
        result, created, calls, _ = self.legacy("adopt_site.sh", *args)
        code, system, harbor_created = self.harbor(["site", "adopt", *args])
        self.assertEqual(code, result.returncode, (result.stderr, system.said))
        # The script ran create_prod.sh rendered with the site's values; harbor runs harbor env create with them
        self.assertEqual(harbor_created is None, created is None)
        if created:
            values = harbor_created[harbor_created.index("create") + 1:]
            for option in ("--site-root", "--binding", "--project", "--publish-folder", "--admin-mail", "--kind"):
                self.assertIn(values[values.index(option) + 1].replace(str(self.folder), ""), created, option)
        bash = [env for command, env in zip(system.commands, system.environments) if command.startswith(f"python3 {BIN} env create")]
        self.assertEqual(bash, [{key: value.replace(str(self.folder), "") for key, value in variables.items() if key not in ("SSHPASS", "R_APACHE_DIR")}
                                for name, _, variables in calls if name == "bash"])
        if result.returncode:
            self.assertEqual(system.said[-1], result.stderr.rstrip("\n"))
        return result, system

    def test_a_published_site_is_adopted(self):
        for working, environment, https in ((None, "Staging", True), ("/var/www/shop-prod.dk/_publish/", "Live", False)):
            with self.subTest(working=working):
                self.published(working=working, environment=environment, https=https)
                result, system = self.compare("shop-prod.dk", "shop.dk")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_what_is_refused(self):
        cases = {
            "itself": lambda: None,
            "missing": lambda: None,
            "project": lambda: (self.published(), (self.web / "shop-prod.dk/Other.csproj").write_text("<Project/>")),
            "environments": lambda: (self.published(), (self.web / "shop-prod.dk/scripts/targets/x").mkdir(parents=True),
                                     (self.web / "shop-prod.dk/scripts/targets/x/publish_prod.sh").write_text("PROD_FOLDER=a\nPROD_BINDING=a.dk\n")),
            "no service": lambda: ((self.web / "shop-prod.dk").mkdir(), self.conf("shop-prod.dk", "shop.dk")),
            "outside": lambda: self.published(working="/srv/shop"),
            "other dll": lambda: self.published(dll="Other"),
        }
        for case, make in cases.items():
            with self.subTest(case=case):
                self.setUp()
                make()
                result, system = self.compare("shop.dk" if case == "itself" else "shop-prod.dk", "shop.dk")
                self.assertEqual(result.returncode, 1)

    def test_a_failed_setup_keeps_its_code(self):
        self.published()
        os.environ["FAIL_CREATE"] = "3"
        try:
            result, created, _, _ = self.legacy("adopt_site.sh", "shop-prod.dk", "shop.dk", env={"FAIL_CREATE": "3"})
            code, system, _ = self.harbor(["site", "adopt", "shop-prod.dk", "shop.dk"])
        finally:
            del os.environ["FAIL_CREATE"]
        self.assertEqual((code, result.returncode), (3, 3))


@NEEDS_BASH_4
class ReleaseTests(Base):
    def environment(self, target, text):
        folder = self.web / f"shop.dk/scripts/targets/{target}"
        folder.mkdir(parents=True)
        (folder / "publish_prod.sh").write_text(text)
        (self.web / "shop.dk/.vns-publish").mkdir(exist_ok=True)
        (self.web / f"shop.dk/.vns-publish/target-{target}.json").write_text("{}")
        (self.web / "shop.dk/scripts/publish_local.sh").write_text("#!/bin/bash\n")

    def compare(self, target, text, env=None):
        self.environment(target, text)
        result, _, calls, sent = self.legacy("adopt_site.sh", "--release", "shop.dk", target, env=env)
        legacy_files = self.files()
        subprocess.run(["rm", "-rf", str(self.web / "shop.dk/scripts"), str(self.web / "shop.dk/.vns-publish")], check=True)
        (self.web / "shop.dk/scripts").mkdir()
        self.environment(target, text)
        answers = {"sshpass": Result(int((env or {}).get("FAIL_SSH", "0")))}
        code, system, _ = self.harbor(["site", "release", "shop.dk", target], answers=answers)
        self.assertEqual(code, result.returncode, (result.stderr, system.said))
        self.assertEqual(self.files(), legacy_files)
        self.assertEqual(system.said[-1], (result.stderr or result.stdout).rstrip("\n"))
        return result, system, calls

    def test_a_local_environment_in_its_own_folder(self):
        result, system, _ = self.compare("staging", "PROD_LOCAL_PATH='/var/www/shop-staging.dk'\nPROD_FOLDER='shop-staging.dk'\nPROD_BINDING='s.shop.dk'\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.web / "shop.dk/scripts/publish.sh").is_file())

    def test_a_remote_environment_loses_its_note(self):
        text = "PROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='pw'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n"
        result, system, calls = self.compare("production", text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0][1][-1].split(" ", 1)[1], system.commands[0].split(" deploy@10.0.0.5 ", 1)[1].split(" ", 1)[1])
        self.setUp()
        result, system, _ = self.compare("production", text, env={"FAIL_SSH": "255"})
        self.assertEqual(result.returncode, 1)

    def test_what_is_refused(self):
        for target, text in (("staging", "PROD_LOCAL_PATH='/var/www/shop.dk/_staging/'\nPROD_FOLDER='s'\nPROD_BINDING='s.dk'\n"),
                             ("iis", "PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//h/s'\nPROD_FOLDER='s'\nPROD_BINDING='s.dk'\n")):
            with self.subTest(target=target):
                self.setUp()
                result, system, _ = self.compare(target, text)
                self.assertEqual(result.returncode, 1)
        result = self.legacy("adopt_site.sh", "--release", "shop.dk")[0]
        code, system, _ = self.harbor(["site", "release", "shop.dk"])
        self.assertEqual((code, system.said[-1]), (result.returncode, result.stderr.strip()))


@NEEDS_BASH_4
class CarrySnippetsTests(Base):
    def test_the_same_snippets_and_helper_are_sent(self):
        self.conf("shop.dk", "test.shop.dk", SNIPPET)
        folder = self.web / "shop.dk/scripts/targets/production"
        folder.mkdir(parents=True)
        (folder / "publish_prod.sh").write_text("PROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='pw'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n")
        result, _, calls, sent = self.legacy("carry_snippets.sh", "shop.dk", env={"VNS_TARGET": "production"})
        self.assertEqual(result.returncode, 0, result.stderr)

        def helper(argv):
            return Result(0, subprocess.run(["python3", *argv[1:]], capture_output=True, text=True, check=True).stdout)
        code, system, _ = self.harbor(["env", "carry-snippets", "shop.dk"], answers={"python3": helper}, target="production")
        self.assertEqual(code, 0, system.said)
        ssh = next(command for command in system.commands if command.startswith("sshpass "))
        self.assertEqual(ssh.split(" deploy@10.0.0.5 ", 1)[1], calls[0][1][-1])
        self.assertEqual(system.inputs[system.commands.index(ssh)], sent)
        snippets = calls[0][1][-1].split("SNIPPETS=", 1)[1].split(" ", 1)[0]
        self.assertEqual(json.loads(base64.b64decode(snippets))[0]["key"], "slash")

    def test_nothing_for_windows_or_without_a_main_config(self):
        folder = self.web / "shop.dk/scripts/targets/iis"
        folder.mkdir(parents=True)
        (folder / "publish_prod.sh").write_text("PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//h/s'\nPROD_FOLDER='s'\nPROD_BINDING='s.dk'\n")
        code, system, _ = self.harbor(["env", "carry-snippets", "shop.dk"], target="iis")
        self.assertEqual((code, system.commands), (0, []))


if __name__ == "__main__":
    unittest.main()
