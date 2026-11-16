#!/usr/bin/env python3
"""Run server-validated site commands concurrently and retain a final summary."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import sys
import threading


# A step a harbor command reports to the admin's window (harbor/core/system.py PROGRESS), and the same with its site
EVENT = re.compile(r"\x1b\]7777;harbor;(\{.*?\})\x07")


def event(data):
    """An event for the admin's window, when it wants them"""
    return f"\x1b]7777;harbor;{json.dumps(data)}\x07" if os.environ.get("HARBOR_PROGRESS") == "1" else ""


def with_site(line, site):
    """Each step named with the site it is a step of: several sites run at once"""
    def named(match):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return match.group(0)
        return event({**data, "site": site}) or match.group(0) if isinstance(data, dict) else match.group(0)
    return EVENT.sub(named, line)


def run_task(task, lock):
    site = task["site"]
    if task.get("skip"):
        return site, "SKIPPED: " + task["skip"]
    with lock:
        print(f"\n=== {site} ==={event({'type': 'site', 'site': site, 'state': 'running'})}", flush=True)
    status = run_command(task, site, lock)
    with lock:
        print(event({"type": "site", "site": site, "state": "done" if status == "OK" else "failed"}), end="", flush=True)
    return site, status


def run_command(task, site, lock):
    try:
        with subprocess.Popen(task["argv"], cwd=task["cwd"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", bufsize=1) as process:
            for line in process.stdout:
                with lock:
                    print(f"[{site}] {with_site(line, site)}", end="", flush=True)
            code = process.wait()
        status = "OK" if code == 0 else f"FAILED: exit {code}"
    except OSError as exc:
        status = f"FAILED: {exc}"
        with lock:
            print(f"[{site}] {status}", file=sys.stderr, flush=True)
    return status


# How many sites run at once. More was too many: 17 syncs to one server opened more SSH logins at once than its sshd
# lets wait (MaxStartups, 10), and the rest were reset; 14 builds at once also crowd the server. HARBOR_MULTI_PARALLEL
# sets another number.
PARALLEL = 4


def parallel():
    try:
        return max(1, int(os.environ.get("HARBOR_MULTI_PARALLEL", PARALLEL)))
    except ValueError:
        return PARALLEL


def run(tasks):
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, min(len(tasks), parallel()))) as executor:
        results = list(executor.map(lambda task: run_task(task, lock), tasks))
    print("\n=== Multi action summary ===", flush=True)
    for site, status in results:
        print(f"{site}: {status}", flush=True)
    return int(any(status.startswith("FAILED") for _, status in results))


if __name__ == "__main__":
    sys.exit(run(json.loads(sys.argv[1])))
