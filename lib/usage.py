#!/usr/bin/env python3
"""Usage limits of the admin's users: how big each site and database a user made may grow, checked once an hour.

A user who may make sites or databases can be given limits (access.py): how many they may make, and how big each may
be. How many is held when one is made (admin/app.py); how big is checked here, by cron, as root: each site a user made
(its folder under /var/www) and each database (its size on its SQL Server). What has grown past its limit is mailed to
the recipients set under Settings, General, through Harbor's mail server: once when it goes over, and once when it is
under again. What is over is kept (usage-state.json), and the admin shows each user what of theirs is.

  usage.py --data DIR            check now, mail what changed (cron runs this)
  usage.py --data DIR --json     check now, print what is over as JSON, mail nothing (Check now in the admin)
  usage.py --install DIR         the hourly cron job, for the admin whose data is in DIR
  usage.py --remove              no cron job
  usage.py --sync DIR            the cron job while the alerts are on or a user has a size limit, else none
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = _load("vns_usage_site_report", "site_report.py")

WEB_ROOT = Path("/var/www")
CRON = Path("/etc/cron.d/vns-usage")


# ---- the settings: who is mailed
def load_settings(data_dir):
    saved = report.read_json(Path(data_dir) / "usage.json", {})
    return {"enabled": saved.get("enabled") is True, "emails": [item for item in saved.get("emails", []) if isinstance(item, str)]}


def load_limits(data_dir):
    """The server's own limits, set by an administrator on a site or a database whoever's it is (1.0.94):
    {"site|<site>": mb, "database|<connection>|<name>": mb}"""
    saved = report.read_json(Path(data_dir) / "usage.json", {}).get("limits")
    return {key: value for key, value in (saved or {}).items() if isinstance(key, str) and isinstance(value, (int, float))
            and not isinstance(value, bool) and value > 0} if isinstance(saved, dict) else {}


def set_limit(data_dir, kind, name, connection="", limit_mb=None):
    """A site's or a database's limit set (a number of MB) or taken away (None)"""
    if kind not in ("site", "database", "environment") or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._@ -]{1,128}", name):
        raise ValueError("Unknown site, environment or database")
    if kind == "database" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", connection or ""):
        raise ValueError("Unknown connection")
    # An environment: its site in name, its id in connection
    if kind == "environment" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", connection or ""):
        raise ValueError("Unknown environment")
    if limit_mb is not None and (isinstance(limit_mb, bool) or not isinstance(limit_mb, (int, float)) or not 1 <= limit_mb <= 10 ** 8):
        raise ValueError("The limit is a size in MB")
    path = Path(data_dir) / "usage.json"
    saved = report.read_json(path, {})
    saved = saved if isinstance(saved, dict) else {}
    limits = saved.get("limits") if isinstance(saved.get("limits"), dict) else {}
    key = f"site|{name}" if kind == "site" else f"environment|{name}|{connection}" if kind == "environment" else f"database|{connection}|{name}"
    if limit_mb is None:
        limits.pop(key, None)
    else:
        limits[key] = round(limit_mb)
    saved["limits"] = limits
    write_json(path, saved)
    # What was over it and is not now (no limit, or one above its size): not over any more at once, without waiting for
    # the check; nothing is mailed for it
    state_path = Path(data_dir) / "usage-state.json"
    state = report.read_json(state_path, {})
    opened = state.get("open") if isinstance(state, dict) and isinstance(state.get("open"), dict) else None
    item = (opened or {}).get("limit|" + key)
    if item and (limit_mb is None or (item.get("size_mb") or 0) <= limit_mb):
        del opened["limit|" + key]
        write_json(state_path, state)
    elif item:
        item["limit_mb"] = round(limit_mb)
        write_json(state_path, state)
    return limits


def validate_settings(data):
    if not isinstance(data, dict):
        raise ValueError("Expected usage alert settings")
    enabled, emails = data.get("enabled", False), data.get("emails", [])
    if not isinstance(enabled, bool):
        raise ValueError("The alerts are on or off")
    if not isinstance(emails, list) or len(emails) > 20 or not all(isinstance(item, str) and report.EMAIL.fullmatch(item) for item in emails):
        raise ValueError("Each recipient must be an e-mail address")
    if enabled and not emails:
        raise ValueError("Give at least one e-mail address for the alerts")
    return {"enabled": enabled, "emails": emails}


def write_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---- what is measured
def site_mb(site, web_root=WEB_ROOT):
    """A site's folder on disk, every environment in it, in MB (None when it is gone)"""
    folder = Path(web_root) / site
    if not folder.is_dir():
        return None
    result = subprocess.run(["du", "-sk", str(folder)], capture_output=True, text=True, timeout=600)
    try:
        return round(int(result.stdout.split()[0]) / 1024, 1)
    except (IndexError, ValueError):
        return None


def environment_mb(site, target):
    """An environment's folder in MB, here or on its server (harbor env size), or None when it cannot be told"""
    harbor = Path(__file__).resolve().parents[1] / "bin" / "harbor"
    try:
        result = subprocess.run(["/usr/bin/python3", str(harbor), "env", "size", site, target], capture_output=True, text=True, timeout=300)
        return json.loads(result.stdout.strip().splitlines()[-1]).get("size_mb")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def all_environments_mb():
    """Every environment of every site with its size, measured where each runs (harbor env size all)"""
    harbor = Path(__file__).resolve().parents[1] / "bin" / "harbor"
    try:
        result = subprocess.run(["/usr/bin/python3", str(harbor), "env", "size", "all"], capture_output=True, text=True, timeout=600)
        found = json.loads(result.stdout.strip().splitlines()[-1])
        return found if isinstance(found, list) else []
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return []


def connection_engine(data_dir, connection):
    """mssql or postgres: what the admin shows beside the connection's name"""
    sql = _load("vns_usage_sql_admin", "sql_admin.py")
    try:
        return sql.engine(sql.connection(str(Path(data_dir) / "sql-connections.json"), connection))
    except (OSError, ValueError):
        return "mssql"


def database_sizes(data_dir, connection, user_only=False):
    """{database: MB} on one of the admin's SQL connections (user_only: not SQL Server's own); empty when it cannot be
    reached"""
    sql = _load("vns_usage_sql_admin", "sql_admin.py")
    try:
        settings = sql.connection(str(Path(data_dir) / "sql-connections.json"), connection)
        return {row["name"]: float(row.get("size_mb") or 0) for row in sql.databases(settings) if not user_only or row.get("id", 5) > 4}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {}


def check(data_dir, measure_site=site_mb, measure_databases=database_sizes, everything=None, measure_environment=None):
    """What has grown past its user's limit now: a list, each with its key, kind, name, user, size and limit. With
    everything (a list), each site and database a user made goes into it as well, with its limit or None."""
    config = report.read_json(Path(data_dir) / "config.json", {})
    over, sizes = [], {}
    for user in config.get("users", []) if isinstance(config.get("users"), list) else []:
        if not isinstance(user, dict) or user.get("role") == "admin":
            continue
        limits, made = user.get("limits") or {}, user.get("made") or {}
        who = user.get("name") or user.get("username") or user.get("id", "")
        if everything is not None or any(site_limit(user, site) is not None for site in user_sites(user)):
            for site in user_sites(user):
                size = measure_site(site)
                if size is None:
                    continue
                item = {"key": f"site|{site}", "kind": "site", "name": site, "user": who, "user_id": user.get("id"), "size_mb": size, "limit_mb": site_limit(user, site)}
                if everything is not None:
                    everything.append(item)
                if item["limit_mb"] is not None and size > item["limit_mb"]:
                    over.append(item)
        for connection, names in (made.get("databases") or {}).items():
            # Each connection has its own limits for the databases on it, and a database its own size
            if everything is not None or any(database_limit(user, connection, name) is not None for name in names):
                if connection not in sizes:
                    sizes[connection] = measure_databases(data_dir, connection)
                for name in names:
                    size = sizes[connection].get(name)
                    if size is None:
                        continue
                    largest = database_limit(user, connection, name)
                    item = {"key": f"database|{connection}|{name}", "kind": "database", "name": name, "connection": connection,
                            "user": who, "user_id": user.get("id"), "size_mb": round(size, 1), "limit_mb": largest}
                    if everything is not None:
                        everything.append(item)
                    if largest is not None and size > largest:
                        over.append(item)
    # The server's own limits, whoever's each is
    measure_environment = measure_environment or environment_mb
    for key, limit in load_limits(data_dir).items():
        kind, _, rest = key.partition("|")
        if kind == "environment":
            site, _, target = rest.partition("|")
            size = measure_environment(site, target)
            item = {"key": "limit|" + key, "kind": "environment", "name": f"{site} ({target})", "site": site, "target": target,
                    "user": "the server's limit", "user_id": None, "size_mb": size, "limit_mb": limit}
        elif kind == "site":
            size = measure_site(rest)
            item = {"key": "limit|" + key, "kind": "site", "name": rest, "user": "the server's limit", "user_id": None, "size_mb": size, "limit_mb": limit}
        else:
            connection, _, name = rest.partition("|")
            if connection not in sizes:
                sizes[connection] = measure_databases(data_dir, connection)
            size = sizes[connection].get(name)
            item = {"key": "limit|" + key, "kind": "database", "name": name, "connection": connection, "user": "the server's limit",
                    "user_id": None, "size_mb": round(size, 1) if size is not None else None, "limit_mb": limit}
        if item["size_mb"] is not None and item["size_mb"] > limit:
            over.append(item)
    return over


def site_limit(user, site):
    """How big a site of the user's may be: its own limit when it has one, else, for a site they made, the one their
    limits give every site. A site they were only given (an administrator's) has a limit only when one is set on it."""
    own = (((user or {}).get("sites") or {}).get(site) or {}).get("size_mb")
    if own is not None:
        return own
    if site in ((user or {}).get("made") or {}).get("sites", []):
        return ((user or {}).get("limits") or {}).get("site_mb")
    return None


def user_sites(user):
    """The sites that are the user's for their usage: those they made, and those they were given with a size of their
    own (an administrator's site lent them for a while: taken away, it is the administrator's again)"""
    made = list(((user or {}).get("made") or {}).get("sites", []))
    given = [site for site, grant in ((user or {}).get("sites") or {}).items()
             if isinstance(grant, dict) and grant.get("size_mb") is not None and site not in made]
    return made + given


def database_limit(user, connection, name):
    """How big a database of the user's may be: its own size when it has one, else the connection's for every database"""
    grant = (((user or {}).get("connections") or {}).get(connection) or {})
    own = (grant.get("sizes") or {}).get(name)
    return own if own is not None else (grant.get("limits") or {}).get("database_mb")


def shows_usage(user):
    """Whether a user has usage to see: they may make sites or databases, or have made some"""
    if not isinstance(user, dict) or user.get("role") == "admin":
        return False
    made = user.get("made") or {}
    return bool(user.get("create_sites") or user_sites(user) or any((made.get("databases") or {}).values())
                or any({"create", "copy"} & set(grant.get("actions") or []) for grant in (user.get("connections") or {}).values()))


def of_user(data_dir, user, names=None, measure_site=site_mb, measure_databases=database_sizes):
    """What one user has made and how big each is now, beside their limits: their sites, and their databases on each
    connection where they may make some or have. names gives each connection's name. Measured now."""
    made, limits = user.get("made") or {}, user.get("limits") or {}
    # Each site with its own limit: the one set on it, else the user's for every site
    sites = [{"name": site, "size_mb": size, "limit_mb": site_limit(user, site)} for site in user_sites(user) for size in [measure_site(site)] if size is not None]
    result = {"sites": {"may_make": bool(user.get("create_sites")) or bool(sites), "count_limit": limits.get("sites"),
                        "size_limit_mb": limits.get("site_mb"), "items": sites}, "connections": []}
    for connection, grant in (user.get("connections") or {}).items():
        own = (made.get("databases") or {}).get(connection, [])
        if not own and not {"create", "copy"} & set(grant.get("actions") or []):
            continue
        sizes = measure_databases(data_dir, connection) if own else {}
        items = [{"name": name, "size_mb": round(sizes[name], 1), "limit_mb": database_limit(user, connection, name)} for name in own if name in sizes]
        result["connections"].append({"id": connection, "name": (names or {}).get(connection) or connection,
                                      "count_limit": (grant.get("limits") or {}).get("databases"),
                                      "size_limit_mb": (grant.get("limits") or {}).get("database_mb"), "items": items,
                                      # Not reached: what they made there is not known now
                                      "unreachable": bool(own) and not sizes})
    return result


def overview(data_dir, sites, connections, measure_site=site_mb, measure_databases=database_sizes, measure_environment=None, measure_environments=None):
    """Every site here and every database on each connection (system databases left out), how big each is now, who
    made it and the limit that holds for it: for an administrator. sites: the site folders; connections: {id: name}."""
    config = report.read_json(Path(data_dir) / "config.json", {})
    users = [user for user in config.get("users", []) if isinstance(user, dict) and user.get("role") != "admin"] if isinstance(config.get("users"), list) else []
    # Who a site is: the user who made it, else one it is given to with a size of its own
    site_owner = {}
    for user in users:
        for site in user_sites(user):
            if site in (user.get("made") or {}).get("sites", []) or site not in site_owner:
                site_owner[site] = user
    result = {"sites": [], "connections": []}
    server_limits = load_limits(data_dir)
    measure_environment = measure_environment or environment_mb
    for site in sites:
        size = measure_site(site)
        if size is None:
            continue
        owner = site_owner.get(site)
        result["sites"].append({"name": site, "size_mb": size, "user": (owner.get("name") or owner.get("username")) if owner else None,
                                "user_id": owner.get("id") if owner else None,
                                "limit_mb": site_limit(owner, site) if owner else None, "server_limit_mb": server_limits.get(f"site|{site}")})
    # Every environment, measured where it runs (here or on its server), with the server's limit on it
    result["environments"] = [{**item, "server_limit_mb": server_limits.get(f"environment|{item['site']}|{item['target']}")}
                              for item in (measure_environments() if measure_environments else all_environments_mb())]
    for connection, label in connections.items():
        sizes = measure_databases(data_dir, connection, user_only=True)
        owners = {name: user for user in users for name in ((user.get("made") or {}).get("databases") or {}).get(connection, [])}
        items = []
        for name, size in sizes.items():
            owner = owners.get(name)
            items.append({"name": name, "size_mb": round(size, 1), "user": (owner.get("name") or owner.get("username")) if owner else None,
                          "user_id": owner.get("id") if owner else None,
                          "limit_mb": database_limit(owner, connection, name) if owner else None,
                          "server_limit_mb": server_limits.get(f"database|{connection}|{name}")})
        result["connections"].append({"id": connection, "name": label, "unreachable": not sizes, "databases": items,
                                      "engine": connection_engine(data_dir, connection)})
    for part in [result["sites"]] + [item["databases"] for item in result["connections"]]:
        part.sort(key=lambda item: -item["size_mb"])
    return result


def size_text(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:g} MB"


def overview_lines(everything):
    """Every site and database the users made, by user, each with its size and limit: under what changed in a mail"""
    lines, users = [], {}
    for item in everything:
        users.setdefault(item["user"], []).append(item)
    for who, items in users.items():
        lines.append(f"{who}:")
        for item in items:
            limit = f" of {size_text(item['limit_mb'])}" + (" (over)" if item["size_mb"] > item["limit_mb"] else "") if item.get("limit_mb") is not None else ", no limit"
            lines.append(f"- {item['kind'].capitalize()} {item['name']}: {size_text(item['size_mb'])}{limit}")
    return lines


def mail_body(new, solved, everything=None):
    lines = []
    if new:
        lines.append("Grown past its limit:")
        lines += [f"- {item['kind'].capitalize()} {item['name']} ({item['user']}): {size_text(item['size_mb'])} of {size_text(item['limit_mb'])}" for item in new]
    if solved:
        lines += ([""] if lines else []) + ["Under its limit again:"]
        lines += [f"- {item['kind'].capitalize()} {item['name']} ({item['user']})" for item in solved]
    if everything:
        lines += ["", "Everything the users made, now:"] + overview_lines(everything)
    return "\n".join(lines) + "\n\nThe limits are set on each user under Settings, Users.\n"


def bar(item):
    """How full: a bar of its size against its limit, green, amber from 80 % and red over it"""
    if item.get("limit_mb") is None:
        return ""
    share = item["size_mb"] / item["limit_mb"] if item["limit_mb"] else 1
    colour = "#dc2626" if share > 1 else "#d97706" if share >= .8 else "#16a34a"
    width = max(2, min(100, round(share * 100)))
    rest = f'<td class="cnt" style="background:#e5e7eb;font-size:0;line-height:0">&nbsp;</td>' if width < 100 else ""
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;border-collapse:separate;'
            f'border-radius:999px;overflow:hidden;height:6px"><tr><td width="{width}%" style="background:{colour};height:6px;font-size:0;line-height:0">'
            f'&nbsp;</td>{rest}</tr></table>')


def amount(item):
    limit = item.get("limit_mb")
    if limit is None:
        return f"{size_text(item['size_mb'])}, no limit"
    return f"{size_text(item['size_mb'])} of {size_text(limit)} &middot; {round(item['size_mb'] / limit * 100) if limit else 100} %"


def mail_html(new, solved, everything=None, host=None, now=None, test=False):
    """The mail as HTML: what went over, what came under, and everything the users made, by user, with how full"""
    host = host or socket.gethostname()
    esc, row, section = report.esc, report.row, report.section
    sections = []
    if new:
        sections.append(section("Grown past its limit", "".join(
            row("red", f"{item['kind']} over", f'<span class="tx">{esc(item["name"])}</span>', (f"Made by {item['user']}" if item.get("user_id") else "The server's own limit"),
                amount(item) + bar(item)) for item in new), len(new)))
    if solved:
        sections.append(section("Under its limit again", "".join(
            row("green", f"{item['kind']} under", f'<span class="tx">{esc(item["name"])}</span>', (f"Made by {item['user']}" if item.get("user_id") else "The server's own limit")) for item in solved),
            len(solved)))
    if everything:
        users = {}
        for item in everything:
            users.setdefault(item["user"], []).append(item)
        parts = []
        for who, items in users.items():
            lines = "".join(
                '<tr><td class="ln" style="padding:10px 0;border-top:1px solid #e5e7eb">'
                f'<span class="mu" style="font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#6b7280">{esc(item["kind"])}</span>'
                f'<div class="tx" style="font-size:14px;line-height:20px;font-weight:600;word-break:break-word">{esc(item["name"])}</div>'
                f'<div class="mu" style="font-size:12px;line-height:18px;color:#6b7280">{amount(item)}</div>{bar(item)}</td></tr>' for item in items)
            parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="rw rl" style="margin:0 0 10px;'
                         'border-collapse:separate;background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px"><tr><td style="padding:12px 16px 4px">'
                         f'<div class="tx" style="font-size:15px;font-weight:700;padding-bottom:8px">{esc(who)}</div>'
                         f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{lines}</table></td></tr></table>')
        sections.append(section("Everything the users made, now", "".join(parts), len(everything)))
    if test:
        tone, title = "blue", f"Test: {len(new)} over {'its' if len(new) == 1 else 'their'} limit"
    elif new:
        tone, title = "red", f"{len(new)} over {'its' if len(new) == 1 else 'their'} limit" + (f", {len(solved)} under again" if solved else "")
    else:
        tone, title = "green", f"{len(solved)} under {'its' if len(solved) == 1 else 'their'} limit again"
    over = sum(1 for item in (everything or []) if item.get("limit_mb") is not None and item["size_mb"] > item["limit_mb"])
    lead = f"{over} of what the users made {'is' if over == 1 else 'are'} over the limit now." if over else "Nothing the users made is over its limit now."
    footer = (f"Usage is checked on <strong>{esc(host)}</strong> every hour. Sent {report.stamp(now or time.time())}. "
              "The limits are set on each user under Settings, Users in Harbor; who is mailed, under Settings, Notifications.")
    if test:
        lead = "A test of the usage alerts: this is how they look, with what is over its limit now. Nothing was kept. " + lead
    return report.page(host, tone, title, lead, sections, footer, "Test: usage limits" if test else "Usage limits")


def test_report(data_dir, emails, mail=None, sender=None, now=None, **measure):
    """A test of the alerts, from their dialog: everything measured now, and the mail they give, with what is over now,
    sent to the addresses typed (not saved). Nothing is kept. Returned as {subject, body, to, error}."""
    emails = validate_settings({"enabled": True, "emails": emails})["emails"]
    everything = []
    over = check(data_dir, everything=everything, **measure)
    host = socket.gethostname()
    subject = f"[{host}] TEST: Usage limits: {len(over)} over"
    body = f"A test of the usage alerts on {host}: this is how they look. Nothing was kept.\n\n" + (
        mail_body(over, [], everything) if over or everything else "Nothing the users made is over its limit.\n")
    error = ""
    try:
        (sender or report.send)(report.load_mail() if mail is None else mail, emails, subject, body,
                                html=mail_html(over, [], everything, host, now, test=True))
    except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own; the user is told which)
        error = f"Not sent: {exc}"
    return {"subject": subject, "body": body, "to": emails if not error else [], "error": error}


def run(data_dir, mail=True, now=None, **measure):
    """Check, keep what is over in usage-state.json, and mail what went over or came under since the last check"""
    data_dir = Path(data_dir)
    now = int(now or time.time())
    # Everything a user made is measured too: Check now shows it, and a mail has it under what changed, databases with
    # no size limit as well
    everything = []
    over = check(data_dir, everything=everything, **measure)
    state_path = data_dir / "usage-state.json"
    state = report.read_json(state_path, {})
    before = state.get("open") if isinstance(state.get("open"), dict) else {}
    current = {item["key"]: item for item in over}
    new = [item for key, item in current.items() if key not in before]
    solved = [item for key, item in before.items() if key not in current]
    mail_error = state.get("mail_error")
    if mail:
        settings = load_settings(data_dir)
        if settings["enabled"] and settings["emails"] and (new or solved):
            try:
                report.send(report.load_mail(), settings["emails"], f"[{socket.gethostname()}] " + ("\U0001F534 " if new else "\u2705 ") + "Usage limits: "
                            + (f"{len(new)} over" if new else "") + (", " if new and solved else "") + (f"{len(solved)} under again" if solved else ""),
                            mail_body(new, solved, everything), html=mail_html(new, solved, everything, now=now))
                mail_error = None
            except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own)
                mail_error = str(exc)
                # Not mailed: it is tried again next time, as if it were new
                current = {key: item for key, item in current.items() if key in before}
        write_json(state_path, {"checked": now, "open": current, "mail_error": mail_error})
    return {"checked": now, "over": over, "mail_error": mail_error, "all": everything}


def needed(data_dir):
    """Whether the hourly check has work: the alerts are on, or a user has a size limit (they are shown what is over)"""
    if load_settings(data_dir)["enabled"]:
        return True
    if load_limits(data_dir):
        return True
    config = report.read_json(Path(data_dir) / "config.json", {})
    for user in config.get("users", []) if isinstance(config.get("users"), list) else []:
        if not isinstance(user, dict) or user.get("role") == "admin":
            continue
        if (user.get("limits") or {}).get("site_mb") is not None:
            return True
        if any(isinstance(grant, dict) and grant.get("size_mb") is not None for grant in (user.get("sites") or {}).values()):
            return True
        if any(((grant or {}).get("limits") or {}).get("database_mb") is not None for grant in (user.get("connections") or {}).values()):
            return True
    return False


def install(data_dir, cron=CRON):
    script = Path(__file__).resolve()
    release = re.fullmatch(r"/opt/harbor/releases/[^/]+/(.+)", str(script))
    if release:
        script = Path("/opt/harbor/current") / release[1]
    for value in (str(script), str(data_dir)):
        if re.search(r"[\s%\"]", value):
            raise ValueError("The cron job needs paths without spaces, quotes or percent signs")
    cron.write_text("SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
                    f'17 * * * * root /usr/bin/python3 "{script}" --data "{data_dir}" 2>&1 | /usr/bin/logger -t vns-usage\n', encoding="utf-8")
    cron.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--install")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--sync", help="the hourly job for DIR as its settings and users need it")
    parser.add_argument("--test-report", action="store_true", help="with --data: a test mail to the addresses on stdin (JSON), as JSON")
    args = parser.parse_args()
    if args.install:
        install(args.install)
    elif args.remove:
        CRON.unlink(missing_ok=True)
    elif args.sync:
        # The hourly job while it has work, none else
        if needed(args.sync):
            install(args.sync)
        else:
            CRON.unlink(missing_ok=True)
    elif args.data and args.test_report:
        try:
            print(json.dumps(test_report(args.data, json.loads(sys.stdin.read() or "[]"))))
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
    elif args.data:
        result = run(args.data, mail=not args.json)
        if args.json:
            print(json.dumps(result))
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
