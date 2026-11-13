"""A site's service as data, for the admin's status window (it was systemctl status in a terminal). Read with
systemctl show and journalctl, here or over SSH, so it works on a server without harbor."""
import re
import shlex

from harbor.environments import settings

PROPERTIES = ("ActiveState", "SubState", "LoadState", "UnitFileState", "Result", "Description", "MainPID",
              "ActiveEnterTimestamp", "InactiveEnterTimestamp", "MemoryCurrent", "CPUUsageNSec", "TasksCurrent", "NRestarts",
              "ExecMainStatus")
SEPARATOR = "--- harbor log ---"
PROBLEM = re.compile(r"\b(?:errors?|fail(?:ed|ure)?|fatal|\w*exception|traceback|warn(?:ing)?s?|crit(?:ical)?)\b", re.I)
UNIT = re.compile(r"[A-Za-z0-9@._:-]+")


def number(value):
    return int(value) if value.isdigit() else None


def timestamp(value):
    """systemctl show --timestamp=unix writes @<seconds>"""
    return int(value[1:]) if value.startswith("@") and value[1:].isdigit() else None


def read(text):
    """The properties and log lines out of what the command printed"""
    head, _, log = text.partition(SEPARATOR + "\n")
    values = {}
    for line in head.splitlines():
        name, equals, value = line.partition("=")
        if equals:
            values[name] = value
    port = re.search(r"(?:localhost|127\.0\.0\.1|\*):([0-9]+)", values.get("ExecStart", "") + "\n" + head)
    lines = [line for line in log.splitlines() if line and not line.startswith("-- ")]
    active = values.get("ActiveState", "")
    return {
        "active": active,
        "sub": values.get("SubState", ""),
        "loaded": values.get("LoadState", ""),
        "enabled": values.get("UnitFileState", ""),
        "result": values.get("Result", ""),
        "description": values.get("Description", ""),
        "pid": number(values.get("MainPID", "")) or None,
        "since": timestamp(values.get("ActiveEnterTimestamp" if active == "active" else "InactiveEnterTimestamp", "")),
        "memory": number(values.get("MemoryCurrent", "")),
        "cpu_seconds": round(number(values.get("CPUUsageNSec", "")) / 1e9, 1) if number(values.get("CPUUsageNSec", "")) is not None else None,
        "tasks": number(values.get("TasksCurrent", "")),
        "restarts": number(values.get("NRestarts", "")),
        "exit_status": number(values.get("ExecMainStatus", "")),
        "port": int(port[1]) if port else None,
        "log": lines,
        "log_problem": any(PROBLEM.search(line) for line in lines),
    }


def command(unit, lines=40):
    service = shlex.quote(f"{unit}.service")
    return (f"systemctl show {service} --no-pager --timestamp=unix -p {','.join(PROPERTIES)}; "
            f"sed -n 's/^ExecStart=/ExecStart=/p' /etc/systemd/system/{service} 2>/dev/null | head -n 1; "
            f"echo {shlex.quote(SEPARATOR)}; journalctl -u {service} -n {int(lines)} --no-pager -o short-iso 2>&1 || true")


def status(system, unit, environment=None, lines=40):
    """The state of a service, here or where an environment runs"""
    if not UNIT.fullmatch(unit):
        raise settings.SettingsError(f"Invalid service name: {unit}")
    if environment is None:
        result = system.run("bash", "-c", command(unit, lines), capture=True, check=False)
    else:
        result = settings.remote_exec(system, environment, command(unit, lines), capture=True, check=False)
    if "ActiveState=" not in result.stdout:
        raise settings.SettingsError((result.stderr or result.stdout).strip() or f"Could not read the state of {unit}.")
    return read(result.stdout)
