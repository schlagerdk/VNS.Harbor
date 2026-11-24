"""A job's log split into its runs in the admin: Harbor's own start line, and acme.sh's (1.0.68)"""
from pathlib import Path
import subprocess
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/static/app.js"


class CronRunsTests(unittest.TestCase):
    def test_runs_of_harbors_jobs_and_of_acme_sh(self):
        source = APP.read_text()
        start = source.index("const CRON_RUN_START = ")
        end = source.index("\n// The end of a job's output", start)
        harness = r"""
const assert = require("node:assert/strict");
// Harbor's backup: its first line starts the run, nothing comes before it
assert.deepEqual(cronRuns(["----- 24.11.2026 00:15 -----", "| 1 | 2dm.dk |", "Backup of everything done! ----- 24.11.2026 00:16 -----", ""]),
	[{ when: "24.11.2026 00:15", lines: ["| 1 | 2dm.dk |", "Backup of everything done! ----- 24.11.2026 00:16 -----"] }]);
// acme.sh's cron job: a run per ===Starting cron===, the end of one cut off before it
assert.deepEqual(cronRuns([
	"[Mon Nov 23 10:14:34 CET 2026] ===End cron===",
	"[Tue Nov 24 10:14:29 CET 2026] ===Starting cron===",
	"[Tue Nov 24 10:14:29 CET 2026] Renew: admin.example.com",
	"[Tue Nov 24 10:14:34 CET 2026] ===End cron===",
	"[Wed Dec  2 09:05:00 CET 2026] ===Starting cron===",
	"[Wed Dec  2 09:05:01 CET 2026] Renew: admin.example.com",
]), [
	{ when: "", lines: ["[Mon Nov 23 10:14:34 CET 2026] ===End cron==="] },
	{ when: "24.11.2026 10:14", lines: ["[Tue Nov 24 10:14:29 CET 2026] Renew: admin.example.com", "[Tue Nov 24 10:14:34 CET 2026] ===End cron==="] },
	{ when: "02.12.2026 09:05", lines: ["[Wed Dec  2 09:05:01 CET 2026] Renew: admin.example.com"] },
]);
// A line that only mentions the words does not start a run
assert.equal(cronRunStart("Renew: ===Starting cron==="), null);
assert.equal(cronRunStart("[Tue Nov 24 10:14:29 CET 2026] Renew: x"), null);
"""
        result = subprocess.run(["node", "-e", source[start:end] + "\n" + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
