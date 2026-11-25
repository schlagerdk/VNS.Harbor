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


def check(data_dir, measure_site=site_mb, measure_databases=database_sizes, everything=None):
    """What has grown past its user's limit now: a list, each with its key, kind, name, user, size and limit. With
    everything (a list), each site and database a user made goes into it as well, with its limit or None."""
    config = report.read_json(Path(data_dir) / "config.json", {})
    over, sizes = [], {}
    for user in config.get("users", []) if isinstance(config.get("users"), list) else []:
        if not isinstance(user, dict) or user.get("role") == "admin":
            continue
        limits, made = user.get("limits") or {}, user.get("made") or {}
        who = user.get("name") or user.get("username") or user.get("id", "")
        if everything is not None or any(site_limit(user, site) is not None for site in made.get("sites", [])):
            for site in made.get("sites", []):
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
    return over


def site_limit(user, site):
    """How big a site of the user's may be: its own limit when it has one, else the one their limits give every site"""
    own = (((user or {}).get("sites") or {}).get(site) or {}).get("size_mb")
    return own if own is not None else ((user or {}).get("limits") or {}).get("site_mb")


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
    return bool(user.get("create_sites") or made.get("sites") or any((made.get("databases") or {}).values())
                or any({"create", "copy"} & set(grant.get("actions") or []) for grant in (user.get("connections") or {}).values()))


def of_user(data_dir, user, names=None, measure_site=site_mb, measure_databases=database_sizes):
    """What one user has made and how big each is now, beside their limits: their sites, and their databases on each
    connection where they may make some or have. names gives each connection's name. Measured now."""
    made, limits = user.get("made") or {}, user.get("limits") or {}
    # Each site with its own limit: the one set on it, else the user's for every site
    sites = [{"name": site, "size_mb": size, "limit_mb": site_limit(user, site)} for site in made.get("sites", []) for size in [measure_site(site)] if size is not None]
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


def overview(data_dir, sites, connections, measure_site=site_mb, measure_databases=database_sizes):
    """Every site here and every database on each connection (system databases left out), how big each is now, who
    made it and the limit that holds for it: for an administrator. sites: the site folders; connections: {id: name}."""
    config = report.read_json(Path(data_dir) / "config.json", {})
    users = [user for user in config.get("users", []) if isinstance(user, dict) and user.get("role") != "admin"] if isinstance(config.get("users"), list) else []
    site_owner = {site: user for user in users for site in (user.get("made") or {}).get("sites", [])}
    result = {"sites": [], "connections": []}
    for site in sites:
        size = measure_site(site)
        if size is None:
            continue
        owner = site_owner.get(site)
        result["sites"].append({"name": site, "size_mb": size, "user": (owner.get("name") or owner.get("username")) if owner else None,
                                "user_id": owner.get("id") if owner else None,
                                "limit_mb": site_limit(owner, site) if owner else None})
    for connection, label in connections.items():
        sizes = measure_databases(data_dir, connection, user_only=True)
        owners = {name: user for user in users for name in ((user.get("made") or {}).get("databases") or {}).get(connection, [])}
        items = []
        for name, size in sizes.items():
            owner = owners.get(name)
            items.append({"name": name, "size_mb": round(size, 1), "user": (owner.get("name") or owner.get("username")) if owner else None,
                          "user_id": owner.get("id") if owner else None,
                          "limit_mb": database_limit(owner, connection, name) if owner else None})
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
                report.send(report.load_mail(), settings["emails"], f"[{socket.gethostname()}] Usage limits: "
                            + (f"{len(new)} over" if new else "") + (", " if new and solved else "") + (f"{len(solved)} under again" if solved else ""),
                            mail_body(new, solved, everything))
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
    config = report.read_json(Path(data_dir) / "config.json", {})
    for user in config.get("users", []) if isinstance(config.get("users"), list) else []:
        if not isinstance(user, dict) or user.get("role") == "admin":
            continue
        if (user.get("limits") or {}).get("site_mb") is not None:
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
