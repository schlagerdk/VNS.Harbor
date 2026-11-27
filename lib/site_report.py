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
import html
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


def send(settings, to, subject, body, html=None):
    """One mail through the mail server, as text and, when given, as HTML beside it; raises when it cannot be sent"""
    if not settings.get("host"):
        raise RuntimeError("No mail server is set up")
    message = EmailMessage()
    message["From"] = settings["sender"]
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
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


# ---- the mail as HTML: tables and inline styles, as mail programs need, light with a dark variant where it is read
# (Apple Mail, iOS, Outlook.com); the text part beside it says the same
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
# tone: (colour, light tint, dark tint, colour on dark)
TONES = {
    "red": ("#dc2626", "#fef2f2", "#341417", "#f87171"),
    "green": ("#16a34a", "#f0fdf4", "#0f2a19", "#4ade80"),
    "amber": ("#d97706", "#fffbeb", "#2f2209", "#fbbf24"),
    "blue": ("#2563eb", "#eff6ff", "#0f1d3a", "#60a5fa"),
    "purple": ("#7c3aed", "#f5f3ff", "#221637", "#a78bfa"),
    "grey": ("#6b7280", "#f3f4f6", "#21262d", "#9ca3af"),
}
ICONS = {"red": "!", "amber": "!", "green": "&#10003;", "blue": "i", "purple": "i", "grey": "i"}
RECOVERED = {
    "down": "Answering again",
    "certificate": "Certificate fixed",
    "certificate_soon": "Certificate renewed",
    "service": "Service running again",
    "stopped": "Site started again",
}


def esc(text):
    return html.escape(str(text), quote=True)


def duration(seconds):
    """How long, in words: 2 d 3 h, 4 h 12 min, 9 min"""
    minutes = max(0, int(seconds) // 60)
    if minutes < 1:
        return "under a minute"
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{minutes} min"


def pill(text, tone):
    colour, light, _, _ = TONES[tone]
    return (f'<span class="t-{tone}-pill" style="display:inline-block;padding:3px 10px;border-radius:999px;background:{light};'
            f'color:{colour};font-size:11px;line-height:16px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;'
            f'white-space:nowrap">{esc(text)}</span>')


def link(name):
    """A site's name, linked to it"""
    return (f'<a href="https://{esc(name)}" class="tx" style="color:#111827;text-decoration:none;'
            f'word-break:break-word">{esc(name)}</a>')


def row(tone, label, title, detail="", meta=""):
    """One thing that is wrong or was: a card with its tone down the left side, a label, a title, why, and when"""
    colour = TONES[tone][0]
    parts = [pill(label, tone),
             f'<div style="margin-top:8px;font-size:16px;line-height:22px;font-weight:600">{title}</div>']
    if detail:
        parts.append(f'<div class="tx2" style="margin-top:2px;font-size:14px;line-height:20px;color:#374151;word-break:break-word">{esc(detail)}</div>')
    if meta:
        parts.append(f'<div class="mu" style="margin-top:6px;font-size:12px;line-height:18px;color:#6b7280">{meta}</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="rw" '
            f'style="margin:0 0 10px;border-collapse:separate;background:#f9fafb;border:1px solid #e5e7eb;border-left:4px solid {colour};'
            f'border-radius:10px"><tr><td style="padding:14px 16px">{"".join(parts)}</td></tr></table>')


def section(heading, content, count=None, note=""):
    badge = (f' <span class="cnt" style="display:inline-block;margin-left:6px;padding:0 8px;border-radius:999px;background:#f3f4f6;'
             f'color:#6b7280;font-size:12px;line-height:20px;font-weight:600;vertical-align:1px">{count}</span>') if count is not None else ""
    lead = f'<div class="mu" style="margin:-4px 0 12px;font-size:13px;line-height:19px;color:#6b7280">{esc(note)}</div>' if note else ""
    return (f'<tr><td class="pd" style="padding:8px 32px 14px">'
            f'<div class="tx" style="margin:0 0 12px;font-size:13px;line-height:18px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
            f'color:#111827">{esc(heading)}{badge}</div>{lead}{content}</td></tr>')


def chips(names):
    """Names that are fine, side by side"""
    return "".join(f'<span class="rw rl" style="display:inline-block;margin:0 6px 6px 0;padding:5px 11px;border:1px solid #e5e7eb;border-radius:8px;'
                   f'background:#f9fafb;font-size:13px;line-height:18px"><span style="color:#16a34a">&#9679;</span>&nbsp;{link(name)}</span>'
                   for name in names)


def page(host, tone, title, lead, sections, footer, kind="Keep-alive"):
    """A whole mail: Harbor and the server on top, what it is about in one line on its tone, the sections, and a footer"""
    colour, light, dark, on_dark = TONES[tone]
    dark_css = "".join(
        f".t-{name}-hero{{background:{values[2]} !important}}.t-{name}-pill{{background:{values[2]} !important;color:{values[3]} !important}}"
        f".t-{name}-icon{{background:{values[3]} !important;color:#0d1117 !important}}"
        for name, values in TONES.items())
    style = ("<style>:root{color-scheme:light dark;supported-color-schemes:light dark}"
             "@media (max-width:620px){.pd{padding-left:18px !important;padding-right:18px !important}.hh{font-size:20px !important;line-height:26px !important}}"
             "@media (prefers-color-scheme:dark){.cd{background:#161b22 !important;border-color:#30363d !important}"
             ".rw{background:#1c2128 !important;border-top-color:#30363d !important;border-right-color:#30363d !important;border-bottom-color:#30363d !important}"
             ".tx,.tx a{color:#e6edf3 !important}.tx2{color:#c9d1d9 !important}.mu,.mu a{color:#8b949e !important}"
             ".rl{border-left-color:#30363d !important}.cnt,.sv{background:#21262d !important;color:#8b949e !important}.ln{border-color:#30363d !important}" + dark_css + "}</style>")
    preheader = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{esc(lead)}'
                 + "&#8203;&nbsp;" * 40 + "</div>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark"><title>{esc(title)}</title>{style}</head>'
        f'<body style="margin:0;padding:0;font-family:{FONT};color:#111827;-webkit-text-size-adjust:100%">{preheader}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td align="center" style="padding:24px 10px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="cd" '
        'style="max-width:600px;background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;border-collapse:separate;overflow:hidden">'
        # Harbor, and the server it is about
        '<tr><td class="pd ln" style="padding:18px 32px;border-bottom:1px solid #e5e7eb">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:middle"><span style="display:inline-block;width:28px;height:28px;border-radius:8px;background:#7c3aed;'
        'color:#ffffff;font-size:15px;line-height:28px;text-align:center;vertical-align:middle">&#9875;</span>'
        f'<span class="tx" style="margin-left:10px;font-size:15px;font-weight:700;vertical-align:middle;color:#111827">Harbor</span>'
        f'<span class="mu" style="margin-left:6px;font-size:13px;vertical-align:middle;color:#6b7280">{esc(kind)}</span></td>'
        f'<td align="right" style="vertical-align:middle"><span class="sv" style="display:inline-block;padding:4px 10px;border-radius:8px;'
        f'background:#f3f4f6;color:#374151;font-size:12px;font-weight:700;letter-spacing:.04em;font-family:ui-monospace,Menlo,Consolas,monospace">'
        f'{esc(host.upper())}</span></td></tr></table></td></tr>'
        # What it is about, in one line on its tone
        f'<tr><td class="pd" style="padding:24px 32px 18px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="t-{tone}-hero" style="background:{light};border-radius:12px;border-collapse:separate"><tr>'
        f'<td width="44" style="padding:18px 0 18px 18px;vertical-align:top"><div class="t-{tone}-icon" style="width:36px;height:36px;border-radius:50%;'
        f'background:{colour};color:#ffffff;font-size:20px;line-height:36px;font-weight:700;text-align:center">{ICONS[tone]}</div></td>'
        f'<td style="padding:16px 18px 16px 14px;vertical-align:top"><div class="tx hh" style="font-size:22px;line-height:28px;font-weight:700;color:#111827;'
        f'word-break:break-word">{esc(title)}</div>'
        f'<div class="tx2" style="margin-top:4px;font-size:14px;line-height:21px;color:#374151">{esc(lead)}</div></td></tr></table></td></tr>'
        + "".join(sections) +
        f'<tr><td class="pd mu ln" style="padding:18px 32px 22px;border-top:1px solid #e5e7eb;font-size:12px;line-height:18px;color:#6b7280">'
        f'{footer}</td></tr>'
        '</table></td></tr></table></body></html>')


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


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
            new.append({"at": int(now), "name": name, "check": check, "event": "recovered", "message": value.get("message", ""),
                        "since": value.get("since")})
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
                    sender(settings, report["emails"], change_subject(changed, host), change_body(changed, opened, wanted, host, now),
                           html=change_html(changed, opened, wanted, host, now))
                saved["mailed"] = sorted(k for k in opened if k.split("|", 1)[1] in wanted)
            if report.get("when") in ("daily", "both"):
                today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                if saved.get("daily") != today and datetime.fromtimestamp(now).hour >= DAILY_HOUR:
                    saved["daily"] = today
                    day = [item for item in events(state, now - 86400) if item["check"] in wanted and item["event"] == "failed"]
                    still = {key: value for key, value in opened.items() if key.split("|", 1)[1] in wanted}
                    if day or still:
                        sender(settings, report["emails"], f"[{host}] Daily site report: {plural(len(still), 'problem')}"
                               + ("" if still else ", all fine now"),
                               daily_body(day, still, host, now), html=daily_html(day, still, host, now))
            saved.pop("mail_error", None)
        except (OSError, RuntimeError, smtplib.SMTPException, ssl.SSLError, ValueError, KeyError) as exc:
            saved["mail_error"] = {"at": int(now), "error": str(exc)}
            print(f"Keep-alive could not send its report: {exc}", file=sys.stderr)
    write_json(state / "report.json", saved)
    return new


def stamp(at):
    return datetime.fromtimestamp(at).strftime("%d.%m.%Y %H:%M")


def tone_of(check):
    """An expiring certificate is a warning, the rest is wrong"""
    return "amber" if check == "certificate_soon" else "red"


def hero_tone(checks):
    checks = set(checks)
    return ("red" if checks - {"certificate_soon"} else "amber") if checks else "green"


def since_text(since, now):
    return f"Since {stamp(since)}" + (f" &middot; {duration(now - since)}" if now - since >= 60 else "")


def footer_text(host, now, extra=""):
    return (f"Keep-alive on <strong>{esc(host)}</strong> checks the sites every few minutes. {extra}"
            f"Sent {stamp(now)}. What is mailed, and to whom, is chosen under Settings, Notifications in Harbor.")


def change_subject(changed, host):
    failed = [item for item in changed if item["event"] == "failed"]
    if failed:
        names = sorted({item["name"] for item in failed})
        mark = "\U0001F534" if hero_tone(item["check"] for item in failed) == "red" else "\U0001F7E1"
        return f"[{host}] {mark} {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}: {CHECKS[failed[0]['check']].lower()}"
    names = sorted({item["name"] for item in changed})
    if len(changed) == 1:
        return f"[{host}] ✅ {names[0]}: {RECOVERED[changed[0]['check']].lower()}"
    return f"[{host}] ✅ {plural(len(changed), 'site problem')} solved: {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}"


def lasted(item):
    return f" (it lasted {duration(item['at'] - item['since'])})" if item.get("since") else ""


def change_body(changed, opened, wanted, host, now=None):
    now = now or time.time()
    lines = [f"Keep-alive on {host} found a change.", ""]
    for item in changed:
        if item["event"] == "failed":
            lines.append(f"Started: {item['name']} - {CHECKS[item['check']]}" + (f": {item['message']}" if item["message"] else ""))
        else:
            lines.append(f"Solved: {item['name']} - {RECOVERED[item['check']]}, was {CHECKS[item['check']].lower()}"
                         + (f": {item['message']}" if item["message"] else "") + lasted(item))
    told = {item["name"] + "|" + item["check"] for item in changed if item["event"] == "failed"}
    still = sorted(key for key in opened if key.split("|", 1)[1] in wanted)
    rest = [key for key in still if key not in told]
    if not still:
        lines += ["", "Nothing is wrong now."]
    elif rest:
        lines += ["", "Also wrong now:"]
        for key in rest:
            name, check = key.split("|", 1)
            lines.append(f"  {name} - {CHECKS[check]} since {stamp(opened[key]['since'])} ({duration(now - opened[key]['since'])}): {opened[key]['message']}")
    return "\n".join(lines) + "\n"


def change_html(changed, opened, wanted, host, now=None):
    now = now or time.time()
    failed = [item for item in changed if item["event"] == "failed"]
    solved = [item for item in changed if item["event"] != "failed"]
    still = sorted(key for key in opened if key.split("|", 1)[1] in wanted)
    tone = hero_tone(key.split("|", 1)[1] for key in still)
    if len(changed) == 1:
        item = changed[0]
        title = f"{item['name']}: " + (CHECKS if failed else RECOVERED)[item["check"]].lower()
    elif failed and solved:
        title = f"{plural(len(failed), 'problem')} started, {len(solved)} solved"
    elif failed:
        title = f"{plural(len(failed), 'new problem')}"
    else:
        title = f"{plural(len(solved), 'problem')} solved"
    sites = len({key.split("|", 1)[0] for key in still})
    lead = f"{plural(len(still), 'problem')} on {plural(sites, 'site')} right now." if still else "Nothing is wrong now: every site checked is fine."
    rows = []
    for item in changed:
        if item["event"] == "failed":
            rows.append(row(tone_of(item["check"]), CHECKS[item["check"]], link(item["name"]), item["message"], since_text(item["at"], now)))
        else:
            was = f"Was {CHECKS[item['check']].lower()}" + (f": {item['message']}" if item["message"] else "")
            meta = f"Solved {stamp(item['at'])}" + (f" &middot; it lasted {duration(item['at'] - item['since'])}" if item.get("since") else "")
            rows.append(row("green", RECOVERED[item["check"]], link(item["name"]), was, meta))
    sections = [section("What changed", "".join(rows), len(changed))]
    told = {item["name"] + "|" + item["check"] for item in failed}
    rest = [key for key in still if key not in told]
    if rest:
        sections.append(section("Also wrong now", "".join(
            row(tone_of(key.split("|", 1)[1]), CHECKS[key.split("|", 1)[1]], link(key.split("|", 1)[0]), opened[key]["message"],
                since_text(opened[key]["since"], now)) for key in rest), len(rest)))
    return page(host, tone, title, lead, sections, footer_text(host, now))


def daily_body(day, still, host, now=None):
    now = now or time.time()
    lines = [f"Keep-alive on {host}, the last day.", "", "Wrong now:" if still else "Nothing is wrong now."]
    for key in sorted(still):
        name, check = key.split("|", 1)
        lines.append(f"  {name} - {CHECKS[check]} since {stamp(still[key]['since'])} ({duration(now - still[key]['since'])}): {still[key]['message']}")
    if day:
        lines += ["", "Broke in the last day:"]
        lines += [f"  {stamp(item['at'])}  {item['name']} - {CHECKS[item['check']]}: {item['message']}"
                  + ("" if item["name"] + "|" + item["check"] in still else " (solved)") for item in day]
    return "\n".join(lines) + "\n"


def daily_html(day, still, host, now=None):
    now = now or time.time()
    keys = sorted(still)
    tone = hero_tone(key.split("|", 1)[1] for key in keys)
    title = f"{plural(len(keys), 'problem')} right now" if keys else "All sites are fine"
    solved = [item for item in day if item["name"] + "|" + item["check"] not in still]
    lead = (f"{plural(len(day), 'problem')} started in the last day" + (f", {len(solved)} of them solved." if day else ".")) if day \
        else "Nothing new broke in the last day."
    sections = []
    if keys:
        sections.append(section("Wrong now", "".join(
            row(tone_of(key.split("|", 1)[1]), CHECKS[key.split("|", 1)[1]], link(key.split("|", 1)[0]), still[key]["message"],
                since_text(still[key]["since"], now)) for key in keys), len(keys)))
    if day:
        sections.append(section("Started in the last day", "".join(
            row("grey" if item in solved else tone_of(item["check"]), CHECKS[item["check"]], link(item["name"]), item["message"],
                f"{stamp(item['at'])} &middot; " + ("solved since" if item in solved else "still wrong")) for item in reversed(day)), len(day)))
    return page(host, tone, title, lead, sections, footer_text(host, now, "This is its report of the last day. "), "Daily report")


def test_report(results, report, host, mail=None, sender=send, now=None):
    """A report of how every site is now, as a test of what the mail looks like: sent to the report's recipients when
    there are any and a mail server, and returned as (subject, body, sent to, error). The log is not touched."""
    now = now or time.time()
    wanted = set((report or {}).get("checks") or CHECKS)
    wrong = [(item["name"], check, message) for item in results for check, message in item["problems"].items() if check in wanted]
    fine = sorted(item["name"] for item in results if not any(check in wanted for check in item["problems"]))
    subject = f"[{host}] TEST: {plural(len(wrong), 'problem')} on {plural(len(results), 'site')}"
    lines = [f"A test run of keep-alive on {host}: this is how its reports look.", "",
             "Wrong now:" if wrong else "Nothing is wrong now."]
    lines += [f"  {name} - {CHECKS[check]}: {message}" for name, check, message in sorted(wrong)]
    if fine:
        lines += ["", "Answering, nothing wrong:"] + [f"  {name}" for name in fine]
    checked = [CHECKS[check] for check in CHECKS if check in wanted]
    lines += ["", "Checked: " + ", ".join(checked) + "."]
    body = "\n".join(lines) + "\n"
    sections = []
    if wrong:
        sections.append(section("Wrong now", "".join(row(tone_of(check), CHECKS[check], link(name), message)
                                                     for name, check, message in sorted(wrong)), len(wrong)))
    if fine:
        sections.append(section("Answering, nothing wrong", chips(fine), len(fine)))
    sections.append(section("Checked", "".join(f'<span style="display:inline-block;margin:0 6px 6px 0">{pill(name, "purple")}</span>' for name in checked)))
    markup = page(host, "blue", f"Test: {plural(len(wrong), 'problem')} on {plural(len(results), 'site')}",
                  "A test run of keep-alive: this is how its reports look. Nothing was logged.", sections,
                  footer_text(host, now), "Test report")
    to = (report or {}).get("emails") or []
    error = ""
    if not to:
        error = "No recipients yet: set up the email report to have the test sent."
    else:
        try:
            sender(load_mail() if mail is None else mail, to, subject, body, html=markup)
        except (OSError, RuntimeError, smtplib.SMTPException, ssl.SSLError, ValueError, KeyError) as exc:
            error = f"Not sent: {exc}"
    return {"subject": subject, "body": body, "to": to if not error else [], "error": error}


def test_mail_html(host, now=None):
    """The test of the mail server, from Settings"""
    now = now or time.time()
    return page(host, "green", "The mail server works",
                "This is a test from Harbor. Harbor's mail, such as keep-alive's reports and usage alerts, comes through this mail server.",
                [], f"Sent from <strong>{esc(host)}</strong> {stamp(now)}.", "Test mail")
