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
import math
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


def load_limit_sources(data_dir):
    """The limits set from another server (the Harbor that manages the site as one of its environments), each with where
    it came from: {key: {host, address, site, target}} (address, site and target: that server's own, to send a change
    back to it)"""
    saved = report.read_json(Path(data_dir) / "usage.json", {}).get("limits_from")
    found = {}
    for key, value in (saved.items() if isinstance(saved, dict) else []):
        if isinstance(value, str):
            value = {"host": value}
        if isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("host"), str):
            found[key] = {"host": value["host"], **{name: str(value.get(name) or "") for name in ("address", "site", "target")},
                          "port": value.get("port") if isinstance(value.get("port"), int) else 22,
                          # Whether the server it was set on mails it (its usage alerts on): else this one does
                          "mails": value.get("mails") is not False}
    return found


def load_environment_of(data_dir):
    """The sites here that are an environment of a site on another server, as that server told when it sent a limit
    (kept when the limit is taken away): {key: {host, address, site, target, port}}, so a limit set here later is sent
    there too (1.0.97)"""
    saved = report.read_json(Path(data_dir) / "usage.json", {}).get("environment_of")
    return {key: value for key, value in saved.items() if isinstance(key, str) and isinstance(value, dict) and value.get("host")} if isinstance(saved, dict) else {}


def load_limit_origins(data_dir):
    """The limits set from another server: {key: its host name}"""
    return {key: value["host"] for key, value in load_limit_sources(data_dir).items()}


def part_limits(limits, site):
    """The limits given to a site's parts (the site itself as main, and each environment): {target: mb}"""
    prefix = f"environment|{site}|"
    return {key[len(prefix):]: value for key, value in limits.items() if key.startswith(prefix)}


def limit_conflict(data_dir, kind, name, connection="", limit_mb=None, measure_environment=None):
    """Why a limit does not add up, or "" (1.0.97): a site's limit is the budget of its solution (the site itself and
    every environment, wherever it runs), and the limits given to its parts are shares of it. Together they are never
    more than it, so a part's is at most what the others leave, and the site's at least what its parts were given."""
    if limit_mb is None or kind not in ("site", "environment"):
        return ""
    limits = load_limits(data_dir)
    size = lambda mb: f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:g} MB"
    # A bound as it can be typed: an upper one rounded down, a lower one up (2105 MB as 2.05 GB)
    down = lambda mb: f"{math.floor(mb / 1024 * 100) / 100:g} GB" if mb >= 1024 else f"{math.floor(mb)} MB"
    up = lambda mb: f"{math.ceil(mb / 1024 * 100) / 100:g} GB" if mb >= 1024 else f"{math.ceil(mb)} MB"
    # What each part uses now, as measured last (the part itself measured again): a limit it is past at once is not set
    uses = part_sizes(data_dir, name)
    if kind == "environment":
        now = part_now(data_dir, name, connection, uses.get(connection), measure_environment)
        if now is not None:
            uses[connection] = now
    if kind == "environment":
        budget = limits.get(f"site|{name}")
        others = {target: mb for target, mb in part_limits(limits, name).items() if target != connection}
        given = sum(others.values())
        rest = sum(mb for target, mb in uses.items() if target != connection and target not in others)
        if connection in uses and limit_mb < uses[connection]:
            return f"At least {up(uses[connection])}, what it uses now: a limit below that is past at once."
        if budget is not None and given + limit_mb + rest > budget and given + limit_mb <= budget:
            return (f"At most {down(max(0, budget - given - rest))}: the site's other parts with no share of their own use {size(rest)} now, "
                    f"and they would be past {name}'s {size(budget)} at once.")
        if budget is not None and given + limit_mb > budget:
            left = max(0, budget - given)
            return (f"Only {size(left)} is left of {name}'s {size(budget)}: {size(given)} is given to its other parts "
                    f"({', '.join(f'{target} {size(mb)}' for target, mb in others.items())}). Give the site more, or another part less."
                    if others else f"At most {size(budget)}, {name}'s limit for the site and all its environments.")
        return ""
    given = part_limits(limits, name)
    if given and sum(given.values()) > limit_mb:
        return (f"At least {size(sum(given.values()))}: that is given to its parts "
                f"({', '.join(f'{target} {size(mb)}' for target, mb in given.items())}). Give them less first.")
    need = sum(given.values()) + sum(mb for target, mb in uses.items() if target not in given)
    if uses and limit_mb < need:
        return f"At least {up(need)}: the site and its environments need that now (their shares, and what the rest uses). Below it, it is past at once."
    return ""


def limit_room(data_dir, site, target, measure_environment=None):
    """The room for a part's share of its site's limit, as limit_conflict holds it to: {budget, uses, min, max, given,
    rest} in MB (max None without a limit on the site). For the server the part runs on, to say before it is typed (1.0.97)"""
    limits = load_limits(data_dir)
    uses = part_sizes(data_dir, site)
    now = part_now(data_dir, site, target, uses.get(target), measure_environment)
    if now is not None:
        uses[target] = now
    budget = limits.get(f"site|{site}")
    others = {name: mb for name, mb in part_limits(limits, site).items() if name != target}
    given = sum(others.values())
    rest = sum(mb for name, mb in uses.items() if name != target and name not in others)
    least = math.ceil(uses.get(target, 0))
    return {"budget": budget, "uses": uses.get(target), "min": least, "given": given, "rest": round(rest, 1),
            "max": None if budget is None else max(0, math.floor(budget - given - rest))}


def part_now(data_dir, site, target, kept=None, measure_environment=None):
    """What a part uses now: measured again (an environment, here or on its server), or as the last check found it,
    whichever is more, as the check holds it to that and the sizes kept for Usage may be older"""
    found = [kept] if kept is not None else []
    if target != "main":
        try:
            found.append((measure_environment or environment_mb)(site, target))
        except Exception:  # noqa: BLE001  (not reached now: the sizes known are used)
            pass
    state = report.read_json(Path(data_dir) / "usage-state.json", {})
    item = ((state.get("open") or {}) if isinstance(state, dict) else {}).get(f"limit|environment|{site}|{target}")
    if isinstance(item, dict):
        found.append(item.get("size_mb"))
    found = [mb for mb in found if isinstance(mb, (int, float))]
    return max(found) if found else None


def part_sizes(data_dir, site):
    """What each part of a site uses, as measured last for Usage: {"main": the site itself, target: environment}; empty
    when it has not been measured"""
    kept = report.read_json(Path(data_dir) / "usage-sizes.json", {})
    sizes = kept.get("sizes") if isinstance(kept, dict) else None
    if not isinstance(sizes, dict):
        return {}
    environments = sizes.get("environments") or []
    found = solution_of(site, (sizes.get("sites") or {}).get(site), environments)
    if not found:
        return {}
    parts = {"main": found["main_mb"]}
    parts.update({item["target"]: item["size_mb"] for item in environments if isinstance(item, dict) and item.get("site") == site and item.get("size_mb") is not None})
    return parts


def set_limit(data_dir, kind, name, connection="", limit_mb=None, origin=None):
    """A site's or a database's limit set (a number of MB) or taken away (None). origin: the server it was set from,
    when it is an environment of a site there (its limit sent here, 1.0.97): its host name, or {host, address, site,
    target}; a limit set here has none"""
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
    origins = saved.get("limits_from") if isinstance(saved.get("limits_from"), dict) else {}
    if limit_mb is not None and origin:
        origins[key] = origin
    else:
        origins.pop(key, None)
    saved["limits_from"] = origins
    # Whose environment it is stays known after its limit is taken away
    if isinstance(origin, dict) and origin.get("site"):
        known = saved.get("environment_of") if isinstance(saved.get("environment_of"), dict) else {}
        known[key] = {name: origin.get(name) for name in ("host", "address", "site", "target", "port") if origin.get(name) is not None}
        saved["environment_of"] = known
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


def solution_of(site, folder_mb, environments):
    """A site as a whole (1.0.97): {solution_mb, main_mb}. Its folder here holds the site itself and the environments in
    it; the solution is that and every environment of it elsewhere (another server, a folder of its own) as measured.
    The site itself (main) is its folder without the environments in it. None when its folder is gone."""
    if folder_mb is None:
        return None
    mine = [item for item in environments or [] if isinstance(item, dict) and item.get("site") == site and item.get("size_mb") is not None]
    inside = sum(item["size_mb"] for item in mine if item.get("in_site"))
    outside = sum(item["size_mb"] for item in mine if not item.get("in_site"))
    return {"solution_mb": round(folder_mb + outside, 1), "main_mb": round(max(0.0, folder_mb - inside), 1)}


def kept_environments(data_dir):
    """The environments as measured last for Usage (usage-sizes.json), or None: a site's solution without measuring them
    over SSH again"""
    kept = report.read_json(Path(data_dir) / "usage-sizes.json", {})
    found = ((kept.get("sizes") or {}).get("environments")) if isinstance(kept, dict) else None
    return found if isinstance(found, list) else None


def solution_mb(site, web_root=WEB_ROOT, environments=None):
    """A site's solution in MB: its folder here and its environments elsewhere"""
    found = solution_of(site, site_mb(site, web_root), environments if environments is not None else all_environments_mb())
    return found["solution_mb"] if found else None


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
        return found if isinstance(found, list) else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        # Not measured: None, so a check takes it as not known, never as nothing there
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


def check(data_dir, measure_site=site_mb, measure_databases=database_sizes, everything=None, measure_environment=None,
          measure_environments=None, unknown=None):
    """What has grown past its user's limit now: a list, each with its key, kind, name, user, size and limit. With
    everything (a list), each site and database a user made goes into it as well, with its limit or None. A site's
    size is its solution: the site and every environment of it, wherever it runs (1.0.97)."""
    config = report.read_json(Path(data_dir) / "config.json", {})
    over, sizes = [], {}
    measured = {}

    def environments():
        if "environments" not in measured:
            measured["environments"] = measure_environments() if measure_environments else all_environments_mb()
        return measured["environments"]

    def whole(site):
        if ("site", site) not in measured:
            folder = measure_site(site)
            found = environments()
            # A part not measured now (its server not reached): the site's size is not known, rather than less
            if found is None or any(item.get("site") == site and item.get("size_mb") is None and not item.get("windows") for item in found):
                measured[("site", site)] = None
                if unknown is not None:
                    unknown.add(f"site|{site}")
            else:
                measured[("site", site)] = solution_of(site, folder, found) if folder is not None else None
        return measured[("site", site)]

    def solution(site):
        found = whole(site)
        return found["solution_mb"] if found else None
    for user in config.get("users", []) if isinstance(config.get("users"), list) else []:
        if not isinstance(user, dict) or user.get("role") == "admin":
            continue
        limits, made = user.get("limits") or {}, user.get("made") or {}
        who = user.get("name") or user.get("username") or user.get("id", "")
        if everything is not None or any(site_limit(user, site) is not None for site in user_sites(user)):
            for site in user_sites(user):
                size = solution(site)
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
    sources = load_limit_sources(data_dir)
    for key, limit in load_limits(data_dir).items():
        kind, _, rest = key.partition("|")
        if kind == "environment":
            site, _, target = rest.partition("|")
            # The site itself is a part as its environments are: its folder without the environments in it
            size = (whole(site) or {}).get("main_mb") if target == "main" else measure_environment(site, target)
            if size is None and unknown is not None:
                unknown.add(key)
            item = {"key": "limit|" + key, "kind": "environment", "name": f"{site} ({target})", "site": site, "target": target,
                    "user": "the server's limit", "user_id": None, "size_mb": size, "limit_mb": limit}
        elif kind == "site":
            size = solution(rest)
            item = {"key": "limit|" + key, "kind": "site", "name": rest, "user": "the server's limit", "user_id": None, "size_mb": size, "limit_mb": limit}
        else:
            connection, _, name = rest.partition("|")
            if connection not in sizes:
                sizes[connection] = measure_databases(data_dir, connection)
            size = sizes[connection].get(name)
            item = {"key": "limit|" + key, "kind": "database", "name": name, "connection": connection, "user": "the server's limit",
                    "user_id": None, "size_mb": round(size, 1) if size is not None else None, "limit_mb": limit}
        # Set from the server that manages it as an environment: shown here, mailed from there
        if key in sources:
            item.update({"from": sources[key]["host"], "user": f"the limit set on {sources[key]['host']}", "mailed_there": sources[key]["mails"]})
        if item["size_mb"] is None and unknown is not None:
            unknown.add(key)
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


def plain_users(data_dir):
    """The users who are not administrators: those limits are set for"""
    config = report.read_json(Path(data_dir) / "config.json", {})
    return [user for user in config.get("users", []) if isinstance(user, dict) and user.get("role") != "admin"] if isinstance(config.get("users"), list) else []


def site_owners(users):
    """Whose each site is: the user who made it, else one it is given to with a size of its own"""
    owner = {}
    for user in users:
        for site in user_sites(user):
            if site in (user.get("made") or {}).get("sites", []) or site not in owner:
                owner[site] = user
    return owner


def site_limits(data_dir, site, measure_site=site_mb):
    """The limits a site is held to and how big it is now (its folder, with the environments in it): for Add
    environment, which tells whether a new environment counts in them (1.0.95)"""
    owner = site_owners(plain_users(data_dir)).get(site)
    limits = load_limits(data_dir)
    return {"size_mb": measure_site(site), "server_limit_mb": limits.get(f"site|{site}"),
            "given_mb": sum(part_limits(limits, site).values()),
            "limit_mb": site_limit(owner, site) if owner else None, "user": (owner.get("name") or owner.get("username")) if owner else None}


def measure_all(data_dir, sites, connections, measure_site=site_mb, measure_databases=database_sizes, measure_environments=None):
    """How big every site here, every environment and every database on each connection is now: what takes time in
    Usage, kept apart from who has what and their limits, so it can be kept a while (usage-sizes.json, 1.0.95)"""
    return {"sites": {site: measure_site(site) for site in sites},
            "environments": (measure_environments() if measure_environments else all_environments_mb()) or [],
            "databases": {connection: measure_databases(data_dir, connection, user_only=True) for connection in connections},
            "engines": {connection: connection_engine(data_dir, connection) for connection in connections}}


def overview_of(data_dir, sizes, connections):
    """Usage from sizes measured (measure_all): with who made each and the limits as they are now, as a limit changed
    shows at once, also on sizes kept from before"""
    users = plain_users(data_dir)
    site_owner = site_owners(users)
    result = {"sites": [], "connections": []}
    server_limits = load_limits(data_dir)
    origins = load_limit_origins(data_dir)
    # A folder here that is an environment of a site (in a folder of its own) is a part of that site, not a site
    parts = environment_folders(sizes["environments"])
    for site, folder in sizes["sites"].items():
        if site in parts:
            continue
        found = solution_of(site, folder, sizes["environments"])
        if found is None:
            continue
        owner = site_owner.get(site)
        # Its size is its solution: the site itself (main) and every environment, wherever it runs
        result["sites"].append({"name": site, "size_mb": found["solution_mb"], "folder_mb": folder, "main_mb": found["main_mb"],
                                "main_limit_mb": server_limits.get(f"environment|{site}|main"),
                                "user": (owner.get("name") or owner.get("username")) if owner else None,
                                "user_id": owner.get("id") if owner else None,
                                "limit_mb": site_limit(owner, site) if owner else None, "server_limit_mb": server_limits.get(f"site|{site}"),
                                "server_limit_from": origins.get(f"site|{site}")})
    # Every environment, measured where it runs (here or on its server), with the server's limit on it
    result["environments"] = [{**item, "server_limit_mb": server_limits.get(f"environment|{item['site']}|{item['target']}")}
                              for item in sizes["environments"]]
    for connection, label in connections.items():
        found = sizes["databases"].get(connection) or {}
        owners = {name: user for user in users for name in ((user.get("made") or {}).get("databases") or {}).get(connection, [])}
        items = []
        for name, size in found.items():
            owner = owners.get(name)
            items.append({"name": name, "size_mb": round(size, 1), "user": (owner.get("name") or owner.get("username")) if owner else None,
                          "user_id": owner.get("id") if owner else None,
                          "limit_mb": database_limit(owner, connection, name) if owner else None,
                          "server_limit_mb": server_limits.get(f"database|{connection}|{name}")})
        result["connections"].append({"id": connection, "name": label, "unreachable": not found, "databases": items,
                                      "engine": sizes.get("engines", {}).get(connection)})
    for part in [result["sites"]] + [item["databases"] for item in result["connections"]]:
        part.sort(key=lambda item: -item["size_mb"])
    return result


def overview(data_dir, sites, connections, measure_site=site_mb, measure_databases=database_sizes, measure_environment=None, measure_environments=None):
    """Every site here and every database on each connection (system databases left out), how big each is now, who
    made it and the limit that holds for it: for an administrator. sites: the site folders; connections: {id: name}."""
    return overview_of(data_dir, measure_all(data_dir, sites, connections, measure_site, measure_databases, measure_environments), connections)


SIZES_KEPT = 600


def environment_folders(environments, web_root=WEB_ROOT):
    """The folders in web_root that are an environment of another site (its folder of its own here)"""
    found = set()
    for item in environments or []:
        if not isinstance(item, dict) or item.get("in_site") or not item.get("local") or item.get("windows"):
            continue
        root = Path(str(item.get("root") or "")).resolve() if item.get("root") else None
        if root and root.parent == Path(web_root).resolve() and root.name != item.get("site"):
            found.add(root.name)
    return found


def environment_ids(site, web_root=WEB_ROOT, state=Path("/var/lib/harbor/sites")):
    """The environments a site has now, as Harbor reads them (harbor/environments/settings.target_ids): from its
    scripts while it has any, else Harbor's copy. A cheap look at files, no measuring"""
    root = Path(web_root) / site
    ids = ["prod"] if (root / "scripts/publish_prod.sh").is_file() else []
    targets = root / "scripts/targets"
    if targets.is_dir():
        ids += [path.name for path in targets.iterdir() if path.is_dir() and (path / "publish_prod.sh").is_file()]
    if ids:
        return set(ids)
    copy = Path(state) / site / "environments"
    return {path.stem for path in copy.glob("*.json")} if copy.is_dir() else set()


def drop_gone_environments(data_dir, sites, web_root=WEB_ROOT, ids=environment_ids):
    """Limits of environments that are gone (deleted, or their site gone): taken away, so a share of one does not stay in
    its site's limit. The environments a site has now: [(site, target)]"""
    gone = []
    for key in load_limits(data_dir):
        kind, _, rest = key.partition("|")
        # A site gone: its limit with it
        if kind == "site" and rest not in sites:
            set_limit(data_dir, "site", rest, limit_mb=None)
            continue
        if kind != "environment":
            continue
        site, _, target = rest.partition("|")
        if target == "main":
            if site not in sites:
                gone.append((site, target))
        elif site not in sites or target not in ids(site, web_root):
            gone.append((site, target))
    for site, target in gone:
        set_limit(data_dir, "environment", site, target, limit_mb=None)
    return gone


def kept_overview(data_dir, sites, connections, fresh=False, now=None, web_root=WEB_ROOT, ids=environment_ids, **measure):
    """Usage at once from the sizes measured last (usage-sizes.json), with measured (when) and stale (older than ten
    minutes, or sites or connections have come or gone since): the page shows it, and asks for fresh sizes after.
    fresh, or nothing kept yet: measured now, and kept. Who has what and the limits are always as they are now."""
    now = time.time() if now is None else now
    path = Path(data_dir) / "usage-sizes.json"
    kept = report.read_json(path, {}) if not fresh else {}
    sizes = kept.get("sizes") if isinstance(kept, dict) else None
    valid = isinstance(sizes, dict) and all(isinstance(sizes.get(key), want) for key, want in
                                             (("sites", dict), ("environments", list), ("databases", dict)))
    if not valid:
        sizes = measure_all(data_dir, sites, connections, **measure)
        kept = {"measured": now, "sizes": sizes}
        write_json(path, kept)
    measured = kept.get("measured") or 0
    # An environment deleted (or made) since: shown as it is now at once, and measured again behind it
    have = {site: ids(site, web_root) for site in sites}
    kept_envs = [item for item in sizes["environments"] if isinstance(item, dict) and item.get("target") in have.get(item.get("site"), set())]
    changed = len(kept_envs) != len(sizes["environments"]) or sum(len(found) for found in have.values()) != len(kept_envs)
    sizes = {**sizes, "environments": kept_envs}
    drop_gone_environments(data_dir, sites, web_root, ids)
    stale = changed or now - measured > SIZES_KEPT or sorted(sizes["sites"]) != sorted(sites) or sorted(sizes["databases"]) != sorted(connections)
    # A site gone since is not shown, though its size is kept until the next measuring
    shown = {**sizes, "sites": {site: size for site, size in sizes["sites"].items() if site in sites}}
    return {**overview_of(data_dir, shown, connections), "measured": measured, "stale": bool(stale)}


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
    unknown = set()
    # Limits of what is gone (a site or environment deleted) taken away first: gone is not "not measured now"
    if "measure_site" not in measure:
        web = Path(WEB_ROOT)
        drop_gone_environments(data_dir, [path.name for path in web.iterdir() if path.is_dir() and not path.is_symlink()] if web.is_dir() else [])
    over = check(data_dir, everything=everything, unknown=unknown, **measure)
    state_path = data_dir / "usage-state.json"
    state = report.read_json(state_path, {})
    before = state.get("open") if isinstance(state.get("open"), dict) else {}
    current = {item["key"]: item for item in over}
    # Not measured now (a server not reached): what was over stays over, and is not said to be fine again
    for key, item in before.items():
        if key not in current and (key.removeprefix("limit|") in unknown or key in unknown):
            current[key] = item
    # Not mailed last time (the mail could not be sent): mailed as new now, while it is still over
    unmailed = set(state.get("unmailed") or [])
    new = [item for key, item in current.items() if key not in before or key in unmailed]
    solved = [item for key, item in before.items() if key not in current]
    # A limit set from the server that manages the site is mailed from there while its usage alerts are on: here it is
    # then only shown. With them off there, it is mailed from here, so it is mailed once, from one of the two.
    mailed_there = lambda item: item.get("from") and item.get("mailed_there", True)
    new = [item for item in new if not mailed_there(item)]
    solved = [item for item in solved if not mailed_there(item)]
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
                # Not mailed: still shown (Needs attention, Usage), and mailed next time, as if it were new
                unmailed |= {item["key"] for item in new}
            else:
                unmailed = set()
        unmailed &= set(current)
        write_json(state_path, {"checked": now, "open": current, "mail_error": mail_error, "unmailed": sorted(unmailed)})
        tell_others(set(current) ^ set(before))
    return {"checked": now, "over": over, "mail_error": mail_error, "all": everything}


def tell_others(changed):
    """What went past a limit or under it again, where the limit is shared with another server (an environment there, or
    a site here that is one of a site there): that server told to check at once too, so both show it (1.0.97). Not when
    this check was asked by that server, so they never tell each other round"""
    keys = sorted(key for key in changed if key.startswith(("limit|environment|", "limit|site|")))
    if not keys or os.environ.get("HARBOR_USAGE_TOLD") == "1":
        return
    harbor = Path(__file__).resolve().parents[1] / "bin" / "harbor"
    try:
        subprocess.Popen(["/usr/bin/python3", str(harbor), "server", "usage-tell", *keys], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


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
