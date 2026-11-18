"""An Umbraco 18+ site's service: an image signing key of its own"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

LIB = Path(__file__).resolve().parents[2] / "lib"


def load(root):
    with mock.patch.dict(os.environ, {"VNS_SYSTEMD_DIR": str(root / "systemd"), "VNS_SERVICE_SECRETS": str(root / "secrets")}):
        spec = importlib.util.spec_from_file_location("harden_service", LIB / "harden_service.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class HardenServiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "systemd").mkdir()
        self.module = load(self.root)
        self.reload = mock.patch.object(self.module, "reload").start()
        self.addCleanup(mock.patch.stopall)

    def site(self, unit, umbraco=None):
        publish = self.root / "www" / unit / "_publish"
        publish.mkdir(parents=True)
        if umbraco:
            (publish / "Shop.deps.json").write_text('{"libraries": {"Umbraco.Cms.Core/%s": {}}}' % umbraco)
        (self.root / "systemd" / f"{unit}.service").write_text(f"[Service]\nExecStart=/usr/lib/dotnet/dotnet {publish}/Shop.dll --urls x\n")
        return self.root / "systemd" / f"{unit}.service.d" / "vns-security.conf"

    def test_umbraco_18_gets_its_key_and_no_server_header(self):
        drop_in = self.site("shop.dk", "18.3.2")
        self.module.main(["shop.dk"])
        text = drop_in.read_text()
        key = self.root / "secrets" / "shop.dk.env"
        self.assertIn(f"EnvironmentFile={key}", text)
        first = key.read_text()
        self.assertRegex(first, r"^Umbraco__CMS__Imaging__HMACSecretKey=[A-Za-z0-9+/]{86}==\n$")
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
        self.reload.assert_called_once()
        # Again: the key stays (a new one would make signed URLs invalid), nothing is reloaded
        self.module.main(["shop.dk"])
        self.assertEqual(key.read_text(), first)
        self.reload.assert_called_once()

    def test_older_umbraco_and_plain_dotnet_get_nothing_and_an_earlier_drop_in_goes(self):
        for unit, umbraco in (("old.dk", "13.5.0"), ("api.dk", None)):
            drop_in = self.site(unit, umbraco)
            drop_in.parent.mkdir(parents=True)
            drop_in.write_text("[Service]\nEnvironment=Kestrel__AddServerHeader=false\n")
            self.module.main([unit])
            self.assertFalse(drop_in.exists())
        self.assertFalse((self.root / "secrets").exists())

    def test_every_service_and_not_python_or_the_admin(self):
        self.site("shop.dk", "18.3.2")
        (self.root / "systemd" / "admin.dk.service").write_text("[Service]\nExecStart=/opt/venv/bin/gunicorn app:app\n")
        self.module.main(["--all"])
        self.assertTrue((self.root / "systemd" / "shop.dk.service.d" / "vns-security.conf").is_file())
        self.assertFalse((self.root / "systemd" / "admin.dk.service.d").exists())


if __name__ == "__main__":
    unittest.main()
