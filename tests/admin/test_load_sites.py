from pathlib import Path
import subprocess
import unittest


APP = Path(__file__).resolve().parents[2] / "admin"


class LoadSitesTests(unittest.TestCase):
	def test_members_sites_stay_while_they_are_read_again(self):
		# On a master, what reads the list while the members answer (Export Site's databases) found no site at all
		source = (APP / "static/app.js").read_text()
		start = source.index("async function loadSites()")
		load = source[start:source.index("\n}\n", start) + 3]
		harness = r"""
const assert = require("node:assert/strict");
let currentUser = {admin: true, master: true}, accessSnapshot = "", serverContext = "", managedElsewhere = new Set(), sitesLoaded = false;
let defaultUmbracoVersion = "", keepAliveProblems = [], currentView = "server";
const site = {name: "test.example.com", environments: [{key: "main", database: "SQLite"}]};
let allSites = [{...site, harbor_server: "test-01"}];
let answer, seen;
const member = new Promise((resolve) => { answer = resolve; });
const api = (url) => {
	if (url === "/api/me") return Promise.resolve(currentUser);
	if (url === "/api/sites") return Promise.resolve({sites: []});
	if (url.startsWith("/api/via/")) { seen = allSites.map((item) => item.name); return member; }
	return Promise.resolve({});
};
const applyAccess = () => {}, userContext = () => {}, monitorUpdateCheck = () => {}, renderSites = () => {}, showWindowsMenu = () => {};
const joinedMembers = () => ["test-01"], onMasterItself = () => false, refreshWindowsSites = () => {};
const $ = () => ({});
(async () => {
	const loading = loadSites();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(seen, ["test.example.com"]);
	answer({sites: [site]});
	await loading;
	assert.deepEqual(allSites.map((item) => item.harbor_server), ["test-01"]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
		result = subprocess.run(["node", "-e", load + harness], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
	unittest.main()
