#!/usr/bin/env python3
"""Job alerts for the jobs of root's crontab (Settings, Notifications): how each run ended, and a mail when a watched job fails.

cron does not record how a job ended. While the alerts are on, root's crontab names harbor-cron-shell as its SHELL, so
cron runs each job through it: it runs the command as cron would (with the shell the crontab had, /bin/sh else), passes
its output on as it comes, and notes when it started, how long it ran, its exit code and the end of its output. A job
is watched when its line in root's crontab has a comment line above it, as its label has:

  # Harbor watch: exit, output, missed, long=60

and what of these makes a run a failure:

  exit     it ended with an exit code other than 0
  output   its output has lines that look like errors (also what it wrote to its log file, or to the journal)
  missed   cron did not run it when its schedule says (checked every 5 minutes)
  long     it ran longer than the minutes given (also while it still runs)

A watched job that fails is mailed once, through Harbor's mail server, to the alerts' recipients, and once when it
works again.

  cron_watch.py --shell -c COMMAND   run a job, as harbor-cron-shell does
  cron_watch.py --check              missed and long running jobs (cron.d runs this every 5 minutes)
  cron_watch.py --enable / --disable the shell in root's crontab and the check, or neither
  cron_watch.py --test EMAIL...      a test mail of how the watched jobs are now
"""
import argparse
from datetime import datetime, timedelta
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


def _load(name, filename):
	spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


report = None


def mailer():
	"""site_report (the mail server, and the mail's HTML), loaded when a mail is sent: a job's run needs none of it"""
	global report
	if report is None:
		report = _load("vns_cron_site_report", "site_report.py")
	return report


SETTINGS = Path("/etc/harbor/cron-alerts.json")
STATE = Path("/var/lib/harbor/cron-watch")
# When each command was saved in Scheduled jobs (harbor/server/tools.py): cron has not run it before then
SAVED = Path("/var/lib/harbor/cron.json")
SHELL = Path("/usr/local/sbin/harbor-cron-shell")
CHECK = Path("/etc/cron.d/vns-cron-watch")
# The shell root's crontab had before (SHELL=), kept beside harbor-cron-shell for the jobs under it
OWN_SHELL = "HARBOR_CRON_SHELL"
NOTE = "# Harbor: each job runs through harbor-cron-shell, which notes how it ended (Settings, Notifications, Job alerts)"

CONDITIONS = {
	"exit": "Ended with an error code",
	"output": "Errors in its output",
	"missed": "Did not run on time",
	"long": "Ran too long",
}
DEFAULT_WATCH = {"exit": True, "missed": True}
LONG_MINUTES = 60
# A run starts within this of when cron should start it; past it, a run not seen is missed
GRACE = 10 * 60
TAIL_BYTES = 65536
TAIL_LINES = 40
WATCH = re.compile(r"^#\s*Harbor watch:\s*(.*?)\s*$")
LABEL = re.compile(r"^#\s*Harbor label:\s*(.*?)\s*$")
ERROR_LINE = re.compile(r"\b(error|errors|failed|failure|fatal|exception|traceback|denied|cannot|could not|unable to)\b", re.I)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ---- what is watched: the comment line above a job
def parse_watch(text):
	"""{exit: True, output: True, missed: True, long: 60} of a watch line's text; unknown words are left out"""
	found = {}
	for word in str(text or "").split(","):
		word = word.strip().lower()
		if word in ("exit", "output", "missed"):
			found[word] = True
		elif re.fullmatch(r"long=\d{1,4}", word) and 1 <= int(word[5:]) <= 1440:
			found["long"] = int(word[5:])
	return found


def watch_text(watch):
	"""The watch line's text of {exit, output, missed, long}: empty when nothing is watched"""
	watch = clean_watch(watch)
	return ", ".join(f"long={value}" if key == "long" else key for key, value in watch.items())


def clean_watch(watch):
	"""What the admin sends, checked: the conditions in their order, long with its minutes"""
	if not isinstance(watch, dict):
		return {}
	found = {}
	for key in CONDITIONS:
		value = watch.get(key)
		if key == "long":
			if value is True:
				value = LONG_MINUTES
			if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1440:
				found[key] = value
		elif value is True:
			found[key] = True
	return found


def error_lines(lines):
	# SQL Server's line after every DBCC says "error" without being one
	return [line for line in lines if ERROR_LINE.search(line) and not line.startswith("DBCC execution completed.")]


SPECIAL = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
		   "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
FIELD = re.compile(r"[0-9A-Za-z*/,\-]+")


def job_parts(line):
	"""(schedule, command) of a crontab line that runs something, or None"""
	text = line.strip()
	if not text or text.startswith("#") or re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", text):
		return None
	words = text.split(None, 1 if text.startswith("@") else 5)
	if text.startswith("@"):
		return (words[0], words[1]) if len(words) == 2 and (words[0] in SPECIAL or words[0] == "@reboot") else None
	if len(words) == 6 and all(FIELD.fullmatch(word) for word in words[:5]):
		return " ".join(words[:5]), words[5]
	return None


def jobs(text):
	"""Root's crontab's jobs: {command: {schedule, label, watch}}; the first of a command that is there twice"""
	found = {}
	label, watch = "", None
	for line in text.splitlines():
		parts = job_parts(line)
		if parts:
			found.setdefault(parts[1], {"schedule": parts[0], "label": label, "watch": watch or {}})
			label, watch = "", None
			continue
		if LABEL.match(line):
			label = " ".join(LABEL.match(line).group(1).split())
		elif WATCH.match(line):
			watch = parse_watch(WATCH.match(line).group(1))
		elif line.strip():
			label, watch = "", None
	return found


def crontab():
	found = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
	return found.stdout if found.returncode == 0 else ""


def key_of(command):
	return hashlib.sha1(command.encode("utf-8")).hexdigest()[:16]


# ---- when cron should have run a schedule
def field_values(text, low, high):
	values = set()
	for part in text.split(","):
		found = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
		if not found:
			return None
		start = low if found.group(1) == "*" else int(found.group(1))
		end = int(found.group(2)) if found.group(2) is not None else high if found.group(1) == "*" or found.group(3) else start
		step = int(found.group(3) or 1)
		if start < low or end > high or start > end or step < 1:
			return None
		values.update(range(start, end + 1, step))
	return values


def previous_run(schedule, before):
	"""The last time (a timestamp, to the minute) at or before `before` that cron runs a schedule, within a year; None for
	@reboot and what is not read here"""
	fields = SPECIAL.get(schedule, schedule).split()
	if len(fields) != 5:
		return None
	minutes, hours, days, months, weekdays = (field_values(fields[0], 0, 59), field_values(fields[1], 0, 23), field_values(fields[2], 1, 31),
											  field_values(fields[3], 1, 12), field_values(fields[4], 0, 7))
	if None in (minutes, hours, days, months, weekdays):
		return None
	if 7 in weekdays:
		weekdays.add(0)
	# cron runs on either day field when both are limited
	both = fields[2] != "*" and fields[4] != "*"
	at = datetime.fromtimestamp(before).replace(second=0, microsecond=0)
	end = at - timedelta(days=366)
	while at > end:
		weekday = (at.weekday() + 1) % 7
		day_ok = (at.day in days or weekday in weekdays) if both else (at.day in days and weekday in weekdays)
		if at.month not in months or not day_ok:
			at = at.replace(hour=23, minute=59) - timedelta(days=1)
			continue
		if at.hour not in hours:
			at = at.replace(minute=59) - timedelta(hours=1)
			continue
		if at.minute in minutes:
			return at.timestamp()
		at -= timedelta(minutes=1)
	return None


# ---- the state: each job's runs, and what is wrong
def read_json(path, default):
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return default


def write_json(path, data):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{os.getpid()}")
	temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
	os.replace(temporary, path)


def load_settings(path=SETTINGS):
	saved = read_json(path, {})
	return {"enabled": saved.get("enabled") is True, "emails": [item for item in saved.get("emails", []) if isinstance(item, str)]}


def validate_settings(data):
	if not isinstance(data, dict):
		raise ValueError("Expected the alerts' settings")
	enabled, emails = data.get("enabled", False), data.get("emails", [])
	if not isinstance(enabled, bool):
		raise ValueError("The alerts are on or off")
	if not isinstance(emails, list) or len(emails) > 20 or not all(isinstance(item, str) and EMAIL.fullmatch(item) for item in emails):
		raise ValueError("Each recipient must be an e-mail address")
	if enabled and not emails:
		raise ValueError("Give at least one e-mail address for the alerts")
	return {"enabled": enabled, "emails": emails}


def runs(state=STATE):
	"""Each job's last runs, by its key: {command, runs: [{at, seconds, code, failed: {condition: why}}], running}"""
	found = {}
	folder = Path(state) / "jobs"
	for path in folder.glob("*.json") if folder.is_dir() else []:
		item = read_json(path, None)
		if isinstance(item, dict) and isinstance(item.get("command"), str):
			found[path.stem] = item
	return found


class Locked:
	"""One at a time: the alerts' state is read and written by each job's run and by the check"""
	def __init__(self, state):
		Path(state).mkdir(parents=True, exist_ok=True)
		self.path = Path(state) / "lock"

	def __enter__(self):
		self.stream = open(self.path, "w")
		fcntl.flock(self.stream, fcntl.LOCK_EX)
		return self

	def __exit__(self, *args):
		self.stream.close()


# ---- a run
def output_target(command):
	"""Where a job's own output goes: ("file", path) for > or >> to a file, ("tag", tag) for | logger -t tag, or None"""
	found = re.search(r"(?<![0-9&])>>?\s*(['\"]?)(/[^\s'\"]+)\1", command)
	if found:
		return None if found.group(2) == "/dev/null" else ("file", found.group(2))
	found = re.search(r"\|\s*(?:/usr/bin/)?logger\b[^|]*?\s-t\s*(['\"]?)([^\s'\"]+)\1", command)
	return ("tag", found.group(2)) if found else None


def file_size(path):
	try:
		return os.stat(path).st_size
	except OSError:
		return None


def written_since(target, size, started):
	"""What the job wrote where its command sends it, in this run: the file's new end, or the journal's lines"""
	if not target:
		return []
	try:
		if target[0] == "file":
			with open(target[1], "rb") as stream:
				end = stream.seek(0, 2)
				start = size if size is not None and size <= end else 0
				stream.seek(max(start, end - TAIL_BYTES))
				return stream.read().decode("utf-8", "replace").splitlines()
		found = subprocess.run(["journalctl", "-t", target[1], "--since", f"@{int(started)}", "-o", "cat", "--no-pager"],
							   capture_output=True, text=True, timeout=20)
		return found.stdout.splitlines()[-400:]
	except (OSError, subprocess.SubprocessError):
		return []


def shell_of():
	shell = os.environ.get(OWN_SHELL) or "/bin/sh"
	return shell if os.path.isabs(shell) and os.access(shell, os.X_OK) and shell != str(SHELL) else "/bin/sh"


def run_job(argv, state=STATE, now=time.time):
	"""A job, run as cron runs it with harbor-cron-shell as its SHELL (argv: -c COMMAND): its output passed on as it
	comes, its run noted and judged. Its exit code is returned, whatever the noting does."""
	shell = shell_of()
	if len(argv) != 2 or argv[0] != "-c":
		os.execv(shell, [shell, *argv])
	command = argv[1]
	try:
		target = output_target(command)
		size = file_size(target[1]) if target and target[0] == "file" else None
		started = now()
		key = key_of(command)
		running = Path(state) / "running" / f"{key}.json"
		write_json(running, {"command": command, "started": started, "pid": os.getpid()})
	except Exception:  # noqa: BLE001  (nothing of the noting may keep the job from running)
		os.execv(shell, [shell, *argv])
	process = subprocess.Popen([shell, "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
	tail = b""
	out = sys.stdout.buffer
	while True:
		chunk = process.stdout.read1(8192) if hasattr(process.stdout, "read1") else process.stdout.read(8192)
		if not chunk:
			break
		try:
			out.write(chunk)
			out.flush()
		except OSError:
			pass
		tail = (tail + chunk)[-TAIL_BYTES:]
	process.stdout.close()
	code = process.wait()
	try:
		ended = now()
		running.unlink(missing_ok=True)
		lines = tail.decode("utf-8", "replace").splitlines()
		record(command, started, ended, code, lines, written_since(target, size, started), state=state, now=ended)
	except Exception as exc:  # noqa: BLE001
		print(f"harbor-cron-shell could not note the run: {exc}", file=sys.stderr)
	return code


def judge(watch, code, seconds, lines):
	"""What made a run a failure, by the conditions watched: {condition: why}"""
	failed = {}
	if watch.get("exit") and code != 0:
		failed["exit"] = f"Exit code {code}"
	if watch.get("output"):
		bad = error_lines(lines)
		if bad:
			failed["output"] = f"{len(bad)} line{'' if len(bad) == 1 else 's'} like: {bad[-1].strip()[:200]}"
	if watch.get("long") and seconds > watch["long"] * 60:
		failed["long"] = f"Ran for {duration(seconds)}, more than {watch['long']} min"
	return failed


def record(command, started, ended, code, lines, written=(), state=STATE, table=None, now=None, settings=None, sender=None):
	"""A run noted (the last 20 of a job's), judged by what its job watches, and mailed when that changes"""
	key = key_of(command)
	found = jobs(crontab() if table is None else table).get(command, {})
	watch = found.get("watch") or {}
	seconds = max(0, ended - started)
	every = list(lines) + list(written)
	failed = judge(watch, code, seconds, every)
	with Locked(state):
		path = Path(state) / "jobs" / f"{key}.json"
		saved = read_json(path, {})
		history = saved.get("runs") if isinstance(saved.get("runs"), list) else []
		run = {"at": int(started), "seconds": round(seconds, 1), "code": code, "failed": failed}
		write_json(path, {"command": command, "runs": ([run] + history)[:20],
						  "tail": [line[:500] for line in every[-TAIL_LINES:]], "errors": [line[:500] for line in error_lines(every)[-10:]]})
		alerts(state, {key: (command, found, failed, every[-TAIL_LINES:], run)}, ran={key}, now=now or ended, settings=settings, sender=sender)


# ---- the check: missed and long running jobs
def check(state=STATE, table=None, now=None, settings=None, sender=None, alive=None):
	"""Each watched job that cron should have run and has not, or that runs longer than it may: mailed as runs are"""
	now = now or time.time()
	table = crontab() if table is None else table
	saved = read_json(SAVED, {}).get("saved") or {}
	alive = alive or pid_alive
	with Locked(state):
		known = runs(state)
		watched = read_json(Path(state) / "alerts.json", {}).get("watched") or {}
		found = {}
		for command, job in jobs(table).items():
			watch = job["watch"]
			if not watch:
				continue
			key = key_of(command)
			since = max(watched.get(key) or now, saved.get(command) or 0)
			last = (known.get(key, {}).get("runs") or [{}])[0].get("at") or 0
			problems = {}
			if watch.get("missed"):
				due = previous_run(job["schedule"], now - GRACE)
				if due and due > since and last < due - 60:
					problems["missed"] = f"It should have run {stamp(due)}"
			current = read_json(Path(state) / "running" / f"{key}.json", None)
			if watch.get("long") and isinstance(current, dict) and alive(current.get("pid")) and now - current.get("started", now) > watch["long"] * 60:
				problems["long"] = f"Has run for {duration(now - current['started'])}, more than {watch['long']} min"
			found[key] = (command, job, problems, known.get(key, {}).get("tail") or [], None)
		alerts(state, found, ran=set(), now=now, settings=settings, sender=sender, table=table)


def pid_alive(pid):
	try:
		os.kill(int(pid), 0)
		return True
	except (OSError, TypeError, ValueError):
		return False


# ---- what is wrong, and the mail when it changes
def alerts(state, found, ran, now, settings=None, sender=None, table=None):
	"""found: {key: (command, job, problems, tail, run)}. A run (ran) settles what its job's runs showed; the check settles
	missed and long running. A job that fails now and did not before is mailed, and one that works again."""
	path = Path(state) / "alerts.json"
	saved = read_json(path, {})
	open_ = saved.get("open") if isinstance(saved.get("open"), dict) else {}
	watched = saved.get("watched") if isinstance(saved.get("watched"), dict) else {}
	started, solved = [], []
	for key, (command, job, problems, tail, run) in found.items():
		if not job.get("watch"):
			continue
		watched.setdefault(key, int(now))
		before = open_.get(key, {}).get("problems") or {}
		# A run settles everything but a long run still going; the check settles only missed and long running
		if key in ran:
			kept = {}
		else:
			kept = {name: why for name, why in before.items() if name not in ("missed", "long")}
		now_wrong = {**kept, **problems}
		item = {"command": command, "name": job.get("label") or command, "problems": now_wrong, "tail": tail[-TAIL_LINES:], "run": run}
		if now_wrong and not before:
			open_[key] = {**item, "since": int(now)}
			started.append(open_[key])
		elif now_wrong:
			open_[key] = {**open_[key], **item}
			new = set(now_wrong) - set(before)
			if new:
				started.append({**open_[key], "new": sorted(new)})
		elif before:
			solved.append({**open_.pop(key), "solved": int(now), "run": run})
	# Not watched any more, or gone from root's crontab: forgotten, without a mail
	if table is not None:
		current = {key_of(command) for command, job in jobs(table).items() if job["watch"]}
		open_ = {key: value for key, value in open_.items() if key in current}
		watched = {key: value for key, value in watched.items() if key in current}
	saved.update(open=open_, watched=watched)
	settings = settings or load_settings()
	if (started or solved) and settings["enabled"] and settings["emails"]:
		try:
			mail = mailer()
			host = socket.gethostname()
			(sender or mail.send)(mail.load_mail(), settings["emails"], subject(started, solved, host), text_body(started, solved, open_, host),
								  html=html_body(started, solved, open_, host, now))
			saved.pop("mail_error", None)
		except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own)
			saved["mail_error"] = {"at": int(now), "error": str(exc)}
			print(f"The job alert could not be sent: {exc}", file=sys.stderr)
	write_json(path, saved)


def stamp(at):
	return datetime.fromtimestamp(at).strftime("%d.%m.%Y %H:%M")


def duration(seconds):
	seconds = int(seconds)
	if seconds < 60:
		return f"{seconds} s"
	minutes = seconds // 60
	days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
	if days:
		return f"{days} d {hours} h" if hours else f"{days} d"
	if hours:
		return f"{hours} h {minutes} min" if minutes else f"{hours} h"
	return f"{minutes} min"


def subject(started, solved, host):
	if started:
		names = [item["name"] for item in started]
		return f"[{host}] \U0001F534 Job failed: {', '.join(names[:2])}{' and more' if len(names) > 2 else ''}"
	names = [item["name"] for item in solved]
	return f"[{host}] ✅ Job works again: {', '.join(names[:2])}{' and more' if len(names) > 2 else ''}"


def text_body(started, solved, open_, host):
	lines = [f"Scheduled jobs on {host}.", ""]
	for item in started:
		lines.append(f"Failed: {item['name']}")
		lines += [f"  {CONDITIONS[name]}: {why}" for name, why in item["problems"].items()]
		lines.append(f"  Command: {item['command']}")
		if item.get("tail"):
			lines += ["  The end of its output:"] + [f"    {line}" for line in item["tail"][-15:]]
		lines.append("")
	for item in solved:
		lines.append(f"Works again: {item['name']} (it failed for {duration(item['solved'] - item['since'])})")
	told = {item["command"] for item in started}
	rest = [item for item in open_.values() if item["command"] not in told]
	if rest:
		lines += ["", "Also failing:"] + [f"  {item['name']} since {stamp(item['since'])}" for item in rest]
	elif not open_:
		lines += ["", "Every watched job works now."]
	return "\n".join(lines) + "\n"


def output_block(lines):
	esc = mailer().esc
	return ('<div class="rw rl tx2" style="margin:-2px 0 12px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;'
			'font:12px/17px ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;color:#374151">'
			+ "<br>".join(esc(line) for line in lines) + "</div>")


def html_body(started, solved, open_, host, now):
	mail = mailer()
	esc, row, section = mail.esc, mail.row, mail.section
	title_of = lambda item: f'<span class="tx">{esc(item["name"])}</span>'
	sections = []
	if started:
		parts = []
		for item in started:
			why = " · ".join(f"{CONDITIONS[name]}: {text}" for name, text in item["problems"].items())
			run = item.get("run") or {}
			meta = f"Since {stamp(item['since'])}" + (f" &middot; last run {stamp(run['at'])}, {duration(run['seconds'])}" if run else "")
			label = CONDITIONS[next(iter(item["problems"]))] if len(item["problems"]) == 1 else "Failed"
			parts.append(row("red", label, title_of(item), why, meta + f'<br><code style="font-size:11px;word-break:break-all">{esc(item["command"])}</code>'))
			if item.get("tail"):
				parts.append(output_block(item["tail"][-15:]))
		sections.append(section("Failed", "".join(parts), len(started)))
	if solved:
		sections.append(section("Works again", "".join(
			row("green", "Works again", title_of(item), f"Was: " + "; ".join(CONDITIONS[name] for name in item["problems"]),
				f"It failed for {duration(item['solved'] - item['since'])}") for item in solved), len(solved)))
	rest = [item for item in open_.values() if item["command"] not in {other["command"] for other in started}]
	if rest:
		sections.append(section("Also failing", "".join(
			row("red", "Failing", title_of(item), " · ".join(CONDITIONS[name] for name in item["problems"]), f"Since {stamp(item['since'])}")
			for item in rest), len(rest)))
	if started:
		title = f"{started[0]['name']} failed" if len(started) == 1 else f"{len(started)} jobs failed"
	else:
		title = f"{solved[0]['name']} works again" if len(solved) == 1 else f"{len(solved)} jobs work again"
	lead = f"{len(open_)} watched job{' is' if len(open_) == 1 else 's are'} failing now." if open_ else "Every watched job works now."
	footer = (f"Root's scheduled jobs on <strong>{esc(host)}</strong>. Sent {stamp(now)}. What is watched, what is a failure and who is "
			  "mailed is chosen under Settings, Notifications in Harbor.")
	return mail.page(host, "red" if open_ else "green", title, lead, sections, footer, "Scheduled jobs")


def test_mail(emails, state=STATE, table=None, now=None, sender=None):
	"""A test of the alerts: how each watched job is now, mailed to the addresses given. Nothing is kept."""
	emails = validate_settings({"enabled": True, "emails": emails})["emails"]
	now = now or time.time()
	host = socket.gethostname()
	mail = mailer()
	esc, row, section = mail.esc, mail.row, mail.section
	open_ = read_json(Path(state) / "alerts.json", {}).get("open") or {}
	known = runs(state)
	parts, lines = [], [f"A test of the alerts of the scheduled jobs on {host}: this is how they look.", ""]
	watched = [(command, job) for command, job in jobs(crontab() if table is None else table).items() if job["watch"]]
	for command, job in watched:
		key = key_of(command)
		name = job["label"] or command
		last = (known.get(key, {}).get("runs") or [None])[0]
		what = ", ".join(CONDITIONS[item].lower() for item in job["watch"])
		meta = (f"Last run {stamp(last['at'])} &middot; {duration(last['seconds'])} &middot; exit code {last['code']}" if last else "Not run since the alerts were on")
		if key in open_:
			parts.append(row("red", "Failing", f'<span class="tx">{esc(name)}</span>', " · ".join(open_[key]["problems"].values()), meta))
			lines.append(f"Failing: {name}: " + "; ".join(open_[key]["problems"].values()))
		else:
			parts.append(row("green", "Works", f'<span class="tx">{esc(name)}</span>', f"Watched for: {what}", meta))
			lines.append(f"Works: {name} (watched for: {what})")
	if not watched:
		lines.append("No job is watched yet.")
	subject_text = f"[{host}] TEST: {len(watched)} watched job{'' if len(watched) == 1 else 's'}, {len(open_)} failing"
	html = mail.page(host, "blue", f"Test: {len(watched)} watched job{'' if len(watched) == 1 else 's'}",
					 "A test of the alerts of the scheduled jobs: this is how they look. Nothing was kept.",
					 [section("Watched jobs", "".join(parts), len(watched))] if parts else [],
					 f"Root's scheduled jobs on <strong>{esc(host)}</strong>. Sent {stamp(now)}.", "Test: scheduled jobs")
	body = "\n".join(lines) + "\n"
	error = ""
	try:
		(sender or mail.send)(mail.load_mail(), emails, subject_text, body, html=html)
	except Exception as exc:  # noqa: BLE001
		error = f"Not sent: {exc}"
	return {"subject": subject_text, "body": body, "to": emails if not error else [], "error": error}


# ---- on and off: the shell in root's crontab, and the check
def wrapper(script):
	"""harbor-cron-shell: this script for a job (-c COMMAND), the crontab's own shell when it cannot be"""
	return ("#!/bin/sh\n"
			"# VNS Harbor: runs each job of root's crontab and notes how it ended (Settings, Notifications, Job alerts)\n"
			f'W="{script}"\n'
			'if [ "$1" = "-c" ] && [ "$#" -eq 2 ] && [ -f "$W" ] && [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 "$W" --shell "$@"; fi\n'
			f'exec "${{{OWN_SHELL}:-/bin/sh}}" "$@"\n')


def with_shell(text, on):
	"""Root's crontab with harbor-cron-shell as its SHELL (its own kept as HARBOR_CRON_SHELL, where it was), or back"""
	lines = [line for line in text.splitlines() if line != NOTE and line.strip() != f"SHELL={SHELL}"]
	shell = re.compile(r"^\s*SHELL\s*=")
	own = re.compile(rf"^\s*{OWN_SHELL}\s*=")
	if on:
		lines = [shell.sub(f"{OWN_SHELL}=", line, 1) for line in lines]
		lines = [NOTE, f"SHELL={SHELL}"] + lines
	else:
		lines = [own.sub("SHELL=", line, 1) for line in lines]
	return "".join(line + "\n" for line in lines)


def script_path():
	script = Path(__file__).resolve()
	release = re.fullmatch(r"/opt/harbor/releases/[^/]+/(.+)", str(script))
	return Path("/opt/harbor/current") / release[1] if release else script


def enable(on):
	script = script_path()
	if re.search(r"[\s%\"$`\\]", str(script)):
		raise ValueError("The alerts need a path without spaces, quotes or percent signs")
	if on:
		SHELL.write_text(wrapper(script), encoding="utf-8")
		SHELL.chmod(0o755)
		CHECK.write_text("SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
						 f'*/5 * * * * root /usr/bin/python3 "{script}" --check 2>&1 | /usr/bin/logger -t vns-cron-watch\n', encoding="utf-8")
		CHECK.chmod(0o644)
	else:
		CHECK.unlink(missing_ok=True)
	table = crontab()
	changed = with_shell(table, on)
	if changed != table and (table or on):
		subprocess.run(["crontab", "-"], input=changed, text=True, check=True)
	if not on:
		SHELL.unlink(missing_ok=True)


def main():
	if len(sys.argv) > 1 and sys.argv[1] == "--shell":
		return run_job(sys.argv[2:])
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--check", action="store_true")
	parser.add_argument("--enable", action="store_true")
	parser.add_argument("--disable", action="store_true")
	parser.add_argument("--test", nargs="+", metavar="EMAIL")
	args = parser.parse_args()
	if args.enable or args.disable:
		enable(args.enable)
	elif args.check:
		check()
	elif args.test:
		try:
			print(json.dumps(test_mail(args.test)))
		except ValueError as exc:
			print(json.dumps({"error": str(exc)}))
	else:
		parser.print_help()
		return 2
	return 0


if __name__ == "__main__":
	sys.exit(main())
