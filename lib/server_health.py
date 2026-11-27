#!/usr/bin/env python3
"""How the server itself is: its disks, memory, swap and load over time, its OS updates, and a mail when one of them
needs someone (Settings, Health and Notifications).

cron runs it every 5 minutes (/etc/cron.d/vns-health, installed with keep-alive): each run adds a sample (kept 7 days),
reads the OS updates once an hour, and, while the alerts are on, mails what crossed its limit and what is fine again:

  disk      a disk used past its percent
  memory    memory used past its percent, two samples in a row (not a moment's peak)
  swap      swap used past its percent
  updates   security updates waiting (or any updates, as chosen)
  reboot    the server asks to be restarted after an update

  server_health.py --run             a sample, the updates when due, and the alerts (cron runs this)
  server_health.py --json            how the server is now, with the samples, as JSON (the admin's Health)
  server_health.py --updates         read the OS updates now
  server_health.py --install         the cron job
  server_health.py --unattended      turn on automatic security updates (unattended-upgrades)
  server_health.py --test EMAIL...   a test mail of how the server is now
"""
import argparse
from datetime import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

STATE = Path("/var/lib/harbor/health")
SETTINGS = Path("/etc/harbor/health-alerts.json")
CRON = Path("/etc/cron.d/vns-health")
AUTO_UPGRADES = Path("/etc/apt/apt.conf.d/20auto-upgrades")
REBOOT = Path("/var/run/reboot-required")
APT_CHECK = Path("/usr/lib/update-notifier/apt-check")
KEEP = 7 * 86400
UPDATES_EVERY = 3600
FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs"}
DEFAULTS = {"enabled": False, "emails": [], "disk": 85, "memory": 90, "swap": 50, "updates": "security", "reboot": True}
CHECKS = {
	"disk": "Disk almost full",
	"memory": "Memory almost full",
	"swap": "Swap in use",
	"updates": "Updates waiting",
	"reboot": "Restart needed",
}
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

report = None


def mailer():
	"""site_report (the mail server, and the mail's HTML), loaded only when a mail is sent"""
	global report
	if report is None:
		spec = importlib.util.spec_from_file_location("vns_health_site_report", Path(__file__).with_name("site_report.py"))
		report = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(report)
	return report


def read_json(path, default):
	try:
		return json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return default


def write_json(path, data):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{os.getpid()}")
	temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
	os.replace(temporary, path)


# ---- the settings
def load_settings(path=SETTINGS):
	saved = read_json(path, {})
	return {**DEFAULTS, **{key: value for key, value in saved.items() if key in DEFAULTS}} if isinstance(saved, dict) else dict(DEFAULTS)


def validate_settings(data):
	if not isinstance(data, dict) or set(data) - set(DEFAULTS):
		raise ValueError("Expected the health alerts' settings")
	data = {**DEFAULTS, **data}
	if not isinstance(data["enabled"], bool):
		raise ValueError("The alerts are on or off")
	emails = data["emails"]
	if not isinstance(emails, list) or len(emails) > 20 or not all(isinstance(item, str) and EMAIL.fullmatch(item) for item in emails):
		raise ValueError("Each recipient must be an e-mail address")
	if data["enabled"] and not emails:
		raise ValueError("Give at least one e-mail address for the alerts")
	for key in ("disk", "memory", "swap"):
		value = data[key]
		# 0: not watched
		if isinstance(value, bool) or not isinstance(value, int) or not (value == 0 or 10 <= value <= 99):
			raise ValueError(f"The {key} limit is a percent from 10 to 99, or 0 for none")
	if data["updates"] not in ("security", "any", "off"):
		raise ValueError("Updates are security, any or off")
	if not isinstance(data["reboot"], bool):
		raise ValueError("Restart needed is on or off")
	return data


# ---- a sample
def memory(meminfo=Path("/proc/meminfo")):
	"""{memory, swap}: the percent used of each (swap None without any)"""
	values = {}
	for line in meminfo.read_text().splitlines():
		name, _, rest = line.partition(":")
		values[name] = int(rest.split()[0]) if rest.split() else 0
	total, available = values.get("MemTotal", 0), values.get("MemAvailable", values.get("MemFree", 0))
	swap_total, swap_free = values.get("SwapTotal", 0), values.get("SwapFree", 0)
	return {"memory": round(100 * (total - available) / total, 1) if total else 0,
			"memory_mb": round(total / 1024), "swap": round(100 * (swap_total - swap_free) / swap_total, 1) if swap_total else None,
			"swap_mb": round(swap_total / 1024)}


def disks(mounts=Path("/proc/mounts"), statvfs=os.statvfs):
	"""Each disk the server keeps files on, once (by its device): {mount: {used: percent, size_gb, free_gb}}"""
	found, seen = {}, set()
	for line in mounts.read_text().splitlines():
		parts = line.split()
		if len(parts) < 3 or parts[2] not in FILESYSTEMS or parts[0] in seen or parts[1].startswith(("/snap/", "/var/snap/")):
			continue
		seen.add(parts[0])
		mount = parts[1].replace("\\040", " ")
		try:
			stat = statvfs(mount)
		except OSError:
			continue
		size = stat.f_blocks * stat.f_frsize
		if not size:
			continue
		free = stat.f_bavail * stat.f_frsize
		used = size - stat.f_bfree * stat.f_frsize
		found[mount] = {"used": round(100 * used / (used + free), 1) if used + free else 0, "size_gb": round(size / 1024 ** 3, 1),
						"free_gb": round(free / 1024 ** 3, 1)}
	return found


def sample(now=None):
	load = Path("/proc/loadavg").read_text().split()
	return {"at": int(now or time.time()), "load": float(load[0]), "cpus": os.cpu_count() or 1, **memory(), "disks": disks()}


# ---- the OS updates
def package_installed(name):
	found = subprocess.run(["dpkg-query", "-W", "-f=${Status}", name], capture_output=True, text=True)
	return found.returncode == 0 and "install ok installed" in found.stdout


def unattended(path=AUTO_UPGRADES):
	"""Whether unattended-upgrades installs the security updates by itself"""
	try:
		text = path.read_text(encoding="utf-8")
	except OSError:
		return False
	return bool(re.search(r'APT::Periodic::Unattended-Upgrade\s+"1"', text)) and package_installed("unattended-upgrades")


def updates(now=None):
	"""The updates waiting: {count, security, packages: [{name, version, security}], reboot, reboot_packages,
	unattended, checked}. From apt's lists as they are (apt's own timer refreshes them)."""
	listed = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade"], capture_output=True, text=True, timeout=120)
	packages = []
	for line in listed.stdout.splitlines():
		found = re.match(r"^Inst (\S+) (?:\[[^\]]*\] )?\((\S+) (.*?)\)", line)
		if found:
			packages.append({"name": found.group(1), "version": found.group(2), "security": "-security" in found.group(3)})
	reboot = REBOOT.exists()
	try:
		reboot_packages = sorted(set(Path(str(REBOOT) + ".pkgs").read_text().split()))
	except OSError:
		reboot_packages = []
	return {"count": len(packages), "security": sum(1 for item in packages if item["security"]), "packages": packages[:300],
			"reboot": reboot, "reboot_packages": reboot_packages, "unattended": unattended(), "checked": int(now or time.time())}


# ---- what is wrong
def problems(current, found_updates, settings, before=None):
	"""{key: why} of what crossed its limit: a disk by its mount (disk:/), memory, swap, updates, reboot"""
	before = before or {}
	wrong = {}
	for mount, disk in (current.get("disks") or {}).items():
		if settings["disk"] and disk["used"] >= settings["disk"]:
			wrong[f"disk:{mount}"] = f"{mount} is {disk['used']:g} % full, {disk['free_gb']:g} GB free"
	if settings["memory"] and current.get("memory", 0) >= settings["memory"]:
		# A moment's peak is not told: memory past its limit twice in a row is
		if before.get("memory_high") or "memory" in before.get("open", {}):
			wrong["memory"] = f"{current['memory']:g} % of {current['memory_mb'] / 1024:.1f} GB used"
	if settings["swap"] and current.get("swap") is not None and current["swap"] >= settings["swap"]:
		wrong["swap"] = f"{current['swap']:g} % of {current['swap_mb'] / 1024:.1f} GB swap used"
	if found_updates:
		count = found_updates["security"] if settings["updates"] == "security" else found_updates["count"] if settings["updates"] == "any" else 0
		if count:
			kind = "security update" if settings["updates"] == "security" else "update"
			wrong["updates"] = f"{count} {kind}{'' if count == 1 else 's'} waiting" + ("" if found_updates["unattended"] else ", and they are not installed by themselves")
		if settings["reboot"] and found_updates["reboot"]:
			names = ", ".join(found_updates["reboot_packages"][:5])
			wrong["reboot"] = "The server asks to be restarted" + (f" after updating {names}" if names else "")
	return wrong


def check_of(key):
	return key.split(":", 1)[0]


def run(state=STATE, now=None, settings=None, sender=None, current=None, read_updates=None):
	"""A sample (kept 7 days), the updates once an hour, and a mail of what crossed its limit or is fine again"""
	now = int(now or time.time())
	Path(state).mkdir(parents=True, exist_ok=True)
	with open(Path(state) / "lock", "w") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		current = current or sample(now)
		samples = [item for item in read_json(Path(state) / "samples.json", []) if isinstance(item, dict) and item.get("at", 0) > now - KEEP]
		samples.append(current)
		write_json(Path(state) / "samples.json", samples)
		found = read_json(Path(state) / "updates.json", None)
		if not isinstance(found, dict) or now - found.get("checked", 0) >= UPDATES_EVERY:
			try:
				found = (read_updates or updates)(now)
				write_json(Path(state) / "updates.json", found)
			except (OSError, subprocess.SubprocessError) as exc:
				print(f"The updates could not be read: {exc}", file=sys.stderr)
		settings = settings or load_settings()
		saved = read_json(Path(state) / "alerts.json", {})
		opened = saved.get("open") if isinstance(saved.get("open"), dict) else {}
		wrong = problems(current, found, settings, saved)
		saved["memory_high"] = bool(settings["memory"] and current.get("memory", 0) >= settings["memory"])
		started = {key: why for key, why in wrong.items() if key not in opened}
		solved = {key: value for key, value in opened.items() if key not in wrong}
		opened = {key: {"why": why, "since": opened.get(key, {}).get("since", now)} for key, why in wrong.items()}
		saved["open"] = opened
		if (started or solved) and settings["enabled"] and settings["emails"]:
			try:
				mail = mailer()
				host = socket.gethostname()
				(sender or mail.send)(mail.load_mail(), settings["emails"], subject(started, solved, host),
									  text_body(started, solved, opened, host), html=html_body(started, solved, opened, host, now, current))
				saved.pop("mail_error", None)
			except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own)
				saved["mail_error"] = {"at": now, "error": str(exc)}
				print(f"The health alert could not be sent: {exc}", file=sys.stderr)
		write_json(Path(state) / "alerts.json", saved)
	return wrong


# ---- the mail
def stamp(at):
	return datetime.fromtimestamp(at).strftime("%d.%m.%Y %H:%M")


def name_of(key):
	return f"{CHECKS['disk']}: {key.split(':', 1)[1]}" if key.startswith("disk:") else CHECKS[key]


def subject(started, solved, host):
	if started:
		names = [name_of(key) for key in started]
		return f"[{host}] \U0001F534 {names[0]}" + (f" and {len(names) - 1} more" if len(names) > 1 else "")
	names = [name_of(key) for key in solved]
	return f"[{host}] ✅ Fine again: {names[0]}" + (f" and {len(names) - 1} more" if len(names) > 1 else "")


def text_body(started, solved, opened, host):
	lines = [f"The health of {host}.", ""]
	lines += [f"Needs someone: {name_of(key)}: {why}" for key, why in started.items()]
	lines += [f"Fine again: {name_of(key)} (was: {value['why']})" for key, value in solved.items()]
	rest = [key for key in opened if key not in started]
	if rest:
		lines += ["", "Still:"] + [f"  {name_of(key)}: {opened[key]['why']} (since {stamp(opened[key]['since'])})" for key in rest]
	elif not opened:
		lines += ["", "Everything is fine now."]
	return "\n".join(lines) + "\n"


def html_body(started, solved, opened, host, now, current=None):
	mail = mailer()
	esc, row, section = mail.esc, mail.row, mail.section
	tone = lambda key: "amber" if check_of(key) in ("updates", "reboot") else "red"
	title = lambda key: f'<span class="tx">{esc(name_of(key))}</span>'
	sections = []
	if started:
		sections.append(section("Needs someone", "".join(row(tone(key), CHECKS[check_of(key)], title(key), why, f"Since {stamp(opened[key]['since'])}")
														  for key, why in started.items()), len(started)))
	if solved:
		sections.append(section("Fine again", "".join(row("green", "Fine again", title(key), f"Was: {value['why']}",
														  f"It lasted {mail.duration(now - value['since'])}") for key, value in solved.items()), len(solved)))
	rest = [key for key in opened if key not in started]
	if rest:
		sections.append(section("Still", "".join(row(tone(key), CHECKS[check_of(key)], title(key), opened[key]["why"], f"Since {stamp(opened[key]['since'])}")
												 for key in rest), len(rest)))
	if current:
		parts = [f"Memory {current.get('memory', 0):g} %"] + ([f"swap {current['swap']:g} %"] if current.get("swap") is not None else []) + \
			[f"{mount} {disk['used']:g} %" for mount, disk in (current.get("disks") or {}).items()] + [f"load {current.get('load', 0):g}"]
		sections.append(section("Now", f'<div class="mu" style="font-size:13px;line-height:20px;color:#6b7280">{esc(" · ".join(parts))}</div>'))
	heading = (name_of(next(iter(started))) if len(started) == 1 else f"{len(started)} things need someone") if started \
		else (f"{name_of(next(iter(solved)))}: fine again" if len(solved) == 1 else f"{len(solved)} things are fine again")
	lead = f"{len(opened)} thing{' needs' if len(opened) == 1 else 's need'} someone now." if opened else "Everything is fine now."
	footer = (f"The health of <strong>{esc(host)}</strong>, checked every 5 minutes. Sent {stamp(now)}. What is watched and who is mailed is "
			  "chosen under Settings, Notifications in Harbor.")
	return mail.page(host, ("red" if any(tone(key) == "red" for key in opened) else "amber") if opened else "green", heading, lead, sections, footer, "Server health")


def test_mail(emails, state=STATE, sender=None, now=None):
	"""How the server is now, by the limits saved, mailed to the addresses given. Nothing is kept."""
	settings = validate_settings({**load_settings(), "enabled": True, "emails": emails})
	now = int(now or time.time())
	host = socket.gethostname()
	current = sample(now)
	wrong = problems(current, read_json(Path(state) / "updates.json", None), settings, {"memory_high": True})
	opened = {key: {"why": why, "since": now} for key, why in wrong.items()}
	mail = mailer()
	html = html_body(wrong, {}, opened, host, now, current) if wrong else mail.page(
		host, "blue", "Test: everything is fine", "A test of the health alerts: this is how they look. Nothing was kept.",
		[], f"The health of <strong>{mail.esc(host)}</strong>. Sent {stamp(now)}.", "Test: server health")
	subject_text = f"[{host}] TEST: server health, {len(wrong)} thing{'' if len(wrong) == 1 else 's'} past its limit"
	body = "A test of the health alerts: this is how they look.\n\n" + text_body(wrong, {}, opened, host)
	error = ""
	try:
		(sender or mail.send)(mail.load_mail(), settings["emails"], subject_text, body, html=html)
	except Exception as exc:  # noqa: BLE001
		error = f"Not sent: {exc}"
	return {"subject": subject_text, "body": body, "to": settings["emails"] if not error else [], "error": error}


# ---- what the admin shows
def overview(state=STATE):
	saved = read_json(Path(state) / "alerts.json", {})
	return {"now": sample(), "samples": read_json(Path(state) / "samples.json", []), "updates": read_json(Path(state) / "updates.json", None),
			"open": saved.get("open") or {}, "mail_error": saved.get("mail_error"), "installed": CRON.is_file(), "settings": load_settings()}


def install(cron=CRON):
	script = Path(__file__).resolve()
	release = re.fullmatch(r"/opt/harbor/releases/[^/]+/(.+)", str(script))
	if release:
		script = Path("/opt/harbor/current") / release[1]
	if re.search(r"[\s%\"]", str(script)):
		raise ValueError("The cron job needs a path without spaces, quotes or percent signs")
	cron.write_text("SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
					f'*/5 * * * * root /usr/bin/python3 "{script}" --run 2>&1 | /usr/bin/logger -t vns-health\n', encoding="utf-8")
	cron.chmod(0o644)


def turn_on_unattended():
	"""unattended-upgrades installed and on, for the security updates (Ubuntu's own defaults for which)"""
	env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
	if not package_installed("unattended-upgrades"):
		subprocess.run(["apt-get", "install", "-y", "unattended-upgrades"], check=True, env=env)
	AUTO_UPGRADES.write_text('APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n', encoding="utf-8")


def main():
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--run", action="store_true")
	parser.add_argument("--json", action="store_true")
	parser.add_argument("--updates", action="store_true")
	parser.add_argument("--install", action="store_true")
	parser.add_argument("--unattended", action="store_true")
	parser.add_argument("--test", nargs="+", metavar="EMAIL")
	args = parser.parse_args()
	if args.install:
		install()
	elif args.run:
		run()
	elif args.updates:
		write_json(STATE / "updates.json", updates())
		print(json.dumps(read_json(STATE / "updates.json", {})))
	elif args.unattended:
		turn_on_unattended()
		write_json(STATE / "updates.json", updates())
	elif args.json:
		print(json.dumps(overview()))
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
