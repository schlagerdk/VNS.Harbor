"""harbor server adopt-certificates: the certificates acme.sh kept in /root/.acme.sh (Harbor before 1.0.67) moved to
where its cron job renews them, and every acme.sh call in Harbor given those folders."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.server import acme  # noqa: E402

CRON = '35 11 * * * "/etc/acmesh"/acme.sh --cron --home "/etc/acmesh" --config-home "/etc/ssl/data" >> /var/log/acme.log 2>&1\n'


class AdoptCertificates(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for path in ("etc/acmesh", "etc/ssl/certs", "etc/ssl/data", "root/.acme.sh/ca"):
            (self.root / path).mkdir(parents=True)
        (self.root / "etc/acmesh/acme.sh").write_text("#!/bin/sh\n")

    def tearDown(self):
        self.folder.cleanup()

    def site(self, *names):
        """An enabled Apache site that answers to names"""
        enabled = self.root / "etc/apache2/sites-enabled"
        enabled.mkdir(parents=True, exist_ok=True)
        (enabled / f"{names[0]}.conf").write_text("<VirtualHost *:443>\n  ServerName " + names[0] + "\n"
                                                  + "".join(f"  ServerAlias {name}\n" for name in names[1:]) + "</VirtualHost>\n")

    def issued(self, domain, conf=True, installed=True, alt="no", target=None, site=True):
        """A certificate as Harbor before 1.0.67 left it: acme.sh's files in /root/.acme.sh, the installed ones in
        /etc/ssl/certs/<domain>_ecc (or target), for a site that answers to it"""
        if site:
            self.site(domain, *(name for name in alt.split(",") if alt != "no"))
        state = self.root / f"root/.acme.sh/{domain}_ecc"
        (state / "backup").mkdir(parents=True)
        (state / "backup/key.bak").write_text("old key")
        target = target or f"/etc/ssl/certs/{domain}_ecc"
        for name, text in ((f"{domain}.key", "key"), ("fullchain.cer", "chain"), (f"{domain}.cer", "cert"), ("ca.cer", "ca")):
            (state / name).write_text(text)
        if conf:
            (state / f"{domain}.conf").write_text(f"Le_Domain='{domain}'\nLe_Alt='{alt}'\nLe_RealKeyPath='{target}/{domain}.key'\n"
                                                  f"Le_RealFullChainPath='{target}/fullchain.cer'\n")
        if installed:
            (self.root / target.lstrip("/")).mkdir(parents=True, exist_ok=True)
            (self.root / target.lstrip("/") / f"{domain}.key").write_text("key")
            (self.root / target.lstrip("/") / "fullchain.cer").write_text("chain")
        return state

    def adopt(self, crontab=CRON, dry_run=False):
        system = RecordingSystem(root=self.root, answers={"crontab -l": Result(0, crontab, "")})
        acme.adopt(system, dry_run)
        return system

    def test_an_installed_certificate_moves_where_the_cron_job_renews_it(self):
        self.issued("shop.example.com")
        system = self.adopt()
        home = self.root / "etc/ssl/certs/shop.example.com_ecc"
        self.assertEqual(sorted(path.name for path in home.iterdir()),
                         ["backup", "ca.cer", "fullchain.cer", "shop.example.com.cer", "shop.example.com.conf", "shop.example.com.key"])
        self.assertEqual((home / "fullchain.cer").read_text(), "chain")
        # Kept, out of acme.sh's way
        self.assertFalse((self.root / "root/.acme.sh/shop.example.com_ecc").exists())
        self.assertTrue((self.root / "root/.acme.sh/moved/shop.example.com_ecc/shop.example.com.conf").is_file())
        self.assertIn(("plan", ["Move the certificate of shop.example.com to /etc/ssl/certs"]), system.events)
        self.assertFalse(system.warned)
        # The cron job is there already
        self.assertFalse(any("--install-cronjob" in command for command in system.commands))

    def test_a_second_run_changes_nothing(self):
        self.issued("shop.example.com")
        self.adopt()
        system = self.adopt()
        self.assertFalse(system.planned)
        self.assertEqual(system.said, [])
        self.assertIn("Every certificate is where acme.sh's cron job renews it.", self.adopt(dry_run=True).said)

    def test_a_removed_failed_or_uninstalled_certificate_stays(self):
        self.issued("gone.example.com", conf=False)
        self.issued("typo.example.com", installed=False)
        system = self.adopt()
        self.assertTrue((self.root / "root/.acme.sh/gone.example.com_ecc").is_dir())
        self.assertTrue((self.root / "root/.acme.sh/typo.example.com_ecc").is_dir())
        self.assertFalse((self.root / "etc/ssl/certs/typo.example.com_ecc").exists())
        # gone.example.com is installed but acme.sh has nothing to renew it with
        self.assertEqual(system.said, ["The certificate of gone.example.com is not known to acme.sh and is not renewed: issue it again."])

    def test_one_for_names_no_site_here_answers_to_stays(self):
        """A server made from a copy of another keeps that one's admin certificate, installed over its own"""
        self.site("admin.example.net")
        (self.root / "etc/ssl/certs/admin.example.net_ecc").mkdir()
        (self.root / "etc/ssl/certs/admin.example.net_ecc/admin.example.net.conf").write_text("Le_Domain='admin.example.net'\n")
        self.issued("admin.example.org", target="/etc/ssl/certs/admin.example.net_ecc", site=False)
        self.issued("shop.example.com", alt="www.shop.example.com,old.shop.example.com")
        (self.root / "etc/apache2/sites-enabled/shop.example.com.conf").write_text(
            "<VirtualHost *:443>\n  ServerName shop.example.com\n  ServerAlias www.shop.example.com\n</VirtualHost>\n")
        system = self.adopt(dry_run=True)
        self.assertIn("Keep admin.example.org: no site here answers to admin.example.org", system.said)
        self.assertIn("Keep shop.example.com: no site here answers to old.shop.example.com", system.said)
        self.adopt()
        self.assertTrue((self.root / "root/.acme.sh/admin.example.org_ecc").is_dir())
        self.assertFalse((self.root / "etc/ssl/certs/admin.example.org_ecc").exists())
        self.assertEqual((self.root / "etc/ssl/certs/admin.example.net_ecc/admin.example.net.conf").read_text(), "Le_Domain='admin.example.net'\n")

    def test_an_international_name_is_told_by_its_punycode(self):
        self.site("tømrer.example.com")
        state = self.issued("xn--tmrer-vua.example.com", target="/etc/ssl/certs/tømrer.example.com_ecc", site=False)
        system = self.adopt()
        self.assertFalse(state.exists())
        self.assertTrue((self.root / "etc/ssl/certs/xn--tmrer-vua.example.com_ecc/xn--tmrer-vua.example.com.conf").is_file())
        # Its installed folder, named in its own letters, is renewed
        self.assertFalse(system.warned, system.said)

    def test_one_in_the_right_place_already_is_kept(self):
        self.issued("shop.example.com")
        (self.root / "etc/ssl/certs/shop.example.com_ecc/shop.example.com.conf").write_text("Le_Domain='shop.example.com'\n")
        self.adopt()
        self.assertTrue((self.root / "root/.acme.sh/shop.example.com_ecc").is_dir())
        self.assertEqual((self.root / "etc/ssl/certs/shop.example.com_ecc/shop.example.com.conf").read_text(), "Le_Domain='shop.example.com'\n")

    def test_a_dry_run_changes_nothing(self):
        self.issued("shop.example.com")
        system = self.adopt(crontab="", dry_run=True)
        self.assertTrue((self.root / "root/.acme.sh/shop.example.com_ecc").is_dir())
        self.assertFalse((self.root / "etc/ssl/certs/shop.example.com_ecc/shop.example.com.conf").exists())
        self.assertIn("Move shop.example.com: installed in /etc/ssl/certs/shop.example.com_ecc/fullchain.cer", system.said)
        self.assertEqual(system.commands, ["crontab -l"])

    def test_the_cron_job_is_installed_when_missing(self):
        system = self.adopt(crontab="")
        self.assertIn("/etc/acmesh/acme.sh --home /etc/acmesh --config-home /etc/ssl/data --install-cronjob", system.commands)

    def test_no_acme_sh_no_change(self):
        (self.root / "etc/acmesh/acme.sh").unlink()
        system = self.adopt()
        self.assertEqual(system.commands, [])


class EveryCallHasTheFolders(unittest.TestCase):
    def test_no_acme_sh_call_without_its_folders(self):
        """Each call names --home and --config-home, as the cron job does (1.0.67)"""
        bare = []
        for path in [*(ROOT / "harbor").rglob("*.py"), ROOT / "lib/common.sh"]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r'system\.run\(ACME, "--|"\$\{_?acme\}" --', line):
                    bare.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
