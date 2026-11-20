"""System.lanes: several destinations at once, each with its steps named in the admin's window"""
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.system import System  # noqa: E402

EVENT = re.compile(r"\x1b\]7777;harbor;(\{.*?\})\x07")


class LaneTests(unittest.TestCase):
    def run_lanes(self, work, items, multi=False):
        out = io.StringIO()
        environ = {"HARBOR_PROGRESS": "1", **({"HARBOR_MULTI": "1"} if multi else {})}
        with mock.patch.dict(os.environ, environ), mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            results = System().lanes(work, items)
        return results, [json.loads(found) for found in EVENT.findall(out.getvalue())], out.getvalue()

    def test_lanes_run_at_once_and_name_their_steps(self):
        # Both must be running at the same time to pass the barrier
        barrier = threading.Barrier(2, timeout=5)

        def work(name, item):
            System().step(f"Sync settings to {name}")
            barrier.wait()
            return item

        results, events, out = self.run_lanes(work, [("a.dk", 1), ("b.dk", 2)])
        self.assertEqual(results, [1, 2])
        self.assertIn({"type": "step", "text": "Sync settings to a.dk", "site": "a.dk"}, events)
        self.assertIn({"type": "site", "state": "done", "site": "b.dk"}, events)
        self.assertIn("[a.dk] Sync settings to a.dk", out)

    def test_a_failure_stays_in_its_lane(self):
        def work(name, item):
            if item == 1:
                raise RuntimeError("down")
            return item != 2

        results, events, _ = self.run_lanes(work, [("a.dk", 1), ("b.dk", 2), ("c.dk", 3)])
        self.assertIsInstance(results[0], RuntimeError)
        self.assertEqual(results[1:], [False, True])
        states = {event["site"]: event["state"] for event in events if event["type"] == "site"}
        self.assertEqual(states, {"a.dk": "failed", "b.dk": "failed", "c.dk": "done"})

    def test_inside_multi_action_one_at_a_time(self):
        running, most = [0], [0]
        lock = threading.Lock()

        def work(name, item):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            threading.Event().wait(0.05)
            with lock:
                running[0] -= 1
            return True

        self.run_lanes(work, [(str(n), n) for n in range(4)], multi=True)
        self.assertEqual(most[0], 1)

    def test_plans_are_named_with_their_lane(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HARBOR_PROGRESS": "1"}), mock.patch.object(sys, "stdout", out):
            System().plan_lanes({"": ["Copy settings from staging"], "a.dk": ["Sync settings to a.dk"]})
        self.assertEqual([json.loads(found) for found in EVENT.findall(out.getvalue())],
                         [{"type": "plan", "steps": ["Copy settings from staging"]},
                          {"type": "plan", "steps": ["Sync settings to a.dk"], "site": "a.dk"}])


if __name__ == "__main__":
    unittest.main()
