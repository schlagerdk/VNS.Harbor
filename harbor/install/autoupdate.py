"""Harbor updates itself by itself, when it is turned on: once a day, at the time chosen, the newest release on GitHub is
installed as Update to … in the admin installs it (harbor self update), and the recipients of the Server health
alerts are mailed when it updated or could not. Off by default; turned on and off under Settings, Updates (1.0.97).

  /etc/cron.d/vns-harbor-update    the job: there while it is on
  /etc/harbor/auto-update.json     the time it runs at
  /var/lib/harbor/auto-update.json how its last run ended
  /var/log/harbor-auto-update.log  what its runs wrote
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import time

from harbor.core.paths import BIN, LIB
from harbor.core.system import Stop

CRON = Path("/etc/cron.d/vns-harbor-update")
CONFIG = Path("/etc/harbor/auto-update.json")
STATE = Path("/var/lib/harbor/auto-update.json")
LOG = Path("/var/log/harbor-auto-update.log")
HEALTH = Path("/etc/harbor/health-alerts.json")
# One update at a time: the one run here, and one started in the admin (both take it in update.update)
LOCK = Path("/run/lock/harbor-update.lock")
DEFAULT_TIME = "04:00"
TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
# Each hour, installed as soon as a release is out (1.0.97): at a minute of the server's own, so servers do not ask
# GitHub at the same moment
SOON = "soon"


def valid(at):
    return at == SOON or bool(TIME.fullmatch(at or ""))


def own_minute():
    return sum(socket.gethostname().encode()) % 60


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def status(system):
    """{on, time, last}: whether it is on, when it runs, and how its last run ended"""
    config = read_json(system.path(CONFIG), {})
    at = config.get("time") if isinstance(config, dict) and valid(str(config.get("time", ""))) else DEFAULT_TIME
    return {"on": system.path(CRON).is_file(), "time": at, "last": read_json(system.path(STATE), None)}


def cron_text(at):
    when = f"{own_minute()} *" if at == SOON else "{1} {0}".format(*(int(part) for part in TIME.fullmatch(at).groups()))
    return ("SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
            f"{when} * * * root /usr/bin/python3 {BIN} self auto-update run >> {LOG} 2>&1\n")


def turn_on(system, at=DEFAULT_TIME):
    if not valid(at):
        raise Stop("The time is HH:MM, such as 04:00, or soon (each hour, as soon as a release is out).")
    step = "Update Harbor by itself as soon as a release is out" if at == SOON else f"Update Harbor by itself each day at {at}"
    system.plan([step])
    system.step(step)
    write_json(system.path(CONFIG), {"time": at})
    cron = system.path(CRON)
    cron.parent.mkdir(parents=True, exist_ok=True)
    cron.write_text(cron_text(at), encoding="utf-8")
    os.chmod(cron, 0o644)
    system.say(("Harbor looks for a new release each hour and installs it as soon as it is out." if at == SOON
                else f"Harbor installs its newest release by itself each day at {at} (server time).") + " The sites keep running while it does.")


def turn_off(system):
    step = "Stop updating Harbor by itself"
    system.plan([step])
    system.step(step)
    cron = system.path(CRON)
    if cron.is_file():
        cron.unlink()
    system.say("Harbor is updated only when someone chooses Update under Settings, Updates.")


def busy(lock=LOCK):
    """Whether an update of Harbor runs now (one started in the admin, or another run of this)"""
    lock = Path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def run(system, lock=LOCK, mailer=None, now=None):
    """Its daily run: the newest release installed when there is one newer than this, and how it ended kept (and
    mailed, when it updated or could not). Nothing is done while another update runs."""
    from harbor.install import update
    now = int(now or time.time())
    installed = update.installed_version()
    if busy(lock):
        result = {"at": now, "result": "busy", "from": installed}
        system.say("Another update of Harbor runs now: this one waits for tomorrow.")
    else:
        try:
            found = update.newer(update.release_versions(system))
        except Stop as error:
            found, result = None, {"at": now, "result": "failed", "from": installed, "error": str(error)}
        if found == []:
            result = {"at": now, "result": "current", "from": installed}
            system.say(f"Harbor is up to date ({installed}).")
        elif found:
            ref = "v" + found[0]
            system.say(f"Update Harbor {installed or '(unknown)'} to {ref}")
            # Run as Update in the admin runs it: the fetched release installs itself
            outcome = system.run("/usr/bin/python3", str(BIN), "self", "update", "--ref", ref, check=False, capture=True)
            output = ((outcome.stdout or "") + (outcome.stderr or "")).strip()
            if output:
                print(output, flush=True)
            result = {"at": now, "result": "updated" if outcome.ok else "failed", "from": installed, "to": found[0]}
            if not outcome.ok:
                result["error"] = (output.splitlines() or ["The update stopped."])[-1][:300]
    write_json(system.path(STATE), result)
    if result["result"] in ("updated", "failed"):
        mail(system, result, mailer)
    return result


def site_report():
    spec = importlib.util.spec_from_file_location("vns_autoupdate_site_report", Path(LIB) / "site_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mail(system, result, mailer=None):
    """Told to the recipients of the Server health alerts (while they are on): it updated, or could not"""
    health = read_json(system.path(HEALTH), {})
    emails = health.get("emails") if isinstance(health, dict) and health.get("enabled") else None
    if not emails:
        return
    report = mailer or site_report()
    host = socket.gethostname()
    updated = result["result"] == "updated"
    to = "v" + result.get("to", "") if result.get("to") else "the newest release"
    title = f"Harbor updated to {to}" if updated else f"Harbor could not update to {to}"
    lead = (f"Harbor on {host} installed {to} by itself (it was v{result.get('from') or '?'}). The sites kept running."
            if updated else f"Harbor on {host} tried to install {to} by itself and stopped; it runs v{result.get('from') or '?'} as before.")
    rows = report.row("green" if updated else "red", "Updated" if updated else "Not updated", f'<span class="tx">{report.esc(title)}</span>',
                      "" if updated else result.get("error", ""), time.strftime("%Y-%m-%d %H:%M", time.localtime(result["at"])))
    footer = (f"Automatic updates of Harbor on <strong>{report.esc(host)}</strong>, turned on and off under Settings, Updates. "
              f"What changed is under Updates, What changed; the run's log is {LOG}.")
    html = report.page(host, "green" if updated else "red", title, lead, [report.section("Harbor", rows)], footer, "Harbor update")
    try:
        report.send(report.load_mail(), emails, f"[{host}] {title}", lead + "\n", html=html)
    except Exception as error:  # noqa: BLE001  (a mail that is not sent does not undo the update)
        system.warn(f"The mail could not be sent: {error}")
