"""How the server itself is, over time, and a mail when it needs someone (lib/server_health.py, 1.0.92)"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("server_health", ROOT_DIR / "lib/server_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

ON = {**health.DEFAULTS, "enabled": True, "emails": ["ops@example.com"]}
UPDATES = {"count": 3, "security": 1, "packages": [], "reboot": False, "reboot_packages": [], "unattended": False, "checked": 0}


def sample(at, memory=40.0, swap=0.0, disk=50.0):
	return {"at": at, "load": 0.5, "cpus": 2, "memory": memory, "memory_mb": 4096, "swap": swap, "swap_mb": 2048,
			"disks": {"/": {"used": disk, "size_gb": 40.0, "free_gb": round(40 * (100 - disk) / 100, 1)}}}


class ServerHealthTests(unittest.TestCase):
	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.state = Path(folder.name)
		self.sent = []

	def send(self, settings, to, subject, body, html=None):
		self.sent.append((to, subject, body, html))

	def run_at(self, at, updates=None, **values):
		return health.run(self.state, now=at, settings=ON, sender=self.send, current=sample(at, **values),
						  read_updates=lambda now: {**(updates or UPDATES), "checked": now})

	def test_memory_and_disk_from_the_kernel(self):
		meminfo = self.state / "meminfo"
		meminfo.write_text("MemTotal:  4000000 kB\nMemFree: 100000 kB\nMemAvailable: 1000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
		self.assertEqual(health.memory(meminfo), {"memory": 75.0, "memory_mb": 3906, "swap": None, "swap_mb": 0})
		mounts = self.state / "mounts"
		mounts.write_text("/dev/sda1 / ext4 rw 0 0\nproc /proc proc rw 0 0\n/dev/sda1 /var/lib/docker ext4 rw 0 0\n/dev/loop1 /snap/core squashfs ro 0 0\n")
		stat = os.statvfs_result((4096, 4096, 1000, 250, 200, 0, 0, 0, 0, 255))
		self.assertEqual(health.disks(mounts, statvfs=lambda path: stat), {"/": {"used": 78.9, "size_gb": 0.0, "free_gb": 0.0}})

	def test_a_disk_past_its_limit_is_mailed_once_and_once_when_it_is_fine(self):
		self.run_at(1000, disk=86)
		self.run_at(1300, disk=87)
		self.assertEqual(len(self.sent), 1, "the disk and the security update waiting in one mail, not again")
		self.assertIn("Disk almost full: /", self.sent[0][1] + self.sent[0][2])
		self.assertIn("/ is 86 % full, 5.6 GB free", self.sent[0][2])
		self.assertIn("1 security update waiting, and they are not installed by themselves", self.sent[0][2])
		self.run_at(1600, disk=70)
		self.assertEqual(len(self.sent), 2)
		self.assertIn("Fine again: Disk almost full: /", self.sent[1][1])
		self.assertIn("Still:", self.sent[1][2], "the update still waits")
		self.assertIn("<!doctype html>", self.sent[1][3])

	def test_memory_past_its_limit_once_is_a_peak_twice_is_told(self):
		quiet = {**UPDATES, "security": 0, "count": 0}
		self.run_at(1000, updates=quiet, memory=95)
		self.assertEqual(self.sent, [])
		self.run_at(1300, updates=quiet, memory=96)
		self.assertEqual(len(self.sent), 1)
		self.assertIn("Memory almost full", self.sent[0][1])

	def test_samples_are_kept_seven_days(self):
		quiet = {**UPDATES, "security": 0, "count": 0}
		self.run_at(1000, updates=quiet)
		self.run_at(1000 + health.KEEP + 10, updates=quiet)
		samples = json.loads((self.state / "samples.json").read_text())
		self.assertEqual([item["at"] for item in samples], [1000 + health.KEEP + 10])

	def test_a_restart_asked_for_and_the_settings_checked(self):
		self.run_at(1000, updates={**UPDATES, "security": 0, "reboot": True, "reboot_packages": ["linux-image-6.8"]})
		self.assertIn("The server asks to be restarted after updating linux-image-6.8", self.sent[0][2])
		with self.assertRaises(ValueError):
			health.validate_settings({"disk": 5})
		with self.assertRaises(ValueError):
			health.validate_settings({"enabled": True, "emails": []})
		self.assertEqual(health.validate_settings({"disk": 0})["disk"], 0, "0: not watched")


if __name__ == "__main__":
	unittest.main()
