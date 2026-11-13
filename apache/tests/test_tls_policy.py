import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest


APACHE = Path(__file__).resolve().parents[1]
POLICY = """# VNS-managed server-side TLS policy. Virtual hosts can override this policy.
SSLProtocol -all +TLSv1.2 +TLSv1.3
SSLCipherSuite SSL ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305
SSLCipherSuite TLSv1.3 TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256
"""


class TlsPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conf = self.root / "etc/apache2"
        (self.conf / "conf-available").mkdir(parents=True)
        (self.conf / "conf-enabled").mkdir()
        self.available = self.conf / "conf-available/vns-tls.conf"
        self.enabled = self.conf / "conf-enabled/vns-tls.conf"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        self.script = self.root / "install_tls_policy.sh"
        # The script as it was, before it ran harbor (tests/test_server_install.py compares harbor with it)
        self.script.write_text((APACHE.parent / "tests/legacy/install_tls_policy.sh").read_text().replace(
            "/etc/apache2", str(self.conf)))
        (self.root / "conf").mkdir()
        (self.root / "conf/vns-tls.conf").write_text(POLICY)
        mocks = {
            "a2query": 'exit "${SSL_MISSING:-0}"',
            "a2enmod": 'echo "a2enmod $*" >> "$CALLS"',
            "a2dismod": 'echo "a2dismod $*" >> "$CALLS"',
            "apache2ctl": '''echo "apache2ctl $*" >> "$CALLS"
if [[ ${FAIL_CONFIG:-0} == 1 ]]; then exit 1; fi''',
            "systemctl": '''echo "systemctl $*" >> "$CALLS"
if [[ $1 == is-active ]]; then exit "${INACTIVE:-0}"; fi
if [[ $1 == reload && ${FAIL_RELOAD:-0} == 1 && ! -e "$RELOAD_FAILED" ]]; then
    touch "$RELOAD_FAILED"
    exit 1
fi''',
        }
        for name, body in mocks.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.env = os.environ | {"PATH": str(self.bin) + ":" + os.environ["PATH"],
                                 "CALLS": str(self.log),
                                 "RELOAD_FAILED": str(self.root / "reload-failed")}

    def run_installer(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args], text=True,
                              capture_output=True, env=self.env | env)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_exact_policy_and_openssl_cipher_availability(self):
        self.assertEqual((APACHE / "conf/vns-tls.conf").read_text(), POLICY)
        lines = POLICY.splitlines()
        tls12 = lines[2].split()[-1]
        tls13 = lines[3].split()[-1]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_ciphers(tls12)
        ciphers = context.get_ciphers()
        self.assertEqual({c["name"] for c in ciphers if c["protocol"] != "TLSv1.3"},
                         set(tls12.split(":")))
        self.assertTrue(ssl.HAS_TLSv1_3)
        self.assertTrue(set(tls13.split(":")).issubset({c["name"] for c in ciphers}))

    def test_install_enable_and_reload_or_start(self):
        for inactive, args, action in (("0", (), "reload"), ("1", (), None),
                                       ("1", ("--start-if-stopped",), "start")):
            with self.subTest(inactive=inactive, action=action):
                self.log.unlink(missing_ok=True)
                result = self.run_installer(*args, INACTIVE=inactive)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.available.read_text(), POLICY)
                self.assertEqual(os.readlink(self.enabled), "../conf-available/vns-tls.conf")
                calls = self.calls()
                self.assertIn("apache2ctl configtest", calls)
                if action:
                    self.assertLess(calls.index("apache2ctl configtest"),
                                    calls.index("systemctl " + action + " apache2"))
                else:
                    self.assertFalse(any("systemctl reload" in call or "systemctl start" in call
                                         for call in calls))

    def test_failed_config_restores_existing_and_new_config(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.available.write_text("previous\n")
                    self.enabled.symlink_to("../conf-available/vns-tls.conf")
                result = self.run_installer("--start-if-stopped", FAIL_CONFIG="1", SSL_MISSING="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("restoring previous", result.stderr)
                self.assertEqual(self.available.exists(), existing)
                self.assertEqual(self.enabled.is_symlink(), existing)
                if existing:
                    self.assertEqual(self.available.read_text(), "previous\n")
                self.assertIn("a2dismod ssl", self.calls())
                self.assertFalse(any("systemctl reload" in call or "systemctl start" in call
                                     for call in self.calls()))

    def test_reload_failure_rolls_back_and_reloads_previous_config(self):
        self.available.write_text("previous\n")
        self.enabled.symlink_to("../conf-available/vns-tls.conf")
        result = self.run_installer(FAIL_RELOAD="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.available.read_text(), "previous\n")
        self.assertEqual(self.calls().count("apache2ctl configtest"), 2)
        self.assertEqual(self.calls().count("systemctl reload apache2"), 2)

    def test_absent_apache_skip_and_init_failure(self):
        self.script.write_text(self.script.read_text().replace(
            "command -v apache2ctl", "command -v vns_missing_apache2ctl"))
        self.conf.rename(self.root / "absent-apache")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipping", result.stdout)
        result = self.run_installer("--start-if-stopped")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.available.exists())

    def test_incomplete_apache_installation_fails(self):
        self.script.write_text(self.script.read_text().replace(
            "command -v apache2ctl", "command -v vns_missing_apache2ctl"))
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete Apache", result.stderr)

    def test_unexpected_enabled_file_is_not_overwritten(self):
        self.enabled.write_text("custom\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.enabled.read_text(), "custom\n")
        self.assertFalse(self.available.exists())

if __name__ == "__main__":
    unittest.main()
