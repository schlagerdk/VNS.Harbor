#!/usr/bin/env python3
"""Commands run one after another as the steps of one task in the admin's window, as harbor's own commands show theirs.

  run_steps.py '[{"title": "Delete shop.example.com", "argv": ["/usr/bin/python3", "/opt/harbor/current/bin/harbor", ...]}, ...]'

Each command's own output is shown only when it fails; a failed step does not stop the ones after it, and the task
ends as failed when any did. The admin builds the list (admin/app.py); nothing typed by a user is run as it is.
"""
import json
import os
import subprocess
import sys


def progress(data):
    if os.environ.get("HARBOR_PROGRESS") == "1":
        print(f"\x1b]7777;harbor;{json.dumps(data)}\x07", end="", flush=True)


def run(steps, runner=subprocess.run):
    progress({"type": "plan", "steps": [step["title"] for step in steps]})
    # Each command's own steps would replace this task's in the window: it runs without them
    env = {key: value for key, value in os.environ.items() if key != "HARBOR_PROGRESS"}
    failed = 0
    for step in steps:
        progress({"type": "step", "text": step["title"]})
        print(step["title"], flush=True)
        result = runner(step["argv"], capture_output=True, text=True, env=env)
        if result.returncode:
            failed += 1
            said = (result.stderr or result.stdout or "").strip().splitlines()
            progress({"type": "warning", "text": f"{step['title']} failed: {said[-1] if said else 'exit ' + str(result.returncode)}"})
            print("\n".join(said[-40:]), file=sys.stderr, flush=True)
    print(f"{len(steps) - failed} of {len(steps)} done" + (f", {failed} failed" if failed else ""), flush=True)
    return 1 if failed else 0


def main():
    steps = json.loads(sys.argv[1])
    if not isinstance(steps, list) or not all(isinstance(step, dict) and isinstance(step.get("title"), str)
                                              and isinstance(step.get("argv"), list) and step["argv"] for step in steps):
        print("Expected a list of steps, each with a title and an argv", file=sys.stderr)
        return 2
    return run(steps)


if __name__ == "__main__":
    sys.exit(main())
