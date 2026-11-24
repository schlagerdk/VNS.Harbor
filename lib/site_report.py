"""What keep-alive found wrong with the sites, kept as a log, and mailed to those who want to know.

Each run gives its results: a site or environment by name, and its problems by check:

  down              it did not answer, or answered with a server error
  certificate       its certificate is expired, for another name, or not trusted
  certificate_soon  its certificate expires within SOON_DAYS
  service           its service has failed (stopped by itself)
  stopped           its service is stopped while Apache still serves it: visitors see the page that says it is updating

A problem that starts, and one that ends, is an event in the log (log.jsonl in keep-alive's state folder). Mail goes
out as the report settings say: when something breaks or comes back (change), once a day with what is wrong and what
broke in the last day (daily), or both, for the checks chosen. The mail server is Harbor's (MAIL, root only).
"""
from datetime import datetime, timedelta
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import sys
import tempfile
import time

CHECKS = {
    "down": "Not answering",
    "certificate": "Certificate error",
    "certificate_soon": "Certificate expires soon",
    "service": "Service failed",
    "stopped": "Site stopped",
}
WHEN = ("change", "daily", "both")
SECURITY = ("starttls", "ssl", "none")
SOON_DAYS = 14
DAILY_HOUR = 7
LOG_LINES = 2000
MAIL = Path("/etc/harbor/secrets/mail.json")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ---- the settings
def validate_report(report):
    """{enabled, emails, when, checks}: who gets the mail, when, and for which checks"""
    if not isinstance(report, dict) or set(report) - {"enabled", "emails", "when", "checks"}:
        raise ValueError("The report has enabled, emails, when and checks")
    enabled = report.get("enabled", False)
    emails = report.get("emails", [])
    when = report.get("when", "change")
    checks = report.get("checks", list(CHECKS))
    if not isinstance(enabled, bool):
        raise ValueError("The report is on or off")
    if not isinstance(emails, list) or len(emails) > 20 or not all(isinstance(item, str) and EMAIL.fullmatch(item) for item in emails):
        raise ValueError("Each recipient must be an e-mail address")
    if enabled and not emails:
        raise ValueError("Give at least one e-mail address for the report")
    if when not in WHEN:
        raise ValueError("The report goes out on change, daily or both")
    if not isinstance(checks, list) or not checks or any(item not in CHECKS for item in checks):
        raise ValueError("Choose at least one thing for the report")
    return {"enabled": enabled, "emails": emails, "when": when, "checks": list(dict.fromkeys(checks))}


def load_mail(path=MAIL):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def validate_mail(data, current=None):
    """{host, port, security, user, password, sender}; an empty password keeps the one saved"""
    current = current or {}
    if not isinstance(data, dict):
        raise ValueError("Expected the mail server's settings")
    host = str(data.get("host", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError("Enter the mail server's host name")
    port = data.get("port", 587)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The port is a number from 1 to 65535")
    security = data.get("security", "starttls")
    if security not in SECURITY:
        raise ValueError("Security is STARTTLS, SSL or none")
    user = str(data.get("user", "")).strip()
    password = data.get("password", "")
    if not isinstance(password, str) or len(password) > 512 or "\n" in password or len(user) > 256:
        raise ValueError("Invalid login")
    sender = str(data.get("sender", "")).strip()
    if not EMAIL.fullmatch(sender):
        raise ValueError("The sender must be an e-mail address")
    return {"host": host, "port": port, "security": security, "user": user,
            "password": password or (current.get("password", "") if user else ""), "sender": sender}


def save_mail(settings, path=MAIL):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mail-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def send(settings, to, subject, body):
    """One mail through the mail server; raises when it cannot be sent"""
    if not settings.get("host"):
        raise RuntimeError("No mail server is set up")
    message = EmailMessage()
    message["From"] = settings["sender"]
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.get("security") == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=30)
    with server:
        if settings.get("security") == "starttls":
            server.starttls(context=context)
        if settings.get("user"):
            server.login(settings["user"], settings.get("password", ""))
        server.send_message(message)


# ---- the checks
def certificate_problems(name, connect=None, port=443, now=None, timeout=10):
    """The certificate a name is served with: {certificate: why} when it is not good, {certificate_soon: when} when it
    expires within SOON_DAYS, else nothing. Not reached at all is not the certificate's problem (down tells it)."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((connect or name, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=name) as tls:
                found = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return {"certificate": exc.verify_message or str(exc)}
    except (OSError, ssl.SSLError):
        return {}
    days = (ssl.cert_time_to_seconds(found["notAfter"]) - (now or time.time())) / 86400
    if days < SOON_DAYS:
        return {"certificate_soon": f"Expires in {max(0, int(days))} day{'' if int(days) == 1 else 's'} ({found['notAfter']})"}
    return {}


# ---- the log and the report
def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.stem + "-")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def events(state, since=0):
    """The log's events, oldest first: {at, name, check, event (failed or recovered), message}"""
    found = []
    try:
        lines = (Path(state) / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("at", 0) >= since:
            found.append(item)
    return found


def append_events(state, new):
    if not new:
        return
    path = Path(state) / "log.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines += [json.dumps(item) for item in new]
    path.write_text("\n".join(lines[-LOG_LINES:]) + "\n", encoding="utf-8")


def record(results, report=None, state=Path("/var/lib/vns/keep-alive"), now=None, mail=None, sender=send, hostname=None):
    """This run's results ([{name, problems: {check: message}}]) against what was wrong before: the problems that
    started or ended go into the log, and to the report's recipients as it says. What is wrong now is kept in
    report.json, with the last mail error and the last daily report."""
    now = now or time.time()
    state = Path(state)
    saved = read_json(state / "report.json", {})
    previous = saved.get("open", {}) if isinstance(saved.get("open"), dict) else {}
    checked = {item["name"] for item in results}
    current = {f"{item['name']}|{check}": message for item in results for check, message in item["problems"].items()}
    opened, new = {}, []
    for key, message in current.items():
        name, check = key.split("|", 1)
        if key in previous:
            opened[key] = {**previous[key], "message": message}
        else:
            opened[key] = {"since": int(now), "message": message}
            new.append({"at": int(now), "name": name, "check": check, "event": "failed", "message": message})
    for key, value in previous.items():
        name, check = key.split("|", 1)
        if key in current:
            continue
        if name in checked:
            new.append({"at": int(now), "name": name, "check": check, "event": "recovered", "message": value.get("message", "")})
        # Not checked this run (its service turned off, the site gone): it is not open any more, and says nothing
    append_events(state, new)
    saved["open"] = opened
    saved["checked"] = int(now)
    report = report or {}
    if report.get("enabled") and report.get("emails"):
        wanted = set(report.get("checks") or CHECKS)
        host = hostname or socket.gethostname()
        try:
            settings = load_mail() if mail is None else mail
            if report.get("when") in ("change", "both"):
                # What is wrong and not told yet (it broke now, or before the report took it in: turned on, or the
                # check chosen later), and what was told and is solved now. Told once each.
                mailed = set(saved.get("mailed") or [])
                key = lambda item: item["name"] + "|" + item["check"]
                fresh = {key(item) for item in new}
                changed = [item for item in new if item["check"] in wanted and (item["event"] == "failed" or key(item) in mailed)]
                changed += [{"at": value["since"], "name": k.split("|", 1)[0], "check": k.split("|", 1)[1], "event": "failed", "message": value["message"]}
                            for k, value in sorted(opened.items()) if k.split("|", 1)[1] in wanted and k not in mailed and k not in fresh]
                if changed:
                    sender(settings, report["emails"], change_subject(changed, host), change_body(changed, opened, wanted, host))
                saved["mailed"] = sorted(k for k in opened if k.split("|", 1)[1] in wanted)
            if report.get("when") in ("daily", "both"):
                today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                if saved.get("daily") != today and datetime.fromtimestamp(now).hour >= DAILY_HOUR:
                    saved["daily"] = today
                    day = [item for item in events(state, now - 86400) if item["check"] in wanted and item["event"] == "failed"]
                    still = {key: value for key, value in opened.items() if key.split("|", 1)[1] in wanted}
                    if day or still:
                        sender(settings, report["emails"], f"[{host}] Daily site report: {len(still)} problem{'' if len(still) == 1 else 's'}",
                               daily_body(day, still, host))
            saved.pop("mail_error", None)
        except (OSError, RuntimeError, smtplib.SMTPException, ssl.SSLError, ValueError, KeyError) as exc:
            saved["mail_error"] = {"at": int(now), "error": str(exc)}
            print(f"Keep-alive could not send its report: {exc}", file=sys.stderr)
    write_json(state / "report.json", saved)
    return new


def stamp(at):
    return datetime.fromtimestamp(at).strftime("%d.%m.%Y %H:%M")


def change_subject(changed, host):
    failed = [item for item in changed if item["event"] == "failed"]
    if failed:
        names = sorted({item["name"] for item in failed})
        return f"[{host}] {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}: {CHECKS[failed[0]['check']].lower()}"
    return f"[{host}] {len(changed)} site problem{'' if len(changed) == 1 else 's'} solved"


def change_body(changed, opened, wanted, host):
    lines = [f"Keep-alive on {host} found a change.", ""]
    for item in changed:
        verb = "Started" if item["event"] == "failed" else "Solved"
        lines.append(f"{verb}: {item['name']} - {CHECKS[item['check']]}" + (f": {item['message']}" if item["message"] else ""))
    still = sorted(key for key in opened if key.split("|", 1)[1] in wanted)
    lines += ["", "Wrong now:" if still else "Nothing is wrong now."]
    for key in still:
        name, check = key.split("|", 1)
        lines.append(f"  {name} - {CHECKS[check]} since {stamp(opened[key]['since'])}: {opened[key]['message']}")
    return "\n".join(lines) + "\n"


def daily_body(day, still, host):
    lines = [f"Keep-alive on {host}, the last day.", "", "Wrong now:" if still else "Nothing is wrong now."]
    for key in sorted(still):
        name, check = key.split("|", 1)
        lines.append(f"  {name} - {CHECKS[check]} since {stamp(still[key]['since'])}: {still[key]['message']}")
    if day:
        lines += ["", "Broke in the last day:"]
        lines += [f"  {stamp(item['at'])}  {item['name']} - {CHECKS[item['check']]}: {item['message']}" for item in day]
    return "\n".join(lines) + "\n"


def test_report(results, report, host, mail=None, sender=send):
    """A report of how every site is now, as a test of what the mail looks like: sent to the report's recipients when
    there are any and a mail server, and returned as (subject, body, sent to, error). The log is not touched."""
    wanted = set((report or {}).get("checks") or CHECKS)
    wrong = [(item["name"], check, message) for item in results for check, message in item["problems"].items() if check in wanted]
    fine = sorted(item["name"] for item in results if not any(check in wanted for check in item["problems"]))
    subject = f"[{host}] TEST: {len(wrong)} problem{'' if len(wrong) == 1 else 's'} on {len(results)} site{'' if len(results) == 1 else 's'}"
    lines = [f"A test run of keep-alive on {host}: this is how its reports look.", "",
             "Wrong now:" if wrong else "Nothing is wrong now."]
    lines += [f"  {name} - {CHECKS[check]}: {message}" for name, check, message in sorted(wrong)]
    if fine:
        lines += ["", "Answering, nothing wrong:"] + [f"  {name}" for name in fine]
    lines += ["", "Checked: " + ", ".join(CHECKS[check] for check in CHECKS if check in wanted) + "."]
    body = "\n".join(lines) + "\n"
    to = (report or {}).get("emails") or []
    error = ""
    if not to:
        error = "No recipients yet: set up the email report to have the test sent."
    else:
        try:
            sender(load_mail() if mail is None else mail, to, subject, body)
        except (OSError, RuntimeError, smtplib.SMTPException, ssl.SSLError, ValueError, KeyError) as exc:
            error = f"Not sent: {exc}"
    return {"subject": subject, "body": body, "to": to if not error else [], "error": error}
