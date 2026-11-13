"""harbor server install-apache-conf / install-tls-policy and harbor site fix-permissions against the scripts they replace"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402

LEGACY = ROOT / "tests/legacy"
APACHE = ROOT / "apache"
PROGRAMS = ("a2enmod", "a2enconf", "a2disconf", "a2dismod", "a2query", "apache2ctl", "systemctl")


class Server:
    """/etc and /var/lib under a folder, and the Apache programs recorded. FAIL_<PROGRAM>=1 makes one fail;
    ACTIVE=1 makes Apache running."""

    def __init__(self, folder, name, apache=True):
        self.root = Path(folder) / name
        if apache:
            for path in ("etc/apache2/conf-available", "etc/apache2/conf-enabled"):
                (self.root / path).mkdir(parents=True)
        self.bin = Path(folder) / f"{name}-bin"
        self.bin.mkdir()
        self.log = Path(folder) / f"{name}.log"
        self.log.write_text("")
        for program in PROGRAMS:
            variable = "FAIL_" + program.upper().replace("2", "_")
            extra = 'if [[ $1 == is-active ]]; then exit $(( ${ACTIVE:-0} ? 0 : 3 )); fi\n' if program == "systemctl" else ""
            (self.bin / program).write_text(f'#!/bin/bash\necho "{program} $*" >>"{self.log}"\n{extra}exit ${{{variable}:-0}}\n')
            (self.bin / program).chmod(0o755)

    def files(self):
        return {str(path.relative_to(self.root)): (("-> " + os.readlink(path)) if path.is_symlink() else path.read_text())
                for path in sorted(self.root.rglob("*")) if path.is_file() or path.is_symlink()}

    def legacy(self, script, *args, env=None, apache_ctl=True):
        text = (LEGACY / script).read_text().replace('cd "$(dirname "$0")"', f"cd {shlex.quote(str(APACHE))}")
        text = text.replace('"$(dirname "$0")/conf/', f'"{APACHE}/conf/')
        text = text.replace("/etc/", f"{self.root}/etc/").replace("/var/lib/vns", f"{self.root}/var/lib/vns")
        path = ["/usr/bin", "/bin"] if not apache_ctl else [str(self.bin), "/usr/bin", "/bin"]
        if not apache_ctl:
            # Everything but apache2ctl
            limited = self.bin.parent / f"{self.bin.name}-no-ctl"
            limited.mkdir(exist_ok=True)
            for program in PROGRAMS:
                if program != "apache2ctl" and not (limited / program).exists():
                    (limited / program).symlink_to(self.bin / program)
            path = [str(limited), "/usr/bin", "/bin"]
        result = subprocess.run(["bash", "-c", text, script, *args], capture_output=True, text=True,
                                env={**os.environ, "PATH": ":".join(path), **(env or {})})
        return result

    def commands(self):
        return self.log.read_text().splitlines()


def failing(*names):
    """Answers that make the named programs fail, as FAIL_<PROGRAM>=1 does for the script"""
    return {name: Result(1) for name in names}


class InstallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def pair(self, apache=True):
        return Server(self.folder, f"old{len(list(self.folder.glob('old*')))}", apache), Server(self.folder, f"new{len(list(self.folder.glob('new*')))}", apache)

    def harbor(self, server, argv, answers=None, active=False, apache_ctl=True):
        answers = dict(answers or {})
        answers.setdefault("systemctl is-active --quiet apache2", Result(0 if active else 3))
        system = RecordingSystem(root=server.root, answers=answers, programs={"apache2ctl"} if apache_ctl else set())
        code = main(argv, system)
        return code, system

    def check(self, old, new, result, code, system, message=""):
        self.assertEqual(code, result.returncode, (message, result.stderr))
        self.assertEqual(new.files(), old.files(), message)
        self.assertEqual(system.commands, old.commands(), message)
        said = [line for line in system.said if line]
        self.assertEqual(said, [line for line in (result.stdout + result.stderr).splitlines() if line and not line.startswith(("Syntax", "ok"))], message)

    def test_server_wide_configs(self):
        for active in (False, True):
            old, new = self.pair()
            result = old.legacy("install_apache_conf.sh", env={"ACTIVE": "1" if active else "0"})
            code, system = self.harbor(new, ["server", "install-apache-conf"], active=active)
            self.check(old, new, result, code, system, f"active={active}")
            self.assertTrue((new.root / "var/lib/vns/offline/app_offline.htm").is_file())

    def test_server_wide_configs_refused_put_back_what_was_there(self):
        for had_before in (False, True):
            old, new = self.pair()
            for server in (old, new):
                if had_before:
                    (server.root / "etc/apache2/conf-available/vns-headers.conf").write_text("# the one before\n")
            result = old.legacy("install_apache_conf.sh", env={"FAIL_APACHE_CTL": "1"})
            code, system = self.harbor(new, ["server", "install-apache-conf"], failing("apache2ctl configtest"))
            self.check(old, new, result, code, system, f"had_before={had_before}")
            self.assertEqual(code, 1)

    def test_server_wide_configs_without_apache(self):
        old, new = self.pair(apache=False)
        result = old.legacy("install_apache_conf.sh")
        code, system = self.harbor(new, ["server", "install-apache-conf"])
        self.check(old, new, result, code, system)

    @NEEDS_BASH_4
    def test_tls_policy(self):
        cases = [  # (args, active, ssl enabled before, fails at configtest)
            ([], True, True, False), ([], False, False, False), (["--start-if-stopped"], False, True, False),
            ([], True, False, True), ([], True, True, True)]
        for args, active, ssl, fails in cases:
            for had_before in (False, True):
                old, new = self.pair()
                for server in (old, new):
                    if had_before:
                        (server.root / "etc/apache2/conf-available/vns-tls.conf").write_text("# older policy\n")
                        (server.root / "etc/apache2/conf-enabled/vns-tls.conf").symlink_to("../conf-available/vns-tls.conf")
                env = {"ACTIVE": "1" if active else "0", "FAIL_A_QUERY": "0" if ssl else "1", "FAIL_APACHE_CTL": "1" if fails else "0"}
                result = old.legacy("install_tls_policy.sh", *args, env=env)
                answers = {**({} if ssl else failing("a2query -m ssl")), **(failing("apache2ctl configtest") if fails else {})}
                code, system = self.harbor(new, ["server", "install-tls-policy", *args], answers, active=active)
                message = f"{args} active={active} ssl={ssl} fails={fails} before={had_before}"
                self.assertEqual(code, result.returncode, (message, result.stderr))
                self.assertEqual(new.files(), old.files(), message)
                self.assertEqual(system.commands, old.commands(), message)

    @NEEDS_BASH_4
    def test_tls_policy_refuses_what_it_did_not_make_and_skips_without_apache(self):
        old, new = self.pair()
        for server in (old, new):
            (server.root / "etc/apache2/conf-enabled/vns-tls.conf").write_text("not a link\n")
        result = old.legacy("install_tls_policy.sh")
        code, system = self.harbor(new, ["server", "install-tls-policy"])
        self.assertEqual((code, new.files()), (result.returncode, old.files()))
        self.assertEqual(system.said[-1].replace(str(new.root), ""), result.stderr.strip().replace(str(old.root), ""))
        for args in ([], ["--start-if-stopped"]):
            old, new = self.pair(apache=False)
            result = old.legacy("install_tls_policy.sh", *args, apache_ctl=False)
            code, system = self.harbor(new, ["server", "install-tls-policy", *args], apache_ctl=False)
            self.assertEqual(code, result.returncode, args)
            self.assertEqual(system.said, (result.stdout + result.stderr).strip().splitlines(), args)


class FixPermissionsTests(unittest.TestCase):
    def test_the_site_here_and_an_environment_elsewhere(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            site = folder / "var/www/shop.dk"
            (site / "scripts/targets/production").mkdir(parents=True)
            (site / "scripts/targets/production/publish_prod.sh").write_text(
                "PROD_IP='10.0.0.5'\nPROD_USER='deploy'\nPROD_PASSWORD='s3 cret'\nPROD_SSH_PORT='22'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n")
            system = RecordingSystem(root=folder)
            self.assertEqual(main(["site", "fix-permissions", "shop.dk"], system), 0)
            self.assertEqual(system.commands[0], f"sudo env SITE=shop.dk ROOT=/var/www/shop.dk python3 {APACHE / 'lib/site_permissions.py'}")
            os.environ["VNS_TARGET"] = "production"
            try:
                system = RecordingSystem(root=folder)
                self.assertEqual(main(["site", "fix-permissions", "shop.dk", "prod"], system), 0)
            finally:
                del os.environ["VNS_TARGET"]
            self.assertTrue(system.commands[0].startswith("sshpass -e ssh -p 22 "))
            self.assertTrue(system.commands[0].endswith("deploy@10.0.0.5 sudo -n env SITE=shop.dk ROOT=/var/www/shop.dk python3 -"))
            self.assertEqual(system.environments[0], {"SSHPASS": "s3 cret"})
            self.assertEqual(system.inputs[0], (APACHE / "lib/site_permissions.py").read_text())
            self.assertEqual(main(["site", "fix-permissions", "shop.dk", "other"], RecordingSystem(root=folder)), 1)
            # An environment on Windows has no SSH: refused with the script's message, not a traceback
            (site / "scripts/targets/production/publish_prod.sh").write_text("PROD_TARGET=windows\nPROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\n")
            os.environ["VNS_TARGET"] = "production"
            try:
                system = RecordingSystem(root=folder)
                self.assertEqual(main(["site", "fix-permissions", "shop.dk", "prod"], system), 1)
            finally:
                del os.environ["VNS_TARGET"]
            self.assertIn("not available for a Windows production site", system.said[-1])
            self.assertEqual(system.commands, [])


if __name__ == "__main__":
    unittest.main()
