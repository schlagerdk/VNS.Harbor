"""A Windows server's sites listed where they are (harbor/server/windows_sites.py): the folders in its share, those left
out not shown, and the files only of a site that is listed"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Stop  # noqa: E402
from harbor.server import windows_sites  # noqa: E402


class WindowsSitesTests(unittest.TestCase):
	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.root = Path(folder.name)
		path = self.root / "var/lib/harbor/servers.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"iis": {"kind": "windows", "share": "//win/sites", "user": "harbor", "domain": ""},
		                            "web": {"host": "10.0.0.6", "user": "root", "port": "22"}}))

	def system(self):
		return RecordingSystem(root=self.root, answers={"rclone lsf :smb:sites --dirs-only": "Alpha/\nAlpha/Umbraco/\nAlpha/bin/\nold-backup/\nbeta/\nbeta/wwwroot/\n"})

	def entry(self):
		return json.loads((self.root / "var/lib/harbor/servers.json").read_text())["iis"]

	def test_listed_with_folders_left_out(self):
		self.assertEqual(main(["servers", "windows-sites", "iis", "on", "--exclude", "old-backup"], self.system()), 0)
		self.assertEqual((self.entry()["list_sites"], self.entry()["exclude"]), (True, ["old-backup"]))
		system = self.system()
		self.assertEqual([item["folder"] for item in windows_sites.folders(system, "iis")], ["Alpha", "beta"])
		# One with an umbraco folder says so: its backend is suggested as /umbraco
		self.assertEqual([item["umbraco"] for item in windows_sites.folders(system, "iis")], [True, False])
		# Every folder, for choosing: the one left out marked
		self.assertEqual([(item["folder"], item["excluded"]) for item in windows_sites.folders(system, "iis", everything=True)],
		                 [("Alpha", False), ("beta", False), ("old-backup", True)])
		# Off: the entry is as it was, the share kept for import
		main(["servers", "windows-sites", "iis", "off"], self.system())
		self.assertNotIn("list_sites", self.entry())
		self.assertNotIn("exclude", self.entry())
		self.assertEqual(self.entry()["share"], "//win/sites")

	def test_files_only_of_a_listed_site(self):
		main(["servers", "windows-sites", "iis", "on", "--exclude", "old-backup"], self.system())
		system = self.system()
		windows_sites.site_files(system, "iis", "Alpha")
		self.assertTrue(any("windows_files.py :smb:sites/Alpha" in line for line in system.commands), system.commands)
		for folder in ("old-backup", "../web", "a/b"):
			with self.subTest(folder=folder), self.assertRaises(Stop):
				windows_sites.site_files(self.system(), "iis", folder)
		# A Linux server is not a Windows server's sites
		with self.assertRaises(Stop):
			windows_sites.folders(self.system(), "web")


if __name__ == "__main__":
	unittest.main()
