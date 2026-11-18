"""Harbor's image signing key taken off again: with it, Umbraco answered 400 to image URLs made by hand"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

LIB = Path(__file__).resolve().parents[2] / "lib"


class HardenServiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "systemd").mkdir()
        with mock.patch.dict(os.environ, {"VNS_SYSTEMD_DIR": str(self.root / "systemd"), "VNS_SERVICE_SECRETS": str(self.root / "secrets")}):
            spec = importlib.util.spec_from_file_location("harden_service", LIB / "harden_service.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.reload = mock.patch.object(self.module, "reload").start()
        self.addCleanup(mock.patch.stopall)

    def test_the_key_goes_where_harbor_put_it_and_nothing_else_is_touched(self):
        drop_in = self.root / "systemd" / "shop.dk.service.d" / "vns-security.conf"
        drop_in.parent.mkdir(parents=True)
        drop_in.write_text("[Service]\nEnvironmentFile=/etc/harbor/secrets/services/shop.dk.env\n")
        own = self.root / "systemd" / "other.dk.service.d" / "custom.conf"
        own.parent.mkdir(parents=True)
        own.write_text("[Service]\n")
        self.module.main(["--all"])
        self.assertFalse(drop_in.exists() or drop_in.parent.exists())
        self.assertTrue(own.exists())
        self.reload.assert_called_once()
        self.module.main(["shop.dk"])
        self.reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
