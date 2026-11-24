"""What a deleted user made, deleted with them: one task, a step each, run one after another (lib/run_steps.py, 1.0.71)"""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("run_steps_under_test", ROOT_DIR / "lib/run_steps.py")
run_steps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_steps)


class RunStepsTests(unittest.TestCase):
    def test_each_step_runs_and_a_failure_does_not_stop_the_rest(self):
        ran = []

        def runner(argv, **kwargs):
            ran.append((argv, kwargs["env"].get("HARBOR_PROGRESS")))
            return subprocess.CompletedProcess(argv, 1 if argv == ["two"] else 0, stdout="", stderr="Msg 3702: in use" if argv == ["two"] else "")

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HARBOR_PROGRESS": "1"}), redirect_stdout(out), redirect_stderr(err):
            code = run_steps.run([{"title": "One", "argv": ["one"]}, {"title": "Two", "argv": ["two"]}, {"title": "Three", "argv": ["three"]}], runner)
        self.assertEqual(code, 1)
        self.assertEqual([argv for argv, _ in ran], [["one"], ["two"], ["three"]])
        # The commands run without the task's own steps, which theirs would replace
        self.assertEqual({progress for _, progress in ran}, {None})
        self.assertIn('"type": "plan", "steps": ["One", "Two", "Three"]', out.getvalue())
        self.assertIn("Two failed: Msg 3702: in use", out.getvalue())
        self.assertIn("2 of 3 done, 1 failed", out.getvalue())

    def test_the_task_is_an_administrators_and_runs_steps_it_builds(self):
        source = (ROOT_DIR / "admin/app.py").read_text()
        tree = ast.parse(source)
        admin_tasks = next(node for node in tree.body if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ADMIN_TASKS")
        self.assertIn("delete_user_made", ast.literal_eval(admin_tasks.value))
        self.assertIn('"delete_login": True', source[source.index('if kind == "delete_user_made":'):source.index('if kind == "delete_site":')])


if __name__ == "__main__":
    unittest.main()
