"""VNS admin site: manage the sites in /var/www, run the VNS scripts, open shells and edit files.

Runs as root behind Apache (HTTPS). Everything except /login and /static requires a logged-in session.
"""
import codecs
import base64
import glob
import hmac
import io
import ipaddress
import importlib.util
import json
import os
import socket
import ssl
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import ptyprocess
from flask import Flask, Response, abort, g, has_request_context, jsonify, redirect, render_template, request, send_file, send_from_directory, session, stream_with_context
from flask_sock import Sock
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
# What the admin writes (its login, audit log, SQL connections, update state, downloaded libraries): the admin site's
# app folder, which its service names (HARBOR_ADMIN_DATA); beside the code only in a working copy
DATA = os.environ.get("HARBOR_ADMIN_DATA") or BASE
CONFIG_PATH = os.environ.get("VNS_ADMIN_CONFIG", os.path.join(DATA, "config.json"))
with open(CONFIG_PATH) as _f:
    cfg = json.load(_f)

WEB_ROOT = os.path.realpath(cfg.get("web_root", "/var/www"))
# The Harbor installation this admin is part of (this app is its admin/ folder), so the admin and harbor are always one
# version. A release is named through /opt/harbor/current, as harbor names it (harbor/core/paths.py): what the admin
# writes or runs never names a release that is removed later.
HARBOR_DIR = os.path.dirname(BASE)
if re.match(r"/opt/harbor/releases/[^/]+$", HARBOR_DIR):
    HARBOR_DIR = "/opt/harbor/current"
LIB_DIR = os.path.join(HARBOR_DIR, "lib")
_files_spec = importlib.util.spec_from_file_location("vns_remote_files", os.path.join(LIB_DIR, "remote_files.py"))
_files_module = importlib.util.module_from_spec(_files_spec)
_files_spec.loader.exec_module(_files_module)
editable_file = _files_module.editable_file
contains_editable_files = _files_module.contains_editable_files
file_metadata = _files_module.file_metadata
_domains_spec = importlib.util.spec_from_file_location("vns_domain_names", os.path.join(LIB_DIR, "domain_names.py"))
_domains_module = importlib.util.module_from_spec(_domains_spec)
_domains_spec.loader.exec_module(_domains_module)
_normalize_dns_name = _domains_module.normalize_dns_name
_display_dns_name = _domains_module.display_dns_name
_normalize_site_name = _domains_module.normalize_site_name
service_name = _domains_module.service_name
# Harbor, installed beside the apache folder (domain_names.py above already needs it): how environments are read
if HARBOR_DIR not in sys.path:
    sys.path.insert(0, HARBOR_DIR)
_publish_spec = importlib.util.spec_from_file_location("vns_publish_status", os.path.join(LIB_DIR, "publish_status.py"))
_publish_module = importlib.util.module_from_spec(_publish_spec)
_publish_spec.loader.exec_module(_publish_module)
_database_spec = importlib.util.spec_from_file_location("vns_database_info", os.path.join(LIB_DIR, "database_info.py"))
_database_module = importlib.util.module_from_spec(_database_spec)
_database_spec.loader.exec_module(_database_module)
_keep_alive_spec = importlib.util.spec_from_file_location(
    "vns_keep_alive", os.path.join(LIB_DIR, "keep_alive.py"))
_keep_alive_module = importlib.util.module_from_spec(_keep_alive_spec)
_keep_alive_spec.loader.exec_module(_keep_alive_module)
_cloudflare_spec = importlib.util.spec_from_file_location("vns_cloudflare", os.path.join(LIB_DIR, "cloudflare.py"))
_cloudflare = importlib.util.module_from_spec(_cloudflare_spec)
_cloudflare_spec.loader.exec_module(_cloudflare)
_mailcow_spec = importlib.util.spec_from_file_location("vns_mailcow", os.path.join(LIB_DIR, "mailcow.py"))
_mailcow = importlib.util.module_from_spec(_mailcow_spec)
_mailcow_spec.loader.exec_module(_mailcow)
_reset_spec = importlib.util.spec_from_file_location("vns_password_reset", os.path.join(LIB_DIR, "password_reset.py"))
_password_reset = importlib.util.module_from_spec(_reset_spec)
_reset_spec.loader.exec_module(_password_reset)
_usage_spec = importlib.util.spec_from_file_location("vns_usage", os.path.join(LIB_DIR, "usage.py"))
_usage = importlib.util.module_from_spec(_usage_spec)
_usage_spec.loader.exec_module(_usage)
_cron_watch_spec = importlib.util.spec_from_file_location("vns_cron_watch", os.path.join(LIB_DIR, "cron_watch.py"))
_cron_watch = importlib.util.module_from_spec(_cron_watch_spec)
_cron_watch_spec.loader.exec_module(_cron_watch)
_health_spec = importlib.util.spec_from_file_location("vns_server_health", os.path.join(LIB_DIR, "server_health.py"))
_health = importlib.util.module_from_spec(_health_spec)
_health_spec.loader.exec_module(_health)
_data_sync_spec = importlib.util.spec_from_file_location("vns_data_sync", os.path.join(LIB_DIR, "data_sync.py"))
_data_sync_module = importlib.util.module_from_spec(_data_sync_spec)
_data_sync_spec.loader.exec_module(_data_sync_module)
ADMIN_SITE = cfg.get("admin_site", "")
_output_problem_spec = importlib.util.spec_from_file_location("vns_output_problem", os.path.join(LIB_DIR, "output_problem.py"))
_output_problem = importlib.util.module_from_spec(_output_problem_spec)
_output_problem_spec.loader.exec_module(_output_problem)
_tokens_spec = importlib.util.spec_from_file_location("vns_publish_tokens", os.path.join(LIB_DIR, "publish_tokens.py"))
_publish_tokens = importlib.util.module_from_spec(_tokens_spec)
_tokens_spec.loader.exec_module(_publish_tokens)
_access_spec = importlib.util.spec_from_file_location("vns_access", os.path.join(LIB_DIR, "access.py"))
access = importlib.util.module_from_spec(_access_spec)
_access_spec.loader.exec_module(access)
SQL_HELPER = os.path.join(LIB_DIR, "sql_admin.py")
_sql_spec = importlib.util.spec_from_file_location("vns_sql_admin", SQL_HELPER)
_sql_module = importlib.util.module_from_spec(_sql_spec)
_sql_spec.loader.exec_module(_sql_module)
_design_spec = importlib.util.spec_from_file_location("vns_sql_design", os.path.join(LIB_DIR, "sql_design.py"))
_sql_design = importlib.util.module_from_spec(_design_spec)
_design_spec.loader.exec_module(_sql_design)
_sql_design.use(_sql_module)
SQL_CONNECTIONS = os.path.join(DATA, "sql-connections.json")
SQL_LOCK = threading.Lock()
APACHE_SITES = "/etc/apache2/sites-available"
APACHE_ENABLED = "/etc/apache2/sites-enabled"
SERVICE_DIR = "/etc/systemd/system"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=cfg["secret_key"],
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=not cfg.get("insecure_cookies", False),
    PERMANENT_SESSION_LIFETIME=2 * 60 * 60,  # idle timeout: the session cookie is refreshed on every request
    MAX_CONTENT_LENGTH=500 * 1024 * 1024,
)
sock = Sock(app)


@app.route("/static/vendor/<path:name>")
def vendor_file(name):
    """The downloaded front-end libraries (xterm.js, Monaco): kept with the admin's data, as a release never changes"""
    return send_from_directory(os.path.join(DATA, "static", "vendor"), name)

NAME_RE = re.compile(r"^[^\W_](?:(?:[^\W_]|-)*[^\W_])?(?:\.[^\W_](?:(?:[^\W_]|-)*[^\W_])?)*$")
BINDING_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCRIPT_RE = re.compile(r"^[\w.-]+\.sh\Z")


@app.route("/api/keep-alive", methods=["GET", "PUT"])
def api_keep_alive():
    try:
        settings = _keep_alive_module.load()
        if request.method == "PUT":
            require_admin()
            # What is sent changes what was there: the interval alone keeps the report, and the other way round
            changes = request.get_json(silent=True)
            if not isinstance(changes, dict):
                return error("Expected keep-alive settings")
            settings = _keep_alive_module.validate({**settings, **changes})
            _keep_alive_module.save(settings)
            if "report" in changes:
                audit("Changed the keep-alive report")
                pushed = pushed_notification("report")
        # When it last ran: cron starts it each minute, and it runs once the interval has gone since then
        try:
            last_run = int(float((_keep_alive_module.STATE / "last-run").read_text()))
        except (OSError, ValueError):
            last_run = None
        return jsonify(**settings, installed=os.path.isfile("/etc/cron.d/vns-keep-alive"), last_run=last_run,
                       mail=bool(_keep_alive_module.report.load_mail().get("host")) if is_admin() else None, **(pushed if request.method == "PUT" and "report" in changes else {}))
    except FileNotFoundError:
        return error("Keep-alive is not installed. Run harbor server init on this server.", 503)
    except ValueError as exc:
        return error(str(exc), 400)
    except OSError:
        app.logger.exception("Could not read or save keep-alive settings")
        return error("Could not read or save keep-alive settings. Inspect the admin app log.", 500)


@app.route("/api/keep-alive/log")
def api_keep_alive_log():
    """What keep-alive finds wrong now, what started and ended lately (newest first), and the report's last mail error"""
    require_admin()
    site_report = _keep_alive_module.report
    state = _keep_alive_module.STATE
    saved = site_report.read_json(state / "report.json", {})
    opened = saved.get("open", {}) if isinstance(saved.get("open"), dict) else {}
    problems = [{"name": key.split("|", 1)[0], "check": key.split("|", 1)[1], **value} for key, value in sorted(opened.items())]
    return jsonify(problems=problems, events=list(reversed(site_report.events(state)[-200:])), checked=saved.get("checked"),
                   mail_error=saved.get("mail_error"), checks=site_report.CHECKS)


@app.route("/api/keep-alive/test", methods=["POST"])
def api_keep_alive_test():
    """A test run of keep-alive: every site checked now, and a test report mailed to the report's recipients and shown"""
    require_admin()
    try:
        # The report as typed (its recipients and checks), when the dialog sends it: else the one saved
        typed = (request.get_json(silent=True) or {}).get("report")
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "keep_alive.py"), "--test-report"], capture_output=True, text=True, timeout=110,
                                input=json.dumps(typed) if isinstance(typed, dict) else "")
        if result.returncode and "ValueError" not in result.stderr and not result.stdout.strip():
            return error((result.stderr.strip().splitlines() or ["The test run failed."])[-1])
        return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        app.logger.error("keep-alive test report: %s", exc)
        return error("The test run could not be made.", 502)


@app.route("/api/usage", methods=["GET", "PUT"])
def api_usage():
    """The usage alerts: who is mailed when a user's site or database grows past its limit (lib/usage.py checks once
    an hour, by cron, when they are on), and what was over at the last check"""
    require_admin()
    settings = _usage.load_settings(DATA)
    if request.method == "PUT":
        try:
            settings = save_usage_alerts(request.get_json(silent=True))
        except ValueError as exc:
            return error(str(exc))
        except (OSError, subprocess.SubprocessError):
            app.logger.exception("Could not save the usage alerts")
            return error("The usage alerts could not be saved. Inspect the admin app log.", 500)
        audit("Changed the usage alerts")
    state = _keep_alive_module.report.read_json(Path(DATA) / "usage-state.json", {})
    opened = state.get("open") if isinstance(state.get("open"), dict) else {}
    return jsonify(**settings, installed=os.path.isfile(str(_usage.CRON)), checked=state.get("checked"), over=list(opened.values()),
                   mail_error=state.get("mail_error"), mail=bool(_keep_alive_module.report.load_mail().get("host")),
                   **(pushed_notification("usage") if request.method == "PUT" else {}))


def save_usage_alerts(data):
    """The usage alerts saved, the server's own limits kept as they are: what is saved now"""
    settings = _usage.validate_settings(data)
    # The server's own limits stay as they are, with where each came from
    saved = _usage.report.read_json(Path(DATA) / "usage.json", {})
    _usage.write_json(os.path.join(DATA, "usage.json"), {**settings, "limits": _usage.load_limits(DATA),
                                                         "limits_from": saved.get("limits_from") or {}})
    sync_usage_job()
    # The servers its environments run on are told whether it mails their limits now: they mail them when it does not
    resend_environment_limits()
    return settings


@app.route("/api/usage/limit", methods=["PUT"])
def api_usage_limit():
    """The server's own limit on a site or a database, whoever's it is: its size in MB, or none (limit_mb null). Past
    it, it shows in Usage and is mailed with the usage alerts. Checked at once."""
    require_admin()
    data = request.get_json(silent=True) or {}
    # A limit set from the server that has the site as an environment: changed there too (over SSH, when Harbor here
    # knows that server), so the two never tell different limits
    # Also one whose limit was taken away there, but which is known as an environment of a site there, where Harbor here
    # can reach that server: a limit set here is sent there, and so shared
    origin = None
    if data.get("kind") == "site":
        origin = _usage.load_limit_origins(DATA).get(f"site|{data.get('name')}")
        if not origin:
            known = _usage.load_environment_of(DATA).get(f"site|{data.get('name')}")
            try:
                from harbor.core.system import System
                from harbor.server.tools import origin_server
                if known and origin_server(System(), {"address": "", **known})[1]:
                    origin = known["host"]
            except (ImportError, OSError):
                pass
    if origin:
        limit = data.get("limit_mb")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 1 <= limit <= 10 ** 8):
            return error("The limit is a size in MB")
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "usage-limit-back", str(data.get("name")),
                                 "none" if limit is None else str(int(limit)), *(["--raise"] if data.get("raise") is True else [])],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            return error(((result.stderr or result.stdout).strip().splitlines() or [f"{origin} did not take the limit."])[-1])
        sync_usage_job(check_now=True)
        audit(f"Set the limit of site {data.get('name')} here and on {origin}")
        return jsonify(limits=_usage.load_limits(DATA), remote={"sent": True, "error": "", "to": origin})
    # A part of the site's limit is never above it, nor the site's below one of its parts
    conflict = _usage.limit_conflict(DATA, data.get("kind"), data.get("name"), data.get("connection") or "", data.get("limit_mb")) \
        if isinstance(data.get("limit_mb"), (int, float)) and not isinstance(data.get("limit_mb"), bool) else ""
    if conflict:
        return error(conflict)
    try:
        _usage.set_limit(DATA, data.get("kind"), data.get("name"), data.get("connection") or "", data.get("limit_mb"))
        sync_usage_job(check_now=True)
        # An environment on another server: its limit sent there too, so that server's admin shows it (1.0.97)
        remote = push_environment_limit(data) if data.get("kind") == "environment" and data.get("connection") != "main" else None
    except ValueError as exc:
        return error(str(exc))
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("Could not save the limit")
        return error("The limit could not be saved. Inspect the admin app log.", 500)
    audit(f"Set the limit of {data.get('kind')} {data.get('name')}")
    return jsonify(limits=_usage.load_limits(DATA), remote=remote)


def resend_environment_limits():
    """Every environment limit sent again to the server it runs on (behind the request: one SSH login each)"""
    limits = [key.split("|") for key in _usage.load_limits(DATA) if key.startswith("environment|")]
    if not limits:
        return
    def resend():
        for _, site, target in limits:
            push_environment_limit({"name": site, "connection": target, "limit_mb": _usage.load_limits(DATA).get(f"environment|{site}|{target}")})
    threading.Thread(target=resend, daemon=True).start()


def push_environment_limit(data):
    """harbor env push-limit: {sent, error}, or None for an environment here. The limit is kept here either way."""
    limit = data.get("limit_mb")
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "env", "push-limit", str(data.get("name")), str(data.get("connection")),
                                 "none" if limit is None else str(int(limit))], capture_output=True, text=True, timeout=60)
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("Could not send the limit")
        return {"sent": False, "error": "It could not be sent to its server."}
    return found if found.get("sent") or found.get("error") else None


def sync_usage_job(check_now=False):
    """The hourly usage check, while the alerts are on or a user has a size limit (lib/usage.py --sync). check_now: a
    check at once as well, in the background, so a limit just changed shows (and is mailed) without waiting the hour"""
    script = os.path.join(LIB_DIR, "usage.py")
    subprocess.run(["/usr/bin/python3", script, "--sync", DATA], check=True, capture_output=True, text=True, timeout=30)
    if check_now and os.path.isfile(str(_usage.CRON)):
        subprocess.Popen(["/usr/bin/python3", script, "--data", DATA], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def usage_over(user):
    """What of the user's is over its limit at the last check: for the warning they are shown. An administrator is
    shown all of it (Needs attention on Web Server, the banner on the SQL page)"""
    state = _keep_alive_module.report.read_json(Path(DATA) / "usage-state.json", {})
    opened = state.get("open") if isinstance(state.get("open"), dict) else {}
    if access.is_admin(user):
        return [item for item in opened.values() if isinstance(item, dict)]
    def theirs(item):
        if item.get("user_id") == user.get("id"):
            return True
        # Past the server's own limit (an administrator's): told to each user who has the site or database too
        if str(item.get("key", "")).startswith("limit|"):
            if item.get("kind") == "site":
                return access.site_access(user, item.get("name")) is not None
            if item.get("kind") == "environment":
                return access.sees_env(user, item.get("site"), item.get("target"))
            return access.can_sql(user, item.get("connection"), item.get("name"), "view")
        return False
    return [item for item in opened.values() if isinstance(item, dict) and theirs(item)]


def user_usage(user):
    names = {item["id"]: item.get("name") for item in _sql_module.public_connections(SQL_CONNECTIONS)} if os.path.isfile(SQL_CONNECTIONS) else {}
    # A site's size is its solution: its folder and its environments elsewhere, as measured last for Usage
    kept = {}
    def measure_site(site):
        if "environments" not in kept:
            kept["environments"] = _usage.kept_environments(DATA)
            if kept["environments"] is None:
                kept["environments"] = _usage.all_environments_mb()
        return _usage.solution_mb(site, WEB_ROOT, kept["environments"])
    return _usage.of_user(DATA, user, names, measure_site=measure_site)


@app.route("/api/usage/all")
def api_usage_all():
    """Every site and database, how big each is, who made it and its limit: Usage under Settings. The sizes measured
    last come at once (measuring environments on other servers over SSH takes a while), with when and whether they are
    stale; ?fresh=1 measures now, and keeps it"""
    require_admin()
    connections = {item["id"]: item.get("name") or item["id"] for item in _sql_module.public_connections(SQL_CONNECTIONS)} if os.path.isfile(SQL_CONNECTIONS) else {}
    # The admin site is Harbor itself: never a site with a size to watch
    sites = [site for site in site_dirs() if site != ADMIN_SITE]
    result = _usage.kept_overview(DATA, sites, connections, fresh=request.args.get("fresh") == "1",
                                  measure_site=lambda site: _usage.site_mb(site, WEB_ROOT))
    # Measured now: what is over is checked at once too, so Needs attention and the mail follow what Usage shows
    if request.args.get("fresh") == "1":
        try:
            sync_usage_job(check_now=True)
        except (OSError, subprocess.SubprocessError):
            app.logger.exception("Could not start the usage check")
    # A limit from another server can be changed here when Harbor here knows that server (Settings, Servers)
    sources = _usage.load_limit_sources(DATA)
    # A site that is an environment of a site on another server, with no limit now: said so, as its limit is a share there
    known = _usage.load_environment_of(DATA)
    for item in result["sites"]:
        if not item.get("server_limit_from") and f"site|{item['name']}" in known:
            item["environment_of"] = known[f"site|{item['name']}"].get("host")
            sources.setdefault(f"site|{item['name']}", {"address": "", **known[f"site|{item['name']}"]})
    if any(item.get("server_limit_from") or item.get("environment_of") for item in result["sites"]):
        try:
            from harbor.core.system import System
            from harbor.server.tools import origin_server
            system = System()
            for item in result["sites"]:
                source = sources.get(f"site|{item['name']}")
                item["server_limit_back"] = bool(source and source.get("site") and origin_server(system, source)[1])
                item["server_limit_address"] = (source or {}).get("address", "")
                item["server_limit_port"] = (source or {}).get("port", 22)
        except (ImportError, OSError):
            pass
    return jsonify(result)


@app.route("/api/usage/room/<site>")
def api_usage_room(site):
    """The room for a site here that is an environment of a site on another server, asked there: its limit dialog
    knows the least and most it may be before it is typed"""
    require_admin()
    if site not in site_dirs():
        abort(404, "No such site.")
    # A part of a site here (?target=): its room from this server's own limits and sizes
    target = request.args.get("target", "")
    if target:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", target):
            abort(400, "Unknown environment")
        return jsonify(**_usage.limit_room(DATA, site, target), site=site, host="")
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "usage-room", site], capture_output=True, text=True, timeout=60)
        return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("usage room")
        return jsonify(error="The room could not be asked for.")


@app.route("/api/usage/site/<site>")
def api_usage_site(site):
    """The limits a site is held to and its size now: Add environment tells whether a new environment counts in them"""
    require_admin()
    if site not in site_dirs():
        abort(404, "No such site.")
    environments = _usage.kept_environments(DATA)
    return jsonify(_usage.site_limits(DATA, site, measure_site=lambda name: _usage.solution_mb(
        name, WEB_ROOT, environments if environments is not None else _usage.all_environments_mb())))


# ---- DNS, from Cloudflare (phase 14, lib/cloudflare.py)

# ---- Mail (phase 15): the mailcow server, its domains, mailboxes and aliases, and each domain's DNS made in Cloudflare.
# An administrator keeps every domain; a user the domains given them, and as many mailboxes there as they may have
# The DNS records Harbor made for each mail domain, by their ids in Cloudflare: taken out with the domain
MAIL_DNS = os.path.join(DATA, "mail-dns.json")
MAIL_LOCK = threading.Lock()


def mail_domain_list():
    """The mailcow server's domains for the user dialog, [{domain, relay}]; None without a server (no Mail part)"""
    settings = _mailcow.load()
    if not settings:
        return None
    try:
        return [{"domain": item["domain"], "relay": item["relay"]} for item in _mailcow.domains(settings)]
    except _mailcow.MailcowError:
        app.logger.exception("Could not read the mailcow domains")
        return []


def mail_domains_for_users():
    """The user dialog's Mail part: the domains by name, and how each takes mail (a Relay All one has no mailboxes)"""
    found = mail_domain_list()
    if found is None:
        return {"mail_domains": None, "mail_relays": {}}
    return {"mail_domains": [item["domain"] for item in found], "mail_relays": {item["domain"]: item["relay"] for item in found if item["relay"]}}


def mail_domain_names():
    """The mailcow server's domains, by name; None without a server"""
    found = mail_domain_list()
    return None if found is None else [item["domain"] for item in found]


def mail_server():
    """The mailcow server, for one who has Mail"""
    settings = _mailcow.load()
    if not settings:
        abort(404, "No mailcow server is set up.")
    if not access.has_mail(me()):
        abort(403, "Mail is not yours.")
    return settings


def mail_domain_of(name):
    """A domain the user may keep, by its name"""
    try:
        name = _mailcow.domain_name(name)
    except ValueError:
        abort(404, "No such domain.")
    if not access.mail_domain(me(), name):
        abort(404, "No such domain.")
    return name


def mail_errors(work):
    """mailcow's and Cloudflare's refusals, and a value that is wrong, as the page shows them"""
    try:
        return work()
    except ValueError as exc:
        return error(str(exc))
    except _mailcow.MailcowError as exc:
        return error(str(exc), 502)
    except _cloudflare.CloudflareError as exc:
        return error(f"Cloudflare: {exc}", 502)


@app.route("/api/mail/settings", methods=["GET", "PUT", "DELETE"])
def api_mail_settings():
    """The mailcow server Mail uses: its host (never its key), tried before it is kept"""
    require_admin()
    current = _mailcow.load()
    if request.method == "DELETE":
        _mailcow.remove()
        audit("Removed the mailcow server")
        return jsonify(_mailcow.shown({}))
    if request.method == "PUT":
        try:
            settings = _mailcow.validate(request.get_json(silent=True), current)
            version = _mailcow.verify(settings)
        except ValueError as exc:
            return error(str(exc))
        except _mailcow.MailcowError as exc:
            return error(f"mailcow did not take it: {exc}")
        _mailcow.save(settings)
        audit(f"Set the mailcow server {settings['host']}")
        return jsonify(**_mailcow.shown(settings), version=version)
    return jsonify(_mailcow.shown(current))


@app.route("/api/mail/status")
def api_mail_status():
    """The server's state: its version, containers and the mail storage's disk"""
    require_admin()
    settings = _mailcow.load()
    if not settings:
        return error("No mailcow server is set up.", 404)
    return mail_errors(lambda: jsonify(**_mailcow.status(settings), host=settings["host"]))


@app.route("/api/mail/rspamd")
def api_mail_rspamd():
    """Rspamd, the spam filter: its counts and the latest messages it scanned (an administrator's)"""
    require_admin()
    settings = _mailcow.load()
    if not settings:
        return error("No mailcow server is set up.", 404)
    return mail_errors(lambda: jsonify(**_mailcow.rspamd(settings), host=settings["host"]))


def mail_zone(name):
    """The Cloudflare zone a mail domain's records go in ({id, name}), or None (no account, or not in it)"""
    settings = _cloudflare.load()
    if not settings:
        return None, None
    try:
        zones = cloudflare_zones(settings)
    except _cloudflare.CloudflareError:
        return settings, None
    found = [zone for zone in zones if name == zone["name"] or name.endswith("." + zone["name"])]
    return settings, max(found, key=lambda zone: len(zone["name"])) if found else None


def mail_dns(mailcow, name):
    """A domain's DNS as mailcow wants it, checked against its zone: {zone, relay, records: [{..., state}]}. A relay
    domain's MX stays with its own mail server"""
    key = _mailcow.dkim(mailcow, name)
    relay = _mailcow.domain(mailcow, name)["relay"]
    wanted = _mailcow.wanted_records(mailcow["host"], name, key, relay)
    cloudflare, zone = mail_zone(name)
    if not zone:
        # Kept elsewhere: checked in public DNS as it answers now (fixed there, not here)
        public = _mailcow.public_records(wanted)
        if public is None:
            return {"zone": None, "relay": relay, "public": False, "records": [{**item, "state": "unknown", "current": []} for item in wanted]}
        return {"zone": None, "relay": relay, "public": True, "records": _mailcow.check_records(wanted, public, _mailcow.Resolver())}
    return {"zone": zone, "relay": relay, "records": _mailcow.check_records(wanted, _cloudflare.records(cloudflare, zone["id"]), _mailcow.Resolver()),
            "settings": cloudflare}


def mail_dns_made(name, entries=None, forget=False, drop=()):
    """The records Harbor made or changed for a domain, kept as it does so: [{id, before, replaced}]. before is what a
    record it changed was (None: one it made), so deleting the domain puts it back; replaced: it took the place of one
    of another type (an address where the CNAME goes), which is made again. Read (and forgotten) as the domain goes"""
    with MAIL_LOCK:
        try:
            with open(MAIL_DNS, encoding="utf-8") as stream:
                kept = json.load(stream)
        except (OSError, ValueError):
            kept = {}
        # Kept before as ids alone: records it made
        found = [entry if isinstance(entry, dict) else {"id": entry, "before": None} for entry in kept.get(name, [])]
        if entries:
            for entry in entries:
                # Changed twice: what it was the first time is what it goes back to
                if not any(old["id"] == entry["id"] for old in found):
                    found.append(entry)
            kept[name] = found
        if drop:
            found = [entry for entry in found if entry["id"] not in drop]
            kept[name] = found
        if forget:
            kept.pop(name, None)
        if entries or forget or drop:
            temporary = MAIL_DNS + ".tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(kept, stream)
            os.replace(temporary, MAIL_DNS)
        return found


def mail_record_body(record):
    """A record as Cloudflare has it, as it is sent to make it again or put it back"""
    body = {"type": record["type"], "name": record["name"], "ttl": record.get("ttl") or 1}
    if record.get("data") and record["type"] in ("SRV", "CAA"):
        body["data"] = record["data"]
    else:
        body["content"] = record["content"]
    if record["type"] == "MX" and record.get("priority") is not None:
        body["priority"] = record["priority"]
    if record["type"] in ("A", "AAAA", "CNAME"):
        body["proxied"] = bool(record.get("proxied"))
    return body


# What stays when a domain goes: its SPF (a site may still send its mail through the server)
MAIL_DNS_KEPT = ("spf",)
# What a relay domain does not need: its mail goes to its own server, which its MX and autodiscover are of
MAIL_DNS_MAILBOXES_ONLY = ("mx", "autodiscover", "autoconfig", "srv")


def mail_dns_kind(entry):
    """What a kept record is for: kept with it since 1.0.113; before that told by what it was (an SPF record)"""
    if entry.get("kind"):
        return entry["kind"]
    before = (entry.get("before") or {}).get("content", "")
    return "spf" if str(before).strip('"').lower().startswith("v=spf1") else ""


def mail_dns_undo(cloudflare, zone, entries):
    """What Harbor did to a domain's DNS undone: what it made taken out, what it changed put back: (out, back, ids)"""
    out = back = 0
    undone = []
    for entry in entries:
        try:
            if entry.get("before") is None:
                _cloudflare.delete(cloudflare, zone["id"], entry["id"])
                out += 1
            elif entry.get("replaced"):
                _cloudflare.delete(cloudflare, zone["id"], entry["id"])
                _cloudflare.create(cloudflare, zone["id"], entry["before"])
                back += 1
            else:
                _cloudflare.update(cloudflare, zone["id"], entry["id"], entry["before"])
                back += 1
            undone.append(entry["id"])
        except (_cloudflare.CloudflareError, ValueError):
            pass
    return out, back, undone


# How many mailboxes a user given a domain may have, unless chosen: as the user window starts them
MAIL_USER_DEFAULT = 5


def mail_grants_reopened(name):
    """A domain that relayed all its mail takes mailboxes again: each user given it with none (as a Relay All domain is
    given) may have the usual number. How many were changed"""
    changed = 0
    with CONFIG_LOCK:
        for user in cfg.get("users", []):
            grant = (user.get("mail_domains") or {}).get(name)
            if isinstance(grant, dict) and grant.get("mailboxes") == 0:
                grant["mailboxes"] = MAIL_USER_DEFAULT
                changed += 1
        if changed:
            save_config()
    if changed:
        audit(f"Mail: {name} takes mailboxes again: {changed} user(s) given it may have {MAIL_USER_DEFAULT}")
    return changed


def mail_dns_access(name):
    """Whether the user sees and keeps a mail domain's DNS: an administrator, or a user with Cloudflare's DNS part and the
    domain's zone given them (as on Cloudflare's page)"""
    user = me()
    if access.is_admin(user):
        return True
    if not access.cloudflare_part(user, "dns"):
        return False
    _settings, zone = mail_zone(name)
    return bool(zone) and access.dns_zone(user, zone["name"])


def mail_fix(mailcow, name, kinds=None, replace=False):
    """The domain's records made in Cloudflare: the missing ones (those of kinds, or all), and with replace the ones
    that differ, changed to what mailcow wants (an SPF record keeps its other senders): [what was done]. What each
    changed one was is kept, to be put back when the domain is deleted"""
    checked = mail_dns(mailcow, name)
    if not checked["zone"]:
        raise ValueError(f"{name} is not in the Cloudflare account: make its records where its DNS is kept.")
    settings, zone_id = checked["settings"], checked["zone"]["id"]
    done, kept = [], []
    full = None
    admin = is_admin()
    locks = _cloudflare.load_locks(CLOUDFLARE_LOCKS)
    for item in checked["records"]:
        if item["state"] == "ok" or (kinds and item["kind"] not in kinds) or (item["state"] == "differs" and not replace):
            continue
        current = item["current"][0] if item["current"] else None
        if item["state"] == "differs" and current:
            # What it is now, whole, to put it back later
            if full is None:
                full = {record["id"]: record for record in _cloudflare.records(settings, zone_id)}
            # One an administrator locked is theirs to change, as on Cloudflare's page
            if not admin and current["id"] in full and _cloudflare.is_locked(locks, zone_id, full[current["id"]]):
                done.append(f"Left {item['type']} {item['name']}: it is locked, an administrator changes it")
                continue
            before = mail_record_body(full[current["id"]]) if current["id"] in full else None
            # A CNAME takes the name alone: an address record there (A or AAAA) is changed into it, never put beside it
            if current["type"] != item["type"]:
                _cloudflare.delete(settings, zone_id, current["id"])
                result = _cloudflare.create(settings, zone_id, _mailcow.record_body(item))
                entry = {"kind": item["kind"], "before": before, "replaced": True}
            else:
                result = _cloudflare.update(settings, zone_id, current["id"], _mailcow.record_body(item, current))
                entry = {"kind": item["kind"], "before": before}
            done.append(f"Changed {item['type']} {item['name']}")
        else:
            result = _cloudflare.create(settings, zone_id, _mailcow.record_body(item))
            entry = {"kind": item["kind"], "before": None}
            done.append(f"Made {item['type']} {item['name']}")
        # Kept: one it made (taken out with the domain), and one it changed whose old form was read (put back); one it
        # changed but could not read is left as it is then
        made_new = item["state"] == "missing" or not current
        if isinstance(result, dict) and result.get("id") and (made_new or entry["before"] is not None):
            kept.append({"id": result["id"], **entry})
        # Made or changed by a user: open to them, as their own records on Cloudflare's page
        if isinstance(result, dict) and result.get("id"):
            _cloudflare.keep_open(CLOUDFLARE_LOCKS, zone_id, result["id"], admin)
    if kept:
        mail_dns_made(name, kept)
    if done:
        audit(f"Mail: DNS of {name}: " + "; ".join(done))
    return done


@app.route("/api/mail/domains", methods=["GET", "POST"])
def api_mail_domains():
    """The domains the user keeps; POST (an administrator): a new domain with its DKIM key, and its DNS made in
    Cloudflare where it is in the account"""
    mailcow = mail_server()
    if request.method == "GET":
        user = me()
        def listing():
            found = [item for item in _mailcow.domains(mailcow) if access.mail_domain(user, item["domain"])]
            for item in found:
                item["limit"] = access.mail_limit(user, item["domain"])
            return jsonify(domains=found, admin=is_admin(), host=mailcow["host"])
        return mail_errors(listing)
    require_admin()
    data = request.get_json(silent=True) or {}

    def make():
        name = _mailcow.add_domain(mailcow, data)
        audit(f"Mail: made the domain {name}")
        steps = [f"Made {name} in mailcow, with its DKIM key"]
        try:
            if data.get("dns", True) is not False:
                steps += mail_fix(mailcow, name)
        except ValueError as exc:
            steps.append(str(exc))
        except _cloudflare.CloudflareError as exc:
            steps.append(f"Its DNS was not made: Cloudflare said {exc}")
        return jsonify(domain=name, steps=steps)
    return mail_errors(make)


@app.route("/api/mail/domains/<name>", methods=["GET", "PUT", "DELETE"])
def api_mail_domain(name):
    """A domain with its mailboxes and aliases (and, for an administrator, its DNS checked); PUT its limits; DELETE it
    with its mailboxes, its DKIM key and the records Harbor made for it"""
    mailcow = mail_server()
    name = mail_domain_of(name)
    if request.method == "GET":
        def read():
            found = {"domain": _mailcow.domain(mailcow, name), "mailboxes": _mailcow.mailboxes(mailcow, name), "aliases": _mailcow.aliases(mailcow, name),
                     "limit": access.mail_limit(me(), name), "admin": is_admin(), "host": mailcow["host"], "dns": mail_dns_access(name),
                     "settings": access.mail_settings(me(), name)}
            return jsonify(found)
        return mail_errors(read)
    # Its settings: an administrator's, and a user's given them with the domain; deleting it is an administrator's alone
    if request.method == "PUT":
        if not access.mail_settings(me(), name):
            abort(403, "Its settings are not yours to change.")

        def change():
            data = request.get_json(silent=True) or {}
            was = _mailcow.domain(mailcow, name)["relay"]
            _mailcow.edit_domain(mailcow, name, data)
            audit(f"Mail: changed the settings of {name}")
            steps = []
            # No longer relaying all its mail: the users given it with no mailbox (as a Relay All domain is given) get the
            # usual number, so the mailboxes it may have now can be made
            if was == "all" and "relay" in data and data.get("relay") != "all":
                given = mail_grants_reopened(name)
                if given:
                    steps.append(f"{given} user{'s' if given != 1 else ''} given it may have {MAIL_USER_DEFAULT} mailboxes")
            # Now a relay domain: what only mailboxes here need goes from its DNS (made: out; changed: back as it was,
            # so its MX is its own mail server's again); its DNS only by one who keeps it, else left for an administrator
            if not was and data.get("relay") and mail_dns_access(name):
                made = mail_dns_made(name)
                cloudflare, zone = mail_zone(name)
                mine = [entry for entry in made if mail_dns_kind(entry) in MAIL_DNS_MAILBOXES_ONLY]
                if mine and zone:
                    out, back, undone = mail_dns_undo(cloudflare, zone, mine)
                    mail_dns_made(name, drop=undone)
                    if out:
                        steps.append(f"Took out {out} DNS record{'s' if out != 1 else ''} it no longer needs")
                    if back:
                        steps.append(f"Put {back} DNS record{'s' if back != 1 else ''} back as {'they' if back != 1 else 'it'} were")
                    audit(f"Mail: {name} is a relay domain: {out} DNS records taken out, {back} put back")
            return jsonify(domain=_mailcow.domain(mailcow, name), steps=steps)
        return mail_errors(change)
    require_admin()
    if (request.get_json(silent=True) or {}).get("confirmation") != name:
        return error("Type the domain's name to delete it.")

    def remove():
        count = _mailcow.delete_domain(mailcow, name)
        steps = [f"Deleted {name} in mailcow, with {count} mailbox{'es' if count != 1 else ''} and its mail"]
        made = mail_dns_made(name)
        cloudflare, zone = mail_zone(name)
        if made and zone:
            out, back, _undone = mail_dns_undo(cloudflare, zone, [entry for entry in made if mail_dns_kind(entry) not in MAIL_DNS_KEPT])
            if out:
                steps.append(f"Took out {out} DNS record{'s' if out != 1 else ''} Harbor made for it")
            if back:
                steps.append(f"Put {back} DNS record{'s' if back != 1 else ''} Harbor changed back as {'they' if back != 1 else 'it'} were")
        mail_dns_made(name, forget=True)
        audit(f"Mail: deleted the domain {name} ({count} mailboxes)")
        return jsonify(steps=steps)
    return mail_errors(remove)


@app.route("/api/mail/domains/<name>/dns", methods=["GET", "POST"])
def api_mail_domain_dns(name):
    """A domain's DNS against what mailcow wants; POST: the records made (kinds, or every missing one), and with replace
    those that differ changed. An administrator's, and a user's who keeps the domain's zone in Cloudflare"""
    mailcow = mail_server()
    name = mail_domain_of(name)
    if not mail_dns_access(name):
        abort(403, "Its DNS is not yours.")
    if request.method == "GET":
        def check():
            found = mail_dns(mailcow, name)
            found.pop("settings", None)
            return jsonify(found)
        return mail_errors(check)
    data = request.get_json(silent=True) or {}
    kinds = data.get("kinds") if isinstance(data.get("kinds"), list) else None
    return mail_errors(lambda: jsonify(steps=mail_fix(mailcow, name, kinds, replace=data.get("replace") is True)))


def mail_mailbox_of(address):
    """A mailbox the user may keep (its domain is theirs), by its address"""
    try:
        address = _mailcow.address(address)
    except ValueError:
        abort(404, "No such mailbox.")
    mail_domain_of(address.rpartition("@")[2])
    return address


@app.route("/api/mail/domains/<name>/mailboxes", methods=["POST"])
def api_mail_mailboxes(name):
    """A new mailbox: as many as the user may have in the domain (mailcow keeps the domain's own limit too)"""
    mailcow = mail_server()
    name = mail_domain_of(name)

    def make():
        # A Relay All domain passes every address on to its own mail server: no mailbox here
        if _mailcow.domain(mailcow, name)["relay"] == "all":
            return error(f"{name} relays all its mail to its own mail server: it has no mailboxes here.", 400)
        limit = access.mail_limit(me(), name)
        if limit is not None and len(_mailcow.mailboxes(mailcow, name)) >= limit:
            return error(f"You have as many mailboxes in {name} as you may ({limit}).", 403)
        made = _mailcow.add_mailbox(mailcow, name, request.get_json(silent=True) or {})
        audit(f"Mail: made the mailbox {made}")
        return jsonify(address=made)
    return mail_errors(make)


@app.route("/api/mail/mailboxes/<address>", methods=["PUT", "DELETE"])
def api_mail_mailbox(address):
    """A mailbox changed (name, quota, password, on or off), or deleted with its mail"""
    mailcow = mail_server()
    address = mail_mailbox_of(address)
    if request.method == "DELETE":
        if (request.get_json(silent=True) or {}).get("confirmation") != address:
            return error("Type the address to delete the mailbox.")

        def remove():
            _mailcow.delete_mailbox(mailcow, address)
            audit(f"Mail: deleted the mailbox {address}")
            return jsonify(ok=True)
        return mail_errors(remove)

    def change():
        _mailcow.edit_mailbox(mailcow, address, request.get_json(silent=True) or {})
        audit(f"Mail: changed the mailbox {address}")
        return jsonify(ok=True)
    return mail_errors(change)


# What lets a mail program find the server by the address alone: Outlook's autodiscover (with its service record) and
# Thunderbird's autoconfig
MAIL_AUTOMATIC = ("autodiscover", "autoconfig", "srv")


def mail_automatic(mailcow, name):
    """Whether a domain's mailboxes are set up by their address alone: its autodiscover and autoconfig records point
    to the server (in Cloudflare, or in public DNS where it is kept elsewhere). {ready, missing: [kinds]}"""
    try:
        records = mail_dns(mailcow, name)["records"]
    except (_mailcow.MailcowError, _cloudflare.CloudflareError, ValueError):
        return {"ready": False, "missing": list(MAIL_AUTOMATIC), "unknown": True}
    wanted = [item for item in records if item["kind"] in MAIL_AUTOMATIC]
    missing = [item["kind"] for item in wanted if item["state"] != "ok"]
    return {"ready": bool(wanted) and not missing, "missing": missing}


@app.route("/api/mail/domains/<name>/automatic")
def api_mail_automatic(name):
    """Whether its mailboxes are set up by their address alone (for the setup shown to whoever keeps them)"""
    mailcow = mail_server()
    name = mail_domain_of(name)
    return jsonify(mail_automatic(mailcow, name))


@app.route("/api/mail/mailboxes/<address>/setup", methods=["POST"])
def api_mail_mailbox_setup(address):
    """A mailbox's setup mailed to another address of its user (the new one is not set up yet), through Harbor's mail
    server; its password in it only when given here, and never in the audit log"""
    mailcow = mail_server()
    address = mail_mailbox_of(address)
    data = request.get_json(silent=True) or {}
    to = str(data.get("to") or "").strip()
    if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", to) or len(to) > 254:
        return error("Give the address to send it to.")
    if to.lower() == address.lower():
        return error("Send it to another address of theirs: this one is not set up yet.")
    secret = str(data.get("password") or "")
    if len(secret) > 256 or any(char in secret for char in "\0\r\n"):
        return error("That password cannot be sent.")
    report = _keep_alive_module.report
    settings = report.load_mail()
    if not settings.get("host"):
        return error("Harbor has no mail server to send it with (Settings, Notifications).", 400)
    host = socket.getfqdn()
    try:
        # The automatic setup only where the domain's DNS lets a program find the server
        automatic = mail_automatic(mailcow, address.rpartition("@")[2])["ready"]
        report.send(settings, [to], f"Your mailbox {address}", report.mailbox_setup_text(address, mailcow["host"], secret, automatic),
                    html=report.mailbox_setup_html(host, address, mailcow["host"], secret, automatic=automatic))
    except Exception as exc:  # noqa: BLE001 - the mail server's own refusal, said as it is
        return error(f"It was not sent: {exc}", 502)
    audit(f"Mail: sent the setup of {address} to {to}" + (" with its password" if secret else ""))
    return jsonify(sent=to)


@app.route("/api/mail/domains/<name>/aliases", methods=["POST"])
@app.route("/api/mail/domains/<name>/aliases/<int:alias>", methods=["DELETE"])
def api_mail_aliases(name, alias=None):
    """A new alias (or forward) in the domain, or one deleted"""
    mailcow = mail_server()
    name = mail_domain_of(name)
    if request.method == "DELETE":
        def remove():
            found = next((item for item in _mailcow.aliases(mailcow, name) if str(item["id"]) == str(alias)), None)
            if not found:
                return error("No such alias.", 404)
            _mailcow.delete_alias(mailcow, alias)
            audit(f"Mail: deleted the alias {found['address']}")
            return jsonify(ok=True)
        return mail_errors(remove)

    def make():
        made = _mailcow.add_alias(mailcow, name, request.get_json(silent=True) or {})
        audit(f"Mail: made the alias {made}")
        return jsonify(address=made)
    return mail_errors(make)


@app.route("/api/dns/settings", methods=["GET", "PUT", "DELETE"])
def api_dns_settings():
    """The Cloudflare account DNS uses: its kind and email (never its key or token), tried before it is kept"""
    require_admin()
    current = _cloudflare.load()
    if request.method == "DELETE":
        _cloudflare.remove()
        _cloudflare.forget_zones(CLOUDFLARE_ZONES)
        audit("Removed the Cloudflare account")
        return jsonify(_cloudflare.shown({}))
    if request.method == "PUT":
        try:
            settings = _cloudflare.validate(request.get_json(silent=True), current)
            count = _cloudflare.verify(settings)
        except ValueError as exc:
            return error(str(exc))
        except _cloudflare.CloudflareError as exc:
            return error(f"Cloudflare did not take it: {exc}")
        _cloudflare.save(settings)
        # The domains read again for the account now set
        try:
            _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=True)
        except _cloudflare.CloudflareError:
            pass
        audit("Set the Cloudflare account")
        return jsonify(**_cloudflare.shown(settings), zones=count)
    # With how many domains are kept and when they were read, for Refresh domains
    shown = _cloudflare.shown(current)
    if current:
        try:
            kept = json.loads(open(CLOUDFLARE_ZONES, encoding="utf-8").read())
            shown.update(zones=len(kept.get("zones") or []), zones_read=kept.get("read"))
        except (OSError, ValueError):
            pass
    return jsonify(shown)


CLOUDFLARE_LOCKS = os.path.join(DATA, "cloudflare-locks.json")
# The account's domains, kept until refreshed under Settings, DNS: every call needs them, and they seldom change
CLOUDFLARE_ZONES = os.path.join(DATA, "cloudflare-zones.json")


def cloudflare_zones(settings, fresh=False):
    return _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=fresh)["zones"]


def dns_zone_names():
    """The domains of the Cloudflare account, by name, for the user dialog; None without an account (no DNS part)"""
    settings = _cloudflare.load()
    if not settings:
        return None
    try:
        return [item["name"] for item in cloudflare_zones(settings)]
    except _cloudflare.CloudflareError:
        app.logger.exception("Could not read the Cloudflare zones")
        return []


def dns_account():
    settings = _cloudflare.load()
    if not settings:
        abort(404, "No Cloudflare account is set up.")
    if not access.has_dns(me()):
        abort(403, "DNS is not yours.")
    return settings


def dns_zone_of(settings, zone):
    """The zone, by its id, where the user may keep it: {id, name}"""
    found = next((item for item in cloudflare_zones(settings) if item["id"] == zone), None)
    # One added in Cloudflare since they were read: read again once
    if not found and _cloudflare.ZONE_ID.fullmatch(zone or ""):
        found = next((item for item in cloudflare_zones(settings, fresh=True) if item["id"] == zone), None)
    if not found or not access.dns_zone(me(), found["name"]):
        abort(404, "No such domain.")
    return found


@app.route("/api/dns/zones")
def api_dns_zones():
    """The domains of the account the user may keep: every one for an administrator"""
    settings = dns_account()
    try:
        found = cloudflare_zones(settings)
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)
    user = me()
    return jsonify(zones=[item for item in found if access.dns_zone(user, item["name"])])


# The Cloudflare accounts a domain may be added to (Settings, DNS, Accounts…): {"allowed": [ids]}; none kept, every one
CLOUDFLARE_ACCOUNTS = os.path.join(DATA, "cloudflare-accounts.json")


def cloudflare_allowed():
    """The account ids domains may be added to, or None for every one"""
    try:
        with open(CLOUDFLARE_ACCOUNTS, encoding="utf-8") as stream:
            found = json.load(stream).get("allowed")
    except (OSError, ValueError, AttributeError):
        return None
    return found if isinstance(found, list) and found else None


@app.route("/api/dns/accounts", methods=["GET", "PUT"])
def api_dns_accounts():
    """The Cloudflare accounts the login reaches, each with whether domains may be added to it; PUT: which may"""
    require_admin()
    settings = _cloudflare.load()
    if not settings:
        return error("No Cloudflare account is set up.", 404)
    try:
        found = _cloudflare.accounts(settings)
    except _cloudflare.CloudflareError as exc:
        return error(f"{exc} (the API token needs Account, Account Settings, Read)", 502)
    if request.method == "PUT":
        chosen = (request.get_json(silent=True) or {}).get("allowed")
        known = {item["id"] for item in found}
        if not isinstance(chosen, list) or not chosen or any(item not in known for item in chosen):
            return error("Choose at least one account.")
        temporary = CLOUDFLARE_ACCOUNTS + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"allowed": sorted(set(chosen))}, stream)
        os.replace(temporary, CLOUDFLARE_ACCOUNTS)
        audit("Cloudflare: domains may be added to " + ", ".join(item["name"] for item in found if item["id"] in chosen))
    allowed = cloudflare_allowed()
    return jsonify(accounts=[{**item, "allowed": allowed is None or item["id"] in allowed} for item in found])


@app.route("/api/dns/zones", methods=["POST"])
def api_dns_zone_add():
    """A domain added to the Cloudflare account (an administrator's): its records read from its DNS now, and the name
    servers to set at its registrar"""
    require_admin()
    settings = _cloudflare.load()
    if not settings:
        return error("No Cloudflare account is set up.", 404)
    data = request.get_json(silent=True) or {}
    try:
        account = data.get("account") or ""
        allowed = cloudflare_allowed()
        if not account:
            found = [item for item in _cloudflare.accounts(settings) if allowed is None or item["id"] in allowed]
            if len(found) != 1:
                return error("Choose the Cloudflare account to add it to.")
            account = found[0]["id"]
        if allowed is not None and account not in allowed:
            return error("Domains are not added to that account (Settings, DNS, Accounts…).")
        zone = _cloudflare.add_zone(settings, data.get("name"), account, data.get("jump_start", True) is not False)
        _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=True)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(f"Cloudflare did not add it: {exc}", 502)
    audit(f"Cloudflare: added the domain {zone['name']}")
    return jsonify(zone=zone)


@app.route("/api/dns/zones/<zone>", methods=["DELETE"])
def api_dns_zone_remove(zone):
    """A domain taken out of the Cloudflare account with all its records (its name typed to confirm)"""
    require_admin()
    settings = dns_account()
    found = dns_zone_of(settings, zone)
    if (request.get_json(silent=True) or {}).get("confirmation") != found["name"]:
        return error("Type the domain's name to remove it.")
    try:
        _cloudflare.remove_zone(settings, zone)
        _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=True)
    except (ValueError, _cloudflare.CloudflareError) as exc:
        return error(f"Cloudflare did not remove it: {exc}", 502)
    audit(f"Cloudflare: removed the domain {found['name']}")
    return jsonify(ok=True)


@app.route("/api/dns/zones/<zone>/activation", methods=["POST"])
def api_dns_zone_activation(zone):
    """A pending domain's name servers looked at again by Cloudflare now"""
    require_admin()
    settings = dns_account()
    dns_zone_of(settings, zone)
    try:
        _cloudflare.activation_check(settings, zone)
        found = next((item for item in _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=True)["zones"] if item["id"] == zone), None)
    except (ValueError, _cloudflare.CloudflareError) as exc:
        return error(f"Cloudflare: {exc}", 502)
    return jsonify(zone=found)


@app.route("/api/dns/zones/refresh", methods=["POST"])
def api_dns_zones_refresh():
    """The account's domains read again from Cloudflare (Settings, DNS): one added or removed there is seen"""
    require_admin()
    settings = _cloudflare.load()
    if not settings:
        return error("No Cloudflare account is set up.", 404)
    try:
        kept = _cloudflare.kept_zones(settings, CLOUDFLARE_ZONES, fresh=True)
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)
    audit(f"Refreshed the Cloudflare domains: {len(kept['zones'])}")
    return jsonify(zones=len(kept["zones"]), read=kept["read"])


@app.route("/api/dns/zones/<zone>/records", methods=["GET", "POST"])
def api_dns_records(zone):
    """A domain's records; POST: a new one, proxied by anyone who may make it"""
    settings = dns_account()
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "POST":
            cloudflare_part_of("dns")
            # Proxied for anyone on a record open to them: a new one is theirs, open also when it is proxied
            body = _cloudflare.record_body(request.get_json(silent=True), found["name"], True)
            made = _cloudflare.create(settings, zone, body)
            if made.get("id"):
                _cloudflare.keep_open(CLOUDFLARE_LOCKS, zone, made["id"], is_admin())
            audit(f"DNS: made {body['type']} {body['name']} in {found['name']}")
            return jsonify(id=made.get("id"))
        # The records only for one who has DNS; the parts they have, for the page's tabs
        # Each record with whether it is locked for users
        locks = _cloudflare.load_locks(CLOUDFLARE_LOCKS)
        records = [dict(item, locked=_cloudflare.is_locked(locks, zone, item)) for item in _cloudflare.records(settings, zone)] \
            if access.cloudflare_part(me(), "dns") else []
        return jsonify(zone=found, records=records, admin=is_admin(),
                       parts=[part for part in _cloudflare.PARTS if access.cloudflare_part(me(), part)])
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/records/<record>", methods=["PUT", "DELETE"])
def api_dns_record(zone, record):
    """A record changed or deleted. A locked record is an administrator's: a user cannot change or delete it"""
    settings = cloudflare_part_of("dns")
    try:
        found = dns_zone_of(settings, zone)
        current = _cloudflare.record(settings, zone, record)
        if not is_admin() and _cloudflare.is_locked(_cloudflare.load_locks(CLOUDFLARE_LOCKS), zone, current):
            return error("This record is locked: only an administrator changes it.", 403)
        if request.method == "DELETE":
            _cloudflare.delete(settings, zone, record)
            _cloudflare.set_lock(CLOUDFLARE_LOCKS, zone, record, None)
            audit(f"DNS: deleted {current['type']} {current['name']} in {found['name']}")
            return jsonify(ok=True)
        body = _cloudflare.record_body(request.get_json(silent=True), found["name"], True, current)
        _cloudflare.update(settings, zone, record, body)
        # A record open to a user stays open when they proxy it, as a new one of theirs: never locked by their own click
        _cloudflare.keep_open(CLOUDFLARE_LOCKS, zone, record, is_admin())
        audit(f"DNS: changed {body['type']} {body['name']} in {found['name']}")
        return jsonify(ok=True)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


def cloudflare_part_of(part):
    """The account, where the user may use this part of Cloudflare"""
    settings = dns_account()
    if not access.cloudflare_part(me(), part):
        abort(403, f"{_cloudflare.PART_NAMES.get(part, part)} is not yours.")
    return settings


@app.route("/api/dns/zones/<zone>/records/<record>/lock", methods=["PUT"])
def api_dns_record_lock(zone, record):
    """A record locked or opened for users: an administrator's choice, kept by Harbor"""
    require_admin()
    settings = dns_account()
    locked = (request.get_json(silent=True) or {}).get("locked")
    if not isinstance(locked, bool):
        return error("Locked is true or false.")
    try:
        found = dns_zone_of(settings, zone)
        current = _cloudflare.record(settings, zone, record)
        _cloudflare.set_lock(CLOUDFLARE_LOCKS, zone, record, locked)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)
    audit(f"DNS: {'locked' if locked else 'opened'} {current['type']} {current['name']} in {found['name']} for users")
    return jsonify(locked=locked)


@app.route("/api/dns/zones/<zone>/purge", methods=["POST"])
def api_dns_purge(zone):
    """Cloudflare's cache of a domain emptied: all of it, or the addresses given (files)"""
    settings = cloudflare_part_of("cache")
    files = (request.get_json(silent=True) or {}).get("files")
    try:
        found = dns_zone_of(settings, zone)
        if files:
            _cloudflare.purge_urls(settings, zone, files if isinstance(files, list) else str(files).split())
        else:
            _cloudflare.purge(settings, zone)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)
    audit(f"Cloudflare: purged the cache of {found['name']}" + (f" ({len(files)} addresses)" if files else ""))
    return jsonify(ok=True)


@app.route("/api/dns/zones/<zone>/settings/<part>", methods=["GET", "PATCH"])
def api_cloudflare_settings(zone, part):
    """A part's zone settings (Cache, SSL/TLS, Security): read, or one changed"""
    if part not in _cloudflare.SETTINGS_OF:
        abort(404)
    settings = cloudflare_part_of(part)
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "PATCH":
            data = request.get_json(silent=True) or {}
            _cloudflare.change_setting(settings, zone, part, str(data.get("setting", "")), data.get("value"))
            audit(f"Cloudflare: {data.get('setting')} of {found['name']} set to {data.get('value')}")
        return jsonify(settings=_cloudflare.zone_settings(settings, zone, part))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/analytics")
def api_cloudflare_analytics(zone):
    """A domain's traffic a day, and what it was made of: the last 7 days, or ?days=30 (or 1)"""
    settings = cloudflare_part_of("analytics")
    try:
        dns_zone_of(settings, zone)
        days = int(request.args.get("days") or 7)
        found = _cloudflare.analytics(settings, zone, days)
        # What it was made of: left out, with why, where the plan or token does not give it
        try:
            parts, why = _cloudflare.breakdown(settings, zone, days), ""
        except _cloudflare.CloudflareError as exc:
            parts, why = None, str(exc)
        return jsonify(days=found, breakdown=parts, breakdown_error=why)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/rule-limits")
def api_cloudflare_rule_limits(zone):
    """How many redirect rules and page rules the domain may have"""
    settings = cloudflare_part_of("rules")
    try:
        dns_zone_of(settings, zone)
        return jsonify(_cloudflare.rule_limits(settings, zone))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/managed-headers", methods=["GET", "PATCH"])
def api_cloudflare_managed_headers(zone):
    """The domain's Managed Transforms: read, or one turned on or off ({header, enabled})"""
    settings = cloudflare_part_of("rules")
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "PATCH":
            data = request.get_json(silent=True) or {}
            headers = _cloudflare.set_managed_header(settings, zone, str(data.get("header") or ""), data.get("enabled") is True)
            audit(f"Cloudflare: {data.get('header')} turned {'on' if data.get('enabled') is True else 'off'} for {found['name']}")
            return jsonify(headers)
        return jsonify(_cloudflare.managed_headers(settings, zone))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/pagerules", methods=["GET", "POST"])
@app.route("/api/dns/zones/<zone>/pagerules/<rule>", methods=["PUT", "PATCH", "DELETE"])
def api_cloudflare_page_rules(zone, rule=None):
    """A domain's page rules: read, a forwarding one made (POST) or changed (PUT), any one on or off (PATCH) or deleted"""
    settings = cloudflare_part_of("rules")
    try:
        found = dns_zone_of(settings, zone)
        data = request.get_json(silent=True) or {}
        if request.method in ("POST", "PUT"):
            _cloudflare.save_page_rule(settings, zone, rule, data)
            audit(f"Cloudflare: {'made' if rule is None else 'changed'} a page rule of {found['name']}: {data.get('pattern')}")
        elif request.method == "PATCH":
            _cloudflare.page_rule_state(settings, zone, rule, data.get("enabled") is True)
            audit(f"Cloudflare: page rule of {found['name']} turned {'on' if data.get('enabled') else 'off'}")
        elif request.method == "DELETE":
            _cloudflare.delete_page_rule(settings, zone, rule)
            audit(f"Cloudflare: deleted a page rule of {found['name']}")
        return jsonify(rules=_cloudflare.page_rules(settings, zone))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/pagerules/<rule>/convert", methods=["GET", "POST"])
def api_cloudflare_page_rule_convert(zone, rule):
    """A page rule made into the newer rules: what it would become (GET), or made so and the page rule turned off (POST)"""
    settings = cloudflare_part_of("rules")
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "GET":
            page_rule = next((item for item in _cloudflare.page_rules(settings, zone) if item["id"] == rule), None)
            if page_rule is None:
                return error("Unknown page rule")
            return jsonify(plan=_cloudflare.page_rule_plan(page_rule))
        plan = _cloudflare.convert_page_rule(settings, zone, rule)
        made = [name for name, key in (("a redirect", "redirect"), ("a cache rule", "cache"), ("a configuration rule", "config")) if plan[key]]
        audit(f"Cloudflare: converted a page rule of {found['name']} to {' and '.join(made) or 'nothing'}, and turned it off")
        return jsonify(plan=plan)
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/rules/<kind>", methods=["GET", "PUT"])
def api_cloudflare_settings_rules(zone, kind):
    """A domain's cache rules or configuration rules: read, or put as given"""
    if kind not in _cloudflare.SETTINGS_PHASES:
        abort(404)
    settings = cloudflare_part_of("rules")
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "PUT":
            rules = (request.get_json(silent=True) or {}).get("rules")
            if not isinstance(rules, list) or len(rules) > 300:
                return error("Expected the rules.")
            _cloudflare.save_settings_rules(settings, zone, kind, rules)
            audit(f"Cloudflare: saved {len(rules)} {'cache' if kind == 'cache' else 'configuration'} rule{'' if len(rules) == 1 else 's'} of {found['name']}")
        return jsonify(rules=_cloudflare.settings_rules(settings, zone, kind))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/dns/zones/<zone>/redirects", methods=["GET", "PUT"])
def api_cloudflare_redirects(zone):
    """A domain's redirect rules: read, or put as given"""
    settings = cloudflare_part_of("rules")
    try:
        found = dns_zone_of(settings, zone)
        if request.method == "PUT":
            rules = (request.get_json(silent=True) or {}).get("rules")
            if not isinstance(rules, list) or len(rules) > 125:
                return error("Expected the redirect rules.")
            _cloudflare.save_redirects(settings, zone, rules)
            audit(f"Cloudflare: saved {len(rules)} redirect rule{'' if len(rules) == 1 else 's'} of {found['name']}")
        return jsonify(rules=_cloudflare.redirect_rules(settings, zone))
    except ValueError as exc:
        return error(str(exc))
    except _cloudflare.CloudflareError as exc:
        return error(str(exc), 502)


@app.route("/api/usage/over")
def api_usage_over():
    """What is past its limit at the last check, as /api/me has it: the page follows a check that ran since it loaded
    (behind a change here, or a limit sent from another server)"""
    return jsonify(over=usage_over(me()))


@app.route("/api/usage/mine")
def api_usage_mine():
    """What the user logged in has made, and how big each is, beside their limits"""
    user = me()
    if not _usage.shows_usage(user):
        abort(404, "There is no usage of yours to show.")
    return jsonify(user_usage(user))


@app.route("/api/users/<user_id>/usage")
def api_user_usage(user_id):
    """A user's usage, as an administrator sees it from Users"""
    require_admin()
    user = access.find(cfg, user_id)
    if user is None:
        abort(404, "Unknown user")
    return jsonify(user_usage(user))


@app.route("/api/usage/check", methods=["POST"])
def api_usage_check():
    """What is over its limit now, measured at once: shown, not mailed"""
    require_admin()
    try:
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "usage.py"), "--data", DATA, "--json"],
                                capture_output=True, text=True, timeout=900)
        return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("usage check")
        return error("The check could not be made. Inspect the admin app log.", 502)


@app.route("/api/usage/test", methods=["POST"])
def api_usage_test():
    """A test of the usage alerts: everything measured now, and their mail sent to the addresses typed, not saved"""
    require_admin()
    emails = (request.get_json(silent=True) or {}).get("emails")
    if not isinstance(emails, list) or not emails:
        return error("Enter the address to send the test to.")
    try:
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "usage.py"), "--data", DATA, "--test-report"],
                                input=json.dumps(emails), capture_output=True, text=True, timeout=900)
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("usage test")
        return error("The test could not be made. Inspect the admin app log.", 502)
    if "subject" not in found:
        return error(found.get("error") or "The test could not be made.")
    audit("Sent a test of the usage alerts")
    return jsonify(found)


@app.route("/api/sign-in", methods=["GET", "PUT"])
def api_sign_in():
    """The sign-in page's settings: the addresses Harbor took over, whose visitors are told to set a new password, and
    whether Forgot password can be offered (it needs the mail server)"""
    require_admin()
    if request.method == "PUT":
        given = (request.get_json(silent=True) or {}).get("moved_from") or []
        if not isinstance(given, list) or len(given) > 10:
            return error("Up to 10 addresses.")
        hosts = []
        for value in given:
            try:
                host = _normalize_dns_name(str(value).strip(), fully_qualified=True).lower()
            except ValueError as exc:
                return error(f"{value}: {exc}")
            if host not in hosts:
                hosts.append(host)
        with CONFIG_LOCK:
            cfg["moved_from"] = hosts
            save_config()
        audit("Set the addresses Harbor took over: " + (", ".join(hosts) or "none"))
    # Those given here, and the admin site's aliases, which count by themselves
    aliases = admin_aliases()
    return jsonify(moved_from=[host for host in moved_from() if host not in aliases or host in cfg.get("moved_from", [])], aliases=aliases,
                   forgot=can_mail(), admin_site=ADMIN_SITE)


@app.route("/api/mail", methods=["GET", "PUT"])
def api_mail():
    """The mail server all of Harbor's mail goes through (root only on disk): its password is never sent back"""
    require_admin()
    site_report = _keep_alive_module.report
    current = site_report.load_mail()
    if request.method == "PUT":
        try:
            current = save_mail_server(request.get_json(silent=True))
        except ValueError as exc:
            return error(str(exc))
        except OSError:
            app.logger.exception("Could not save the mail server")
            return error("The mail server could not be saved.", 500)
        audit("Changed the mail server")
    return jsonify(**{key: current.get(key, default) for key, default in (("host", ""), ("port", 587), ("security", "starttls"), ("user", ""), ("sender", ""))},
                   password_set=bool(current.get("password")), **pushed_notification("mail"))


def save_mail_server(data):
    """The mail server saved (an empty password keeps the one saved): what is saved now"""
    site_report = _keep_alive_module.report
    current = site_report.validate_mail(data, site_report.load_mail())
    site_report.save_mail(current)
    return current


@app.route("/api/mail/test", methods=["POST"])
def api_mail_test():
    """A test mail through the mail server, to an address given"""
    require_admin()
    data = request.get_json(silent=True) or {}
    to = str(data.get("to", "")).strip()
    site_report = _keep_alive_module.report
    if not site_report.EMAIL.fullmatch(to):
        return error("Enter the address to send the test to.")
    # With settings, those (as typed, not saved yet; an empty password is the one saved); without, the saved ones
    try:
        settings = site_report.validate_mail(data["settings"], site_report.load_mail()) if isinstance(data.get("settings"), dict) else site_report.load_mail()
    except ValueError as exc:
        return error(str(exc))
    try:
        site_report.send(settings, [to], f"[{socket.gethostname()}] \u2705 Test from Harbor",
                         "This is a test from Harbor. Harbor's mail, such as keep-alive's reports, comes through this mail server.\n",
                         html=site_report.test_mail_html(socket.gethostname()))
    except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own; the user is told which)
        return error(f"The test mail could not be sent: {exc}", 502)
    return jsonify(ok=True)


# ---------------------------------------------------------------- security

FAILS = {}
GLOBAL_FAILS = []
FAIL_LOCK = threading.Lock()
WINDOW = 15 * 60
MAX_FAILS_PER_IP = 5
MAX_FAILS_GLOBAL = 60


def client_ip():
    # Asked by the master: where the one on its page comes from, as it says (only the master's call says it)
    if getattr(g, "from_master", False) and request.headers.get("X-Harbor-Client"):
        from harbor.server.master import client_address
        return client_address(request.headers["X-Harbor-Client"]) or request.remote_addr or ""
    ip = request.remote_addr or ""
    if ip in ("127.0.0.1", "::1"):
        # Apache appends the real client address as the last X-Forwarded-For entry
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            ip = forwarded.split(",")[-1].strip()
    return ip


def login_locked(ip):
    now = time.time()
    with FAIL_LOCK:
        recent = [t for t in FAILS.get(ip, []) if now - t < WINDOW]
        FAILS[ip] = recent
        GLOBAL_FAILS[:] = [t for t in GLOBAL_FAILS if now - t < WINDOW]
        return len(recent) >= MAX_FAILS_PER_IP or len(GLOBAL_FAILS) >= MAX_FAILS_GLOBAL


def record_failure(ip):
    now = time.time()
    with FAIL_LOCK:
        FAILS.setdefault(ip, []).append(now)
        GLOBAL_FAILS.append(now)


HARBOR_API = "/api/harbor/v1/"
HARBOR_SECRETS = "/etc/harbor/secrets/sites"


def harbor_token_site(header):
    """(site, environment) of the site whose VNS.Harbor token this is (harbor/umbraco/backoffice.py), or None"""
    if not header.startswith("Bearer ") or len(header) > 200:
        return None
    token = header[len("Bearer "):].strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        return None
    for path in glob.glob(os.path.join(HARBOR_SECRETS, "*", "connection", "*.json")):
        try:
            with open(path, encoding="utf-8") as stream:
                known = json.load(stream).get("token", "")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(known, str) and known and hmac.compare_digest(known, token):
            return os.path.basename(os.path.dirname(os.path.dirname(path))), os.path.splitext(os.path.basename(path))[0]
    return None


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


MASTER_TOKEN = "/etc/harbor/secrets/master-token"
# On a member: the master it belongs to ({name, address, joined}), written by the master when it joins it
MEMBERSHIP = "/var/lib/harbor/master.json"


def member_of():
    """The master this server is a member of: {name, address}, or None"""
    try:
        with open(MEMBERSHIP, encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and found.get("address") else None


def users_from_master():
    """On a member, users are made on the master: only its own calls change them here"""
    master = member_of()
    if master and not g.get("from_master"):
        abort(403, f"Users are made on {master.get('name') or 'the master'} ({master['address']}), as this server is its member.")


@app.route("/api/membership/users", methods=["PUT"])
def api_membership_users():
    """(On a member) the master's users that have something here, as the master sends them: each with what it may here,
    checked against this server's own sites and connections. Those it sent before and no longer sends go. Its users
    log in on the master; an administrator also here (with the master's password), should the master be down"""
    if not g.get("from_master") or not access.is_admin(me()):
        abort(403, "Only the master sends its users.")
    given = (request.get_json(silent=True) or {}).get("users")
    if not isinstance(given, list) or len(given) > 2000:
        return error("Expected the users.")
    skipped = []
    with CONFIG_LOCK:
        users = cfg.setdefault("users", [])
        own = {user["username"].lower() for user in users if not user.get("from_master")}
        kept = {user["id"]: user for user in users if user.get("from_master")}
        sent = []
        for data in given:
            if not isinstance(data, dict) or not re.fullmatch(r"u_[0-9a-f]{6,32}", str(data.get("id", ""))):
                continue
            if str(data.get("username", "")).lower() in own:
                # One of this server's own users has the name: kept, and said
                skipped.append(str(data.get("username")))
                continue
            try:
                user = access.clean(data, kept.get(data["id"]), sites=site_environments(), connections=sql_connection_ids())
            except ValueError as exc:
                skipped.append(f"{data.get('username')}: {exc}")
                continue
            user["id"] = data["id"]
            user["from_master"] = True
            user["version"] = int(data.get("version") or 1)
            user["password_hash"] = str(data.get("password_hash") or "") if user["role"] == "admin" else ""
            sent.append(user)
        cfg["users"] = [user for user in users if not user.get("from_master")] + sent
        save_config()
    linux_users_changed()
    audit(f"The master sent {len(sent)} user{'' if len(sent) == 1 else 's'}" + (f"; kept out: {', '.join(skipped)}" if skipped else ""))
    return jsonify(users=len(sent), skipped=skipped)


def left_master():
    """(On a member that leaves its master) the master's users go, their Linux users with them: they log in on the
    master, and have nothing here now. Its administrators stay only where this server has no administrator of its own,
    as its own then, so it is never left with no one to log in. What the master set (notifications kept or not) goes
    too. The names of the users taken away."""
    with CONFIG_LOCK:
        users = cfg.setdefault("users", [])
        own_admin = any(access.is_admin(user) and not user.get("from_master") for user in users)
        kept, gone = [], []
        for user in users:
            if not user.get("from_master"):
                kept.append(user)
            elif access.is_admin(user) and not own_admin and user.get("password_hash"):
                kept.append({key: value for key, value in user.items() if key != "from_master"})
            else:
                gone.append(user.get("username") or user.get("id"))
        cfg["users"] = kept
        save_config()
    try:
        os.remove(NOTIFICATIONS_OWN)
    except FileNotFoundError:
        pass
    linux_users_changed()
    return gone


@app.route("/api/master/forget-member", methods=["POST"])
def api_master_forget_member():
    """(On a master) a member that left or is removed: its access is taken off every user and template, so no one is
    said to have something there, and nothing is sent there again"""
    require_admin()
    name = str((request.get_json(silent=True) or {}).get("member") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name):
        return error("Not a server.")
    changed = 0
    with CONFIG_LOCK:
        for item in [*cfg.get("users", []), *[template.get("access") or {} for template in cfg.get("user_templates", [])]]:
            servers = item.get("servers")
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                changed += 1
        if changed:
            save_config()
    audit(f"{name} is no longer a member: its access taken off {changed} user{'' if changed == 1 else 's'} and template{'' if changed == 1 else 's'}")
    return jsonify(changed=changed)


@app.route("/api/membership", methods=["GET", "PUT", "DELETE"])
def api_membership():
    """(On a member) the master it belongs to: set by the master when it joins it (PUT), taken away when it leaves"""
    require_admin()
    if request.method != "GET":
        if not g.get("from_master"):
            abort(403, "Only the master makes this server its member.")
        if request.method == "DELETE":
            try:
                os.remove(MEMBERSHIP)
            except FileNotFoundError:
                pass
            gone = left_master()
            audit("Left the master: users are made here again" + (f"; its users taken away: {', '.join(gone)}" if gone else ""))
        else:
            data = request.get_json(silent=True) or {}
            name, address = str(data.get("name") or "")[:80], str(data.get("address") or "")[:300]
            if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?/?", address):
                return error("The master's address starts with https://")
            os.makedirs(os.path.dirname(MEMBERSHIP), exist_ok=True)
            with open(MEMBERSHIP, "w", encoding="utf-8") as stream:
                json.dump({"name": name, "address": address.rstrip("/"), "joined": int(time.time())}, stream)
            audit(f"Joined the master {name} ({address}): users are made there")
    return jsonify(member_of=member_of())


def master_call():
    """Whether this request is the master's (harbor master call, run here over SSH by the master): from this server
    itself, not through Apache (which adds X-Forwarded-For), with the token only root here can read"""
    sent = request.headers.get("X-Harbor-Master", "")
    if not sent or request.remote_addr not in ("127.0.0.1", "::1") or request.headers.get("X-Forwarded-For"):
        return False
    try:
        with open(MASTER_TOKEN, encoding="utf-8") as stream:
            expected = stream.read().strip()
    except OSError:
        return False
    return len(expected) >= 32 and hmac.compare_digest(sent, expected)


@app.before_request
def protect():
    # The master asking a member (phase 13): as root here, no session or CSRF token, its token instead
    if request.headers.get("X-Harbor-Master") is not None:
        if not master_call():
            return jsonify(error="Not the master"), 401
        g.from_master = True
        # For one of its users (the master sends them here): as that user, so what they may here decides
        acting = request.headers.get("X-Harbor-As", "")
        if acting:
            user = access.find(cfg, acting)
            if user is None or not user.get("from_master"):
                return jsonify(error="Not a user the master gave this server"), 403
            g.user = user
        else:
            g.user = access.root_user(cfg)
        return None
    # The VNS.Harbor package in a site: its own token, no session (harbor_api below)
    if request.path.startswith("/api/harbor/v1/"):
        g.harbor_site = harbor_token_site(request.headers.get("Authorization", ""))
        if g.harbor_site is None:
            return jsonify(error="Not a site Harbor knows"), 401
        return None
    # A publish from a machine without SSH: a user's publish token, no session (publish_api below)
    if request.path.startswith("/api/publish/"):
        user_id = _publish_tokens.user_for(request.headers.get("Authorization", ""))
        g.user = access.find(cfg, user_id) if user_id else None
        if g.user is None:
            return jsonify(error="Not a publish token Harbor knows. Make one with Publish from your machine on the site in Harbor."), 401
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not (expected and hmac.compare_digest(sent, expected)):
            # A sign-in page left open past the session's idle timeout, or one from before a sign-in in another tab,
            # sends a token the session no longer has. A form sent from this site's own page is no forgery: the
            # browser says so in Sec-Fetch-Site, which no other site can set. Any other gets a fresh sign-in page.
            if request.path in ("/login", "/forgot", "/reset"):
                if request.headers.get("Sec-Fetch-Site") != "same-origin":
                    session.clear()
                    return render_template("login.html", csrf=csrf_token(), error="The sign-in page had expired. Sign in again.", **sign_in_page()), 400
            elif request.path == "/logout" and not session.get("uid"):
                return redirect("/login")
            elif not expected:
                # The session ran out: back to the sign-in page, as for any request without one
                session.clear()
                return jsonify(error="Not logged in"), 401
            else:
                return jsonify(error="Invalid CSRF token"), 403
    if request.path.startswith("/ws/") and urlparse(request.headers.get("Origin", "")).netloc != request.headers.get("Host", ""):
        return jsonify(error="Bad origin"), 403
    # An export's zip, by its link's secret (Settings, Export): what a customer is sent, without a login
    public = request.path in ("/login", "/forgot", "/reset") or request.path.startswith("/static/") or request.path.startswith("/download/") and request.method in ("GET", "HEAD")
    g.user = session_user()
    if not public and g.user is None:
        session.clear()
        if request.path.startswith("/api/") or request.path.startswith("/ws/"):
            return jsonify(error="Not logged in"), 401
        # Sent here from an address Harbor took over (?from=): the sign-in page is told so too
        came = request.args.get("from", "")
        return redirect("/login" + (f"?from={urllib.parse.quote(came)}" if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", came) else ""))


def session_user():
    """The user of this session, or None when it has none, or its user was removed, disabled or given a new
    password since (each of those changes the user's version, which the session keeps)"""
    user_id = session.get("uid")
    if user_id is None and session.get("user") and session.get("user") == cfg.get("user"):
        user_id = access.ROOT  # a session from before there were more users
    user = access.find(cfg, user_id) if user_id else None
    if user is None or user.get("id") != access.ROOT and session.get("version") != user.get("version", 1):
        return None
    return user


def me():
    return getattr(g, "user", None) or session_user()


def is_admin():
    return access.is_admin(me())


def require_admin():
    if not is_admin():
        abort(403, "Only an administrator can do this.")


def require_site(site, env, action):
    """Stop the request unless the user may do action on this environment of the site"""
    if not access.can(me(), site, env, action):
        abort(403, "You do not have access to do this here.")


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if response.headers.pop("X-Harbor-Preview", None):
        # A picture or PDF shown in the file browser's preview: framed by the admin only, and nothing of it runs
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        "worker-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'"
    )
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def asset_version():
    """A version in the address of app.js and style.css that changes with them, so a browser never keeps an old copy"""
    static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    try:
        version = max(int(os.path.getmtime(os.path.join(static, name))) for name in ("app.js", "style.css"))
    except OSError:
        version = 0
    return {"asset_version": version}


# ---- the sign-in page: where it was before (an address Harbor took over), and a forgotten password

RESET_LINKS = os.path.join(DATA, "password-resets.json")
# When each user last signed in: kept apart from the config, which is not written at every sign-in
LOGINS = os.path.join(DATA, "logins.json")
LOGINS_LOCK = threading.Lock()


def last_logins():
    """{user id: when they last signed in (seconds)}"""
    try:
        found = json.loads(Path(LOGINS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in found.items() if isinstance(value, (int, float))} if isinstance(found, dict) else {}


def note_login(user_id):
    """A sign-in noted for the user's card (Users): a failure to write it never stops the sign-in"""
    try:
        with LOGINS_LOCK:
            logins = last_logins()
            logins[user_id] = int(time.time())
            temporary = f"{LOGINS}.{os.getpid()}"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(logins, stream)
            os.replace(temporary, LOGINS)
    except OSError:
        app.logger.exception("Could not note the sign-in")
RESET_ASKED = {}
RESET_PER_IP = 5           # asked from one address in WINDOW
RESET_PER_USER = 3         # mails to one user in an hour


def moved_from():
    """The addresses Harbor took over: those given in Settings (General, Sign-in), and the admin site's own aliases (an
    old address pointed at Harbor, as account.vns.dk). One who comes from them is told to set a new password"""
    given = [host for host in cfg.get("moved_from", []) if isinstance(host, str)]
    return given + [host for host in admin_aliases() if host not in given]


def admin_aliases():
    """The admin site's other names in Apache (ServerAlias): not its own"""
    if not ADMIN_SITE:
        return []
    try:
        with open(f"/etc/apache2/sites-available/{ADMIN_SITE}.conf", encoding="utf-8") as stream:
            text = stream.read()
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].lower() == "serveralias":
            found.extend(host.lower() for host in parts[1:] if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) and host.lower() != ADMIN_SITE.lower())
    return list(dict.fromkeys(found))


def came_from_moved():
    """The address Harbor took over that this visit came from: it is that address (a binding), it was sent here from it
    (?from=, as a redirect is best made, since a browser seldom says where it came from), or a page there linked here"""
    hosts = {host.lower() for host in moved_from()}
    for host in ((request.host or "").split(":")[0], request.args.get("from", ""), urlparse(request.referrer or "").hostname or ""):
        if host and host.lower() in hosts:
            return host.lower()
    # Sent on by a redirect (?from=) from another name under Harbor's own domain (account.example.com to
    # harbor.example.com): that is an address it took over, though none is given in Settings
    came = request.args.get("from", "").lower()
    if ADMIN_SITE and came != ADMIN_SITE.lower() and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", came) and "." in ADMIN_SITE:
        domain = ".".join(ADMIN_SITE.lower().split(".")[-2:])
        if came.endswith("." + domain):
            return came
    return ""


def can_mail():
    return bool(_keep_alive_module.report.load_mail().get("host"))


def sign_in_page():
    """What every sign-in page has: whether Forgot password can be offered (a mail server to send the link)"""
    return {"forgot": can_mail()}


def reset_allowed(ip, user_id):
    """A forgotten password may be asked for: not too often from one address, nor mailed too often to one user"""
    now = time.time()
    with FAIL_LOCK:
        asked = RESET_ASKED.setdefault(ip, [])
        asked[:] = [when for when in asked if now - when < WINDOW]
        asked.append(now)
        if len(asked) > RESET_PER_IP:
            return False
        if user_id:
            mailed = RESET_ASKED.setdefault("user:" + user_id, [])
            mailed[:] = [when for when in mailed if now - when < 3600]
            if len(mailed) >= RESET_PER_USER:
                return False
            mailed.append(now)
    return True


def reset_link(token):
    """The link in the mail: to Harbor's own address, never one taken from the request (a forged Host header)"""
    return f"https://{ADMIN_SITE}/reset?token={token}" if ADMIN_SITE else f"{request.host_url.rstrip('/')}/reset?token={token}"


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Forgot password: a link mailed to the user to set a new one. The answer is the same whether the user exists or
    not, so it cannot be used to find out who has an account"""
    said = ""
    error = ""
    if request.method == "POST":
        if not can_mail():
            error = "Harbor cannot send mail yet: ask an administrator to set a new password for you."
        else:
            asked = request.form.get("username", "")
            user = access.for_reset(cfg, asked)
            to = access.mail_of(user) if user else ""
            if reset_allowed(client_ip(), user["id"] if user and to else None) and user and to:
                token = _password_reset.make(RESET_LINKS, user["id"])
                link = reset_link(token)
                host = ADMIN_SITE or request.host
                body = (f"Someone asked to set a new password for {user['username']} on Harbor ({host}).\n\n"
                        f"Open this link to choose a new password. It works once, within {_password_reset.TRY_MINUTES} minutes:\n{link}\n\n"
                        "If it was not you, do nothing: your password stays as it is, and the link runs out by itself.\n")
                try:
                    _keep_alive_module.report.send(_keep_alive_module.report.load_mail(), [to], "Set a new password for Harbor", body,
                                                   html=_keep_alive_module.report.reset_mail_html(host, user["username"], link, _password_reset.TRY_MINUTES))
                    audit(f"Mailed a link to set a new password to {user['username']}")
                except Exception:  # noqa: BLE001  (smtplib, ssl and socket each have their own: logged, the answer is the same)
                    app.logger.exception("Could not mail the password link")
            time.sleep(1)
            said = (f"If there is an account with that username or email, a link to set a new password is on its way to its email. "
                    f"It works once, within {_password_reset.TRY_MINUTES} minutes.")
    return render_template("login.html", csrf=csrf_token(), mode="forgot", said=said, error=error, **sign_in_page())


@app.route("/reset", methods=["GET", "POST"])
def reset_password():
    """The link from the mail: a new password, twice. The link's secret goes from the address into the session at once,
    so it is not left in the browser's history"""
    token = request.args.get("token")
    if token:
        session["reset_token"] = token
        return redirect("/reset")
    token = session.get("reset_token", "")
    user_id = _password_reset.peek(RESET_LINKS, token)
    user = access.find(cfg, user_id) if user_id else None
    if user is None or user.get("disabled") or user.get("id") == access.ROOT:
        session.pop("reset_token", None)
        return render_template("login.html", csrf=csrf_token(), mode="reset", expired=True, **sign_in_page())
    error = ""
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 12 or len(password) > 128:
            error = "The password must be 12 to 128 characters."
        elif password != request.form.get("again", ""):
            error = "The two passwords are not the same."
        elif _password_reset.use(RESET_LINKS, token) != user["id"]:
            session.pop("reset_token", None)
            return render_template("login.html", csrf=csrf_token(), mode="reset", expired=True, **sign_in_page())
        else:
            with CONFIG_LOCK:
                user = access.find(cfg, user_id)
                user["password_hash"] = generate_password_hash(password)
                # Every session they had ends: whoever had their old password is out
                user["version"] = user.get("version", 1) + 1
                save_config()
            linux_users_changed()
            audit(f"{user['username']} set a new password from a mailed link")
            session.clear()
            session["login_said"] = "Your new password is set. Sign in with it."
            return redirect("/login")
    return render_template("login.html", csrf=csrf_token(), mode="reset", username=user["username"], error=error, **sign_in_page())


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        ip = client_ip()
        if login_locked(ip):
            error = "Too many failed attempts. Try again later."
        else:
            found = access.by_username(cfg, request.form.get("username", ""))
            # The same work whether the name exists or not, so a wrong name cannot be told from a wrong password
            password_hash = found[1] if found else cfg["password_hash"]
            password_ok = check_password_hash(password_hash, request.form.get("password", "")) and found is not None
            if password_ok:
                user = access.find(cfg, found[0])
                session.clear()
                session.permanent = True
                session["uid"] = found[0]
                session["user"] = user["username"]
                session["version"] = user.get("version", 1)
                csrf_token()
                with FAIL_LOCK:
                    FAILS.pop(ip, None)
                note_login(found[0])
                return redirect("/")
            record_failure(ip)
            time.sleep(1)
            error = "Wrong username or password."
    return render_template("login.html", csrf=csrf_token(), error=error, said=session.pop("login_said", ""), moved=came_from_moved(),
                           **sign_in_page()), (429 if "Too many" in error else 200)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def index():
    user = me()
    start = cfg.get("start_view", "sites") if user.get("id") == access.ROOT else user.get("start_view", cfg.get("start_view", "sites"))
    # A user without the Web Server starts on SQL Server, and the other way round
    if not access.is_admin(user):
        shown = top_menu(user)
        start = "sql" if (start == "sql" and shown["sql"]) or not shown["sites"] else "sites"
    else:
        # An administrator starts where the top menu leads: on SQL Server when Web Server is not in it, and the other way
        shown = menu_shown()
        start = "sql" if not shown["web"] else "sites" if not shown["sql"] else start
    return render_template("index.html", csrf=csrf_token(), admin_site=ADMIN_SITE, hostname=socket.gethostname(), user=user.get("name") or user["username"],
                           start_view=start, role="admin" if access.is_admin(user) else "user", header=header_shown(user), site_tools=site_tools(),
                           menu=top_menu(user))


def site_tools():
    """Settings' Sites group: Convert DB with sites here, Export Site with sites or exports kept here (one of a site deleted
    with its export is restored from there). A master's are its members': it has them while it has members"""
    if has_members():
        return {"convert": True, "export": True}
    sites = has_sites()
    return {"convert": sites, "export": sites or bool(glob.glob(os.path.join(EXPORTS_DIR, "*", "meta.json")))}


@app.route("/api/site-tools")
def api_site_tools():
    """Settings' Sites group of this server: asked by a master's page working on it (the page itself has the master's)"""
    require_admin()
    return jsonify(site_tools())


MENU_SETTINGS = ("auto", "show", "hide")
# What an administrator sees in the header beside the menu: the site search, the server's addresses, Files and Terminal;
# each their own
HEADER_PARTS = ("search", "addresses", "files", "terminal")


def header_shown(user):
    saved = (cfg.get("header") if user.get("id") == access.ROOT else user.get("header")) or {}
    return {part: saved.get(part) is not False for part in HEADER_PARTS}


def has_sites():
    """Whether this server has sites of its own: a folder in the web root that is not the admin's"""
    try:
        names = site_dirs()
    except OSError:
        return True
    return any(name not in (ADMIN_SITE, "html") and not os.path.isfile(os.path.join(WEB_ROOT, name, "app", "config.json")) for name in names)


def menu_shown():
    """Web Server and SQL Server in the top menu for an administrator, {"web": bool, "sql": bool}: as set, or (auto)
    Web Server while there are sites here, SQL Server while SQL Server is installed here or there is a SQL connection.
    One of them is always shown: the admin has a start page."""
    # A master (phase 13) shows its members' sites and SQL connections on them: on auto, both are there
    master = has_members()
    shown = {"web": menu_part_shown("web_menu", lambda: master or (has_sites() and not os.path.exists(NO_WEB))), "sql": menu_part_shown("sql_menu", lambda: master or sql_in_use())}
    if not shown["web"] and not shown["sql"]:
        shown["web" if cfg.get("web_menu", "auto") != "hide" or cfg.get("sql_menu", "auto") == "hide" else "sql"] = True
    return shown


def top_menu(user):
    """What the top menu has for this user, as the page shows it once it knows them (applyAccess): in the page from the
    start, so a reload never shows a part they do not have before it is taken away"""
    admin = access.is_admin(user)
    if admin:
        menu = menu_shown()
    else:
        # A master's user: also what they have on each member, as that member tells the page (its /api/me) once it asks
        there = [{**part, "role": "user"} for name, part in (user.get("servers") or {}).items() if name in member_names() and isinstance(part, dict)]
        menu = {"web": access.has_web(user) or any(access.has_web(part) for part in there),
                "sql": access.has_sql(user) or any(access.has_sql(part) for part in there)}
    return {"sites": bool(menu["web"]), "sql": bool(menu["sql"]),
            # DNS: where a Cloudflare account is set up, for a user given domains, and for an administrator unless it
            # is taken out of the menu (Settings, Top menu)
            "dns": bool(_cloudflare.load()) and access.has_dns(user) and not (admin and cfg.get("dns_menu") == "hide"),
            # Mail: where a mailcow server is set up, for an administrator and a user given domains
            "mail": bool(_mailcow.load()) and access.has_mail(user),
            "server": admin}


@app.route("/api/menu-shown")
def api_menu_shown():
    """Web Server and SQL Server in this server's top menu: asked by a master's page working on it, whose own menu is
    the master's (SQL Server hidden there may be on here)"""
    require_admin()
    return jsonify(menu_shown())


def menu_part_shown(key, in_use):
    setting = cfg.get(key, "auto")
    return setting == "show" if setting in ("show", "hide") else in_use()


def sql_in_use():
    """SQL Server or the SQL tools installed here (with the tools, SQL Servers elsewhere are reached), or a SQL connection"""
    if os.path.exists("/opt/mssql/bin/sqlservr") or _sql_module.sqlcmd_path():
        return True
    try:
        return bool(_sql_module.connections(SQL_CONNECTIONS))
    except (OSError, ValueError):
        return True


@app.route("/api/preferences", methods=["PUT"])
def api_preferences():
    data = request.get_json(silent=True) or {}
    if "dns_menu" in data:
        # DNS in the top menu for administrators: while a Cloudflare account is set up (auto), or not (hide)
        require_admin()
        if data["dns_menu"] not in ("auto", "hide"):
            return error("DNS in the menu is auto or hide.")
        with CONFIG_LOCK:
            cfg["dns_menu"] = data["dns_menu"]
            save_config()
        return jsonify(dns=data["dns_menu"])
    if "sql_menu" in data or "web_menu" in data:
        # Whether Web Server and SQL Server are in the top menu: the server's, for its administrators
        require_admin()
        changes = {key: data[key] for key in ("web_menu", "sql_menu") if key in data}
        if any(value not in MENU_SETTINGS for value in changes.values()):
            return error("In the menu is auto, show or hide.")
        with CONFIG_LOCK:
            cfg.update(changes)
            save_config()
        return jsonify(menu={"web": cfg.get("web_menu", "auto"), "sql": cfg.get("sql_menu", "auto")}, shown=menu_shown())
    if "header" in data:
        # What the header shows beside the menu: each administrator's own (root's is the server's)
        require_admin()
        given = data.get("header")
        if not isinstance(given, dict) or any(key not in HEADER_PARTS or not isinstance(value, bool) for key, value in given.items()):
            return error("The header shows search, addresses, files and terminal, each on or off.")
        with CONFIG_LOCK:
            target = cfg if me().get("id") == access.ROOT else me()
            target["header"] = {**header_shown(me()), **given}
            save_config()
        return jsonify(header=header_shown(me()))
    if "public_address" in data:
        # The server's address as the internet sees it, for a server behind NAT: what DNS records point to
        require_admin()
        address = str(data.get("public_address") or "").strip()
        if address and not (valid_ip(address) or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", address)):
            return error("Enter an IP address or a host name, or leave it empty.")
        with CONFIG_LOCK:
            cfg["public_address"] = address
            save_config()
        if "start_view" not in data:
            return jsonify(public_address=address)
    start_view = data.get("start_view")
    if start_view not in ("sites", "sql"):
        return error("The start page must be sites or sql")
    with CONFIG_LOCK:
        # Root's start page is the server's; every other user has their own
        if me().get("id") == access.ROOT:
            cfg["start_view"] = start_view
        else:
            me()["start_view"] = start_view
        save_config()
    return jsonify(start_view=start_view)


# ------------------------------------------------------------------ DNS

def valid_ip(value):
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def server_addresses():
    """This server's own addresses: every global one its network has, the internal ones of a server behind NAT too"""
    found = []
    try:
        result = subprocess.run(["ip", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            match = re.search(r"\binet6?\s+([0-9a-fA-F:.]+)/", line)
            if match and match.group(1) not in found:
                found.append(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def is_private(address):
    import ipaddress
    try:
        ip = ipaddress.ip_address(address)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


PUBLIC_ADDRESS = {}
PUBLIC_ADDRESS_LOCK = threading.Lock()


def detected_public_address():
    """The server's public IPv4 address as the internet sees it, asked of an outside service.
    A server behind NAT knows only its internal address. Kept for an hour, or five minutes after a failure."""
    with PUBLIC_ADDRESS_LOCK:
        cached = PUBLIC_ADDRESS.get("value")
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        address = ""
        for url in ("https://1.1.1.1/cdn-cgi/trace", "https://api.ipify.org"):
            try:
                with urllib.request.urlopen(url, timeout=4) as response:
                    text = response.read(4096).decode("ascii", "replace")
            except (OSError, ValueError) as exc:
                app.logger.warning("Could not detect the public address from %s: %s", url, exc)
                continue
            match = re.search(r"(?m)^ip=(\S+)$", text) or re.fullmatch(r"\s*(\S+)\s*", text)
            candidate = match.group(1) if match else ""
            if valid_ip(candidate) and ":" not in candidate and not is_private(candidate):
                address = candidate
                break
        PUBLIC_ADDRESS["value"] = (address, time.monotonic() + (3600 if address else 300))
        return address


@app.route("/api/server-address")
def api_server_address():
    """Where a site's DNS should point: the public address set under Preferences,
    else the public address found automatically, else the server's own"""
    addresses = server_addresses()
    public = cfg.get("public_address", "")
    detected = "" if public else detected_public_address()
    ipv4 = [item for item in addresses if ":" not in item]
    best = public or detected or next((item for item in ipv4 if not is_private(item)), "") or (ipv4[0] if ipv4 else "")
    return jsonify(name=socket.getfqdn(), addresses=addresses, public=public, detected=detected, address=best,
                   behind_nat=not public and not detected and bool(ipv4) and all(is_private(item) for item in ipv4))


@app.route("/api/dns")
def api_dns():
    """What a name points to now, and whether that is this server (or the address the page names)"""
    name = (request.args.get("name") or "").strip().rstrip(".").lower()
    if not re.fullmatch(r"(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?)*", name):
        return error("Not a domain name")
    try:
        found = sorted({info[4][0] for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)})
    except (socket.gaierror, UnicodeError, OSError):
        found = []
    expected = (request.args.get("expect") or "").strip()
    ours = own_addresses(expected)
    # A CNAME is suggested only for this server, not for an address given for another
    host = "" if expected else host_name_for(name, ours, request.host)
    proxy = "Cloudflare" if via_cloudflare(found) else ""
    return jsonify(name=name, addresses=found, here=bool(proxy) or bool(found) and any(item in ours for item in found), host=host, proxy=proxy)


# Cloudflare's published ranges (cloudflare.com/ips). A proxied name resolves to these, never to the server
# behind them, so a name that resolves only to Cloudflare is taken as pointing here.
CLOUDFLARE = tuple(ipaddress.ip_network(item) for item in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32"))


def via_cloudflare(found):
    """Whether every address a name resolves to is Cloudflare's"""
    def ours(address):
        try:
            return any(ipaddress.ip_address(address.split("%")[0]) in network for network in CLOUDFLARE)
        except ValueError:
            return False
    return bool(found) and all(ours(address) for address in found)


# Domains whose every name points here (a wildcard DNS record, *.domain), for a new site's temporary address until its
# own domain is ready (1.0.77)
def temp_domains():
    return [name for name in cfg.get("temp_domains", []) if isinstance(name, str)]


def temp_domains_shown():
    """The domains as the page shows them: the name, and in its own letters"""
    return [{"name": name, "display": display_dns_name(name)} for name in temp_domains()]


def wildcard_points_here(domain):
    """Whether *.domain points here: a name under it no one made resolves to this server"""
    return points_here(f"harbor-check-{secrets.token_hex(4)}.{domain}")


@app.route("/api/temp-domains", methods=["GET", "POST", "DELETE"])
def api_temp_domains():
    """The domains for temporary addresses: read by those who may create sites, added (when *.domain points here) and
    removed by an administrator"""
    user = me()
    if request.method == "GET":
        if not (access.is_admin(user) or user.get("create_sites")):
            abort(403, "You do not have access to do this.")
        return jsonify(domains=temp_domains_shown())
    require_admin()
    data = request.get_json(silent=True) or {}
    name = dns_name(str(data.get("name") or request.args.get("name") or "").strip().rstrip(".").removeprefix("*."), "domain", fully_qualified=True).lower()
    with CONFIG_LOCK:
        domains = temp_domains()
        if request.method == "DELETE":
            cfg["temp_domains"] = [item for item in domains if item != name]
        else:
            if name in domains:
                return error(display_dns_name(name) + " is in the list already.")
            if not wildcard_points_here(name):
                address = cfg.get("public_address") or detected_public_address() or "this server's address"
                return error(f"*.{display_dns_name(name)} does not point to this server. Add a wildcard record for it "
                             f"(* A {address}), then add it again.")
            cfg["temp_domains"] = sorted(domains + [name])
        save_config()
    audit(("Removed" if request.method == "DELETE" else "Added") + " the temporary address domain " + name)
    return jsonify(domains=temp_domains_shown())


def points_here(name):
    """Whether a DNS name resolves to this server now. Let's Encrypt issues a certificate only then."""
    return not not_pointing([name])


def not_pointing(names, expect=""):
    """The names that do not resolve to this server, or to the server expect names (a remote environment's)"""
    ours = own_addresses(expect)
    missing = []
    for name in names:
        try:
            found = {info[4][0] for info in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)}
        except (socket.gaierror, UnicodeError, OSError):
            found = set()
        if not found & ours and not via_cloudflare(found):
            missing.append(name)
    return missing


def certificate_names_point(site, data):
    """Issue certificate needs every name of the certificate to point to the server that issues it"""
    if data.get("scope") == "test":
        binding = conf_binding(site)
        names = [binding, *server_aliases(read_text(os.path.join(APACHE_SITES, site + ".conf")), binding)]
        expect = ""
    else:
        prod = parse_prod(os.path.join(WEB_ROOT, site), target_arg(site, data)) or {}
        names = [prod.get("PROD_BINDING", "")]
        # A remote environment issues its certificate on its own server
        expect = "" if prod.get("PROD_LOCAL_PATH") else prod.get("PROD_IP", "")
    names = [name.lower() for name in names if name and "." in name and name != "*"]
    missing = not_pointing(names, expect)
    if missing:
        abort(400, ", ".join(display_dns_name(name) for name in missing)
              + (" does" if len(missing) == 1 else " do") + " not point to " + (expect or "this server")
              + " yet, so no certificate can be issued. Change the DNS records first.")


def own_addresses(expected=""):
    """The addresses that count as this server (or as the address the page names)"""
    ours = {expected} if expected else set(server_addresses()) | {cfg.get("public_address", "") or detected_public_address()} - {""}
    # A public address set as a host name counts by what it resolves to
    for item in list(ours):
        if item and not valid_ip(item):
            try:
                ours |= {info[4][0] for info in socket.getaddrinfo(item, None, proto=socket.IPPROTO_TCP)}
            except (socket.gaierror, UnicodeError, OSError):
                pass
    return ours


def host_name_for(name, ours, visited=""):
    """This server's own DNS name, for a subdomain's CNAME to it. A domain itself (narhat.dk) can only
    have an A record, so it gets none. First the name the admin is visited on, without its first label
    while the rest is still a subdomain (admin.test-01.vns.dk gives test-01.vns.dk, admin.vns.dk stays).
    Then the server's full host name, and its short host name under one of the name's parent domains.
    Only a name that resolves to this server is offered, so another machine is never suggested."""
    labels = name.split(".")
    if len(labels) < 3:
        return ""
    candidates = []
    visited = re.sub(r":\d+$", "", (visited or "").strip().lower()).rstrip(".")
    if "." in visited and not valid_ip(visited.strip("[]")):
        parts = visited.split(".")
        if len(parts) >= 4:
            candidates.append(".".join(parts[1:]))
        candidates.append(visited)
    short = socket.gethostname().split(".")[0].lower()
    fqdn = socket.getfqdn().lower().rstrip(".")
    if "." in fqdn and not fqdn.endswith(".arpa") and not fqdn.startswith("localhost"):
        candidates.append(fqdn)
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", short):
        candidates += [short + "." + ".".join(labels[index:]) for index in range(1, len(labels) - 1)]
    for candidate in dict.fromkeys(candidates):
        if candidate == name:
            continue
        try:
            resolved = {info[4][0] for info in socket.getaddrinfo(candidate, None, proto=socket.IPPROTO_TCP)}
        except (socket.gaierror, UnicodeError, OSError):
            continue
        if resolved & ours:
            return candidate
    return ""


# ------------------------------------------------------------------ helpers

@app.errorhandler(HTTPException)
def http_error(exc):
    if request.path.startswith("/api/"):
        return jsonify(error=exc.description), exc.code
    return exc


def error(message, status=400):
    return jsonify(error=message), status


def site_dirs():
    names = []
    for name in sorted(os.listdir(WEB_ROOT)):
        if name.startswith(".") or not NAME_RE.fullmatch(name):
            continue
        if os.path.isdir(os.path.join(WEB_ROOT, name)) and not os.path.islink(os.path.join(WEB_ROOT, name)):
            names.append(name)
    return names


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


PROD_KEYS = ("PROD_LOCAL_PATH", "PROD_IP", "PROD_USER", "PROD_SSH_PORT", "PROD_FOLDER", "PROD_BINDING", "PROD_PORT_HTTP",
             "PROD_TARGET", "PROD_WINDOWS_SHARE", "PROD_SMB_USER", "PROD_HTTPS", "PROD_ENVIRONMENT")


TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TARGET_SCRIPTS = ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh")


def target_folder(root, target="prod"):
    """Scripts of an environment: scripts/targets/<id>, or scripts for the older single environment (id "prod")."""
    return os.path.join(root, "scripts") if target == "prod" else os.path.join(root, "scripts", "targets", target)


def keep_harbor_state():
    """After a task: Harbor's copy of the sites' state equal to the site folders again (harbor/state). A task can have
    made, changed, moved or deleted an environment; the scripts it ran do not keep the copy themselves."""
    harbor = os.path.join(HARBOR_DIR, "bin", "harbor")
    # Only the admin itself, which runs as root: never a test run as a user
    if not os.path.isfile(harbor) or os.geteuid() != 0:
        return
    try:
        result = subprocess.run(["/usr/bin/python3", harbor, "state", "sync"], capture_output=True, text=True, timeout=120)
        if result.returncode or result.stderr.strip() or result.stdout.strip():
            app.logger.warning("harbor state sync: %s", (result.stdout + result.stderr).strip() or result.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.warning("harbor state sync could not run: %s", exc)


# What each script in /vns/apache did, as the harbor command it ran (they were wrappers; they go with the cutover)
HARBOR_WRAPPERS = {
    "adopt_site.sh": ["site", "adopt"],
    "adopt_site.sh --release": ["site", "release"],
    "change_binding.sh": ["site", "binding"],
    "change_binding_prod.sh": ["env", "binding"],
    "copy_site.sh": ["site", "copy"],
    "create_site.sh": ["site", "create"],
    "data_sync.sh": ["env", "data-sync"],
    "delete_site.sh": ["site", "delete"],
    "fix_permissions.sh": ["site", "fix-permissions"],
    "move_env.sh": ["env", "move"],
    "move_site.sh": ["site", "move"],
    "prod_action.sh": ["env", "action"],
    "register_env.sh": ["env", "register"],
    "remote_site.sh": ["env", "remote"],
    "setup_site.sh": ["site", "setup"],
    "sync_env.sh": ["env", "sync"],
    "test_action.sh": ["site", "action"],
    "windows_files.sh": ["env", "windows-files"],
}


def harbor_argv(script):
    """The harbor command a wrapper in /vns/apache ran, to be run with that wrapper's arguments"""
    return ["/usr/bin/python3", HARBOR_COMMAND, *HARBOR_WRAPPERS[script]]


HARBOR_SITES = "/var/lib/harbor/sites"
# What each system script did, as the harbor command its wrapper calls (a migrated site has no scripts, phase 6)
HARBOR_SCRIPTS = {
    "publish.sh": ["site", "publish"], "publish_local.sh": ["site", "publish"], "clear_logs.sh": ["site", "clear-logs"],
    "clear_logs_local.sh": ["site", "clear-logs"], "check_service.sh": ["site", "check-service"], "edit_conf.sh": ["site", "edit-conf"],
    "edit_service.sh": ["site", "edit-service"], "restart_service.sh": ["site", "restart-service"],
    "issue_certificat.sh": ["site", "reissue-certificate"], "outdated.sh": ["site", "outdated"],
    "update_packages.sh": ["site", "update-packages"], "fix_permissions.sh": ["site", "fix-app-permissions"],
    "change_env.sh": ["site", "action", "@", "change_env"], "restart_apache.sh": ["server", "restart-apache"],
}
HARBOR_TARGET_SCRIPTS = {"publish_prod.sh": ["env", "publish"], "clear_logs_prod.sh": ["env", "clear-logs"], "sync_prod.sh": ["env", "sync"]}


def site_migrated(site):
    """Migrated (harbor migrate, phase 6): Harbor holds its settings and runs what its scripts did; its folder has none"""
    return os.path.isfile(os.path.join(HARBOR_SITES, site, "migrated.json"))


def migrated_site_json(site):
    try:
        with open(os.path.join(HARBOR_SITES, site, "site.json")) as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def custom_scripts_dir(site, script=""):
    """A migrated site's own scripts: they stay in its scripts folder (Harbor's own scripts are gone from there). A site
    published from a machine has the ones its publishes send in Harbor's state, where only root can write
    (harbor/sites/receive_build.py); those written on the server stay in its scripts folder. With a script: the folder
    that has it, Harbor's state first"""
    owned = own_scripts_dir(site)
    if script and os.path.isfile(os.path.join(owned, script)):
        return owned
    return os.path.join(WEB_ROOT, site, "scripts")


def own_scripts_dir(site):
    return os.path.join(HARBOR_SITES, site, "scripts")


# A custom script's name: a file in a scripts folder, never a path or a hidden file
CUSTOM_SCRIPT_RE = re.compile(r"[\w-][\w.-]{0,99}\.sh")


def env_scripts_dir(site, target):
    """Where an environment's own custom scripts are: the scripts folder in its site folder here, or None for one on
    another Linux server (they are there, in its site folder). An environment on Windows has none."""
    prod = parse_prod(os.path.join(WEB_ROOT, site), target) or {}
    if prod.get("PROD_TARGET") == "windows":
        abort(400, "An environment on Windows has no custom scripts")
    if not prod.get("PROD_LOCAL_PATH"):
        return None
    full = os.path.realpath(prod["PROD_LOCAL_PATH"])
    if not full.startswith(WEB_ROOT + os.sep):
        abort(400, "The environment's folder is not under " + WEB_ROOT)
    return os.path.join(full, "scripts")


def listed_scripts(folder):
    try:
        return sorted(name for name in os.listdir(folder) if CUSTOM_SCRIPT_RE.fullmatch(name) and os.path.isfile(os.path.join(folder, name)))
    except OSError:
        return []


def owned_by_root(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return info.st_uid == 0 and not info.st_mode & 0o022


def scripts_safe(folder):
    """Whether a scripts folder and every script in it are root's, and no one else may write them: else they are not
    run (runnable), and the menu offers to make them root's"""
    if not os.path.isdir(folder):
        return True
    return owned_by_root(folder) and all(owned_by_root(os.path.join(folder, name)) for name in os.listdir(folder) if name.endswith(".sh"))


def make_root_only(folder):
    """A scripts folder and all in it root's, and no one else may write them; links are left as they are"""
    for here, folders, files in os.walk(folder):
        for path in [here, *(os.path.join(here, name) for name in folders + files)]:
            if os.path.islink(path):
                continue
            os.chown(path, 0, 0)
            os.chmod(path, os.stat(path).st_mode & 0o7755)


def root_only(path):
    """A script the admin runs as root, and its folder: root's, and no one else may write them. A site's app user
    (www-data) owns the site's folder and could swap in a scripts folder of its own; such a script is not run"""
    for item in (path, os.path.dirname(path)):
        try:
            info = os.stat(item)
        except OSError:
            return False
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
    return True


def runnable(path):
    if not root_only(path):
        abort(400, f"{os.path.basename(path)} is not run: it or its folder can be changed by someone other than root, and it "
                   "runs as root. Check what it does, then an administrator makes the scripts root's with Make them root's… "
                   f"in Run custom script (or: chown -R root:root {os.path.dirname(path)} && chmod -R go-w {os.path.dirname(path)})")
    return path


def migrated_scripts(site):
    """The scripts a migrated site shows: the system scripts it had, and its own (in its scripts folder, and the ones its
    publishes from a machine sent, in Harbor's state)"""
    names = set(name for name in migrated_site_json(site).get("scripts", []) if isinstance(name, str) and SCRIPT_RE.match(name))
    for own in (custom_scripts_dir(site), own_scripts_dir(site)):
        if os.path.isdir(own):
            names |= {name for name in os.listdir(own) if SCRIPT_RE.match(name) and os.path.isfile(os.path.join(own, name))}
    return sorted(names)


def target_scripts(site, target, kind=""):
    """The scripts an environment has: in its folder, or for a migrated site what it had (or, for a newer one, what an
    environment of its kind gets)"""
    root = os.path.join(WEB_ROOT, site)
    if not site_migrated(site):
        return [script for script in TARGET_SCRIPTS if os.path.isfile(os.path.join(target_folder(root, target), script))]
    saved = (migrated_site_json(site).get("targets") or {}).get(target)
    if isinstance(saved, list):
        return [script for script in TARGET_SCRIPTS if script in saved]
    return [script for script in TARGET_SCRIPTS if script == "publish_prod.sh" or kind == "umbraco"]


def harbor_script_command(site, script, target="prod"):
    """The command a script of a migrated site runs: the harbor command its wrapper called, or the site's own script"""
    harbor = ["/usr/bin/python3", HARBOR_COMMAND]
    if script in HARBOR_TARGET_SCRIPTS and target:
        words = HARBOR_TARGET_SCRIPTS[script] + [site] + (["main", target] if script == "sync_prod.sh" else [])
        return ["/usr/bin/env", "VNS_TARGET=" + target, *harbor, *words]
    if script in HARBOR_SCRIPTS:
        words = HARBOR_SCRIPTS[script]
        return harbor + ([site if word == "@" else word for word in words] if "@" in words
                         else words + ([site] if words[0] == "site" else []))
    folder = custom_scripts_dir(site, script)
    path = os.path.realpath(os.path.join(folder, script))
    if os.path.dirname(path) != os.path.realpath(folder) or not os.path.isfile(path):
        abort(400, "Unknown script")
    return ["/bin/bash", runnable(path)]


def publish_entry(site, target=None):
    """What publish_status.py both runs for the site itself (target None) or an environment: its script, or harbor"""
    root = os.path.join(WEB_ROOT, site)
    scope = "test" if target is None else target_scope(target)
    if site_migrated(site):
        return "harbor:" + scope
    if target is None:
        folder = os.path.join(root, "scripts")
        local = next((name for name in ("publish_local.sh", "publish.sh") if os.path.isfile(os.path.join(folder, name))), None)
        return os.path.realpath(os.path.join(folder, local)) if local else None
    return os.path.realpath(os.path.join(target_folder(root, target), "publish_prod.sh"))


def target_ids(root):
    """The site's environments: from its folder while their scripts are there, else from Harbor's copy (harbor/state)"""
    from harbor.core.system import System
    from harbor.environments import settings
    return [name for name in settings.target_ids(System(), root) if TARGET_RE.match(name)]


ENVIRONMENT_ORDER = ("development", "test", "staging", "production")


def environment_order(root, targets):
    """Environments in the order the work goes, by the environment they are: Development, Test, Staging, Production
    (others last), then by name"""
    def key(target):
        name = ((parse_prod(root, target) or {}).get("PROD_ENVIRONMENT") or "").lower()
        return (ENVIRONMENT_ORDER.index(name) if name in ENVIRONMENT_ORDER else len(ENVIRONMENT_ORDER), target)
    return sorted(targets, key=key)


def target_scope(target):
    return "prod" if target == "prod" else "target-" + target


def target_arg(site, data):
    """The environment selected in a request. Without one, the older single environment."""
    target = data.get("target") or "prod"
    if not isinstance(target, str) or not TARGET_RE.match(target) or target not in target_ids(os.path.join(WEB_ROOT, site)):
        abort(400, "Unknown environment")
    return target


def with_target(argv, target):
    return argv if target == "prod" else ["/usr/bin/env", "VNS_TARGET=" + target, *argv]


def adopted_sites(names):
    """Sites in their own folder that are environments of another site. They are listed under that site only."""
    adopted = set()
    for name in names:
        root = os.path.join(WEB_ROOT, name)
        for target in target_ids(root):
            prod = parse_prod(root, target) or {}
            local = os.path.realpath(prod.get("PROD_LOCAL_PATH") or "")
            if prod.get("PROD_LOCAL_PATH") and local.startswith(WEB_ROOT + os.sep):
                folder = os.path.relpath(local, WEB_ROOT).split(os.sep)[0]
                if folder != name:
                    adopted.add(folder)
    return adopted


MANAGED_DIR = "/var/lib/vns-managed"


def managed_by(name):
    """The main site and server this site is an environment of, when that is another server."""
    try:
        with open(os.path.join(MANAGED_DIR, name), encoding="utf-8") as note:
            lines = note.read().splitlines()
    except OSError:
        return None
    return {"site": lines[0] if lines else "", "host": lines[1] if len(lines) > 1 else ""}


def service_dll(name):
    """Name of the dll a site's service runs, without .dll."""
    match = re.search(r"^ExecStart=(.+)$", read_text(os.path.join(SERVICE_DIR, service_name(name) + ".service")), re.M)
    for argument in shlex.split(match.group(1)) if match else []:
        if argument.endswith(".dll"):
            return os.path.basename(argument)[:-4]
    return ""


def project_assembly(project):
    match = re.search(r"<AssemblyName>\s*([^<\s]+)\s*</AssemblyName>", read_text(project))
    return match.group(1) if match else os.path.splitext(os.path.basename(project))[0]


def adoption_candidates(sites):
    """For sites without a project: the main sites that build the dll they run. Only those can publish to them."""
    builders = {}
    for site in sites:
        projects = glob.glob(os.path.join(glob.escape(os.path.join(WEB_ROOT, site["name"])), "*.csproj"))
        if len(projects) == 1 and not site.get("admin"):
            builders.setdefault(project_assembly(projects[0]), []).append(site["name"])
    for site in sites:
        if (not site["has_project"] and not site.get("admin") and not site.get("managed_by")
                and site["kind"] in ("csharp", "umbraco") and len(site["environments"]) == 1):
            site["adopt_into"] = builders.get(service_dll(site["name"]), [])


def parse_prod(root, target="prod"):
    """An environment's settings, as harbor reads them (its script, else Harbor's copy). The password is never given."""
    from harbor.core.system import System
    from harbor.environments import settings
    try:
        found = settings.values(System(), root, target)
    except (OSError, settings.SettingsError):
        return None
    if found is None:
        return None
    values = {}
    for key in PROD_KEYS:
        value = found.get(key[len("PROD_"):], "")
        values[key] = "" if value.startswith("@") else value
    return values


def conf_binding(unit):
    match = re.search(r"^\s*ServerName\s+(\S+)", read_text(os.path.join(APACHE_SITES, unit + ".conf")), re.M)
    return match.group(1) if match else ""


def service_port(unit):
    match = re.search(r"(?:localhost|127\.0\.0\.1):(\d+)", read_text(os.path.join(SERVICE_DIR, service_name(unit) + ".service")))
    return match.group(1) if match else ""


def unit_of(name):
    return os.path.isfile(os.path.join(SERVICE_DIR, service_name(name) + ".service"))


def https_status(unit):
    text = read_text(os.path.join(APACHE_SITES, unit + ".conf"))
    secure = bool(re.search(r"<VirtualHost\s+[^>]*:443>", text))
    certificate = re.search(r'^\s*SSLCertificateFile\s+"?([^"\s]+)', text, re.M)
    return {"scheme": "https" if secure else "http",
            "https": secure and bool(certificate and os.path.isfile(certificate.group(1)))}


REMOTE_STATUS = {}


def publishable_targets(site, root):
    """The environments to publish to together. A site deleted on its server is not created again by accident.
    A server that does not answer has no exists value, so it is tried and reports its own error."""
    targets = []
    for target in target_ids(root):
        prod = parse_prod(root, target) or {}
        remote = prod and not prod.get("PROD_LOCAL_PATH") and prod.get("PROD_TARGET") != "windows"
        if not remote or remote_status(site, target).get("exists") is not False:
            targets.append(target)
    return targets


def remote_key(site, target="prod"):
    return site if target == "prod" else site + "@" + target


def remote_command(site, action, timeout=60, target="prod", **kwargs):
    if target != "prod":
        kwargs["env"] = {**kwargs.get("env", os.environ), "VNS_TARGET": target}
    try:
        result = subprocess.run(
            [*harbor_argv("remote_site.sh"), site, action],
            capture_output=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        app.logger.error("Remote %s failed for %s: %s", action, site, exc)
        abort(502, "Could not connect to the remote server")
    if result.returncode:
        message = result.stderr or result.stdout
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if action == "config" and result.stdout:
            try:
                details = json.loads(result.stdout)
                if isinstance(details, dict) and isinstance(details.get("error"), str):
                    message = details["error"]
            except ValueError:
                pass
        app.logger.error("Remote %s failed for %s: %s", action, site, message)
        abort(502, message.strip() or "Remote action failed")
    return result


def sync_prod_binding(site, target, binding):
    """A binding changed on the server is saved here too, also in scripts made before publish read it from the server."""
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", binding):
        return
    if site_migrated(site):
        save_migrated_setting(site, target, "BINDING", binding)
        return
    path = os.path.join(target_folder(os.path.join(WEB_ROOT, site), target), "publish_prod.sh")
    try:
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        saved = re.search(r"^PROD_BINDING=(\S*)", text, re.M)
        if not saved or saved.group(1).strip("'\"") == binding:
            return
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text[:saved.start(1)] + "'" + binding + "'" + text[saved.end(1):])
    except OSError as exc:
        app.logger.warning("Could not save the binding of %s: %s", site, exc)


WINDOWS_HTTPS = {}


def keep_alive_states():
    """How environments on other servers answered keep-alive, while the answer is recent: three runs, at least 30 minutes"""
    try:
        interval = _keep_alive_module.load()["interval_minutes"]
    except (OSError, ValueError):
        interval = 5
    return _keep_alive_module.environment_states(max_age=max(1800, interval * 180))


def probe_url(url):
    """Request a site's address the way a visitor does. Returns how it went: answered (also a redirect or a
    login), http-error, refused, certificate or unreachable, and the error."""
    request = urllib.request.Request(url, headers={"User-Agent": "VNS-Admin", "Cache-Control": "no-cache"})
    opener = urllib.request.build_opener(_keep_alive_module.NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            response.read(1)
        return "answered", ""
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (301, 302, 303, 307, 308, 401, 403):
            return "answered", ""
        return "http-error", f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return "certificate", str(reason)
        if isinstance(reason, ConnectionRefusedError):
            return "refused", "Connection refused"
        return "unreachable", str(reason)
    except (OSError, ValueError) as exc:
        return ("certificate" if isinstance(exc, ssl.SSLError) else "unreachable"), str(exc)


def save_migrated_setting(site, target, key, value):
    """A setting of a migrated site's environment, in Harbor's copy (its truth), when it differs"""
    from harbor.core.system import System
    from harbor.environments import settings
    root = os.path.join(WEB_ROOT, site)
    try:
        if (settings.values(System(), root, target) or {}).get(key) != value:
            settings.update(System(), root, target, {key: value})
    except (OSError, settings.SettingsError) as exc:
        app.logger.warning("Could not save %s of %s %s: %s", key, site, target, exc)


def windows_probe(site, target, binding, saved):
    """Whether a Windows environment runs, and on HTTPS. IIS has no SSH, so its address is requested like a
    visitor would, at most once a minute: HTTPS with a valid certificate first, then HTTP. A clear answer
    about HTTPS is saved in its publish_prod.sh, so the next publish sets the redirect and the URL to match.
    No answer keeps what is saved, and leaves the state to keep-alive."""
    key = (site, target)
    cached = WINDOWS_HTTPS.get(key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    result = {"https": saved, "state": "", "error": ""}
    if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", binding or ""):
        outcome, error = probe_url(f"https://{binding}/")
        if outcome in ("answered", "http-error"):
            result = {"https": True, "state": "active" if outcome == "answered" else "failed", "error": error}
        elif outcome in ("refused", "certificate"):
            plain, plain_error = probe_url(f"http://{binding}/")
            result = {"https": False, "state": {"answered": "active", "unreachable": ""}.get(plain, "failed"),
                      "error": plain_error}
    result["checked"] = int(time.time())
    WINDOWS_HTTPS[key] = (time.time(), result)
    if result["https"] != saved and site_migrated(site):
        save_migrated_setting(site, target, "HTTPS", "y" if result["https"] else "n")
    elif result["https"] != saved:
        path = os.path.join(target_folder(os.path.join(WEB_ROOT, site), target), "publish_prod.sh")
        try:
            with open(path, encoding="utf-8") as stream:
                text = stream.read()
            updated = re.sub(r"^PROD_HTTPS=\S*", "PROD_HTTPS=" + ("y" if result["https"] else "n"), text, count=1, flags=re.M)
            if updated != text:
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(updated)
                app.logger.info("%s %s now answers on %s", site, target, "HTTPS" if result["https"] else "HTTP")
        except OSError as exc:
            app.logger.warning("Could not save HTTPS of %s %s: %s", site, target, exc)
    return result


WINDOWS_DETAILS = {}
WINDOWS_DETAILS_LOCK = threading.Lock()


def windows_details(site, target, environment):
    """The .NET and database of a Windows environment, from its runtimeconfig.json and appsettings files.
    They are read over SMB in the background, at most every 10 minutes, so the page shows what was read last
    and never waits for the share."""
    key = (site, target)
    with WINDOWS_DETAILS_LOCK:
        checked, value, running = WINDOWS_DETAILS.get(key, (0, {}, False))
        if running or time.time() - checked < 600:
            return value
        WINDOWS_DETAILS[key] = (checked, value, True)

    def read():
        found = value
        try:
            with app.app_context():
                answer = windows_files(site, target, {"action": "details", "environment": environment})
            found = {key: answer[key] for key in answer if key != "ok"}
        except Exception as exc:  # noqa: BLE001 - a share that does not answer only leaves the details unknown
            app.logger.warning("Could not read the details of %s %s: %s", site, target, exc)
        with WINDOWS_DETAILS_LOCK:
            WINDOWS_DETAILS[key] = (time.time(), found, False)

    threading.Thread(target=read, daemon=True).start()
    return value


def remote_status(site, target="prod"):
    key = remote_key(site, target)
    cached = REMOTE_STATUS.get(key)
    if cached and time.time() - cached[0] < 15:
        return cached[1]
    try:
        result = remote_command(site, "status", timeout=20, target=target, text=True)
        status = json.loads(result.stdout)
        if not isinstance(status, dict) or not {"unit", "state", "scheme", "https", "exists"} <= status.keys():
            raise ValueError("Invalid remote status response")
        if not status.get("binding"):
            status.pop("binding", None)
        else:
            sync_prod_binding(site, target, status["binding"])
    except HTTPException as exc:
        status = {"https": None, "state": "unknown", "remote_error": exc.description}
    except (ValueError, TypeError) as exc:
        app.logger.error("Invalid remote status for %s: %s", site, exc)
        status = {"https": None, "state": "unknown", "remote_error": "Invalid remote status response"}
    REMOTE_STATUS[key] = (time.time(), status)
    return status


def service_environment(unit):
    try:
        result = subprocess.run(
            ["systemctl", "show", service_name(unit) + ".service", "--property=Environment", "--value"],
            capture_output=True, text=True, timeout=10, check=True)
        variables = dict(item.split("=", 1) for item in shlex.split(result.stdout) if "=" in item)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("Could not read environment for %s: %s", unit, exc)
        return "Unknown"
    return variables.get("DOTNET_ENVIRONMENT") or variables.get("ASPNETCORE_ENVIRONMENT", "")


def server_aliases(config_text, binding=""):
    aliases = []
    seen = {binding.lower()} if binding else set()
    for line in config_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2 or fields[0].lower() != "serveralias":
            continue
        for alias in fields[1:]:
            normalized = alias.lower()
            if normalized not in seen:
                seen.add(normalized)
                aliases.append(alias)
    return aliases


def environment_info(key, label, unit, binding, port, path, states, **extra):
    service = bool(unit) and unit_of(unit)
    info = {"key": key, "label": label, "unit": unit if service else "", "binding": binding, "port": port, "path": path,
            "state": states.get(unit, "") if service else ""}
    info.update(extra)
    if unit:
        config_text = read_text(os.path.join(APACHE_SITES, unit + ".conf"))
        info["aliases"] = server_aliases(config_text, binding)
    info["environment"] = service_environment(unit) if service else ""
    info.update(https_status(unit=unit))
    if service:
        service_text = read_text(os.path.join(SERVICE_DIR, service_name(unit) + ".service"))
        match = re.search(r'^WorkingDirectory=(.+)$', service_text, re.M)
        if match:
            working = os.path.realpath(match.group(1).strip().strip('"'))
            if working.startswith(WEB_ROOT + os.sep) and os.path.isdir(working):
                info["publish_path"] = rel(working)
                started = re.search(r"([^\s/]+)\.dll\b", service_text)
                info["dotnet"] = _database_module.dotnet_runtime(working, started.group(1) if started else "")
                info["editable_configs"] = [filename for filename in ("appsettings.json",)
                                            if os.path.isfile(os.path.join(working, filename))]
                try:
                    if info["environment"] == "Unknown":
                        raise ValueError("Service environment unavailable")
                    settings = subprocess.run(
                        ["systemctl", "show", service_name(unit) + ".service", "--property=Environment", "--value"],
                        capture_output=True, text=True, timeout=10, check=True)
                    variables = dict(item.split("=", 1) for item in shlex.split(settings.stdout) if "=" in item)
                    info.update(_database_module.database_details(working, info["environment"], variables))
                except (OSError, subprocess.SubprocessError, ValueError):
                    app.logger.warning("Could not read database configuration for %s", unit)
                    info["database"] = "Unknown"
    return info


def site_units(name):
    """Unit names the admin may control for a site: its own service and a local production service."""
    units = [name]
    root = os.path.join(WEB_ROOT, name)
    for target in target_ids(root):
        prod = parse_prod(root, target)
        if prod and prod["PROD_LOCAL_PATH"] and NAME_RE.match(prod["PROD_FOLDER"] or ""):
            units.append(prod["PROD_FOLDER"])
    return units


def umbraco_version(root, projects):
    """Version of the Umbraco.Cms package from the csproj (or Directory.Packages.props)."""
    files = projects + [os.path.join(root, "Directory.Packages.props"), os.path.join(root, "Directory.Build.props")]
    for path in files:
        text = read_text(path)
        for tag in re.findall(r"<PackageReference\b[^>]*>|<PackageVersion\b[^>]*>", text):
            if not re.search(r'(?:Include|Update)\s*=\s*"Umbraco\.Cms"', tag):
                continue
            match = re.search(r'Version\s*=\s*"([^"]+)"', tag)
            if match:
                return match.group(1)
        match = re.search(r'Include\s*=\s*"Umbraco\.Cms"[^>]*>\s*<Version>([^<]+)</Version>', text)
        if match:
            return match.group(1).strip()
    return ""


def package_references(projects):
    packages = {}
    errors = []
    for path in projects:
        try:
            project = ET.parse(path).getroot()
        except ET.ParseError as exc:
            errors.append("%s: %s" % (os.path.basename(path), exc))
            continue
        for element in project.iter():
            if element.tag.rsplit("}", 1)[-1] != "PackageReference":
                continue
            name = element.attrib.get("Include") or element.attrib.get("Update")
            if not name:
                continue
            version = element.attrib.get("Version", "")
            if not version:
                version_node = next((child for child in element if child.tag.rsplit("}", 1)[-1] == "Version"), None)
                version = (version_node.text or "").strip() if version_node is not None else ""
            if name not in packages or version:
                packages[name] = version
    return [{"name": name, "version": version} for name, version in sorted(packages.items(), key=lambda item: item[0].lower())], errors


def published_project_info(root, service_text):
    candidates = []
    match = re.search(r"^ExecStart=(.+)$", service_text, re.M)
    if match:
        for argument in shlex.split(match.group(1)):
            if argument.endswith(".dll") and os.path.isabs(argument):
                candidates.append(argument[:-4] + ".deps.json")
    if not candidates:
        candidates = sorted(glob.glob(os.path.join(root, "*.deps.json")))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as stream:
                data = json.load(stream)
            libraries = data.get("libraries", {})
            version = next((name.split("/", 1)[1] for name in libraries
                            if name.startswith("Umbraco.Cms/")), "")
            return {"kind": "umbraco" if version else "csharp", "umbraco_version": version}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            app.logger.warning("Could not read published project metadata %s: %s", path, exc)
    return None


def setup_kind(root):
    """What a folder that was put in /var/www by hand holds, as setup_site.sh sees it"""
    projects = glob.glob(os.path.join(root, "*.csproj"))
    if projects:
        return "umbraco project" if any("Umbraco.Cms" in read_text(path) for path in projects) else "C# project"
    for folder in (root, os.path.join(root, "_publish")):
        for runtime in glob.glob(os.path.join(folder, "*.runtimeconfig.json")):
            name = os.path.basename(runtime)[:-len(".runtimeconfig.json")]
            if os.path.isfile(os.path.join(folder, name + ".dll")):
                return "umbraco build" if '"Umbraco.Cms/' in read_text(os.path.join(folder, name + ".deps.json")) else "C# build"
    if os.path.isfile(os.path.join(root, "app", "app.py")):
        return "python"
    return "html"


# A C# project's own scripts: building, its packages, environments made from it, its environment
PROJECT_SCRIPTS = {"publish.sh", "publish_local.sh", "outdated.sh", "create_prod.sh", "change_env.sh"}


def read_local_publish(name):
    try:
        with open(os.path.join(HARBOR_SITES, name, "local-publish.json"), encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return None
    return {"binding": str(found.get("binding", ""))} if isinstance(found, dict) else None


def harbor_package(root, projects):
    if projects:
        return any(re.search(r'<PackageReference\s+Include="VNS\.Harbor"', read_text(path), re.I) for path in projects)
    return any(os.path.isfile(os.path.join(root, folder, "VNS.Harbor.dll")) for folder in ("", "_publish"))


def site_info(name, states):
    root = os.path.join(WEB_ROOT, name)
    conf = os.path.join(APACHE_SITES, name + ".conf")
    service_text = read_text(os.path.join(SERVICE_DIR, service_name(name) + ".service"))
    conf_text = read_text(conf)

    binding = ""
    match = re.search(r"^\s*ServerName\s+(\S+)", conf_text, re.M)
    if match:
        binding = match.group(1)
    port = ""
    match = re.search(r"(?:localhost|127\.0\.0\.1):(\d+)", service_text)
    if match:
        port = match.group(1)
    aspnet = re.search(r"ASPNETCORE_ENVIRONMENT=(\S+)", service_text)

    projects = [path for path in glob.glob(os.path.join(root, "*.csproj")) if os.path.isfile(path)]
    umbraco = ""
    packages = []
    package_errors = []
    if projects:
        kind = "umbraco" if any("Umbraco.Cms" in read_text(p) for p in projects) else "csharp"
        packages, package_errors = package_references(projects)
        if kind == "umbraco":
            umbraco = umbraco_version(root, projects)
    elif os.path.isfile(os.path.join(root, "app", "app.py")):
        kind = "python"
    else:
        published = published_project_info(root, service_text)
        kind = published["kind"] if published else "html"
        umbraco = published["umbraco_version"] if published else ""

    scripts = []
    scripts_dir = os.path.join(root, "scripts")
    if site_migrated(name):
        scripts = migrated_scripts(name)
    elif os.path.isdir(scripts_dir):
        scripts = sorted(f for f in os.listdir(scripts_dir) if SCRIPT_RE.match(f) and os.path.isfile(os.path.join(scripts_dir, f)))
    # Its own scripts where only root can write them, as a publish from a machine puts them
    owned = os.path.join(HARBOR_SITES, name, "scripts")
    if os.path.isdir(owned):
        scripts = sorted(set(scripts) | {f for f in os.listdir(owned) if SCRIPT_RE.match(f) and os.path.isfile(os.path.join(owned, f))})
    if not projects:
        # A build with no project here (published from a machine, or put here by hand) is run as it is: a project's
        # tools do nothing for it, also where they were recorded for it before
        scripts = [script for script in scripts if script not in PROJECT_SCRIPTS]

    first_label = "Test" if not aspnet or aspnet.group(1) != "Production" else "Production"
    if name == ADMIN_SITE:
        first_label = "Admin"
    environments = [environment_info("main", first_label if kind in ("csharp", "umbraco") else "Live", name, binding, port, name, states,
                                     environment=aspnet.group(1) if aspnet else "", apache=os.path.exists(os.path.join(APACHE_ENABLED, name + ".conf")))]

    for target in target_ids(root):
        prod = parse_prod(root, target)
        if not prod:
            continue
        key = "prod" if target == "prod" else "target:" + target
        target_files = target_scripts(name, target, kind)
        scripts = sorted(set(scripts) | set(target_files))
        details = {"target": target, "scripts": target_files}
        local = prod["PROD_LOCAL_PATH"]
        if prod.get("PROD_TARGET") == "windows":
            share = prod["PROD_WINDOWS_SHARE"]
            probe = windows_probe(name, target, prod["PROD_BINDING"], prod["PROD_HTTPS"] != "n")
            secure = probe["https"]
            # The admin's own request, or else what keep-alive saw last
            answer = ({"state": probe["state"], "error": probe["error"], "checked": probe["checked"]} if probe["state"]
                      else keep_alive_states().get(f"{name}/{target}", {}))
            environments.append({**details, "key": key, "label": "Production", "mode": "windows", "unit": "", "state": answer.get("state", ""),
                                 "state_error": answer.get("error", ""), "state_checked": answer.get("checked"), "binding": prod["PROD_BINDING"],
                                 "port": "", "path": "", "host": share[2:].split("/", 1)[0], "user": prod["PROD_SMB_USER"], "folder": share,
                                 "environment": prod["PROD_ENVIRONMENT"] or "Unknown", "scheme": "https" if secure else "http", "https": secure,
                                 "database": "Unknown",
                                 **windows_details(name, target, prod["PROD_ENVIRONMENT"] or "Production")})
        elif local:
            full = os.path.realpath(local)
            inside = full == WEB_ROOT or full.startswith(WEB_ROOT + os.sep)
            unit = prod["PROD_FOLDER"]
            # An environment with its own folder under the web root is a separate site that was moved here
            separate = inside and target != "prod" and os.path.relpath(full, WEB_ROOT).split(os.sep)[0] != name
            environments.append(environment_info(key, "Production", unit, prod["PROD_BINDING"] or conf_binding(unit), prod["PROD_PORT_HTTP"] or service_port(unit),
                                                 rel(full) if inside else "", states, mode="local", exists=os.path.isdir(full), separate=separate,
                                                 custom_scripts=listed_scripts(os.path.join(full, "scripts")) if inside else [],
                                                 custom_scripts_safe=scripts_safe(os.path.join(full, "scripts")) if inside else True, **details))
        else:
            environments.append({**details, "key": key, "label": "Production", "mode": "remote", "unit": "", "state": "", "binding": prod["PROD_BINDING"],
                                 "port": prod["PROD_PORT_HTTP"], "path": "", "host": prod["PROD_IP"], "user": prod["PROD_USER"],
                                 "ssh_port": prod["PROD_SSH_PORT"], "folder": prod["PROD_FOLDER"], "environment": "Unknown",
                                 **remote_status(name, target)})

    updates = ""
    package_updates = []
    if projects:
        try:
            current = _publish_module.fingerprint(root)
        except (OSError, ValueError) as exc:
            app.logger.warning("Could not read publish status for %s: %s", name, exc)
            current = None
        if current is not None:
            try:
                details = _publish_module.updates_details(root, current)
                updates = details["status"]
                package_updates = details["packages"]
            except (OSError, ValueError):
                app.logger.warning("Could not read package update status for %s", name)
                updates = "error"
        for environment in environments:
            scope = target_scope(environment["target"]) if "target" in environment else "test"
            environment["publish_status"] = "unknown"
            if current is not None:
                try:
                    environment["publish_status"] = _publish_module.status(root, scope, current)
                except (OSError, ValueError) as exc:
                    app.logger.warning("Could not read %s publish status for %s: %s", scope, name, exc)

    data_sync = {}
    if kind == "umbraco":
        try:
            data_sync = _data_sync_module.load(root)
        except ValueError as exc:
            app.logger.warning("Could not read data sync settings for %s: %s", name, exc)
    for environment in environments:
        site_id = environment.get("target", "main")
        environment["data_sync"] = _data_sync_module.clean(data_sync.get(site_id), site_id)
    # An environment moved from Windows and not taken over yet: Refresh from Windows and Take over on its card
    try:
        with open(os.path.join(HARBOR_SITES, name, "windows-move.json"), encoding="utf-8") as stream:
            moves = json.load(stream)
    except (OSError, ValueError):
        moves = {}
    for environment in environments:
        moved = moves.get(environment.get("target", "")) if isinstance(moves, dict) else None
        if isinstance(moved, dict):
            environment["windows_move"] = {"from": moved.get("windows", ""), "server": moved.get("server", "")}
    for environment in environments:
        environment["display_binding"] = display_dns_name(environment.get("binding", ""))
        environment["display_aliases"] = [display_dns_name(alias) for alias in environment.get("aliases", [])]
    # A folder put in /var/www by hand: no Apache config and no service. Apache's own default site is not one.
    # A C# site with an Apache config but no service was only half set up (its build may have failed): Set up site finishes it.
    not_set_up = name != ADMIN_SITE and not service_text and (
        (not os.path.exists(conf) and not (name == "html" and os.path.exists(os.path.join(APACHE_SITES, "000-default.conf"))))
        or kind in ("csharp", "umbraco"))
    return {
        "name": name,
        "display_name": display_dns_name(name),
        "kind": kind,
        "has_project": bool(projects),
        # Imported from a live Windows server, and not taken over yet: Refresh from Windows and Take over
        "windows_source": os.path.isfile(os.path.join(HARBOR_SITES, name, "windows-source.json")),
        "project": os.path.splitext(os.path.basename(projects[0]))[0] if len(projects) == 1 else "",
        "managed_by": managed_by(name),
        # Published from a developer's machine (harbor site receive-build): its binding, for the script shown again
        "local_publish": read_local_publish(name),
        # The VNS.Harbor package, which the Backend button logs in with: in the project, or in the build of a site
        # without one here (an environment managed from another server)
        "harbor_package": harbor_package(root, projects),
        "updates": updates,
        "package_updates": package_updates,
        "umbraco_version": umbraco,
        # The .NET the main site runs, or else the one its project targets
        "dotnet": (environments[0].get("dotnet") if environments else "") or (
            _database_module.project_dotnet(root) if kind in ("csharp", "umbraco") else ""),
        "packages": packages,
        "package_errors": package_errors,
        "scripts": scripts,
        # Its scripts folder root's alone, or its scripts are not run (an administrator can make it so)
        "custom_scripts_safe": scripts_safe(os.path.join(root, "scripts")),
        "admin": name == ADMIN_SITE,
        "answers_any_name": default_site_name() == name,
        "not_set_up": not_set_up,
        "setup_kind": setup_kind(root) if not_set_up else "",
        "environments": environments,
    }


def service_states(names):
    site_names = [n for n in names if unit_of(n)]
    units = [service_name(n) + ".service" for n in site_names]
    if not units:
        return {}
    try:
        out = subprocess.run(["systemctl", "is-active", *units], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(zip(site_names, out))


def safe_path(relative):
    full = os.path.realpath(os.path.join(WEB_ROOT, (relative or "").lstrip("/")))
    if full != WEB_ROOT and not full.startswith(WEB_ROOT + os.sep):
        abort(400, "Path outside the web root")
    return full


def rel(full):
    return os.path.relpath(full, WEB_ROOT).replace(os.sep, "/") if full != WEB_ROOT else ""


def server_files():
    return request.path.startswith("/api/server-fs/")


def fs_root():
    root = (request.view_args or {}).get("root")
    if root is None:
        return WEB_ROOT
    full = safe_path(root)
    if full == WEB_ROOT or not os.path.isdir(full):
        abort(400, "Invalid site file root")
    return full


def fs_path(relative):
    full = fs_full_path(relative)
    if server_files():
        require_admin()
    elif not is_admin():
        site, env = file_env(full)
        action = (request.endpoint or "").replace("fs_", "")
        if site is None:
            abort(403, "You do not have access to this folder.")
        file_access(site, env, action, os.path.relpath(full, file_env_root(site, env)))
    return full


def fs_full_path(relative):
    if server_files():
        path = os.path.join("/", (relative or "").lstrip("/"))
        if request.endpoint in ("fs_rename", "fs_delete") and os.path.basename(path) not in ("", ".", ".."):
            return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        return os.path.realpath(path)
    if not (request.view_args or {}).get("root"):
        return safe_path(relative)
    root = fs_root()
    full = os.path.realpath(os.path.join(root, (relative or "").lstrip("/")))
    if full != root and not full.startswith(root + os.sep):
        abort(400, "Path outside the site file root")
    return full


def unit_env(site, unit):
    """The environment a local service belongs to: the main site, or the local environment run by it"""
    if unit == site:
        return "main"
    root = os.path.join(WEB_ROOT, site)
    for target in target_ids(root):
        if (parse_prod(root, target) or {}).get("PROD_FOLDER") == unit:
            return target
    return "main"


def file_env_root(site, env):
    if env == "main":
        return os.path.join(WEB_ROOT, site)
    return os.path.realpath((parse_prod(os.path.join(WEB_ROOT, site), env) or {}).get("PROD_LOCAL_PATH") or os.path.join(WEB_ROOT, site))


def file_env(full):
    """The site and environment a local path is in: the deepest folder that is a site's or a local environment's"""
    best, found = "", (None, None)
    for site in site_dirs():
        root = os.path.join(WEB_ROOT, site)
        candidates = [("main", root)] + [(target, os.path.realpath(path)) for target in target_ids(root)
                                         if (path := (parse_prod(root, target) or {}).get("PROD_LOCAL_PATH"))]
        for env, folder in candidates:
            if (full == folder or full.startswith(folder + os.sep)) and len(folder) > len(best):
                best, found = folder, (site, env)
    return found


# Folders a user never reaches, so they are not shown either: what is in scripts/ runs as root
USER_HIDDEN_FOLDERS = ("scripts", ".vns-publish")
# Harbor's own files where a site runs, wherever they are in its folder: vns-harbor.json holds the site's token and login
# key for Harbor (the VNS.Harbor package), .harbor-published what the last publish put there
USER_HIDDEN_FILES = ("vns-harbor.json", ".harbor-published")


def hidden_from_user(path):
    """Whether a path, relative to an environment's folder, is in a folder or is a file only administrators see"""
    parts = (path or "").replace("\\", "/").strip("/").split("/")
    return not is_admin() and (parts[0] in USER_HIDDEN_FOLDERS or parts[-1].lower() in USER_HIDDEN_FILES)


def names_hidden_file():
    """A new name in the request (New file, New folder, Rename, Upload) that is one of Harbor's own files"""
    names = [upload.filename or "" for upload in request.files.getlist("files")] if request.files else []
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict) and isinstance(body.get("name"), str):
        names.append(body["name"])
    return any(os.path.basename(name.replace("\\", "/")).strip().lower() in USER_HIDDEN_FILES for name in names)


def file_access(site, env, action, path):
    """A user's file action on an environment: Files there, or appsettings*.json alone with Edit appsettings.
    scripts/ is never theirs: what is there runs as root."""
    if action in ("save", "upload", "delete", "rename", "mkdir", "newfile", "create"):
        audit("Files %s on %s · %s: %s" % (action, site, env, path or "/"))
    if is_admin():
        return
    path = (path or "").replace("\\", "/").strip("/")
    if hidden_from_user(path) or ".." in path.split("/") or names_hidden_file():
        abort(403, "You do not have access to this folder.")
    if access.can(me(), site, env, "files"):
        return
    name = path.rsplit("/", 1)[-1].lower()
    if action in ("read", "save") and re.fullmatch(r"appsettings(\.[a-z0-9_-]+)?\.json", name) and access.can(me(), site, env, "appsettings"):
        return
    abort(403, "You do not have access to these files.")


def fs_relative(full):
    if server_files():
        return full.lstrip("/")
    if not (request.view_args or {}).get("root"):
        return rel(full)
    root = fs_root()
    return os.path.relpath(full, root).replace(os.sep, "/") if full != root else ""


def is_site_root(full):
    return full == "/" or full == WEB_ROOT or os.path.dirname(full) == WEB_ROOT or (not server_files() and full == fs_root())


def own_like_parent(path):
    parent = os.stat(os.path.dirname(path))
    os.chown(path, parent.st_uid, parent.st_gid)


# ----------------------------------------------------------------- sites API

def local_sa_password(data, adding=False):
    """sa on SQL Server here (or postgres on PostgreSQL here), without a password typed: the password Harbor keeps (it
    set the server up). An administrator's alone: it may do anything on the server, so a user types the password of
    the login they use (1.0.82: anyone who could add connections was given it)"""
    if not is_admin():
        return
    local = str(data.get("host", "")).strip().strip("[]").lower() in ("localhost", "127.0.0.1", "::1", ".", socket.gethostname().lower())
    if data.get("engine") == "postgres":
        if (adding or data.get("harbor_password")) and not data.get("password") and str(data.get("username", "")).strip().lower() == "postgres" and local:
            try:
                with open("/etc/harbor/secrets/postgres.json", encoding="utf-8") as stream:
                    data["password"] = json.load(stream).get("postgres_password", "")
            except (OSError, ValueError):
                abort(400, "Harbor has no postgres password for PostgreSQL here: type it.")
        return
    if (adding or data.get("harbor_password")) and not data.get("password") and str(data.get("username", "")).strip().lower() == "sa" and local:
        try:
            with open("/etc/harbor/secrets/sql.json", encoding="utf-8") as stream:
                data["password"] = json.load(stream).get("sa_password", "")
        except (OSError, ValueError):
            abort(400, "Harbor has no sa password for SQL Server here: type it.")


@app.route("/api/sql/connections/test", methods=["POST"])
def api_sql_connection_test():
    """A connection as typed, tried without saving it: whether it logs in, and what SQL Server says when it does not"""
    user = me()
    if not (access.is_admin(user) or user.get("manage_connections")):
        abort(403, "The SQL connections are locked for you.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a connection object")
    # Editing one: its engine is the saved one's
    if data.get("id"):
        try:
            data["engine"] = _sql_module.engine(_sql_module.connection(SQL_CONNECTIONS, data["id"]))
        except ValueError:
            pass
    kind = "postgres" if data.get("engine") == "postgres" else "mssql"
    if not data.get("id") and kind not in access.connection_engines(user):
        abort(403, f"You may not add connections to {'PostgreSQL' if kind == 'postgres' else 'Microsoft SQL Server'}.")
    if not _sql_module.tools_path(kind):
        return error(f"Install the {'PostgreSQL' if kind == 'postgres' else 'Microsoft SQL Server'} tools on the admin server first.", 409)
    source = "Harbor's own " + ("postgres" if kind == "postgres" else "sa") if data.get("harbor_password") else "typed" if data.get("password") else "saved"
    local_sa_password(data, True)
    # Editing one with the password left blank: the saved one
    if data.get("id") and not data.get("password"):
        try:
            data["password"] = _sql_module.connection(SQL_CONNECTIONS, data["id"])["password"]
        except ValueError:
            pass
    settings = {key: data.get(key) for key in ("name", "host", "username", "password")} | {
        "name": data.get("name") or "Test", "port": int(data.get("port") or _sql_module.DEFAULT_PORTS[kind]),
        "trust_certificate": data.get("trust_certificate") is True, "engine": kind}
    try:
        rows = _sql_module.databases(settings)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return jsonify(ok=False, error=str(_sql_module.connect_error(settings, exc, source)))
    user_databases = [row["name"] for row in rows if row.get("id", 0) > 4]
    return jsonify(ok=True, databases=len(user_databases))


@app.route("/api/sql/connections", methods=["GET", "POST", "PUT", "DELETE"])
def api_sql_connections():
    try:
        # Which engines' tools the admin server has: a connection needs its own
        tools = {kind: bool(_sql_module.tools_path(kind)) for kind in _sql_module.ENGINES}
        tools_available = any(tools.values())
        if request.method in ("POST", "PUT"):
            sent = request.get_json(silent=True) or {}
            kind = sent.get("engine") if request.method == "POST" else None
            if request.method == "PUT" and isinstance(sent.get("id"), str):
                try:
                    kind = _sql_module.engine(_sql_module.connection(SQL_CONNECTIONS, sent["id"]))
                except ValueError:
                    kind = None
            kind = "postgres" if kind == "postgres" else "mssql"
            # A user adds connections only to the SQL Servers they were given (May add SQL connections)
            if request.method == "POST" and kind not in access.connection_engines(me()):
                abort(403, f"You may not add connections to {'PostgreSQL' if kind == 'postgres' else 'Microsoft SQL Server'}.")
            if not tools[kind]:
                return error(f"Install the {'PostgreSQL' if kind == 'postgres' else 'Microsoft SQL Server'} tools on the admin server first.", 409)
        user = me()
        if not access.has_sql(user):
            abort(403, "You do not have access to SQL Server.")
        if request.method != "GET" and not (access.is_admin(user) or user.get("manage_connections")):
            abort(403, "The SQL connections are locked for you.")
        # A user sees the connections they were given, and marks which they may change
        def listed():
            items = _sql_module.public_connections(SQL_CONNECTIONS)
            if not access.is_admin(user):
                items = [item for item in items if item["id"] in (user.get("connections") or {})]
            # An administrator sees who added each one
            # An administrator sees who added each one, and those users added apart from the others
            def who(item):
                found = maker("connections", item["id"]) or {}
                return {"added_by": found.get("name", ""), "added_by_user": bool(found.get("user"))}
            # The login, backup folder and SSH only to an administrator and to the user who added the connection; the
            # address and port go to all who have it, for its connection string (Connect…)
            hidden = ("username", "backup_directory", "ssh_enabled", "ssh_host", "ssh_username", "ssh_port")
            return [dict({key: value for key, value in item.items() if access.is_admin(user) or owns_connection(item["id"]) or key not in hidden},
                         locked=not owns_connection(item["id"]), **(who(item) if access.is_admin(user) else {}))
                    for item in items]
        with SQL_LOCK:
            if request.method in ("POST", "PUT"):
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    abort(400, "Expected a connection object")
                source = "Harbor's own sa" if data.get("harbor_password") else "typed" if data.get("password") else "saved"
                local_sa_password(data, request.method == "POST")
                # It is tried before it is saved: a connection that cannot log in is not kept
                check = (lambda fields: _sql_module.check_login(fields, source)) if data.get("test", True) is not False else None
                if request.method == "PUT":
                    if not isinstance(data.get("id"), str) or not data["id"]:
                        abort(400, "Select a SQL connection")
                    if not owns_connection(data["id"]):
                        abort(403, "This connection is locked for you.")
                    identifier = _sql_module.edit_connection(SQL_CONNECTIONS, data, data["id"], check=check)
                    audit("Changed SQL connection " + str(data.get("name") or identifier))
                else:
                    if not access.is_admin(user):
                        # One to a server there is a connection to already (an administrator's, not given to them)
                        host = str(data.get("host", "")).strip().strip("[]").rstrip(".").casefold()
                        port = data.get("port") or _sql_module.DEFAULT_PORTS["postgres" if data.get("engine") == "postgres" else "mssql"]
                        if any(_sql_module.engine(item) == ("postgres" if data.get("engine") == "postgres" else "mssql")
                               and item["host"].strip("[]").rstrip(".").casefold() == host and item["port"] == port
                               for item in _sql_module.connections(SQL_CONNECTIONS)):
                            return error("There is a connection to this server already. An administrator can give it to you, with the databases you may use.")
                    identifier = _sql_module.add_connection(SQL_CONNECTIONS, data, check=check)
                    audit("Added SQL connection " + str(data.get("name") or identifier))
                    note_made("connections", identifier)
                    # What a user makes is theirs: every database on it, and every action
                    if not access.is_admin(user):
                        with CONFIG_LOCK:
                            user.setdefault("connections", {})[identifier] = {
                                "databases": ["*"], "actions": sorted(access.SQL_ACTION_NAMES), "owned": True}
                            save_config()
                return jsonify(connections=listed(), id=identifier, tools_available=tools_available, tools=tools)
            elif request.method == "DELETE":
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    abort(400, "Expected a connection object")
                if not owns_connection(data.get("id")):
                    abort(403, "This connection is locked for you.")
                _sql_module.remove_connection(SQL_CONNECTIONS, data.get("id"))
                audit("Removed SQL connection " + str(data.get("id")))
                forget_made_by("connections", data.get("id"))
            return jsonify(connections=listed(), tools_available=tools_available, tools=tools)
    except (OSError, ValueError) as exc:
        app.logger.error("SQL connection operation failed: %s", exc)
        return error(str(exc))


@app.route("/api/sql/<connection_id>/query", methods=["POST"])
def api_sql_query(connection_id):
    data = request.get_json(silent=True) or {}
    require_sql(connection_id, data.get("database") or None, "query")
    try:
        with SQL_LOCK:
            settings = _sql_module.connection(SQL_CONNECTIONS, connection_id)
        started = time.time()
        # Not an administrator: the script is kept to the database it was given for
        output = _sql_module.run_query(settings, data.get("database"), data.get("query"), sandbox=not access.is_admin(me()))
        results, messages = _sql_module.parse_results(output)
        return jsonify(results=results, messages=messages, seconds=round(time.time() - started, 2),
                       output=output.replace(_sql_module.COLUMN_SEPARATOR, " | "))
    except ValueError as exc:
        return error(str(exc))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # SQL errors are shown in the dialog, as they are what the person needs to fix the script
        return jsonify(error=unplaced(str(exc), locals().get("settings"))[-20000:]), 422


@app.route("/api/sql/<connection_id>/tables")
def api_sql_tables(connection_id):
    require_sql(connection_id, request.args.get("database", ""), "view")
    try:
        with SQL_LOCK:
            settings = _sql_module.connection(SQL_CONNECTIONS, connection_id)
        return jsonify(tables=_sql_module.tables(settings, request.args.get("database", "")))
    except ValueError as exc:
        return error(str(exc))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        app.logger.error("SQL tables failed: %s", exc)
        return error(str(exc), 502)


def sql_settings(connection_id):
    with SQL_LOCK:
        return _sql_module.connection(SQL_CONNECTIONS, connection_id)


SQL_SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
PG_SYSTEM_DATABASES = {"postgres", "template0", "template1"}


def unplaced(text, settings=None):
    """What a server said, for a user who is not told where the server is: its address as "the server" (an
    administrator, and the user who added the connection, are told)"""
    if not settings or is_admin() or owns_connection(settings.get("id")):
        return text
    host, port = str(settings.get("host", "")), str(settings.get("port", ""))
    for place in (f"{host}:{port}", f"{host},{port}", host):
        if place and place not in (":", ","):
            text = text.replace(place, "the server")
    return text


def sql_failure(exc, settings=None):
    """What SQL Server said, for the dialog that sent it: it is what the person needs to put right"""
    if isinstance(exc, ValueError):
        return error(unplaced(str(exc), settings))
    return jsonify(error=unplaced(_sql_design.readable(exc), settings)[-20000:]), 422


@app.route("/api/sql/<connection_id>/table", methods=["GET", "POST"])
def api_sql_table(connection_id):
    """A table's columns for the designer (GET), or a change to a table (POST: create, alter, rename, drop), as its SQL
    first (run false) and then run"""
    data = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    database = data.get("database", "")
    require_sql(connection_id, database, "view" if request.method == "GET" else "tables")
    try:
        settings = sql_settings(connection_id)
        # Microsoft SQL Server's designer, or PostgreSQL's
        design = _sql_design.for_connection(settings) or _sql_design
        engine = "PostgreSQL" if _sql_module.postgres(settings) else "SQL Server"
        if request.method == "GET":
            table = design.read_table(settings, database, data.get("schema") or design.DEFAULT_SCHEMA, data.get("table", "")) if data.get("table") else None
            # A new table is made in one of the database's schemas
            schemas = [] if table else design.schemas(settings, database)
            return jsonify(table=table, schemas=schemas, types=design.OFFERED, functions=design.FUNCTIONS,
                           length_types=design.LENGTH_TYPES, max_types=sorted(design.MAX_TYPES),
                           decimal_types=sorted(design.DECIMAL_TYPES), scale_types=sorted(design.SCALE_TYPES),
                           engine=_sql_module.engine(settings))
        action, schema, table = data.get("action"), data.get("schema") or design.DEFAULT_SCHEMA, data.get("table", "")
        # The server's own databases are not designed here: a table changed in them can break the server
        if str(database).strip().lower() in (PG_SYSTEM_DATABASES if _sql_module.postgres(settings) else SQL_SYSTEM_DATABASES):
            return error(f"{database} is one of {engine}'s own databases: its tables are not changed here.", 403)
        if action == "create":
            sql = design.plan_create(schema, table, data.get("columns"))
        elif action == "alter":
            sql = design.plan_alter(design.read_table(settings, database, schema, table), data.get("columns"))
        elif action == "rename":
            sql = design.plan_rename(schema, table, data.get("new_name", ""))
        elif action == "drop":
            if data.get("confirmation") != table:
                return error("Type the table's name to drop it.")
            sql = design.plan_drop(schema, table)
        else:
            return error("Unknown table action")
        if data.get("run") is True:
            design.apply(settings, sql, database)
            audit(f"SQL table {action}: {database}.{schema}.{table}")
        return jsonify(sql=sql, ran=data.get("run") is True)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return sql_failure(exc, locals().get("settings"))


@app.route("/api/sql/<connection_id>/logins", methods=["GET", "POST"])
def api_sql_logins(connection_id):
    """The SQL logins of the server and the databases they reach (GET), or a change to one (POST), its SQL first (with
    the password masked) and then run. An administrator's, and a user's who may change logins on the connection: they
    see the databases they may, and change only the logins that reach no other database, never a sysadmin"""
    require_sql(connection_id, None, "logins")
    try:
        settings = sql_settings(connection_id)
        # Microsoft SQL Server's logins, or PostgreSQL's roles
        design = _sql_design.for_connection(settings) or _sql_design
        every = design.logins(settings)
        found = scoped_logins(me(), connection_id, every)
        if request.method == "GET":
            # An administrator sees who made each login
            if access.is_admin(me()):
                for login in found["logins"]:
                    login["made_by"] = made_by("logins", login["name"], connection_id)
            return jsonify(**found)
        data = request.get_json(silent=True) or {}
        current = next((item for item in found["logins"] if item["name"] == data.get("name")), None)
        if current is not None and current.get("protected") and not access.is_admin(me()):
            return error("This login reaches databases that are not yours, or is a sysadmin: an administrator changes it.", 403)
        if data.get("action") in ("unsysadmin",) and not access.is_admin(me()):
            return error("Only an administrator takes sysadmin away.", 403)
        if not access.is_admin(me()) and any(not isinstance(item, dict) or item.get("database") not in found["databases"]
                                             for item in data.get("databases") or []):
            return error("You may give a login only the databases you have.", 403)
        if data.get("action") != "create" and current is None:
            return error("No such login")
        if data.get("action") == "create" and current is not None:
            return error("There is a login of that name")
        # One there already that is not theirs: the same words as any name that cannot be used, so a user learns
        # nothing of the logins they do not see (the server's own refusal named it)
        if data.get("action") == "create" and not access.is_admin(me()) and any(
                str(item.get("name", "")).lower() == str(data.get("name", "")).strip().lower() for item in every["logins"]):
            return error("That name cannot be used. Choose another.")
        if data.get("action") == "delete" and data.get("confirmation") != data.get("name"):
            return error("Type the login's name to delete it.")
        sql = design.plan_login(data.get("action"), data, settings, current)
        if data.get("run") is True:
            design.apply(settings, sql)
            audit(f"SQL login {data.get('action')}: {data.get('name')}")
            if data.get("action") == "create":
                note_made("logins", data.get("name"), connection_id)
            elif data.get("action") == "delete":
                forget_made_by("logins", data.get("name"), connection_id)
            # A login a user makes is theirs to see and change, also before it has a database
            if data.get("action") in ("create", "delete") and not access.is_admin(me()):
                change = access.record_made if data["action"] == "create" else access.forget_made
                grant(me()["id"], lambda target: change(target, "logins", data.get("name"), connection_id))
        return jsonify(sql=design.masked(sql, data.get("password")), ran=data.get("run") is True)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return sql_failure(exc, locals().get("settings"))


@app.route("/api/sql/<connection_id>")
def api_sql_status(connection_id):
    try:
        with SQL_LOCK:
            settings = _sql_module.connection(SQL_CONNECTIONS, connection_id)
        require_sql(connection_id, None, "view")
        databases = _sql_module.databases(settings)
        backups = _sql_module.backups(settings)
        # A user sees the databases they were given, and the backups of those
        if not is_admin():
            databases = [item for item in databases if access.can_sql(me(), connection_id, item.get("name"), "view")]
            backups = [item for item in backups if access.can_sql(me(), connection_id, item.get("database_name") or item.get("database"), "view")]
            # Where a backup lies on the server is the administrator's: a user cannot reach the file whatever else they
            # may, and acts on it by its id
            backups = [{key: value for key, value in item.items() if key != "path"} for item in backups]
        # An administrator sees who made each database
        made = {item["name"]: maker("databases", item["name"], connection_id) for item in databases if item.get("id", 5) > 4} if is_admin() else {}
        # Who owns each, and those users made, to be kept apart
        return jsonify(databases=databases, backups=backups, made_by={name: who["name"] for name, who in made.items() if who},
                       user_made=sorted(name for name, who in made.items() if who and who["user"]),
                       local_backup_access=_sql_module.local_backup_access(settings),
                       backup_file_access=_sql_module.backup_file_access(settings))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        app.logger.error("SQL overview failed: %s", exc)
        return error(str(exc), 502)


SYSTEM_UPDATE_LOCK = threading.Lock()
SYSTEM_UPDATE_CACHE = {}


@app.route("/api/system-update/run", methods=["GET", "POST"])
def api_run_system_update():
    require_admin()
    path = Path(DATA) / "system-update.json"
    log = Path(DATA) / "system-update.log"
    with SYSTEM_UPDATE_LOCK:
        state = json.loads(path.read_text()) if path.exists() else {"status": "idle"}
        if request.method == "GET":
            if state["status"] == "error":
                state["output"] = log.read_text(errors="replace")[-12000:] if log.exists() else ""
            elif state["status"] == "running" and log.exists():
                # The step it is on: the last line the update wrote, without terminal colours
                lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                         for line in log.read_text(errors="replace")[-4000:].splitlines()]
                state["step"] = next((line for line in reversed(lines) if line), "")[:160]
            return jsonify(state)
        if state["status"] == "running":
            abort(409, "A system update is already running")
        # A rollback runs as an update does: the admin starts again on the release it goes back to
        rollback = (request.get_json(silent=True) or {}).get("action") == "rollback"
        if rollback:
            earlier = rollback_release()
            if not earlier:
                abort(400, "There is no earlier release of Harbor to go back to.")
            command = ["/usr/bin/python3", HARBOR_COMMAND, "self", "rollback"]
        else:
            _, command, _ = build_command({"kind": "update_system"})
        helper = Path(LIB_DIR) / "system_update.py"
        if not helper.is_file():
            abort(400, "System update helper is missing")
        job = uuid.uuid4().hex
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                # The versions are kept with the job, so a page reloaded during the update still shows them
                state = {"id": job, "status": "running", "started": time.time(), "from": installed_version(),
                         "to": "v" + earlier["version"] if rollback else update_target(), "rollback": rollback}
                temporary = path.with_suffix(".tmp")
                with open(temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as status_stream:
                    json.dump(state, status_stream)
                os.replace(temporary, path)
                subprocess.Popen([sys.executable, str(helper), str(path), job, *command],
                                 cwd="/", stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        except OSError as exc:
            state.update(status="error", error=str(exc))
            path.write_text(json.dumps(state))
            app.logger.error("Could not start system update: %s", exc)
            abort(500, "Could not start system update")
        return jsonify(state)


def _update_source():
    """Where updates come from, and where the installed code came from (harbor/install/update.py): an installation
    from another repository (VNS 1.x) takes any release there as newer, as VNS Harbor starts its own versions"""
    try:
        from harbor.install import update as harbor_update
        return harbor_update.REPOSITORY, harbor_update.installed_repository(HARBOR_DIR)
    except (ImportError, OSError):
        return "schlagerdk/VNS.Harbor", ""


GITHUB_REPO, INSTALLED_FROM = _update_source()
UPDATE_DETAILS = {}


def installed_version():
    """The version in VERSION of the installed VNS, or empty for an installation from before versions"""
    text = read_text(os.path.join(HARBOR_DIR, "VERSION")).strip()
    return text if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text) else ""


def installed_revision():
    """The commit the installed code is: the release's (release.json), or .vns-version beside the admin (VNS 1.x)"""
    try:
        release = json.loads(read_text(os.path.join(HARBOR_DIR, "release.json")) or "{}")
    except ValueError:
        release = {}
    if release.get("revision"):
        return release["revision"]
    path = Path(DATA) / ".vns-version"
    return path.read_text().strip() if path.exists() else ""


def version_key(value):
    return tuple(int(part) for part in value.split("."))


def github_get(url, raw=False):
    request = urllib.request.Request(url, headers={"User-Agent": "VNS-Admin", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    return body if raw else json.loads(body)


GITHUB_RELEASES = {"etag": None, "releases": None, "blocked_until": 0.0}


def github_releases():
    """The published releases, newest first, or None when there are none or GitHub cannot be read. GitHub is
    asked with the ETag of the last answer: an unchanged answer (304) does not count against its rate limit."""
    now = time.time()
    if now < GITHUB_RELEASES["blocked_until"]:
        return GITHUB_RELEASES["releases"]
    headers = {"User-Agent": "VNS-Admin", "Accept": "application/vnd.github+json"}
    if GITHUB_RELEASES["etag"] and GITHUB_RELEASES["releases"] is not None:
        headers["If-None-Match"] = GITHUB_RELEASES["etag"]
    request = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", errors="replace"))
            etag = response.headers.get("ETag")
        releases = []
        for release in data if isinstance(data, list) else []:
            tag = str(release.get("tag_name", "")).lstrip("vV")
            if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tag):
                continue
            releases.append({"version": tag, "name": str(release.get("name") or "v" + tag)[:200],
                             "notes": str(release.get("body") or "")[:20000], "url": str(release.get("html_url", "")),
                             "date": str(release.get("published_at") or "")})
        releases.sort(key=lambda item: version_key(item["version"]), reverse=True)
        GITHUB_RELEASES.update(etag=etag, releases=releases)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
            reset = float(exc.headers.get("X-RateLimit-Reset") or 0)
            GITHUB_RELEASES["blocked_until"] = max(now + 60, min(reset, now + 3600))
            app.logger.warning("GitHub rate limit reached; the release check waits")
        elif exc.code != 304:
            app.logger.warning("Could not read the releases: %s", exc)
    except (OSError, ValueError) as exc:
        app.logger.warning("Could not read the releases: %s", exc)
    return GITHUB_RELEASES["releases"]


def commit_details(installed, latest):
    """Before the first release: the latest commit on main, and a link to every change since the installed one.
    Read from GitHub once per new commit."""
    if UPDATE_DETAILS.get("latest") == latest and UPDATE_DETAILS.get("installed") == installed:
        return UPDATE_DETAILS["details"]
    details = {"compare_url": f"https://github.com/{GITHUB_REPO}/compare/{installed}...{latest}" if installed
               else f"https://github.com/{GITHUB_REPO}/commits/main"}
    try:
        commit = github_get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{latest}")
        message = str(commit.get("commit", {}).get("message", ""))
        details["latest_commit"] = {"subject": message.split("\n", 1)[0][:300], "body": message.partition("\n")[2].strip()[:3000],
                                    "url": str(commit.get("html_url", "")), "date": str(commit.get("commit", {}).get("committer", {}).get("date", ""))}
    except (OSError, ValueError, AttributeError) as exc:
        app.logger.warning("Could not read the latest commit: %s", exc)
    UPDATE_DETAILS.update(latest=latest, installed=installed, details=details)
    return details


RELEASE_TAGS = {"checked_at": 0.0, "versions": None}


def release_versions():
    """The released versions, newest first, from the repository's tags. git does not count against GitHub's
    limit on API calls, so the version is known also when the release notes cannot be read. Kept a minute."""
    if RELEASE_TAGS["versions"] is not None and time.time() - RELEASE_TAGS["checked_at"] < 60:
        return RELEASE_TAGS["versions"]
    result = subprocess.run(["git", "ls-remote", "--tags", "--refs", f"https://github.com/{GITHUB_REPO}.git"],
                            capture_output=True, text=True, timeout=15, check=True)
    versions = [line.split("refs/tags/v", 1)[1] for line in result.stdout.splitlines() if "refs/tags/v" in line]
    versions = sorted((version for version in versions if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)),
                      key=version_key, reverse=True)
    RELEASE_TAGS.update(checked_at=time.time(), versions=versions)
    return versions


def update_target():
    """What an update installs: the newest release, or main before there is one"""
    try:
        versions = release_versions()
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("Could not read the releases: %s", exc)
        abort(502, "Could not read the releases on GitHub. Try again in a minute.")
    return "v" + versions[0] if versions else ""


@app.route("/api/system-update")
def api_system_update():
    require_admin()
    with SYSTEM_UPDATE_LOCK:
        if request.args.get("refresh") != "1" and SYSTEM_UPDATE_CACHE and time.monotonic() - SYSTEM_UPDATE_CACHE["checked_at"] < 60:
            return jsonify(**SYSTEM_UPDATE_CACHE["result"])
        try:
            installed = installed_revision()
            if installed and not re.fullmatch(r"[0-9a-f]{40}", installed):
                raise ValueError("Invalid installed version marker")
            version = installed_version()
            versions = release_versions()
            if versions:
                # The tags decide which release an update installs. The notes come from GitHub's API, which may be
                # out of reach (it allows few calls without a login): then the versions are shown without them.
                newest = versions[0]
                newer_versions = [item for item in versions if not version or INSTALLED_FROM != GITHUB_REPO
                                  or version_key(item) > version_key(version)]
                notes = {release["version"]: release for release in (github_releases() or [])}
                releases = [notes.get(item) or {"version": item, "name": "v" + item, "notes": "", "url": f"https://github.com/{GITHUB_REPO}/releases/tag/v{item}", "date": ""}
                            for item in newer_versions[:10]]
                response = {"status": "available" if newer_versions else "current", "mode": "release", "installed": installed,
                            "installed_version": version, "latest_version": newest, "releases": releases,
                            "notes_unavailable": bool(newer_versions) and not any(item in notes for item in newer_versions),
                            "compare_url": f"https://github.com/{GITHUB_REPO}/compare/v{version}...v{newest}" if version and newer_versions
                            else f"https://github.com/{GITHUB_REPO}/releases/tag/v{newest}"}
            elif INSTALLED_FROM != GITHUB_REPO:
                # The new repository has no release yet: there is nothing to update to (never its branch)
                response = {"status": "current", "mode": "release", "installed": installed, "installed_version": version,
                            "latest_version": version, "releases": []}
            else:
                # Before the first release: the newest commit on main (only in the repository it was installed from)
                result = subprocess.run(
                    ["git", "ls-remote", f"https://github.com/{GITHUB_REPO}.git", "HEAD"],
                    capture_output=True, text=True, timeout=15, check=True)
                fields = result.stdout.strip().split()
                if len(fields) != 2 or fields[1] != "HEAD" or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
                    raise ValueError("GitHub returned an invalid version")
                latest = fields[0]
                response = {"status": "unknown" if not installed else "available" if installed != latest else "current",
                            "mode": "commit", "installed": installed, "latest": latest, "installed_version": version}
                if response["status"] != "current":
                    response.update(commit_details(installed, latest))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            app.logger.error("System update check failed: %s", exc)
            response = {"status": "error", "error": "Could not check GitHub for updates. Try again in one minute."}
        response["rollback"] = rollback_release()
        SYSTEM_UPDATE_CACHE.update(checked_at=time.monotonic(), result=response)
        return jsonify(**response)


@app.route("/api/system-update/auto")
def api_system_update_auto():
    """Whether Harbor updates itself, when, and how its last run ended (harbor/install/autoupdate.py)"""
    require_admin()
    try:
        from harbor.core.system import System
        from harbor.install import autoupdate
        return jsonify(autoupdate.status(System()))
    except (ImportError, OSError) as exc:
        app.logger.error("Could not read the automatic updates: %s", exc)
        return jsonify(on=False, time="04:00", last=None, unavailable=True)


def rollback_release():
    """The release a rollback goes back to ({version, name}), or None: only one installed before the one in use, from
    VNS Harbor (harbor self rollback)"""
    try:
        from harbor.core.system import System
        from harbor.install import release
        earlier, _ = release.rollback_target(System())
    except (ImportError, OSError, ValueError):
        return None
    return {"version": earlier["version"], "name": earlier["name"]} if earlier else None


UPDATE_CHECK_LOCK = threading.Lock()
UPDATE_CHECK = {"running": False, "completed": 0, "total": 0, "errors": [], "pending": [], "results": {}}


def check_site_updates(name):
    try:
        result = _publish_module.check_updates(os.path.join(WEB_ROOT, name))
        with UPDATE_CHECK_LOCK:
            UPDATE_CHECK["results"][name] = result
    except (OSError, ValueError, RuntimeError) as exc:
        app.logger.error("Package update check failed for %s: %s", name, exc, exc_info=True)
        with UPDATE_CHECK_LOCK:
            UPDATE_CHECK["errors"].append(name)
    finally:
        with UPDATE_CHECK_LOCK:
            if name in UPDATE_CHECK["pending"]:
                UPDATE_CHECK["pending"].remove(name)
            UPDATE_CHECK["completed"] += 1


def check_all_updates(names):
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(check_site_updates, names))
    finally:
        with UPDATE_CHECK_LOCK:
            UPDATE_CHECK["running"] = False


@app.route("/api/package-updates", methods=["GET", "POST"])
def api_package_updates():
    if request.method == "POST":
        require_admin()
    with UPDATE_CHECK_LOCK:
        if request.method == "POST" and not UPDATE_CHECK["running"]:
            names = [name for name in site_dirs() if name != ADMIN_SITE
                     and any(os.path.isfile(path) for path in glob.glob(os.path.join(WEB_ROOT, name, "*.csproj")))]
            # The overview sends the sites it shows, so a search or filter limits the check to them
            wanted = (request.get_json(silent=True) or {}).get("sites")
            if wanted is not None:
                if not isinstance(wanted, list) or any(not isinstance(name, str) for name in wanted):
                    return error("Invalid site selection", 400)
                names = [name for name in names if name in set(wanted)]
                if not names:
                    return error("None of the selected sites has a C# project to check", 400)
            UPDATE_CHECK.update(running=True, completed=0, total=len(names), errors=[], pending=list(names), results={})
            try:
                threading.Thread(target=check_all_updates, args=(names,), daemon=True).start()
            except RuntimeError:
                UPDATE_CHECK["running"] = False
                app.logger.exception("Could not start package update checks")
                return error("Could not start package update checks", 500)
        return jsonify(**UPDATE_CHECK)

# The newest Umbraco on nuget.org, asked in the background and kept an hour (a failed ask is tried again in 5 minutes)
NEWEST_UMBRACO = {"versions": [], "checked": 0.0, "asking": False}
NEWEST_UMBRACO_LOCK = threading.Lock()


def umbraco_version_key(version):
    return [int(part) if part.isdigit() else 0 for part in version.split("+")[0].split(".")]


def newest_umbraco_of(versions, major):
    """The newest stable Umbraco of the given major version, or none"""
    fitting = [item for item in versions if isinstance(item, str) and VERSION_RE.fullmatch(item) and "-" not in item
               and item.split(".")[0] == major]
    return max(fitting, key=umbraco_version_key) if fitting else ""


def ask_newest_umbraco():
    versions = None
    try:
        request = urllib.request.Request("https://api.nuget.org/v3-flatcontainer/umbraco.cms/index.json", headers={"User-Agent": "VNS-Harbor"})
        with urllib.request.urlopen(request, timeout=10) as response:
            versions = json.loads(response.read(4 * 1024 * 1024).decode("utf-8")).get("versions")
    except (OSError, ValueError) as exc:
        app.logger.warning("Could not read Umbraco's versions on nuget.org: %s", exc)
    with NEWEST_UMBRACO_LOCK:
        NEWEST_UMBRACO["asking"] = False
        if isinstance(versions, list):
            NEWEST_UMBRACO.update(versions=versions, checked=time.time())
        else:
            NEWEST_UMBRACO["checked"] = time.time() - 3600 + 300


def default_umbraco_version():
    """The Umbraco version a new site gets when none is given: the newest on nuget.org of the major version harbor site
    create is made for (DEFAULT_UMBRACO_VERSION: its templates and the VNS.Harbor package follow that major), else that
    version itself. Never waits for nuget.org: the first ask gives the known version and asks in the background."""
    text = read_text(os.path.join(HARBOR_DIR, "harbor/sites/create.py")) or ""
    match = re.search(r'^DEFAULT_UMBRACO_VERSION = "([0-9][0-9A-Za-z.\-]*)"', text, re.M)
    known = match.group(1) if match else ""
    if not known:
        return ""
    with NEWEST_UMBRACO_LOCK:
        if not NEWEST_UMBRACO["asking"] and time.time() - NEWEST_UMBRACO["checked"] > 3600:
            NEWEST_UMBRACO["asking"] = True
            try:
                threading.Thread(target=ask_newest_umbraco, daemon=True).start()
            except RuntimeError:
                NEWEST_UMBRACO["asking"] = False
        versions = NEWEST_UMBRACO["versions"]
    newest = newest_umbraco_of(versions, known.split(".")[0])
    return newest if newest and umbraco_version_key(newest) > umbraco_version_key(known) else known


def parse_dotnet_templates(output):
    lines = output.splitlines()
    for index, line in enumerate(lines):
        columns = list(re.finditer(r"-{2,}", line))
        if len(columns) >= 4 and not line.replace("-", "").strip():
            break
    else:
        raise ValueError("Could not parse the dotnet new list table.")
    templates = []
    seen = set()
    for line in lines[index + 1:]:
        if not line.strip():
            continue
        values = [line[column.start():columns[position + 1].start() if position + 1 < len(columns) else None].strip()
                  for position, column in enumerate(columns)]
        name, short_names, language, tags = values[:4]
        if not name or not short_names:
            raise ValueError("Incomplete template entry in dotnet new list.")
        for short_name in short_names.split(","):
            short_name = short_name.strip()
            if not re.fullmatch(r"[A-Za-z0-9.][A-Za-z0-9._-]*", short_name):
                raise ValueError("Invalid template short name in dotnet new list.")
            if short_name not in seen:
                seen.add(short_name)
                templates.append({"name": name, "short_name": short_name, "language": language, "tags": tags})
    if not templates:
        raise ValueError("dotnet new list returned no installed templates.")
    return sorted(templates, key=lambda template: (template["short_name"].casefold(), template["short_name"]))


@app.route("/api/dotnet/templates")
def api_dotnet_templates():
    if not (is_admin() or me().get("create_sites")):
        abort(403, "You cannot create sites.")
    try:
        templates = installed_dotnet_templates()
        packages = installed_template_packages()
        return jsonify(templates=[template | packages.get(template["short_name"], {}) for template in templates])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("Could not list .NET templates: %s", exc)
        return error("Could not load installed .NET templates. Check the .NET SDK and server logs.", 502)


def dotnet_template_output(arguments):
    env = dict(os.environ, HOME="/root", DOTNET_CLI_UI_LANGUAGE="en-US",
               LANG="C.UTF-8", DOTNET_NOLOGO="1", TERM="dumb")
    root = dotnet_root_dir()
    if root:
        env["DOTNET_ROOT"] = root
    # From a folder that is always there: dotnet asks for its working folder as it starts, and the admin's own can be a
    # release folder an update has just replaced ("Could not load installed .NET templates")
    result = subprocess.run(["dotnet", "new", *arguments], capture_output=True, text=True, timeout=60, env=env, cwd="/")
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "dotnet template command failed")
    return result.stdout


def installed_dotnet_templates():
    templates = parse_dotnet_templates(dotnet_template_output(
        ["list", "--type", "project", "--language", "C#", "--tag", "Web"]))
    return [template for template in templates
            if "web" in template["tags"].casefold().split("/")
            and "C#" in template["language"]
            and not re.search(r"\b(?:library|worker|test|desktop|extension|plugin)\b",
                              template["tags"], re.I)
            and template["short_name"] not in ("blazorwasm", "blazorwasm-empty")]


def parse_template_packages(output):
    packages = {}
    package = ""
    version = ""
    in_templates = False
    for line in output.splitlines():
        if not line.strip():
            continue
        if re.fullmatch(r"   \S.*", line):
            package = line.strip()
            version = ""
            in_templates = False
        elif line.strip().startswith("Version:"):
            version = line.strip().removeprefix("Version:").strip()
        elif line.strip() == "Templates:":
            in_templates = True
        elif line.strip().endswith(":"):
            in_templates = False
        elif in_templates:
            match = re.search(r"\(([^()]+)\)(?:\s+.*)?$", line.strip())
            if not match:
                raise ValueError("Could not parse installed template package entries.")
            for short_name in match.group(1).split(","):
                short_name = short_name.strip()
                details = {"package": package, "installed_version": version}
                if short_name in packages and packages[short_name] != details:
                    raise ValueError("Multiple installed packages provide template: " + short_name)
                packages[short_name] = details
    return packages


def installed_template_packages():
    return parse_template_packages(dotnet_template_output(["uninstall"]))


@app.route("/api/sites")
def api_sites():
    names = site_dirs()
    adopted = adopted_sites(names)
    names = [name for name in names if name not in adopted]
    units = []
    for n in names:
        units.extend(site_units(n))
    states = service_states(units)
    with ThreadPoolExecutor(max_workers=8) as executor:
        sites = list(executor.map(lambda name: site_info(name, states), names))
    adoption_candidates(sites)
    with UPDATE_CHECK_LOCK:
        update_check = dict(UPDATE_CHECK, pending=list(UPDATE_CHECK["pending"]),
                            results=dict(UPDATE_CHECK["results"]), errors=list(UPDATE_CHECK["errors"]))
    user = me()
    if not access.is_admin(user):
        # A user sees the sites and environments they were given, and nothing of the rest
        visible = []
        for site in sites:
            if site["name"] == ADMIN_SITE:
                continue
            environments = [env for env in site["environments"]
                            if access.sees_env(user, site["name"], "main" if env.get("key") == "main" else env.get("target") or "prod")]
            if environments:
                # Whether they have all of it (Delete everything): every environment, the main site too
                shown = {**site, "environments": environments, "adopt_into": [], "managed_by": site.get("managed_by"),
                         "has_all": len(environments) == len(site["environments"])}
                # The NuGet packages only to those who may see them
                if not access.can(user, site["name"], "main", "packages_view"):
                    shown.update(packages=[], package_updates=[], package_errors=[], updates="", packages_hidden=True)
                visible.append(shown)
        sites = visible
        names = {site["name"] for site in sites}
        update_check = dict(update_check, pending=[name for name in update_check["pending"] if name in names],
                            results={name: value for name, value in update_check["results"].items() if name in names})
    else:
        # An administrator sees who each site is given to, and who made it
        for site in sites:
            site["given_to"] = given_to(lambda person, name=site["name"]: access.site_access(person, name) is not None)
            maker = made_by("sites", site["name"])
            if maker:
                site["made_by"] = maker
    return jsonify(sites=sites, defaults={"umbraco_version": default_umbraco_version()}, update_check=update_check)


@app.route("/api/sites/<name>/service", methods=["POST"])
def api_service(name):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    unit = data.get("unit") or name
    require_site(name, (data.get("target") or "prod") if data.get("scope") == "prod" else unit_env(name, unit), "service")
    audit("%s %s" % (str(action).capitalize(), unit if data.get("scope") != "prod" else name + " · " + (data.get("target") or "prod")))
    if data.get("scope") == "prod":
        target = data.get("target") or "prod"
        remote_site(name, target)
        if action not in ("start", "stop", "restart"):
            return error("Unknown service action")
        remote_command(name, action, target=target, text=True)
        REMOTE_STATUS.pop(remote_key(name, target), None)
        return jsonify(ok=True)
    if name not in site_dirs() or action not in ("start", "stop", "restart") or unit not in site_units(name):
        return error("Unknown site, service or action")
    if not unit_of(unit):
        return error("The site has no service")
    if unit == ADMIN_SITE and action != "restart":
        return error("The admin site cannot stop itself")
    result = subprocess.run(["systemctl", action, service_name(unit)], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return error((result.stderr or result.stdout).strip() or "systemctl failed", 500)
    return jsonify(ok=True)


def apache_error_logs():
    """The Apache error logs the admin shows: the server's, and the admin site's own."""
    paths = ["/var/log/apache2/error.log"]
    if ADMIN_SITE:
        paths += ["/var/log/apache2/" + ADMIN_SITE + suffix for suffix in ("-rewrite-error.log", "-443-error.log")]
    return [path for path in paths if os.path.isfile(path)]


@app.route("/api/server-logs/clear", methods=["POST"])
def api_server_logs_clear():
    require_admin()
    # Only a log the viewer shows can be cleared. It is emptied, not removed, so Apache keeps writing to it.
    path = (request.get_json(silent=True) or {}).get("path")
    if path not in apache_error_logs():
        return error("Unknown log file")
    try:
        os.truncate(path, 0)
    except OSError as exc:
        app.logger.error("Could not clear %s: %s", path, exc)
        return error("Could not clear the log file.", 502)
    return jsonify(ok=True)


HARBOR_COMMAND = os.path.join(HARBOR_DIR, "bin", "harbor")
SQL_EDITIONS = ("Developer", "Express", "Evaluation", "Standard", "Enterprise")


@app.route("/api/servers")
def api_servers():
    """The other servers and what each runs (harbor servers status), and the release this server would install"""
    require_admin()
    if not os.path.isfile(HARBOR_COMMAND):
        return error("Harbor is missing. Update VNS first.", 500)
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "servers", "status"], capture_output=True, text=True, timeout=90)
        found = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("harbor servers status: %s", exc)
        return error("The servers could not be read.", 502)
    current = os.path.realpath("/opt/harbor/current")
    return jsonify(servers=found, release=os.path.basename(current) if os.path.islink("/opt/harbor/current") else "")


def member_entry(name):
    """A member of this master, from Servers: its entry, or 404"""
    try:
        with open("/var/lib/harbor/servers.json", encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        found = {}
    entry = found.get(name) if isinstance(found, dict) else None
    if not isinstance(entry, dict) or not entry.get("member"):
        abort(404, "Not a member of this Harbor")
    return entry


@app.route("/api/via/<server>/<path:rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_via(server, rest):
    """A member's admin, from the master's page: the call sent on over SSH (harbor master call there) as it came, an
    upload with its files too, and its answer given back as it is, a download with its name. So the master's page works
    on a member as that member's own page does. An administrator is root there; a user is themselves there, as the
    master sent them, so what they may there decides"""
    user = me()
    if not access.is_admin(user) and not (user.get("servers") or {}).get(server):
        abort(403, "You have nothing on that server.")
    from harbor.core.system import Stop, System
    from harbor.server import master
    entry = member_entry(server)
    # The path as it was sent, its %-escapes kept (a site's name in it), from what the server read
    sent = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI") or request.full_path
    prefix = f"/api/via/{server}/"
    path = "/api/" + sent[len(prefix):] if sent.startswith(prefix) else "/api/" + rest + ("?" + request.query_string.decode() if request.query_string else "")
    raw = None if request.method == "GET" else {"type": request.content_type or "", "data": base64.b64encode(request.get_data()).decode()}
    try:
        answer = master.ask(System(), server, entry, request.method, path.rstrip("?"), raw=raw, as_user=None if access.is_admin(user) else user["id"],
                            client=client_ip())
    except Stop as exc:
        return error(str(exc), 502)
    # A member on an older Harbor (1.0.101) answers with its JSON only, not the bytes as they were
    content = base64.b64decode(answer["data"]) if "data" in answer else json.dumps(answer.get("body")).encode()
    response = Response(content, status=answer.get("status") or 502, content_type=answer.get("type") or "application/json")
    if answer.get("disposition"):
        response.headers["Content-Disposition"] = answer["disposition"]
    return response


@sock.route("/ws/via/<server>/term/<sid>")
def ws_via(ws, server, sid):
    """A member's terminal or task, from the master's page: its lines bridged over SSH (harbor master ws there)"""
    origin = request.headers.get("Origin", "")
    user = session_user()
    if user is None or urlparse(origin).netloc != request.headers.get("Host", "") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
        return
    if not access.is_admin(user) and not (user.get("servers") or {}).get(server):
        return
    from harbor.core.system import System
    from harbor.server import master
    try:
        entry = member_entry(server)
    except Exception:  # noqa: BLE001  (abort's 404: nothing to bridge to)
        return
    acting = "" if access.is_admin(user) else f" --as {user['id']}"
    argv = [*master.ssh(System(), entry), *master.on(entry, f"{master.REMOTE_HARBOR} master ws /ws/term/{sid}{acting}")]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def pump():
        try:
            for line in process.stdout:
                ws.send(line.rstrip("\n"))
        except Exception:  # noqa: BLE001  (the page went: nothing to send to)
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=pump, daemon=True).start()
    try:
        while process.poll() is None:
            message = ws.receive()
            if message is None:
                break
            process.stdin.write(str(message).replace("\n", " ") + "\n")
            process.stdin.flush()
    except Exception:  # noqa: BLE001  (the page or the member went)
        pass
    finally:
        process.kill()


@app.route("/api/master/sql")
def api_master_sql():
    """The master's members and their SQL connections, each as its own admin lists them (harbor master sql)"""
    require_admin()
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "master", "sql"], capture_output=True, text=True, timeout=180)
        found = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("harbor master sql: %s", exc)
        return error("The members could not be asked.", 502)
    return jsonify(members=in_server_order(found))


@app.route("/api/master/sites")
def api_master_sites():
    """The master's members and their sites, each as its own admin lists them (harbor master sites)"""
    require_admin()
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "master", "sites"], capture_output=True, text=True, timeout=180)
        found = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("harbor master sites: %s", exc)
        return error("The members could not be asked.", 502)
    return jsonify(members=in_server_order(found))


def servers_command(data):
    """A Servers action as a harbor command, and its variables (the password never on the command line)"""
    action, name = data.get("action"), data.get("name", "")
    harbor = ["/usr/bin/python3", HARBOR_COMMAND, "servers"]
    # Update all: Harbor's release here on each of the servers named, one after another
    if action == "install_all":
        names = data.get("names")
        if not isinstance(names, list) or not names or len(names) > 50 or any(not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", item) for item in names):
            abort(400, "Name the servers to update")
        return f"Update Harbor on {len(names)} servers", harbor + ["install", *dict.fromkeys(names)], {}
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        abort(400, "Give the server a name of lower case letters, digits and -")
    if action == "add":
        host, user, port, password = data.get("host", ""), data.get("user") or "root", str(data.get("port") or "22"), data.get("password", "")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
            abort(400, "Give the server's address")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(user)) or not port.isdigit():
            abort(400, "Give the login and SSH port")
        if not isinstance(password, str) or len(password) > 256:
            abort(400, "Invalid password")
        # On a master, a server added is its member at once (Harbor installed there and joined)
        return f"Add server {name}", harbor + ["add", name, host, "--user", user, "--port", port, *(["--member"] if has_members() else [])], {"HARBOR_SSH_PASSWORD": password}
    if action == "install":
        return f"Install Harbor on {name}", harbor + ["install", name], {}
    if action == "add_windows":
        # A Windows server's share and login, kept for Import from Windows; the password in the task's environment
        share, user, domain = windows_login(data)
        return f"Add Windows server {name}", harbor + ["add-windows", name, share, "--user", user, *(["--domain", domain] if domain else [])], {"VNS_SMB_PASSWORD": data["password"]}
    if action == "sql":
        # Its SQL Server takes new databases from Convert, Move and Copy, or keeps its own
        state = data.get("state")
        if state not in ("on", "closed"):
            abort(400, "Choose whether its SQL Server takes new databases")
        return (f"{name}'s SQL Server takes new databases" if state == "on" else f"{name}'s SQL Server keeps its databases"), harbor + ["sql", name, state], {}
    if action == "windows_sites":
        # Its sites listed on Web Server, where they are (the folders in its share), with the folders left out
        exclude = data.get("exclude") or []
        if not isinstance(exclude, list) or len(exclude) > 500 or any(not windows_folder_ok(folder) for folder in exclude):
            abort(400, "Choose the folders to leave out")
        on = data.get("on") is True
        WINDOWS_SITES_SEEN.pop(name, None)
        return (f"List the sites of {name}" if on else f"No longer list the sites of {name}"), \
            harbor + ["windows-sites", name, "on" if on else "off", *[part for folder in exclude for part in ("--exclude", folder)]], {}
    if action == "rollback":
        return f"Roll back Harbor on {name}", harbor + ["rollback", name], {}
    if action == "join":
        # A member makes no members: only a server that is no one's member is a master
        if member_of():
            abort(400, "This server is a member of a master: only the master makes members.")
        return f"Make {name} a member", harbor + ["join", name], {}
    if action == "leave":
        return f"{name} no longer a member", harbor + ["leave", name], {}
    if action == "web":
        state = data.get("state")
        if state not in ("on", "closed", "off"):
            abort(400, "Choose whether it hosts sites")
        titles = {"on": f"{name} hosts sites", "closed": f"{name} keeps its sites", "off": f"{name} hosts no sites"}
        return titles[state], harbor + ["web", name, state], {}
    if action == "edit":
        # Its name, and for a Linux server its address (harbor servers edit): what is left empty stays
        new_name, host = data.get("new_name", "") or "", data.get("host", "") or ""
        if not isinstance(new_name, str) or (new_name and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", new_name)):
            abort(400, "Give the server a name of lower case letters, digits and -")
        if not isinstance(host, str) or (host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host)):
            abort(400, "Give the server's new address")
        if not new_name and not host:
            abort(400, "Nothing to change")
        return f"Edit server {name}", harbor + ["edit", name, *(["--name", new_name] if new_name else []), *(["--host", host] if host else [])], {}
    if action == "remove":
        options = ["--uninstall"] if data.get("uninstall") is True else []
        return f"Remove server {name}", harbor + ["remove", name, *options], {}
    abort(400, "Unknown action")


@app.route("/api/server/crontab", methods=["GET", "POST"])
def api_server_crontab():
    """Root's crontab, line by line (harbor server crontab): read, or saved from the editor"""
    require_admin()
    if not os.path.isfile(HARBOR_COMMAND):
        return error("Harbor is missing.", 500)
    if request.method == "GET":
        try:
            result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "crontab", "list"], capture_output=True, text=True, timeout=30)
            entries = json.loads(result.stdout)
            # Microsoft SQL Server's backups are suggested while it and its tools are here
            sql_ready = os.path.exists("/opt/mssql/bin/sqlservr") and any(os.access(path, os.X_OK) for path in ("/opt/mssql-tools18/bin/sqlcmd", "/opt/mssql-tools/bin/sqlcmd"))
            harbor_jobs = {entry.get("harbor") for entry in entries if isinstance(entry, dict)}
            # PostgreSQL's backup while PostgreSQL is here and Harbor has its password
            postgres_ready = os.path.isdir("/etc/postgresql") and os.path.isfile("/etc/harbor/secrets/postgres.json")
            # Each of Harbor's jobs that is not in cron: removed, it is suggested again
            logrotate = {"on": os.path.isfile("/etc/logrotate.d/vns-harbor-jobs")}
            return jsonify(entries=entries, logrotate=logrotate, suggest=[job for job in ("backup_everything", "shrink_logs") if sql_ready and job not in harbor_jobs]
                           + (["postgres_backup"] if postgres_ready and "postgres_backup" not in harbor_jobs else []))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            app.logger.error("harbor server crontab list: %s", exc)
            return error("Root's crontab could not be read.", 502)
    entries = (request.get_json(silent=True) or {}).get("entries")
    if not isinstance(entries, list) or len(entries) > 500:
        return error("No crontab to save.")
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "crontab", "save"], input=json.dumps(entries),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("harbor server crontab save: %s", exc)
        return error("Root's crontab could not be saved.", 502)
    if result.returncode:
        return error((result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip() else "Root's crontab could not be saved.")
    audit("Saved root's crontab")
    return jsonify(ok=True)


@app.route("/api/health")
def api_health():
    """How the server is: now, its samples of the last 7 days, its OS updates, what is past its limit (lib/server_health.py)"""
    require_admin()
    try:
        return jsonify(_health.overview())
    except OSError:
        app.logger.exception("health")
        return error("The server's health could not be read.", 502)


@app.route("/api/health/updates", methods=["GET", "POST"])
def api_health_updates():
    """The OS updates as last read (GET), or read again now from apt's lists (POST)"""
    require_admin()
    if request.method == "GET":
        return jsonify(_health.read_json(_health.STATE / "updates.json", None))
    try:
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "server_health.py"), "--updates"], capture_output=True, text=True, timeout=180)
        return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("health updates")
        return error("The updates could not be read. Inspect the admin app log.", 502)


@app.route("/api/health/alerts", methods=["GET", "PUT"])
def api_health_alerts():
    """The health alerts: who is mailed, and the limits of each check"""
    require_admin()
    settings = _health.load_settings()
    if request.method == "PUT":
        try:
            settings = save_health_alerts(request.get_json(silent=True))
        except ValueError as exc:
            return error(str(exc))
        except OSError:
            app.logger.exception("Could not save the health alerts")
            return error("The health alerts could not be saved. Inspect the admin app log.", 500)
        audit("Changed the health alerts")
    saved = _health.read_json(_health.STATE / "alerts.json", {})
    return jsonify(**settings, installed=_health.CRON.is_file(), mail=bool(_keep_alive_module.report.load_mail().get("host")),
                   open=saved.get("open") or {}, mail_error=saved.get("mail_error"), **(pushed_notification("health") if request.method == "PUT" else {}))


def save_health_alerts(data):
    """The health alerts saved, their job installed when it is not: what is saved now"""
    settings = _health.validate_settings(data)
    _health.write_json(_health.SETTINGS, settings)
    os.chmod(_health.SETTINGS, 0o600)
    if not _health.CRON.is_file():
        _health.install()
    return settings


@app.route("/api/health/alerts/test", methods=["POST"])
def api_health_alerts_test():
    """A test of the health alerts: how the server is now, by the limits saved, mailed to the addresses typed"""
    require_admin()
    emails = (request.get_json(silent=True) or {}).get("emails")
    if not isinstance(emails, list) or not emails or not all(isinstance(item, str) for item in emails):
        return error("Enter the address to send the test to.")
    try:
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "server_health.py"), "--test", *emails], capture_output=True, text=True, timeout=60)
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("health alerts test")
        return error("The test could not be made. Inspect the admin app log.", 502)
    if "subject" not in found:
        return error(found.get("error") or "The test could not be made.")
    return jsonify(found)


@app.route("/api/server/cron-alerts", methods=["GET", "PUT"])
def api_cron_alerts():
    """Email alerts for the scheduled jobs (lib/cron_watch.py): who is mailed, and on or off. On, root's crontab runs
    each job through harbor-cron-shell, which notes how it ended; what of each job is watched is in root's crontab."""
    require_admin()
    settings = _cron_watch.load_settings()
    if request.method == "PUT":
        try:
            settings = save_job_alerts(request.get_json(silent=True))
        except ValueError as exc:
            return error(str(exc))
        except (OSError, subprocess.SubprocessError):
            app.logger.exception("Could not save the job alerts")
            return error("The job alerts could not be saved. Inspect the admin app log.", 500)
        audit("Turned the job alerts " + ("on" if settings["enabled"] else "off"))
    saved = _cron_watch.read_json(_cron_watch.STATE / "alerts.json", {})
    return jsonify(**settings, installed=_cron_watch.CHECK.is_file(), mail=bool(_keep_alive_module.report.load_mail().get("host")),
                   failing=len(saved.get("open") or {}), mail_error=saved.get("mail_error"), **(pushed_notification("jobs") if request.method == "PUT" else {}))


def save_job_alerts(data):
    """The job alerts saved, root's crontab's jobs watched or not as they say: what is saved now"""
    settings = _cron_watch.validate_settings(data)
    result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "cron_watch.py"), "--enable" if settings["enabled"] else "--disable"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise OSError((result.stderr.strip().splitlines() or ["cron_watch.py failed"])[-1])
    _cron_watch.write_json(_cron_watch.SETTINGS, settings)
    os.chmod(_cron_watch.SETTINGS, 0o600)
    return settings


# ---------------------------------------------------------------- notifications from the master
# A master's notifications (its mail server, the site report, the usage, health and job alerts) are its members' too:
# sent to each when changed and when it joins. A member may keep any of them its own instead (overridden there)
NOTIFICATION_KINDS = ("mail", "report", "usage", "health", "jobs")
NOTIFICATIONS_OWN = os.path.join(DATA, "notifications-own.json")


def notifications_own():
    """The notifications this member keeps its own, not the master's"""
    try:
        with open(NOTIFICATIONS_OWN, encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return []
    return [kind for kind in found if kind in NOTIFICATION_KINDS] if isinstance(found, list) else []


def notification_settings():
    """This master's notifications as a member takes them: {kind: settings} (the mail server with its password: it goes
    over SSH to a member's root only)"""
    found = {}
    mail = _keep_alive_module.report.load_mail()
    if mail.get("host"):
        found["mail"] = {key: mail.get(key) for key in ("host", "port", "security", "user", "password", "sender")}
    try:
        report = _keep_alive_module.load().get("report")
        if report:
            found["report"] = report
    except (OSError, ValueError):
        pass
    found["usage"] = _usage.load_settings(DATA)
    found["health"] = _health.load_settings()
    found["jobs"] = _cron_watch.load_settings()
    return found


def push_notifications(kinds=None, only=None):
    """This master's notifications sent to its members (or one): {member: error} of those that did not take them"""
    if not has_members():
        return {}
    from harbor.core.system import System
    from harbor.server import master
    system = System()
    settings = {kind: value for kind, value in notification_settings().items() if kinds is None or kind in kinds}
    if not settings:
        return {}
    failed = {}
    for name, entry in master.members(system).items():
        if only and name != only:
            continue
        try:
            answer = master.ask(system, name, entry, "PUT", "/api/notifications/inherit", body=settings)
            if answer.get("status") != 200:
                failed[name] = (answer.get("body") or {}).get("error") or f"answered {answer.get('status')}"
        except Exception as exc:  # noqa: BLE001  (a member that fails is said; the master's own are saved)
            failed[name] = str(exc)
    return failed


def pushed_notification(kind):
    """After the master's own were changed (not a member's, through the master): sent to its members, and what failed
    said in the answer"""
    if getattr(g, "from_master", False) or not has_members():
        return {}
    failed = push_notifications([kind])
    return {"members_error": "; ".join(f"{name}: {why}" for name, why in failed.items())} if failed else {}


@app.route("/api/notifications/inherit", methods=["PUT"])
def api_notifications_inherit():
    """On a member: the master's notifications taken, but for those it keeps its own"""
    if not getattr(g, "from_master", False):
        return error("Only the master sends its notifications.", 403)
    sent = request.get_json(silent=True)
    if not isinstance(sent, dict):
        return error("Expected the master's notifications")
    own = notifications_own()
    save = {"mail": save_mail_server, "usage": save_usage_alerts, "health": save_health_alerts, "jobs": save_job_alerts}
    taken = []
    try:
        for kind, value in sent.items():
            if kind not in NOTIFICATION_KINDS or kind in own:
                continue
            if kind == "report":
                try:
                    settings = _keep_alive_module.load()
                except FileNotFoundError:
                    continue
                _keep_alive_module.save(_keep_alive_module.validate({**settings, "report": value}))
            else:
                save[kind](value)
            taken.append(kind)
    except ValueError as exc:
        return error(str(exc))
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("Could not take the master's notifications")
        return error("The master's notifications could not be saved. Inspect the admin app log.", 500)
    if taken:
        audit("Took the master's notifications: " + ", ".join(taken))
    return jsonify(taken=taken, own=own)


@app.route("/api/notifications/own", methods=["GET", "PUT"])
def api_notifications_own():
    """On a member: the notifications it keeps its own ({own: [kind]}); the rest are the master's"""
    require_admin()
    if request.method == "PUT":
        own = (request.get_json(silent=True) or {}).get("own")
        if not isinstance(own, list) or any(kind not in NOTIFICATION_KINDS for kind in own):
            return error("Expected the notifications kept here")
        _usage.write_json(NOTIFICATIONS_OWN, sorted(set(own)))
        audit("Notifications kept here: " + (", ".join(sorted(set(own))) or "none (all the master's)"))
    return jsonify(own=notifications_own(), member=bool(member_of()))


@app.route("/api/master/notifications/push", methods=["POST"])
def api_master_notifications_push():
    """On a master: its notifications sent to its members again (or one: member), say when one joins or takes the
    master's again"""
    require_admin()
    only = (request.get_json(silent=True) or {}).get("member") or None
    if only is not None and only not in member_names():
        return error("Not a member.")
    failed = push_notifications(only=only)
    if failed:
        return error("; ".join(f"{name}: {why}" for name, why in failed.items()), 502)
    return jsonify(ok=True)


@app.route("/api/server/cron-alerts/test", methods=["POST"])
def api_cron_alerts_test():
    """A test of the job alerts: how each watched job is now, mailed to the addresses typed, not saved"""
    require_admin()
    emails = (request.get_json(silent=True) or {}).get("emails")
    if not isinstance(emails, list) or not emails or not all(isinstance(item, str) for item in emails):
        return error("Enter the address to send the test to.")
    try:
        result = subprocess.run(["/usr/bin/python3", os.path.join(LIB_DIR, "cron_watch.py"), "--test", *emails],
                                capture_output=True, text=True, timeout=60)
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("job alerts test")
        return error("The test could not be made. Inspect the admin app log.", 502)
    if "subject" not in found:
        return error(found.get("error") or "The test could not be made.")
    return jsonify(found)


@app.route("/api/server/crontab/log", methods=["POST"])
def api_server_crontab_log():
    """A job of root's crontab: when cron started it, and the end of its output (harbor server crontab log)"""
    require_admin()
    command = str((request.get_json(silent=True) or {}).get("command", "")).strip()
    if not command or "\n" in command or len(command) > 4000:
        return error("No job to show.")
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "crontab", "log"], input=json.dumps({"command": command}),
                                capture_output=True, text=True, timeout=60)
        return jsonify(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("harbor server crontab log: %s", exc)
        return error("The job's log could not be read.", 502)


@app.route("/api/server/crontab/log/clear", methods=["POST"])
def api_server_crontab_log_clear():
    """Empties the file a job of root's crontab writes its output to (harbor server crontab clear-log)"""
    require_admin()
    command = str((request.get_json(silent=True) or {}).get("command", "")).strip()
    if not command or "\n" in command or len(command) > 4000:
        return error("No job to clear the log of.")
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "crontab", "clear-log"], input=json.dumps({"command": command}),
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("harbor server crontab clear-log: %s", exc)
        return error("The job's log could not be cleared.", 502)
    said = (result.stderr or result.stdout).strip()
    if result.returncode:
        return error(said.splitlines()[-1] if said else "The job's log could not be cleared.")
    audit(f"Cleared the log of a scheduled job: {command}")
    return jsonify(ok=True)


@app.route("/api/your-address")
def api_your_address():
    """The address this request comes from, as Apache sees it: for Allowed IPs, so one's own is added with a click."""
    return jsonify(address=client_ip())


@app.route("/api/admin-access", methods=["GET", "POST"])
def api_admin_access():
    """Who may reach this admin site (harbor admin access): read, or set. A list that leaves out the address this
    request comes from would lock the one setting it out at once: that takes confirm_lockout."""
    require_admin()
    mine = client_ip()
    if request.method == "GET":
        try:
            result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "admin", "access", ADMIN_SITE], capture_output=True, text=True, timeout=30)
            settings = json.loads(result.stdout)
            return jsonify(addresses=settings["addresses"], response=settings.get("response", "forbidden"),
                           target=settings.get("target", ""), yours=mine)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            app.logger.error("harbor admin access: %s", exc)
            return error("Who may reach the admin site could not be read.", 502)
    data = request.get_json(silent=True) or {}
    addresses = data.get("addresses")
    if not isinstance(addresses, list) or len(addresses) > 100 or not all(isinstance(item, str) for item in addresses):
        return error("No addresses given.")
    networks = []
    for item in addresses:
        try:
            networks.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            return error(f"Not an IP address or range: {item}")
    try:
        yours = ipaddress.ip_address(mine)
        locked_out = bool(networks) and not any(yours in network for network in networks)
    except ValueError:
        locked_out = bool(networks)
    response = data.get("response") or "forbidden"
    target = str(data.get("target") or "").strip()
    if response not in ("forbidden", "notfound", "redirect"):
        return error("Choose what others get.")
    if response == "redirect" and not re.fullmatch(r"https?://[^\s\"'<>\\]+", target):
        return error("Redirect to a whole address, like https://www.google.dk/.")
    if locked_out and data.get("confirm_lockout") is not True:
        return error(f"Your address now, {mine}, is not among them: you would be locked out at once.", 409)
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "admin", "access", ADMIN_SITE,
                                 *(["--allow", *[str(network) if network.num_addresses > 1 else str(network.network_address) for network in networks],
                                    "--others", response, *(["--redirect-to", target] if response == "redirect" else [])] if networks else ["--everyone"])],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("harbor admin access: %s", exc)
        return error("Who may reach the admin site could not be saved.", 502)
    if result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        return error(lines[-1] if lines else "Who may reach the admin site could not be saved.")
    audit("Admin site reachable from " + (" ".join(addresses) + {"forbidden": "", "notfound": ", others get 404", "redirect": ", others sent to " + target}[response]
                                          if addresses else "everywhere"))
    return jsonify(ok=True)


@app.route("/api/server/tools")
def api_server_tools():
    """What the server has for the sites: SQL Server, sqlcmd, the sa password, cron jobs, .NET, Node.js (harbor server status)"""
    require_admin()
    if not os.path.isfile(HARBOR_COMMAND):
        return error("Harbor is missing. Update VNS first.", 500)
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "server", "status"], capture_output=True, text=True, timeout=60)
        return jsonify(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.error("harbor server status: %s", exc)
        return error("The server's tools could not be read.", 502)


# What installs something to host on: a master hosts nothing itself (its members do), so none of it is done there
HOSTING_INSTALLS = ("sql_install", "pg_install", "pg_tools_install", "tools_install", "dotnet_install", "node_install")


def server_tool_command(data):
    """A Server tools action as a harbor command, and the variables it gets (the sa password never on the command line)"""
    tool = data.get("tool")
    if tool in HOSTING_INSTALLS and has_members():
        abort(400, "This server is a master: it hosts nothing itself. Install it on the member that is to host it.")
    harbor = ["/usr/bin/python3", HARBOR_COMMAND]
    tools_flag = "--with-tools" if data.get("with_tools") is True else "--without-tools"
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    if tool in ("sql_install", "sa_password"):
        password = data.get("sa_password")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 128 or "\n" in password:
            abort(400, "The sa password needs at least 8 characters")
        env["HARBOR_SA_PASSWORD"] = password
    if tool == "sql_install":
        if data.get("edition") not in SQL_EDITIONS:
            abort(400, "Choose an edition")
        # Where Microsoft has no SQL Server for this Ubuntu, the dialog says so and the one for an older Ubuntu is installed
        older = ["--older-ubuntu"] if data.get("older_ubuntu") is True else []
        return "Install Microsoft SQL Server", harbor + ["sql", "install-server", "--edition", data["edition"], tools_flag, *older], env
    if tool == "sa_password":
        return "Set the sa password", harbor + ["sql", "set-sa-password"], env
    if tool == "sql_update":
        return "Update Microsoft SQL Server", harbor + ["sql", "update-server", "--yes"], env
    if tool == "sql_uninstall":
        delete = data.get("delete_data") is True
        if delete and data.get("confirmation") != "DELETE":
            abort(400, "Type DELETE to delete the databases")
        return "Remove Microsoft SQL Server", harbor + ["sql", "uninstall-server", "--yes", "--delete-data" if delete else "--keep-data", tools_flag], env
    # PostgreSQL (phase 10): its password never on the command line either
    if tool in ("pg_install", "pg_password"):
        password = data.get("postgres_password")
        if not isinstance(password, str) or len(password) < 8 or len(password) > 128 or "\n" in password:
            abort(400, "The postgres password needs at least 8 characters")
        env["HARBOR_POSTGRES_PASSWORD"] = password
        return ("Install PostgreSQL", harbor + ["postgres", "install"], env) if tool == "pg_install" else ("Set the postgres password", harbor + ["postgres", "set-password"], env)
    if tool == "pg_update":
        return "Update PostgreSQL", harbor + ["postgres", "update"], env
    if tool == "pg_uninstall":
        delete = data.get("delete_data") is True
        if delete and data.get("confirmation") != "DELETE":
            abort(400, "Type DELETE to delete the databases")
        return "Remove PostgreSQL", harbor + ["postgres", "uninstall", *(["--delete-data"] if delete else []), *(["--with-tools"] if data.get("with_tools") is True else [])], env
    if tool == "pg_tools_install":
        return "Install the PostgreSQL tools", harbor + ["postgres", "install-tools"], env
    if tool == "pg_tools_uninstall":
        return "Remove the PostgreSQL tools", harbor + ["postgres", "uninstall-tools"], env
    if tool == "tools_install":
        return "Install the Microsoft SQL Server tools", harbor + ["sql", "install-sql-tools"], env
    if tool == "tools_uninstall":
        return "Remove the Microsoft SQL Server tools", harbor + ["sql", "uninstall-sql-tools", "--yes"], env
    if tool in ("cron_on", "cron_off"):
        # One of Harbor's jobs (as Scheduled jobs suggests it), or both
        job = data.get("job")
        if job is not None and job not in ("backup_everything", "shrink_logs", "postgres_backup"):
            abort(400, "Not one of Harbor's jobs")
        return ("Turn on" if tool == "cron_on" else "Turn off") + " SQL backups", harbor + ["server", "cron", tool[5:], *(["--job", job] if job else [])], env
    if tool in ("dotnet_install", "dotnet_update", "dotnet_remove"):
        channel = data.get("channel")
        if not isinstance(channel, str) or not re.fullmatch(r"[0-9]{1,2}\.0", channel) or int(channel.split(".")[0]) < 8:
            abort(400, "Choose a .NET version")
        action = tool.split("_")[1]
        # A master hosts nothing: its last .NET can go too
        return f"{action.capitalize()} .NET {channel}", harbor + ["server", "dotnet", action, channel, *(["--last"] if action == "remove" and has_members() else [])], env
    if tool == "node_install":
        major = str(data.get("major", ""))
        if not re.fullmatch(r"[0-9]{2}", major) or int(major) < 18:
            abort(400, "Choose a Node.js version")
        return f"Install Node.js {major}", harbor + ["server", "node", "install", major], env
    if tool == "node_remove":
        return "Remove Node.js", harbor + ["server", "node", "remove"], env
    if tool == "os_update":
        scope = data.get("scope")
        if scope not in ("security", "all"):
            abort(400, "Install the security updates or all of them")
        return ("Install the security updates" if scope == "security" else "Install the OS updates"), harbor + ["server", "os-update", scope], env
    if tool in ("unattended_on", "unattended_off"):
        on = tool == "unattended_on"
        return ("Turn on" if on else "Turn off") + " automatic security updates", harbor + ["server", "unattended-upgrades", "on" if on else "off"], env
    if tool in ("harbor_autoupdate_on", "harbor_autoupdate_off"):
        # Harbor updated by itself each day at the time chosen (harbor/install/autoupdate.py)
        if tool == "harbor_autoupdate_off":
            return "Stop updating Harbor by itself", harbor + ["self", "auto-update", "off"], env
        at = str(data.get("at", ""))
        if at != "soon" and not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", at):
            abort(400, "Choose the time of day")
        return ("Update Harbor as soon as a release is out" if at == "soon" else f"Update Harbor by itself at {at}"), \
            harbor + ["self", "auto-update", "on", "--at", at], env
    if tool in ("logrotate_on", "logrotate_off"):
        return ("Rotate the jobs' logs" if tool == "logrotate_on" else "Stop rotating the jobs' logs"), harbor + ["server", "crontab", tool.replace("_", "-")], env
    if tool == "cron_run":
        # Harbor runs it only when it is a job in root's crontab
        command = str(data.get("command", "")).strip()
        if not command or "\n" in command or "\r" in command or len(command) > 4000:
            abort(400, "No job to run")
        return "Run a scheduled job", harbor + ["server", "crontab", "run", "--command=" + command], env
    if tool in ("samba_on", "samba_password"):
        # Root's Samba password never on the command line. Sharing a server whose root has one already needs none.
        password = data.get("samba_password")
        if password or tool == "samba_password":
            if not isinstance(password, str) or len(password) < 6 or len(password) > 128 or "\n" in password:
                abort(400, "The Samba password needs at least 6 characters")
            env["HARBOR_SAMBA_PASSWORD"] = password
        if tool == "samba_on":
            return "Share the server over Samba", harbor + ["server", "samba", "on"], env
        return "Give root a new Samba password", harbor + ["server", "samba", "password"], env
    if tool == "ssh_root":
        mode = data.get("mode")
        if mode not in ("yes", "prohibit-password", "no"):
            abort(400, "Choose how root may log in")
        return "Set root's SSH login", harbor + ["server", "ssh-root", mode], env
    if tool == "samba_save":
        share = data.get("share") if isinstance(data.get("share"), dict) else {}
        from harbor.server.samba import SHARE_NAME
        name, path = str(share.get("name", "")).strip(), str(share.get("path", "")).strip()
        if not SHARE_NAME.fullmatch(name) or not path.startswith("/"):
            abort(400, "Give the share a name and a full folder path")
        texts = {key: str(share.get(key, "")).strip() for key in ("force_user", "valid_users", "comment")}
        if any("\n" in value or len(value) > 200 for value in texts.values()):
            abort(400, "A setting goes on one line")
        old = str(data.get("old_name", "")).strip()
        argv = ["server", "samba", "save-share", name, "--path", path, *(["--old-name", old] if old else []),
                *(["--read-only"] if share.get("read_only") is True else []), *(["--hidden"] if share.get("browseable") is False else []),
                *(["--guest-ok"] if share.get("guest_ok") is True else []),
                *[part for key, flag in (("force_user", "--force-user"), ("valid_users", "--valid-users"), ("comment", "--comment")) if texts[key] for part in (flag, texts[key])]]
        return f"{'Change' if old else 'Add'} the Samba share {name}", harbor + argv, env
    if tool in ("samba_add_user", "samba_user_password", "samba_delete_user"):
        name = str(data.get("name", "")).strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
            abort(400, "A user name is lower case letters, digits, - and _")
        if tool != "samba_delete_user":
            password = data.get("samba_password")
            if not isinstance(password, str) or len(password) < 6 or len(password) > 128 or "\n" in password:
                abort(400, "The Samba password needs at least 6 characters")
            env["HARBOR_SAMBA_PASSWORD"] = password
        action = {"samba_add_user": "add-user", "samba_user_password": "user-password", "samba_delete_user": "delete-user"}[tool]
        title = {"add-user": f"Add the Samba user {name}", "user-password": f"Give {name} a new Samba password", "delete-user": f"Delete the Samba user {name}"}[action]
        return title, harbor + ["server", "samba", action, name], env
    if tool == "samba_delete":
        name = str(data.get("name", "")).strip()
        if not name:
            abort(400, "Choose a share")
        return f"Delete the Samba share {name}", harbor + ["server", "samba", "delete-share", name], env
    if tool in ("samba_start", "samba_stop"):
        return f"Turn Samba {'on' if tool == 'samba_start' else 'off'}", harbor + ["server", "samba", tool[6:]], env
    if tool == "samba_off":
        return "Stop sharing the server over Samba", harbor + ["server", "samba", "off"], env
    abort(400, "Unknown tool")


@app.route("/api/server-logs")
def api_server_logs():
    require_admin()
    source = request.args.get("source", "admin")
    if source not in ("admin", "apache"):
        return error("Unknown log source")
    try:
        if source == "admin":
            if not ADMIN_SITE:
                return error("The admin service is not configured.", 409)
            argv = ["journalctl", "-u", service_name(ADMIN_SITE), "-n", "300", "--no-pager", "-o", "short-iso"]
            sources = [f"journalctl -u {service_name(ADMIN_SITE)}.service"]
        else:
            paths = apache_error_logs()
            if not paths:
                return error("No Apache error logs were found.", 404)
            argv = ["tail", "-v", "-n", "300", "--", *paths]
            sources = paths
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        if result.returncode:
            app.logger.error("Could not read %s logs: %s", source, result.stderr)
            return error("Could not read server logs. Check server permissions.", 502)
        return jsonify(text=result.stdout, sources=sources)
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.error("Could not read %s logs: %s", source, exc)
        return error("Could not read server logs.", 502)


def harbor_modules():
    """The harbor package, installed beside the apache folder the admin runs the scripts of"""
    install = os.path.realpath(HARBOR_DIR)
    if install not in sys.path:
        sys.path.insert(0, install)
    from harbor.core.system import System
    from harbor.environments import settings as harbor_settings
    from harbor.sites import service as harbor_service
    return System, harbor_settings, harbor_service


def backoffice_environment(site):
    """main, or the id of an environment of the site; the backoffice login is for a site on this server"""
    target = request.values.get("target") or "main"
    return "main" if target == "main" else target_arg(site, {"target": target})


def main_environment(site):
    """The environment the main site runs as (ASPNETCORE_ENVIRONMENT or DOTNET_ENVIRONMENT in its service)"""
    service_text = read_text(os.path.join(SERVICE_DIR, service_name(site) + ".service"))
    match = re.search(r"(?:ASPNETCORE|DOTNET)_ENVIRONMENT=([^\s\"']+)", service_text)
    return match.group(1) if match else "Unknown"


# ---- The VNS.Harbor package's API (phase 9b): a site asks with its own token, and only about itself
def harbor_api_destinations(site, source):
    """Where the site may sync to: its other environments, with what Sync setup lets it send each"""
    root = os.path.join(WEB_ROOT, site)
    config = _data_sync_module.load(root)
    found = []
    for destination in ["main", *target_ids(root)]:
        if destination == source:
            continue
        try:
            kinds = _data_sync_module.kinds(config, source, destination)
        except ValueError:
            kinds = []
        try:
            _data_sync_module.settings_allowed(config, source, destination)
            settings_allowed = destination != "main"
        except ValueError:
            settings_allowed = False
        if kinds or settings_allowed:
            prod = parse_prod(root, destination) or {} if destination != "main" else {}
            binding = prod.get("PROD_BINDING") or (conf_binding(site) if destination == "main" else "")
            if destination == "main":
                secure = https_status(site)["scheme"] == "https"
            else:
                secure = prod.get("PROD_HTTPS") != "n"
            # The main site by the environment it runs as, as the admin shows it, never "main"
            environment = prod.get("PROD_ENVIRONMENT") or (main_environment(site) if destination == "main" else destination)
            found.append({"id": destination, "environment": environment,
                          "binding": binding, "url": f"{'https' if secure else 'http'}://{binding}/" if binding else "",
                          "settings": settings_allowed, "data": kinds})
    # Always in the order the environments follow: the main site, Development, Test, Staging, Production
    order = ["main", "development", "test", "staging", "production"]
    found.sort(key=lambda item: (order.index(item["environment"].lower()) if item["environment"].lower() in order else len(order), item["id"]))
    return found


@app.route(HARBOR_API + "site")
def harbor_api_site():
    site, source = g.harbor_site
    if not os.path.isdir(os.path.join(WEB_ROOT, site)):
        return error("The site is not on this server", 404)
    # The environment it syncs from by name, as its destinations are named: "Development → Production" in the dashboard
    if source == "main":
        name = main_environment(site)
    else:
        name = (parse_prod(os.path.join(WEB_ROOT, site), source) or {}).get("PROD_ENVIRONMENT") or source
    destinations = harbor_api_destinations(site, source)
    sources = harbor_api_sources(site, source)
    try:
        add_sync_history(site, source, destinations, sources)
        recent = recent_syncs(site, source)
    except (ImportError, OSError, ValueError) as exc:
        app.logger.error("Sync history of %s could not be read: %s", site, exc)
        recent = []
    return jsonify(site=site, environment=source, environment_name=name, setup=harbor_api_setup(site, source),
                   destinations=destinations, sources=sources, recent=recent)


def environment_name(site, site_id):
    """An environment of a site by name, as the dashboard shows it: the main site by the environment it runs as"""
    if site_id == "main":
        return main_environment(site)
    return (parse_prod(os.path.join(WEB_ROOT, site), site_id) or {}).get("PROD_ENVIRONMENT") or site_id


def harbor_api_sources(site, destination):
    """The sites that may sync to this one, and what: the dashboard shows when they last did, and whether they have
    changed since (harbor_api_destinations seen from the other side)"""
    root = os.path.join(WEB_ROOT, site)
    config = _data_sync_module.load(root)
    found = []
    for source in ["main", *target_ids(root)]:
        if source == destination:
            continue
        try:
            kinds = _data_sync_module.kinds(config, source, destination)
        except ValueError:
            kinds = []
        try:
            _data_sync_module.settings_allowed(config, source, destination)
            settings_allowed = True
        except ValueError:
            settings_allowed = False
        if kinds or settings_allowed:
            found.append({"id": source, "environment": environment_name(site, source), "settings": settings_allowed, "data": kinds})
    order = ["development", "test", "staging", "production"]
    found.sort(key=lambda item: (order.index(item["environment"].lower()) if item["environment"].lower() in order else len(order), item["id"]))
    return found


SYNC_PRINTS = {}
SYNC_PRINTS_LOCK = threading.Lock()


def current_prints(site, site_id):
    """What a site has now, by kind ({} when its files are not on this server). Kept 30 seconds: the dashboard and
    the condition that shows its tab ask at once."""
    from harbor.core.system import System
    from harbor.environments import history
    from harbor.sites.actions import site_named
    key = (site, site_id)
    with SYNC_PRINTS_LOCK:
        cached = SYNC_PRINTS.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
    system = System()
    folder = history.local_folder(system, site_named(system, site), site_id)
    prints = history.fingerprints(system.path(folder)) if folder and system.path(folder).is_dir() else {}
    with SYNC_PRINTS_LOCK:
        SYNC_PRINTS[key] = (time.monotonic(), prints)
    return prints


def add_sync_history(site, here, destinations, sources):
    """Each destination and source with its last syncs and whether the sending site has changed since"""
    from harbor.core.system import System
    from harbor.environments import history
    system = System()
    kinds = lambda item: (["settings"] if item["settings"] else []) + list(item["data"])  # noqa: E731
    mine = current_prints(site, here) if destinations else {}
    config = _data_sync_module.load(os.path.join(WEB_ROOT, site))
    for item in destinations:
        item["history"] = name_history(history.between(system, site, here, item["id"], kinds(item), mine))
        item["settings_behind"] = settings_behind(system, site, item["id"], config, here, item["data"]) if item["data"] else ""
    for item in sources:
        item["history"] = name_history(history.between(system, site, item["id"], here, kinds(item), current_prints(site, item["id"])))


def settings_behind(system, site, destination, config, source="main", kinds=("content", "media")):
    """Why data may not go to destination yet (its settings are behind the main site's), or "": as harbor env
    data-sync decides it. A destination that may not receive settings keeps its own."""
    from harbor.environments import history
    from harbor.sites.actions import site_named
    if destination == "main":
        return ""
    try:
        _data_sync_module.settings_allowed(config, "main", destination)
    except ValueError:
        return ""
    return history.settings_behind(system, site_named(system, site), destination, current_prints(site, "main"), source=source, kinds=tuple(kinds))


def sync_who(entry):
    """Who started a sync, as they are named now: a Harbor user by their name (or username), with VNS Harbor; one in
    Umbraco as Harbor was told"""
    user_id = entry.get("who_id") if isinstance(entry, dict) else ""
    if user_id:
        label = access.label(cfg, user_id)
        if label:
            return f"{label} (Harbor)"
    return (entry or {}).get("who", "")


def name_history(history):
    """The last syncs of a destination or source, with who started them as they are named now"""
    for found in (history or {}).values():
        if found.get("last"):
            found["last"]["who"] = sync_who(found["last"])
            found["last"].pop("who_id", None)
    return history


def recent_syncs(site, here, count=10, kinds=None):
    """The newest syncs from or to this site, with the environments by name"""
    from harbor.core.system import System
    from harbor.environments import history
    names = {}
    found = []
    for entry in history.entries(System(), site):
        if here not in (entry.get("from"), entry.get("to")):
            continue
        # The Sync dialog of one kind lists only syncs of that kind
        if kinds and not set(entry.get("kinds", [])) & set(kinds):
            continue
        for key in ("from", "to"):
            if entry.get(key) not in names:
                names[entry.get(key)] = environment_name(site, entry.get(key))
        found.append({"time": entry.get("time"), "from": names[entry.get("from")], "to": names[entry.get("to")],
                      "kinds": entry.get("kinds", []), "ok": entry.get("ok"), "who": sync_who(entry)})
        if len(found) == count:
            break
    return found


def harbor_api_setup(site, source):
    """What Sync setup lets the site itself send and receive, so the dashboard can show it (a Development that
    receives data by mistake is seen there)"""
    try:
        own = _data_sync_module.clean(_data_sync_module.load(os.path.join(WEB_ROOT, site)).get(source), source)
    except ValueError:
        return None
    data = [kind for kind in _data_sync_module.KINDS if own[kind]] if own["enabled"] else []
    return {"sends": {"settings": own["settings_send"], "data": data if own["send"] else []},
            # The main site never receives settings
            "receives": {"settings": own["settings_receive"] and source != "main", "data": data if own["receive"] else []}}


@app.route(HARBOR_API + "sync", methods=["POST"])
def harbor_api_sync():
    """Settings or data from this site to one of its environments: the same task as the admin's Sync"""
    site, source = g.harbor_site
    data = request.get_json(silent=True) or {}
    destination, what = data.get("destination"), data.get("what")
    allowed = {item["id"]: item for item in harbor_api_destinations(site, source)}
    if what not in ("settings", "data"):
        return error("Choose settings or data.", 400)
    # all: every environment that may receive it from this site
    names = [item for item, found in allowed.items() if found["settings" if what == "settings" else "data"]] if destination == "all" else [destination]
    if destination == "all" and what == "data":
        # Data to all goes where the settings are in sync; harbor refuses the others
        from harbor.core.system import System
        config = _data_sync_module.load(os.path.join(WEB_ROOT, site))
        names = [name for name in names if not settings_behind(System(), site, name, config, source, allowed[name]["data"])]
    if not names or any(name not in allowed or not allowed[name]["settings" if what == "settings" else "data"] for name in names):
        return error("This site may not sync that there. Sync setup in Harbor decides.", 403)
    payload = ({"kind": "sync_env", "site": site, "source": source, "destination": ",".join(names)} if what == "settings"
               else {"kind": "data_sync", "site": site, "source": source, "destination": ",".join(names)})
    title, argv, cwd = build_command(payload)
    # Who started it, for the history: the Umbraco user the package names
    who = data.get("who") if isinstance(data.get("who"), str) else ""
    who = re.sub(r"[\x00-\x1f]", "", who).strip()[:80]
    with SESSIONS_LOCK:
        if sum(1 for s in SESSIONS.values() if s.running) >= MAX_SESSIONS:
            return error("Harbor is busy. Try again in a moment.", 503)
        session_obj = TermSession(title + " (from Umbraco)", argv, cwd, {"HARBOR_SYNC_WHO": f"{who} (Umbraco)" if who else "Umbraco"},
                                  auto_close=True, sites=terminal_activity_sites(payload),
                                  scope=terminal_scope(payload), targets=terminal_targets(payload), owner=access.ROOT, locked=True)
        session_obj.process = True
        session_obj.harbor_site = (site, source)
        SESSIONS[session_obj.id] = session_obj
    audit(f"Started from Umbraco ({site}, {source}): {title}")
    return jsonify(task=session_obj.id)


HARBOR_EVENT = re.compile(rb"\x1b\]7777;harbor;(\{.*?\})\x07")


@app.route(HARBOR_API + "task/<sid>")
def harbor_api_task(sid):
    """A sync the site started: its steps so far, and how it ended"""
    with SESSIONS_LOCK:
        task = SESSIONS.get(sid)
    if task is None or getattr(task, "harbor_site", None) != g.harbor_site:
        return error("Unknown task", 404)
    with task.lock:
        output = bytes(task.buffer)
    steps = []
    for match in HARBOR_EVENT.finditer(output):
        try:
            event = json.loads(match.group(1))
        except ValueError:
            continue
        # A step not taken (skip) is no step of the dashboard's list
        if isinstance(event, dict) and isinstance(event.get("text"), str) and event.get("type", "step") in ("step", "warning", "warn"):
            steps.append({"type": event.get("type", "step"), "text": event["text"]})
    return jsonify(running=task.running, exit=task.exit, steps=steps[-200:])


# A build sent to POST /api/publish/<site>, until harbor site receive-build has read it (root only)
PUBLISH_UPLOADS = "/var/lib/harbor/incoming"


@app.route("/api/publish-token", methods=["GET", "POST", "DELETE"])
def api_publish_token():
    """The logged-in user's publish token: whether they have one, a new one (shown this once), or none"""
    user = me()
    if request.method == "POST":
        return jsonify(token=_publish_tokens.make(user["id"]), **_publish_tokens.info(user["id"]))
    if request.method == "DELETE":
        _publish_tokens.remove(user["id"])
    return jsonify(**_publish_tokens.info(user["id"]))


def publish_target(name):
    """A site published from a machine that the token's user may publish: here, a build with no project, not an
    environment of a site elsewhere, not the admin. A new site is made in Harbor, not by a publish"""
    if not isinstance(name, str) or not NAME_RE.match(name) or not os.path.isdir(os.path.join(WEB_ROOT, name)):
        abort(404, f"No site {name} here. Make it in Harbor first (New site, Published from an external source).")
    site = site_arg({"site": name})
    if not access.can(g.user, site, "main", "publish"):
        abort(403, f"You may not publish {site}.")
    if glob.glob(os.path.join(WEB_ROOT, site, "*.csproj")) or managed_by(site) or site == ADMIN_SITE:
        abort(400, f"{site} is not published from a machine: it is built here, or managed from another server.")
    return site


@app.route("/api/publish/<name>/info")
def publish_api_info(name):
    """What the publish script asks before it builds: the server's architecture"""
    publish_target(name)
    return jsonify(architecture=os.uname().machine)


@app.route("/api/publish/<name>/architecture")
def publish_api_architecture(name):
    """The same as plain text, for a script that cannot read JSON (Windows' cmd)"""
    publish_target(name)
    return Response(os.uname().machine + "\n", mimetype="text/plain")


@app.route("/api/publish/<name>", methods=["POST"])
def publish_api(name):
    """A build from a machine, as a tar (build/ and scripts/): harbor site receive-build --stdin, its output streamed
    back as it comes, and a last line Harbor: OK, or Harbor: FAILED (exit n) that the script checks"""
    site = publish_target(name)
    commit = request.args.get("commit", "")
    dirty = request.args.get("dirty", "")
    appsettings = request.args.get("appsettings", "")
    keep = request.args.getlist("keep")
    if not re.fullmatch(r"[0-9a-f]{0,40}", commit) or dirty not in ("", "y", "n") or appsettings not in ("", "build", "server") or len(keep) > 20:
        abort(400, "Invalid publish options")
    if not request.content_length:
        abort(411, "Send the build with its length (curl -T).")
    os.makedirs(PUBLISH_UPLOADS, exist_ok=True)
    descriptor, upload = tempfile.mkstemp(dir=PUBLISH_UPLOADS, prefix=f".{site}-upload-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
    except Exception:
        os.unlink(upload)
        raise
    who = (g.user.get("name") or g.user.get("username") or g.user.get("id") or "") + " (publish token)"
    argv = ["/usr/bin/python3", HARBOR_COMMAND, "site", "receive-build", site, "--stdin", "--by", who[:120],
            *(["--commit", commit] if commit else []), *(["--dirty", dirty] if dirty else []),
            *(["--appsettings", appsettings] if appsettings else []), *(f"--keep={path}" for path in keep)]
    app.logger.info("Publish of %s from a machine by %s", site, who)

    def output():
        try:
            with open(upload, "rb") as build:
                process = subprocess.Popen(argv, stdin=build, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           errors="replace", bufsize=1, cwd="/",
                                           env={key: value for key, value in os.environ.items() if key != "HARBOR_PROGRESS"})
                for line in process.stdout:
                    yield line
                code = process.wait()
            yield "Harbor: OK\n" if code == 0 else f"Harbor: FAILED (exit {code})\n"
        finally:
            os.unlink(upload)
    # Line by line as it runs: nothing between it and the machine may hold the output back
    return Response(stream_with_context(output()), mimetype="text/plain",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# A Python site's outdated packages, as pip last told them: {site: (when, [{name, version, latest}])}
PIP_OUTDATED = {}
PIP_OUTDATED_FOR = 3600


def requirement_names(path):
    """The packages a requirements.txt names, as pip compares them (lower case, - for _ and .)"""
    names = set()
    for line in read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(0)).lower())
    return names


def python_outdated(site, refresh=False):
    """The packages in a Python site's requirements.txt that have newer versions (pip list --outdated in its venv),
    kept an hour: Update Python packages is shown only when there are some"""
    root = os.path.join(WEB_ROOT, site)
    pip = os.path.join(root, "venv/bin/pip")
    if not os.path.isfile(pip):
        return []
    known = PIP_OUTDATED.get(site)
    if known and not refresh and time.time() - known[0] < PIP_OUTDATED_FOR:
        return known[1]
    try:
        result = subprocess.run([pip, "list", "--outdated", "--format=json", "--disable-pip-version-check"], capture_output=True,
                                text=True, timeout=90, cwd=root, env=dict(os.environ, PIP_NO_INPUT="1"))
        listed = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        app.logger.warning("Could not list outdated packages of %s: %s", site, exc)
        return []
    wanted = requirement_names(os.path.join(root, "app/requirements.txt"))
    found = [{"name": item.get("name", ""), "version": item.get("version", ""), "latest": item.get("latest_version", "")}
             for item in listed if isinstance(item, dict)
             and (not wanted or re.sub(r"[-_.]+", "-", str(item.get("name", ""))).lower() in wanted)]
    PIP_OUTDATED[site] = (time.time(), found)
    return found


@app.route("/api/sites/<name>/python-outdated")
def api_python_outdated(name):
    site = site_arg({"site": name})
    return jsonify(outdated=python_outdated(site, request.args.get("refresh") == "1"))


@app.route("/api/sites/<name>/permissions")
def api_site_permissions(name):
    """Whether Fix permissions has anything to fix on the main site (scope test) or an environment (scope prod), so its
    button is shown only then: a file not owned by www-data, or one it cannot read. Nothing is changed."""
    site = site_arg({"site": name})
    scope = request.args.get("scope", "test")
    if scope not in ("test", "prod") or site == ADMIN_SITE:
        abort(400, "Invalid permissions target")
    target = target_arg(site, {"target": request.args.get("target")}) if scope == "prod" else "prod"
    if not access.can(me(), site, target if scope == "prod" else "main", "files"):
        abort(403, "No access to its files")
    argv = with_target(["/usr/bin/python3", HARBOR_COMMAND, "site", "fix-permissions", site, scope, "--check"], target)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=HARBOR_DIR)
        return jsonify(json.loads(result.stdout.strip().splitlines()[-1]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        app.logger.exception("permissions check")
        return jsonify(needed=None, error="The check could not be made")


@app.route("/api/sites/<name>/backoffice-login")
def api_backoffice_login(name):
    """A one-time link that logs in to the site's backoffice as its chosen Umbraco user (the VNS.Harbor package), or
    no link when the site has none chosen: the Backend button then opens the backoffice's own login"""
    require_admin()
    site = site_arg({"site": name})
    environment = backoffice_environment(site)
    System, _, _ = harbor_modules()
    from harbor.core.system import Stop
    from harbor.umbraco import backoffice
    system = System()
    # A site managed from another server is that Harbor's: a link from here connected it to this one (its token), and
    # its sync could no longer reach its own. The Backend button opens the backoffice's own login there
    if managed_by(site):
        return jsonify(url=None)
    if not backoffice.backoffice_user(system, site, environment) or not backoffice.uses_package(system, _harbor_site(site)):
        return jsonify(url=None)
    try:
        return jsonify(url=backoffice.login_link(system, site, environment, who=me().get("name") or me().get("id") or ""))
    except Stop as stop:
        return error(str(stop) or "No login link could be made", 400)


def _harbor_site(name):
    harbor_modules()
    from harbor.core.templates import Site
    return Site(name)


@app.route("/api/sites/<name>/backoffice-user", methods=["GET", "POST"])
def api_backoffice_user(name):
    """The Umbraco user the Backend button logs in as. Saving it also connects the site to Harbor (vns-harbor.json)."""
    require_admin()
    site = site_arg({"site": name})
    environment = backoffice_environment(site)
    System, _, _ = harbor_modules()
    from harbor.core.system import Stop
    from harbor.umbraco import backoffice
    system = System()
    if request.method == "GET":
        return jsonify(user=backoffice.backoffice_user(system, site, environment), package=backoffice.uses_package(system, _harbor_site(site)))
    user = (request.get_json(silent=True) or {}).get("user", "")
    if not isinstance(user, str):
        abort(400, "Invalid user")
    # Saving connects the site to this Harbor: never one managed from another server, which knows it and its token
    managing = managed_by(site)
    if managing:
        return error(f"{site} is managed from {managing.get('host') or 'another server'}: set its Backoffice login there.", 400)
    try:
        backoffice.set_backoffice_user(system, site, user.strip(), environment)
        if user.strip() and backoffice.uses_package(system, _harbor_site(site)):
            backoffice.connect(system, site, environment, request.host_url.rstrip("/"))
    except Stop as stop:
        return error(str(stop) or "Could not save", 400)
    return jsonify(ok=True)


@app.route("/api/sites/<name>/service")
def api_service_status(name):
    """A service's state and latest log lines, for the status window (Check service). Here or where an environment runs."""
    require_admin()
    site = site_arg({"site": name})
    System, harbor_settings, harbor_service = harbor_modules()
    try:
        if request.args.get("target"):
            target = target_arg(site, {"target": request.args.get("target")})
            environment = harbor_settings.load(System(), os.path.join(WEB_ROOT, site), target)
            if environment.windows:
                abort(400, "A Windows environment's service is IIS: check it there")
            unit = service_name(environment.FOLDER)
            state = harbor_service.status(System(), unit, environment if not environment.local else None)
        else:
            unit = request.args.get("unit") or site
            if unit not in site_units(site):
                abort(400, "Unknown service")
            unit = service_name(unit)
            state = harbor_service.status(System(), unit)
    except harbor_settings.SettingsError as error:
        return error(str(error), 502)
    return jsonify(unit=unit, **state)


@app.route("/api/sites/<name>/logs")
def api_logs(name):
    require_site(name, (request.args.get("target") or "prod") if request.args.get("scope") == "prod" else unit_env(name, request.args.get("unit") or name), "logs")
    if request.args.get("scope") == "prod":
        target = request.args.get("target") or "prod"
        prod = remote_site(name, target)
        return jsonify(text=remote_command(name, "logs", target=target, text=True).stdout,
                       sources=[f"journalctl -u {service_name(prod.get('PROD_FOLDER') or name)}.service on {prod.get('PROD_IP') or target}"])
    unit = request.args.get("unit") or name
    if name not in site_dirs() or unit not in site_units(name):
        return error("Unknown site or service")
    result = subprocess.run(["journalctl", "-u", service_name(unit), "-n", "300", "--no-pager", "-o", "short-iso"], capture_output=True, text=True, timeout=20)
    # The service's log is in systemd's journal, not a file: the command that reads it is shown instead
    return jsonify(text=result.stdout or result.stderr, sources=[f"journalctl -u {service_name(unit)}.service"])


def remote_site(name, target="prod"):
    if name not in site_dirs():
        abort(400, "Unknown site")
    if not isinstance(target, str) or not TARGET_RE.match(target):
        abort(400, "Unknown environment")
    prod = parse_prod(os.path.join(WEB_ROOT, name), target)
    if not prod or prod["PROD_LOCAL_PATH"] or prod.get("PROD_TARGET") == "windows":
        abort(400, "This site has no remote production target")
    return prod


# ------------------------------------------------------------------- profile

CONFIG_LOCK = threading.Lock()


def save_config():
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


# Bindings and certificates became three actions, and NuGet packages two (1.0.82): those who had them keep what they gave
if any([access.split_bindings(cfg), access.split_packages(cfg)]):
    save_config()


@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    user = me()
    root = user.get("id") == access.ROOT
    if request.method == "GET":
        return jsonify(user=user["username"], name=user.get("name", ""))
    data = request.get_json(silent=True) or {}
    try:
        display_name = access.clean_name(data.get("name", user.get("name", "")))
    except ValueError as exc:
        return error(str(exc))
    ip = client_ip()
    if login_locked(ip):
        return error("Too many failed attempts. Try again later.", 429)
    if not check_password_hash(cfg["password_hash"] if root else user.get("password_hash", ""), data.get("current_password", "")):
        record_failure(ip)
        time.sleep(1)
        return error("The current password is wrong.", 403)
    name = (data.get("username") or "").strip() or user["username"]
    new_password = data.get("new_password") or ""
    if not access.USERNAME_RE.match(name):
        return error("Invalid username. Use letters, numbers and . _ @ - (max 64).")
    if new_password and len(new_password) < 12:
        return error("The new password must be at least 12 characters.")
    if name == user["username"] and not new_password and display_name == user.get("name", ""):
        return error("Nothing to change.")
    with CONFIG_LOCK:
        if name != user["username"] and access.taken(cfg, name, None if root else user["id"]):
            return error("That username is taken.")
        if root:
            cfg["user"] = name
            cfg["name"] = display_name
            if new_password:
                cfg["password_hash"] = generate_password_hash(new_password)
                # A new secret key signs out every other session; this one is re-signed below
                cfg["secret_key"] = secrets.token_hex(32)
                app.secret_key = cfg["secret_key"]
        else:
            user["username"] = name
            user["name"] = display_name
            if new_password:
                user["password_hash"] = generate_password_hash(new_password)
                # The user's other sessions end; this one goes on with the new version
                user["version"] = user.get("version", 1) + 1
                session["version"] = user["version"]
        save_config()
    if not root:
        linux_users_changed()
    session["user"] = name
    return jsonify(ok=True, user=name, name=display_name)


# ------------------------------------------------------------------- users

def site_environments():
    """Every site and its environment keys ("main" and the environments' ids), for the users window"""
    found = {}
    for name in site_dirs():
        # The admin site runs as root and manages everything: it is never given to a user
        if name == ADMIN_SITE:
            continue
        root = os.path.join(WEB_ROOT, name)
        found[name] = ["main", *target_ids(root)]
    return found


def sql_connections_public():
    try:
        with SQL_LOCK:
            return _sql_module.public_connections(SQL_CONNECTIONS)
    except (OSError, ValueError):
        return []


def sql_connection_ids():
    return [item["id"] for item in sql_connections_public()]


def owns_connection(identifier):
    """Admins, and a user who made the connection, may change or remove it; for others it is locked"""
    user = me()
    return access.is_admin(user) or bool(((user.get("connections") or {}).get(identifier) or {}).get("owned"))


def scoped_logins(user, connection_id, found):
    """The logins as a user who is not an administrator sees them: only the databases they may view, and a login that
    reaches any other, or is a sysadmin, kept from them"""
    if access.is_admin(user):
        return found
    mine = [database for database in found["databases"] if access.can_sql(user, connection_id, database, "view")]
    made = set(((user.get("made") or {}).get("logins") or {}).get(connection_id, []))
    logins = []
    for login in found["logins"]:
        reached = [item["database"] for item in login["databases"] + login.get("unlinked", [])]
        # Theirs: it reaches only their databases, and one at least, or they made it. Others' are not shown at all:
        # sa, a sysadmin, a login of another database, or one of no database someone else made
        if login.get("sysadmin") or login["protected"] or not all(database in mine for database in reached):
            continue
        if not reached and login["name"] not in made:
            continue
        logins.append(dict(login, protected=False))
    return dict(found, logins=logins, databases=mine)


def require_sql(identifier, database, action):
    if not access.can_sql(me(), identifier, database, action):
        abort(403, "You do not have access to this database." if database else "You do not have access to this connection.")


# ------------------------------------------------------------------- audit log

AUDIT_LOG = os.path.join(DATA, "audit.log")
AUDIT_LOCK = threading.Lock()


def audit(message):
    """Who did what, and from where: a line per change, for administrators to read"""
    user = me() or {}
    entry = {"time": int(time.time()), "user": user.get("username", "?"), "ip": client_ip() if has_request_context() else "", "message": message}
    try:
        with AUDIT_LOCK:
            fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(entry) + "\n")
    except OSError as exc:
        app.logger.warning("Could not write the audit log: %s", exc)


@app.route("/api/audit", methods=["GET", "DELETE"])
def api_audit():
    require_admin()
    if request.method == "DELETE":
        # Root alone: an administrator cannot remove the lines about what they did
        if me()["id"] != access.ROOT:
            abort(403, "Only the server's own administrator can clear the activity log.")
        with AUDIT_LOCK:
            fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        audit("Cleared the activity log")
    try:
        with open(AUDIT_LOG) as stream:
            lines = stream.readlines()[-1000:]
    except FileNotFoundError:
        lines = []
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        # Root's own lines are shown to root only: other administrators do not know it exists
        if me()["id"] != access.ROOT and entry.get("user") == cfg.get("user"):
            continue
        entries.append(entry)
    return jsonify(entries=entries)


def has_members():
    """Whether this Harbor is a master: a server in Servers has joined it as a member"""
    try:
        with open("/var/lib/harbor/servers.json", encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return False
    return isinstance(found, dict) and any(isinstance(entry, dict) and entry.get("member") for entry in found.values())


def member_names():
    """This master's members, by name"""
    try:
        with open("/var/lib/harbor/servers.json", encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return []
    names = sorted(name for name, entry in found.items() if isinstance(entry, dict) and entry.get("member")) if isinstance(found, dict) else []
    # In the order the administrator chose (Servers, Order), those not in it after them by name
    chosen = [name for name in cfg.get("server_order") or [] if name in names]
    return chosen + [name for name in names if name not in chosen]


def in_server_order(items):
    """A list of the members' (each {server, …}) in the order of the members"""
    order = {name: index for index, name in enumerate(member_names())}
    return sorted(items, key=lambda item: order.get(item.get("server"), len(order))) if isinstance(items, list) else items


@app.route("/api/master/push-users", methods=["POST"])
def api_master_push_users():
    """(On a master) its users sent to its members again: after a member is updated, so it keeps them by its own new
    rules (one before 1.0.107 gave Delete with Files, and kept it)"""
    require_admin()
    said = pushed_said(push_master_users())
    return jsonify(ok=not said, said=said)


@app.route("/api/master/server-order", methods=["PUT"])
def api_master_server_order():
    """(On a master) the order its members are shown in everywhere: the server picker, Web Server, SQL Server, Health,
    Usage, Servers"""
    require_admin()
    order = (request.get_json(silent=True) or {}).get("order")
    members = member_names()
    if not isinstance(order, list) or any(name not in members for name in order):
        return error("Name the members in the order to show them.")
    with CONFIG_LOCK:
        cfg["server_order"] = list(dict.fromkeys(order))
        save_config()
    audit("Changed the order of the servers: " + ", ".join(cfg["server_order"]))
    return jsonify(members=member_names())


NO_WEB = "/var/lib/harbor/no-web"
# This server keeps the sites it has and takes no new one (harbor server web closed)
CLOSED = "/var/lib/harbor/no-new-sites"


def web_state():
    """Whether this server hosts sites: on, closed (keeps its sites, takes no new one) or off"""
    return "off" if os.path.exists(NO_WEB) else "closed" if os.path.exists(CLOSED) else "on"


def takes_no_sites(why):
    """Refused where a site, an environment, a copy or an import would land on this server and it takes none"""
    state = web_state()
    if state == "off":
        abort(400, f"This server hosts no sites: {why} on another (Settings, Servers on the master).")
    if state == "closed":
        abort(400, f"This server keeps its sites and takes no new one: {why} on another (Settings, Servers on the master).")


def members_marked(flag):
    """This master's members whose entry in Servers has the flag: no_web (they host no sites), closed (they keep theirs)"""
    try:
        with open("/var/lib/harbor/servers.json", encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return []
    return [name for name in member_names() if isinstance(found.get(name), dict) and found[name].get(flag)]


def members_no_web():
    """This master's members that host no sites"""
    return members_marked("no_web")


def member_hosts():
    """Each member's own admin host (admin.<its name>, as Servers learnt it), for the page working on it"""
    try:
        with open("/var/lib/harbor/servers.json", encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return {}
    hosts = {}
    for name, entry in (found.items() if isinstance(found, dict) else []):
        if isinstance(entry, dict) and entry.get("member"):
            names = entry.get("names") or []
            own = names[0] if names else entry.get("fqdn") or ""
            if own:
                hosts[name] = f"admin.{own}"
    return hosts


@app.route("/api/me")
def api_me():
    """Who is logged in and what they may do: the page shows only what can be used"""
    user = me()
    admin = access.is_admin(user)
    return jsonify(id=user["id"], username=user["username"], name=user.get("name", ""), admin=admin, root=user["id"] == access.ROOT,
                   # A master (phase 13): its members' sites on Web Server, for an administrator
                   master=admin and has_members(),
                   # The servers the page can work on: an administrator every member, a user those they were given
                   members=member_names() if admin else [name for name in member_names() if (user.get("servers") or {}).get(name)],
                   # Where each member's own admin is (admin.<its name>): the page names things on it by it, not the master's
                   member_hosts=member_hosts(),
                   # The members that host no sites (a SQL Server's): no New site there, no Web Server there for a user
                   members_no_web=members_no_web(),
                   # The members that keep their sites and take no new one: no New site or environment there
                   members_closed=members_marked("closed"),
                   # This server itself: whether it hosts sites (harbor server web), and takes new ones
                   hosts_sites=web_state() != "off", takes_sites=web_state() == "on",
                   # A master's user on a member: whether their environments may be made here, and on which others
                   create_envs=bool(user.get("create_envs")), envs_on=user.get("envs_on") or [],
                   # A member: its users are made on its master
                   member_of=member_of() if admin else None,
                   # On with nothing in it (no site, no connection, nothing they may make) is no access: not shown
                   web=access.has_web(user), sql=access.has_sql(user),
                   # Made as many as they may: the buttons that make one more are not shown (the server refuses it too)
                   # None on a server that takes no new site; on a master, none when no member takes one
                   create_sites=(admin or bool(user.get("create_sites")) and not access.may_make(user, "sites", lambda made: os.path.isdir(os.path.join(WEB_ROOT, made))))
                   and (bool(set(member_names()) - set(members_no_web()) - set(members_marked("closed"))) if admin and has_members() else web_state() == "on"),
                   manage_connections=admin or bool(user.get("manage_connections")),
                   connection_engines=access.connection_engines(user),
                   # Where a new site can have a temporary address, for those who may create sites
                   temp_domains=temp_domains_shown() if admin or user.get("create_sites") else [],
                   usage=_usage.shows_usage(user), usage_over=usage_over(user),
                   # A user's header: the site search, never the server's addresses, files or terminal
                   header=header_shown(user) if admin else {part: part == "search" for part in HEADER_PARTS},
                   sites={} if admin else {site: {"envs": grant.get("envs", []), "new_envs": grant.get("new_envs", False),
                                                  "known": grant.get("known", []), "actions": access.site_actions(grant.get("actions", []))}
                                           for site, grant in (user.get("sites") or {}).items()},
                   # A connection where they have made as many databases as they may: full, so Create and Copy are not shown
                   connections={} if admin else {connection: dict(grant, full=bool(access.may_make(user, "databases", connection=connection)))
                                                  for connection, grant in (user.get("connections") or {}).items()},
                   menu=menu_shown() if admin else {"web": access.has_web(user), "sql": access.has_sql(user)},
                   menu_setting={"web": cfg.get("web_menu", "auto"), "sql": cfg.get("sql_menu", "auto"), "dns": cfg.get("dns_menu", "auto")},
                   # DNS and Mail in the menu, as the page has them from the start (top_menu)
                   dns=top_menu(user)["dns"], mail=top_menu(user)["mail"], mail_server=admin and bool(_mailcow.load()),
                   # Harbor's own mail server (Settings, Notifications): a mailbox's setup can be mailed with it
                   can_mail=can_mail(),
                   dns_account=admin and bool(_cloudflare.load()),
                   dns_domains=[] if admin else user.get("dns_domains", []),
                   cloudflare_parts=list(_cloudflare.PARTS) if admin else user.get("cloudflare_parts", ["dns"]))


LINUX_USERS_LOCK = threading.Lock()
LINUX_USERS = "/var/lib/harbor/users.json"


def sync_linux_users():
    """A Linux user for each user of the admin, as it is now (harbor users sync, phase 8a). Only the admin itself, which
    runs as root: never a test run as a user. One at a time, the last change wins."""
    if not os.path.isfile(HARBOR_COMMAND) or os.geteuid() != 0:
        return
    with LINUX_USERS_LOCK:
        try:
            result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "users", "sync", "--config", CONFIG_PATH],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                app.logger.warning("harbor users sync: %s", (result.stdout + result.stderr).strip() or result.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            app.logger.warning("harbor users sync could not run: %s", exc)


def linux_users_changed():
    threading.Thread(target=sync_linux_users, daemon=True).start()


# What a user's shell sees of /etc: only what a shell, editors, git and dotnet need. Never Apache, SSH, systemd,
# certificates' keys, Harbor or anything else of the server's.
SHELL_ETC = ("passwd", "group", "nsswitch.conf", "profile", "profile.d", "bash.bashrc", "bash_completion", "bash_completion.d",
             "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "localtime", "timezone", "hosts", "host.conf", "resolv.conf", "inputrc",
             "terminfo", "environment", "ssl/certs", "ca-certificates", "alternatives", "vim", "nanorc", "mime.types", "magic",
             "locale.alias", "default/locale", "lsb-release", "os-release", "debian_version", "gitconfig")
SHELL_HIDDEN = ("/srv", "/opt", "/vns", "/root", "/mnt", "/media", "/boot", "/snap")


def local_environment_folders(root):
    """{environment: its folder} of a site's environments on this server, in /var/www"""
    found = {}
    for target in target_ids(root):
        values = parse_prod(root, target) or {}
        folder = (values.get("PROD_LOCAL_PATH") or "").rstrip("/")
        if folder and values.get("PROD_TARGET") != "windows":
            folder = os.path.realpath(folder)
            if folder.startswith(WEB_ROOT + os.sep) and folder != os.path.realpath(root) and os.path.isdir(folder):
                found[target] = folder
    return found


def shell_folders(user):
    """The folders a user's shell holds, and those it hides: the main site's folder of each site they have Terminal on
    there, and each environment's folder here they have it on. In a main site's folder an environment they do not have
    is hidden, so they see neither its files nor (with only an environment) the main site's: the folders above an
    environment's are empty in the shell, but for the way to it."""
    folders, hidden = [], []
    for site in sorted(user.get("sites") or {}):
        root = os.path.realpath(os.path.join(WEB_ROOT, site))
        if not os.path.isdir(root) or os.path.dirname(root) != os.path.realpath(WEB_ROOT):
            continue
        environments = local_environment_folders(root)
        main = access.can(user, site, "main", "terminal")
        if main:
            folders.append(root)
        for target, folder in environments.items():
            if access.can(user, site, target, "terminal"):
                if not (main and folder.startswith(root + os.sep)):
                    folders.append(folder)
            elif main and folder.startswith(root + os.sep):
                hidden.append(folder)
    return folders, hidden


def user_shell(site, cwd):
    """A shell as the user's own Linux user, locked to their sites (phase 8c): a systemd sandbox where /var/www holds
    only the sites they have Terminal on, /home only their own, /etc only what a shell needs, the rest of the system
    read-only, other processes invisible, and no way to become anyone else."""
    root = os.path.join(WEB_ROOT, site)
    inside = os.path.relpath(cwd, root)
    # Never the folders with Harbor's passwords; an environment may be in a folder of its own outside the site's
    if not inside.startswith("..") and inside.split(os.sep)[0] in ("scripts", ".vns-publish"):
        abort(403, "You do not have access to this folder.")
    user = me()
    name = linux_names().get(user["id"], "")
    if not name:
        # Made by the sync that runs after every change to the users; it may still be running
        linux_users_changed()
        abort(409, "Your Linux user is being made. Try again in a moment.")
    folders, hidden = shell_folders(user)
    # The folder it starts in is one they have Terminal on: the main site's (not an environment's in it), or an
    # environment's
    real = os.path.realpath(cwd)
    reaches = [folder for folder in folders if real == folder or real.startswith(folder + os.sep)]
    if not reaches or any(real == folder or real.startswith(folder + os.sep) for folder in hidden):
        abort(403, "You do not have access to do this.")
    properties = ["NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes", "PrivateDevices=yes",
                  "ProtectProc=invisible", "ProtectKernelTunables=yes", "ProtectKernelLogs=yes", "ProtectKernelModules=yes",
                  "ProtectControlGroups=yes", "ProtectHostname=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
                  "TemporaryFileSystem=/etc:ro", "TemporaryFileSystem=/var:ro",
                  *("BindReadOnlyPaths=-/etc/" + item for item in SHELL_ETC),
                  "BindPaths=/home/" + name, *("BindPaths=" + folder for folder in folders),
                  *("InaccessiblePaths=-" + item for item in (*SHELL_HIDDEN, *hidden)), "WorkingDirectory=" + cwd]
    argv = ["/usr/bin/systemd-run", "--quiet", "--pty", "--wait", "--collect", "--uid=" + name, "--gid=" + name]
    for item in properties:
        argv += ["-p", item]
    for item in ("HOME=/home/" + name, "USER=" + name, "LOGNAME=" + name, "SHELL=/bin/bash", "TERM=xterm-256color", "LANG=C.UTF-8",
                 "PATH=/usr/local/bin:/usr/bin:/bin", "DOTNET_ROOT=/usr/lib/dotnet", "DOTNET_CLI_TELEMETRY_OPTOUT=1"):
        argv += ["-E", item]
    return argv + ["/bin/bash", "-l"]


@app.route("/api/users/<user_id>/sftp", methods=["GET", "POST"])
def api_user_sftp(user_id):
    """A user's SFTP address and public keys, and their password and keys set (harbor users sftp-password/-keys)"""
    require_admin()
    user = next((item for item in cfg.get("users", []) if item.get("id") == user_id), None)
    if user is None:
        return error("Unknown user.", 404)
    if request.method == "GET":
        name = linux_names().get(user_id, "")
        keys = ""
        if name:
            try:
                with open(f"/home/{name}/.ssh/authorized_keys") as handle:
                    keys = handle.read()
            except OSError:
                pass
        return jsonify(linux=name, keys=keys, host=request.host.split(":")[0], port=22)
    data = request.get_json(silent=True) or {}
    if not user.get("sftp"):
        return error("Turn on SFTP for the user first.")
    # Their Linux user and its SFTP rights first, as the sync after the save may still be running
    sync_linux_users()
    harbor = ["/usr/bin/python3", HARBOR_COMMAND, "users"]
    steps = []
    if isinstance(data.get("password"), str) and data["password"]:
        steps.append((harbor + ["sftp-password", user_id], None, {"HARBOR_SFTP_PASSWORD": data["password"]}))
    if isinstance(data.get("keys"), str):
        steps.append((harbor + ["sftp-keys", user_id], data["keys"], {}))
    for argv, given, env in steps:
        try:
            result = subprocess.run(argv, input=given or "", capture_output=True, text=True, timeout=60, env=os.environ | env)
        except (OSError, subprocess.SubprocessError) as exc:
            return error(f"SFTP could not be set: {exc}", 502)
        if result.returncode:
            return error((result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout).strip() else "SFTP could not be set.")
    audit("Changed SFTP of " + user["username"])
    return jsonify(ok=True)


def linux_names():
    """{admin user id: Linux user}"""
    try:
        with open(LINUX_USERS) as handle:
            found = json.load(handle)
        return {key: value.get("linux", "") for key, value in found.items() if isinstance(value, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


@app.route("/api/user-templates", methods=["POST"])
def api_user_templates():
    """A template saved: a new one, or the one its id names. Rules to start new users from: what they may do on each
    site and SQL connection they are given, never which."""
    require_admin()
    users_from_master()
    with CONFIG_LOCK:
        templates = access.templates(cfg)
        data = request.get_json(silent=True) or {}
        name = " ".join(str(data.get("name", "")).split())
        same = next((item for item in templates if item["id"] == data.get("id")), None) if data.get("id") else None
        if data.get("id") and same is None:
            return error("The template is gone. Close the window and open it again.", 404)
        if any(item["name"].casefold() == name.casefold() and item is not same for item in templates):
            return error("There is a template of that name already.")
        try:
            template = access.clean_template(data, existing=same)
        except ValueError as exc:
            return error(str(exc))
        cfg["user_templates"] = [template if item is same else item for item in templates] if same else templates + [template]
        save_config()
    audit("Saved the user template " + template["name"])
    return jsonify(template=template, templates=cfg["user_templates"])


@app.route("/api/user-templates/<template_id>", methods=["DELETE"])
def api_user_template(template_id):
    require_admin()
    users_from_master()
    with CONFIG_LOCK:
        templates = access.templates(cfg)
        gone = next((item for item in templates if item["id"] == template_id), None)
        if gone is None:
            abort(404, "No such template")
        cfg["user_templates"] = [item for item in templates if item is not gone]
        save_config()
    audit("Deleted the user template " + gone["name"])
    return jsonify(templates=cfg["user_templates"])


def push_master_users():
    """On a master: its users sent to its members, each with what it may there. {member: what it said}; nothing when
    this server has no members"""
    if not has_members():
        return {}
    from harbor.core.system import System
    from harbor.server import master
    try:
        return master.push_users(System(), cfg.get("users", []))
    except Exception as exc:  # noqa: BLE001  (a member that fails is said; the user is saved here)
        app.logger.exception("Could not send the users to the members")
        return {"members": {"error": str(exc)}}


def pushed_said(result):
    """What went wrong sending the users to the members, in a line: "" when nothing did"""
    # The error names its member already where it says why (linux-02 runs a Harbor without …)
    problems = [item["error"] if item["error"].startswith(name) else f"{name}: {item['error']}" for name, item in result.items() if item.get("error")]
    problems += [f"{name} kept out {', '.join(item['skipped'])}" for name, item in result.items() if item.get("skipped")]
    return "; ".join(problems)


def hosts_nothing(user):
    """On a master: a user's access here is Cloudflare's alone, as it hosts nothing (their sites and SQL are on the
    members, under servers)"""
    if has_members():
        user.update(web=False, create_sites=False, sftp=False, sites={}, sql=False, manage_connections=False, connections={})
    return user


def mail_login(user, password, asked):
    """A user's login mailed to them, when it was asked for as they were made or changed: {mailed: to} or {mail_error:
    why}, nothing when it was not asked. The user is saved either way. With no password given now (a user changed, an
    email added later), their password is not known: the mail has a link to choose one, as Forgot password's, for days"""
    if not asked:
        return {}
    to = access.mail_of(user)
    if not to:
        return {"mail_error": "There is no email to send it to."}
    if not can_mail():
        return {"mail_error": "Harbor cannot send mail yet: set up the mail server under Notifications."}
    host = ADMIN_SITE or request.host
    link = f"https://{ADMIN_SITE}/" if ADMIN_SITE else request.host_url
    if user.get("disabled"):
        return {"mail_error": "The user is locked: unlock them first."}
    report = _keep_alive_module.report
    days = _password_reset.WELCOME_MINUTES // (24 * 60)
    choose = "" if password else reset_link(_password_reset.make(RESET_LINKS, user["id"], minutes=_password_reset.WELCOME_MINUTES))
    body = (f"{'Hi ' + user['name'] + '. ' if user.get('name') else ''}An account on Harbor ({host}) is ready for you.\n\n"
            f"Address: {link}\nUsername: {user['username']}\n"
            + (f"Password: {password}\n\nChange the password under Profile once you are in, and delete this mail.\n" if password
               else f"\nChoose your password with this link. It works once, within {days} days:\n{choose}\n"))
    try:
        report.send(report.load_mail(), [to], "Your login for Harbor", body,
                    html=report.login_mail_html(host, user.get("name", ""), user["username"], password, link, choose=choose or None, days=days))
    except Exception as exc:  # noqa: BLE001  (smtplib, ssl and socket each have their own: said, the user is saved)
        app.logger.exception("Could not mail the login")
        return {"mail_error": f"The user is saved, but the mail was not sent: {exc}"}
    audit(f"Mailed the login of {user['username']} to them")
    return {"mailed": to}


@app.route("/api/users", methods=["GET", "POST"])
def api_users():
    require_admin()
    if request.method == "GET":
        sites = site_environments()
        names = linux_names()
        logins = last_logins()
        listed = [dict(user, linux=names.get(user.get("id"), ""), last_login=logins.get(user.get("id"))) for user in access.visible_users(cfg, me())]
        return jsonify(users=listed, sites=sites, connections=sql_connections_public(),
                       site_actions=access.SITE_ACTIONS, sql_actions=access.SQL_ACTIONS, templates=access.templates(cfg),
                       dns_zones=dns_zone_names(), **mail_domains_for_users(), can_mail=can_mail(),
                       # On a master: the members a user can be given access on, in the user window
                       members=member_names() if has_members() and not member_of() else [])
    users_from_master()
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if len(password) < 12:
        return error("The password must be at least 12 characters.")
    with CONFIG_LOCK:
        try:
            user = access.clean(data, sites=site_environments(), connections=sql_connection_ids())
        except ValueError as exc:
            return error(str(exc))
        if access.taken(cfg, user["username"]):
            return error("That username is taken.")
        hosts_nothing(user)
        user["password_hash"] = generate_password_hash(password)
        user["created_by"] = me()["id"]
        cfg.setdefault("users", []).append(user)
        save_config()
    linux_users_changed()
    audit("Created user " + user["username"] + " (" + user["role"] + ")")
    # A size limit given or taken away: the hourly check runs while one is there
    try:
        sync_usage_job(check_now=True)
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("Could not set the usage check's cron job")
    pushed = pushed_said(push_master_users())
    return jsonify(user=access.public(user), **mail_login(user, password, data.get("send_login") is True), **({"members_error": pushed} if pushed else {}))


@app.route("/api/users/<user_id>", methods=["PUT", "DELETE"])
def api_user(user_id):
    require_admin()
    users_from_master()
    with CONFIG_LOCK:
        users = cfg.setdefault("users", [])
        # Root is not among them: no one can change or remove it here, or learn that it exists
        existing = next((user for user in users if user.get("id") == user_id), None)
        if existing is None:
            return error("Unknown user.", 404)
        if user_id == me()["id"]:
            return error("Change your own name and password under Profile. Another administrator can change your access.")
        if request.method == "DELETE":
            users.remove(existing)
            save_config()
            linux_users_changed()
            audit("Deleted user " + existing["username"])
            pushed = pushed_said(push_master_users())
            return jsonify(ok=True, **({"members_error": pushed} if pushed else {}))
        data = request.get_json(silent=True) or {}
        try:
            user = access.clean(data, existing, sites=site_environments(), connections=sql_connection_ids())
        except ValueError as exc:
            return error(str(exc))
        if access.taken(cfg, user["username"], user_id):
            return error("That username is taken.")
        hosts_nothing(user)
        password = data.get("password") or ""
        if password:
            if len(password) < 12:
                return error("The password must be at least 12 characters.")
            user["password_hash"] = generate_password_hash(password)
        # A new password ends the user's other sessions. Access is read again on every request, so a change to it
        # counts at once without logging them out; a locked user is turned away by the same check.
        user["version"] = existing.get("version", 1) + (1 if password else 0)
        for key in ("created_by", "start_view"):
            if key in existing:
                user[key] = existing[key]
        users[users.index(existing)] = user
        save_config()
    linux_users_changed()
    audit("Changed user " + user["username"])
    # A size limit given or taken away: the hourly check runs while one is there
    try:
        sync_usage_job(check_now=True)
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("Could not set the usage check's cron job")
    pushed = pushed_said(push_master_users())
    return jsonify(user=access.public(user), **mail_login(user, password, data.get("send_login") is True), **({"members_error": pushed} if pushed else {}))


# ------------------------------------------------------------- terminal API

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
BUFFER_MAX = 400 * 1024
MAX_SESSIONS = 12
def dotnet_root_dir():
    """Where .NET is installed. Login shells get DOTNET_ROOT from /etc/profile.d, tasks started by the service do not."""
    if os.environ.get("DOTNET_ROOT"):
        return os.environ["DOTNET_ROOT"]
    exe = shutil.which("dotnet")
    return os.path.dirname(os.path.realpath(exe)) if exe else ""


DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def terminal_activity_sites(payload):
    kind = payload.get("kind")
    if kind == "shell" or (kind == "remote_tool" and payload.get("tool") == "shell"):
        return []
    if kind in ("bulk", "delete_user_made"):
        return payload.get("sites", [])
    return [payload["site"]] if payload.get("site") else []


def terminal_targets(payload):
    """The environments a task works on, so only their cards show it. Empty is all of them."""
    kind = payload.get("kind")
    if kind in ("sync_env", "data_sync"):
        destination = payload.get("destination")
        items = destination if isinstance(destination, list) else str(destination or "").split(",")
        if "all" in items:
            return []
        return [str(item) for item in [payload.get("source"), *items] if item and item != "main"]
    if kind == "move_env":
        return [str(payload.get("environment_id") or "prod")]
    if kind in ("create_prod", "register_env"):
        return ["(new)"]  # A new environment has no card yet
    if kind == "data_sync_settings":
        return [] if payload.get("id") == "main" else [str(payload.get("id") or "prod")]
    if terminal_scope(payload) == "prod" and kind != "publish_both" and kind != "bulk":
        return [str(payload.get("target") or "prod")]
    return []


def terminal_process(payload):
    """An action, shown in a window of its own and listed under Processes, or a terminal to type in (a shell)"""
    return terminal_auto_close(payload) and payload.get("kind") not in ("shell", "remote_tool")


def terminal_scope(payload):
    kind = payload.get("kind")
    if kind == "publish_both" and payload.get("environments_only"):
        return "prod"
    if kind == "publish_both" or (kind == "bulk" and payload.get("action") in ("both", "clear_logs_all")):
        return "all"
    if kind == "bulk":
        return "prod" if payload.get("action") in ("prod", "clear_logs_prod") else "main"
    if kind == "move_site":
        return "all"
    if payload.get("scope") in ("all", "prod", "test", "main"):
        return "main" if payload["scope"] == "test" else payload["scope"]
    if kind in ("remote_tool", "create_prod", "sync_env", "move_env", "register_env", "data_sync", "data_sync_settings", "env_script"):
        return "prod"
    if kind == "script":
        script = payload.get("script", "")
        if script.endswith("_prod.sh") or script == "create_prod.sh":
            return "prod"
    return "main"


def terminal_auto_close(payload):
    kind = payload.get("kind")
    if kind in ("shell", "create_site", "setup_site", "copy_site", "register_env", "create_prod", "sync_env", "data_sync", "data_sync_settings", "move_env", "move_site", "adopt_site", "release_site", "delete_site", "delete_user_made", "change_binding", "prod_action", "dotnet_package", "fix_permissions", "publish_both", "bulk", "update_package", "sql_action", "install_sql_tools", "server_tool", "servers",
                # Moving sites and environments from Windows: an action with its steps, not a terminal
                "windows_import", "windows_refresh", "windows_refresh_all", "windows_take_over",
                "windows_move", "windows_move_refresh", "windows_move_refresh_all", "windows_move_take_over",
                # A site's database converted (Settings, Convert)
                "convert_site", "convert_delete", "convert_rollback", "convert_move", "sql_transfer", "site_export", "export_restore"):
        return True
    if kind == "remote_tool":
        return payload.get("tool") == "shell"
    if kind == "apache":
        return payload.get("tool") == "restart"
    if kind == "env_script":
        return True
    if kind == "script":
        return payload.get("script") not in {
            "check_service.sh", "edit_service.sh", "edit_conf.sh",
        }
    return False


def terminal_output_problem(text):
    return _output_problem.output_problem(text)


class TermSession:
    def __init__(self, title, argv, cwd, extra_env=None, auto_close=False, update_root=None, sites=None, scope="main", targets=None,
                 owner=None, locked=False, after=None):
        self.id = uuid.uuid4().hex[:12]
        # Who started it; a locked task takes no keys (a user's), so Ctrl+C cannot leave it for a shell;
        # after runs when it has ended, with its exit status (what a user created becomes theirs)
        self.owner = owner
        self.locked = locked
        self.after = after
        self.title = title
        self.sites = sites or []
        self.scope = scope
        self.targets = targets or []
        self.created = time.time()
        self.exit = None
        self.auto_close = auto_close
        self.output_problem = False
        self.output_tail = ""
        self.update_root = update_root
        self.update_fingerprint = _publish_module.fingerprint(update_root) if update_root else None
        self.no_outdated = False
        self.buffer = bytearray()
        self.subscribers = set()
        self.lock = threading.Lock()
        env = {
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "HOME": "/root",
            "USER": "root",
            "SHELL": "/bin/bash",
            "PATH": os.environ.get("PATH", DEFAULT_PATH),
            # harbor commands report their steps for the task window (harbor/core/system.py)
            "HARBOR_PROGRESS": "1",
        }
        dotnet_root = dotnet_root_dir()
        if dotnet_root:
            env["DOTNET_ROOT"] = dotnet_root
        env.update(extra_env or {})
        self.proc = ptyprocess.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(30, 120))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                data = os.read(self.proc.fd, 4096)
            except OSError:
                break
            if not data:
                break
            lines = (self.output_tail + decoder.decode(data)).split("\n")
            self.output_tail = lines.pop()
            if self.update_root:
                self.no_outdated |= "No outdated dependencies were detected" in "\n".join(lines + [self.output_tail])
            self.output_problem |= any(terminal_output_problem(line) for line in lines)
            if len(self.output_tail) > BUFFER_MAX:
                self.output_problem |= terminal_output_problem(self.output_tail)
                self.output_tail = self.output_tail[-4096:]
            with self.lock:
                self.buffer += data
                if len(self.buffer) > BUFFER_MAX:
                    del self.buffer[: len(self.buffer) - BUFFER_MAX]
                for q in list(self.subscribers):
                    q.put(data)
        try:
            self.proc.wait()
        except Exception:
            pass
        self.output_problem |= terminal_output_problem(self.output_tail + decoder.decode(b"", final=True))
        status = self.proc.exitstatus
        self.exit = status if status is not None else -(self.proc.signalstatus or 1)
        if getattr(self, "update_root", None):
            update_status = "error" if self.exit != 0 or self.output_problem else "current" if self.no_outdated else "available"
            try:
                _publish_module.record(self.update_root, "updates", self.update_fingerprint, status=update_status)
            except (OSError, ValueError):
                app.logger.exception("Could not save package update status for %s", self.update_root)
                self.output_problem = True
        REMOTE_STATUS.clear()
        keep_harbor_state()
        if getattr(self, "after", None):
            try:
                with app.app_context():
                    self.after(self.exit)
            except Exception:
                app.logger.exception("Could not finish %s", self.title)
        # A site made, given or deleted: the site groups follow (phase 8b)
        linux_users_changed()
        with self.lock:
            for q in list(self.subscribers):
                q.put(None)

    @property
    def running(self):
        return self.exit is None

    def write(self, text):
        if self.running and not getattr(self, "locked", False):
            os.write(self.proc.fd, text.encode("utf-8"))

    def resize(self, rows, cols):
        if self.running:
            self.proc.setwinsize(max(2, min(rows, 500)), max(2, min(cols, 500)))

    def kill(self):
        if not self.running:
            return
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:
                break
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(0.1)


def prune_sessions():
    now = time.time()
    with SESSIONS_LOCK:
        for sid, s in list(SESSIONS.items()):
            if not s.running and not s.subscribers and now - s.created > 3600:
                del SESSIONS[sid]


def site_arg(data):
    site = data.get("site", "")
    if site not in site_dirs():
        abort(400, "Unknown site")
    return site


DEFAULT_SITE_LINK = "/etc/apache2/sites-enabled/00-vns-default.conf"


def any_name_site(check_taken=True):
    """A site that answers any name no other site has (an empty binding) is the server's: an administrator's to make,
    and only one. A user is asked for a binding, and never told of a site they do not know."""
    if not access.is_admin(me()):
        abort(400, "Give the site a binding: the domain it answers on.")
    if check_taken and default_site_name():
        abort(400, default_site_name() + " already answers any name. Only one site can.")


def default_site_name():
    """The site that answers any name no other site has, as set up by create_site.sh with the binding *"""
    try:
        target = os.readlink(DEFAULT_SITE_LINK)
    except OSError:
        return ""
    name = os.path.basename(target)
    return name[:-5] if name.endswith(".conf") else ""


def dns_name(value, label, fully_qualified=False, short=False):
    """A site folder name, a fully qualified DNS name, or with short also one without a dot"""
    try:
        if short:
            return _normalize_dns_name(value).lower()
        return _normalize_dns_name(value, fully_qualified=True) if fully_qualified else _normalize_site_name(value)
    except ValueError as error:
        abort(400, "Invalid " + label + ": " + str(error))


def display_dns_name(value):
    try:
        return _display_dns_name(value)
    except UnicodeError as error:
        app.logger.warning("Could not decode DNS label %s: %s", value, error)
        return value


def windows_share(value):
    """Normalize \\\\server\\share\\folder to //server/share/folder. A folder below the share is required."""
    if not isinstance(value, str):
        abort(400, "Invalid Windows folder")
    share = value.strip().replace("\\", "/").rstrip("/")
    match = re.fullmatch(r"//([A-Za-z0-9][A-Za-z0-9.-]{0,252})((?:/[^/\x00-\x1f:*?\"<>|]+){2,})", share)
    if not match or any(part in (".", "..") for part in match.group(2).split("/")):
        abort(400, "The Windows folder must look like //server/share/folder")
    return share


def windows_site_folder(base, folder):
    """The IIS site's folder: the share the sites are in, and the site's folder name, like /var/www/<folder> on
    Linux. A share that already ends in the folder name is used as it is, so a full path is not doubled."""
    if not isinstance(base, str):
        abort(400, "Invalid Windows share")
    base = base.strip().replace("\\", "/").rstrip("/")
    if base.rsplit("/", 1)[-1].lower() == folder.lower() and base.count("/") >= 4:
        return windows_share(base)
    return windows_share(base + "/" + folder)


def production_setup_env(data, site, moving=False):
    """Validate modal input for create_prod.sh and keep credentials out of argv."""
    target = data.get("target", "")
    if target not in ("local", "remote", "windows", *(("same",) if moving else ())):
        abort(400, "Invalid production target")
    binding = dns_name(data.get("binding", ""), "production binding", fully_qualified=True)
    # On Windows the folder name is the IIS site's folder in the share, and its name in the admin
    folder = dns_name(data.get("folder", ""), "production site name")
    https = data.get("https", "y")
    if https not in ("y", "n"):
        abort(400, "Invalid production protocol")
    environment = data.get("environment", "Production")
    if environment not in ("Development", "Test", "Staging", "Production"):
        abort(400, "Invalid production environment")

    values = {
        "VNS_PROD_MODAL": "1",
        "VNS_PROD_TARGET": target,
        "VNS_PROD_FOLDER": folder,
        "VNS_PROD_BINDING": binding,
        "VNS_PROD_ENVIRONMENT": environment,
        "VNS_PROD_LOCAL_PATH": "",
        "VNS_PROD_IP": "",
        "VNS_PROD_USER": "",
        "VNS_PROD_PASSWORD": "",
        "VNS_PROD_SSH_PORT": "",
        "VNS_PROD_HTTPS": https,
        "VNS_PROD_SMB_SHARE": "",
        "VNS_PROD_SMB_USER": "",
        "VNS_PROD_SMB_PASSWORD": "",
        "VNS_PROD_SMB_DOMAIN": "",
    }
    test_binding = conf_binding(site).lower()
    if target == "local":
        takes_no_sites("put it")
        local_path = data.get("local_path", "")
        if not isinstance(local_path, str):
            abort(400, "Invalid local production folder")
        site_root = os.path.realpath(os.path.join(WEB_ROOT, site))
        target_path = os.path.realpath(local_path) if os.path.isabs(local_path) else ""
        try:
            within_site = os.path.commonpath((site_root, target_path)) == site_root
        except ValueError:
            within_site = False
        if within_site:
            # A subfolder of the main site's folder, as for a new environment; a copy or migration needs a new one
            if target_path == site_root:
                abort(400, "Choose a subfolder of this site's folder, or a folder of its own")
            if moving and os.path.exists(target_path):
                abort(400, "%s already exists" % target_path)
        else:
            # Outside this site it is a site of its own: its folder, service and Apache config share one name
            name = os.path.basename(target_path)
            if os.path.dirname(target_path) != os.path.realpath(WEB_ROOT):
                abort(400, "The folder must be directly under %s" % WEB_ROOT)
            if os.path.exists(target_path):
                abort(400, "%s already exists" % target_path)
            if moving:
                folder = dns_name(name, "production site name")
                values["VNS_PROD_FOLDER"] = folder
            elif name != folder:
                abort(400, "A folder outside this site must have the same name as the production site: %s/%s" % (WEB_ROOT, folder))
        if service_name(folder) == service_name(site) or binding == test_binding:
            abort(400, "A local production site needs a different name and binding than the test site")
        values["VNS_PROD_LOCAL_PATH"] = target_path
    elif target == "same":
        # move_env.sh uses the login saved for the source
        pass
    elif target == "windows":
        # Another server is one in Servers: what is made where stays known, and its login is kept in one place
        kept = kept_server(data, "windows")
        if not kept:
            abort(400, "Add the Windows server in Servers first, then choose it here")
        data = {**data, "smb_share": kept["share"], "smb_user": kept["user"], "smb_password": kept["password"], "smb_domain": kept["domain"]}
        user = data.get("smb_user", "")
        password = data.get("smb_password", "")
        domain = data.get("smb_domain", "")
        if not isinstance(user, str) or not re.fullmatch(r"[^\x00-\x1f\"/\\\[\]:;|=,+*?<>@]{1,104}", user):
            abort(400, "Invalid SMB username. Enter the domain separately.")
        if not isinstance(password, str) or not 1 <= len(password) <= 4096:
            abort(400, "Invalid SMB password")
        if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,255}", domain):
            abort(400, "Invalid SMB domain")
        values.update({
            "VNS_PROD_SMB_SHARE": windows_site_folder(data.get("smb_share", ""), folder),
            "VNS_PROD_SMB_USER": user,
            "VNS_PROD_SMB_PASSWORD": password,
            "VNS_PROD_SMB_DOMAIN": domain,
        })
    else:
        if not data.get("remote_server"):
            abort(400, "Add the Linux server in Servers first, then choose it here")
        host, user, password, port = ssh_login(data)
        values.update({
            "VNS_PROD_IP": host,
            "VNS_PROD_USER": user,
            "VNS_PROD_PASSWORD": password,
            "VNS_PROD_SSH_PORT": port,
        })
    return values


def kept_server(data, kind):
    """A server chosen from Servers in a dialog, or None: Linux {host, user, port}, reached with Harbor's key; Windows
    {share, user, domain, password}, the password read here, never sent to the page"""
    name = data.get("windows_server" if kind == "windows" else "remote_server")
    if not name:
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        abort(400, "Unknown server")
    from harbor.core.system import System
    from harbor.server import servers
    system = System()
    entry = servers.servers(system, kind).get(name)
    if not entry:
        abort(400, f"No {'Windows' if kind == 'windows' else 'Linux'} server {name} in Servers")
    if kind == "windows":
        share, user, password, domain = servers.windows_login(system, name)
        return {"share": share, "user": user, "password": password, "domain": domain}
    return {"host": entry["host"], "user": entry["user"], "port": str(entry["port"])}


@app.route("/api/servers/kept")
def api_servers_kept():
    """The servers in Servers, for the dialogs that choose one: never a password. A user (a master's) gets the Linux
    servers the master lets them make sites on, for an environment there, and no other"""
    user = me()
    from harbor.core.system import System
    from harbor.server import servers
    # One that hosts no sites (a SQL Server's), or keeps its own and takes no new one, is no place for a site or an
    # environment
    kept = {name: entry for name, entry in learnt_servers().items() if not entry.get("no_web") and not entry.get("closed")}
    if not access.is_admin(user):
        allowed = set(user.get("envs_on") or [])
        kept = {name: entry for name, entry in kept.items() if name in allowed and entry.get("kind") != "windows"}
    found = []
    for name, entry in sorted(kept.items()):
        if entry.get("kind") == "windows":
            found.append({"name": name, "kind": "windows", "label": entry.get("share", ""), "share": entry.get("share", ""),
                          "user": entry.get("user", ""), "domain": entry.get("domain", "")})
        else:
            found.append({"name": name, "kind": "linux", "label": f"{entry['user']}@{entry['host']}", "host": entry["host"],
                          "user": entry["user"], "port": entry["port"], "public": entry.get("public", ""), "fqdn": entry.get("fqdn", ""),
                          "names": entry.get("names") or []})
    return jsonify(servers=found)


@app.route("/api/servers/<name>/wildcard")
def api_server_wildcard(name):
    """The domain a server has a wildcard for: one of its names (admin.<name>) or its full name, where any name under
    it (a made-up one) resolves to the server's public address. Sites there are named under it: umb18.kikayo.dk."""
    require_admin()
    entry = learnt_servers().get(name)
    if not entry or entry.get("kind") == "windows":
        return error("Unknown server", 404)
    targets = {entry.get("public"), entry.get("host")} - {None, ""}
    probe = "harbor-check-" + secrets.token_hex(4)
    for domain in [*(entry.get("names") or []), entry.get("fqdn") or ""]:
        if not domain or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
            continue
        try:
            found = {info[4][0] for info in socket.getaddrinfo(f"{probe}.{domain}.", None, proto=socket.IPPROTO_TCP)}
        except (socket.gaierror, UnicodeError, OSError):
            continue
        if found & targets or via_cloudflare(sorted(found)):
            return jsonify(domain=domain)
    return jsonify(domain="")


def learnt_servers():
    """The servers in Servers, with what their status learns (public address, full name) asked for the ones that do not
    have it yet: so a DNS check behind NAT, or a temporary alias, is right without opening Servers first"""
    from harbor.core.system import System
    from harbor.server import servers
    kept = servers.servers(System())
    missing = [name for name, entry in kept.items() if entry.get("kind") != "windows" and (not entry.get("public") or "fqdn" not in entry or "names" not in entry)]
    for name in missing:
        try:
            subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "servers", "status", name], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
    return servers.servers(System()) if missing else kept


def ssh_login(data):
    """The server, username, password (empty: an SSH key) and port of a Linux server, as entered in a dialog, or the
    one chosen from Servers (Harbor's key, no password)"""
    kept = kept_server(data, "linux")
    if kept:
        return kept["host"], kept["user"], "", kept["port"]
    host = data.get("host", "")
    user = data.get("user", "")
    password = data.get("password", "")
    port = str(data.get("ssh_port", ""))
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        abort(400, "Invalid remote host")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
        abort(400, "Invalid SSH username")
    if not isinstance(password, str) or len(password) > 4096:
        abort(400, "Invalid SSH password")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        abort(400, "Invalid SSH port")
    return host, user, password, port


def create_prod_command(site):
    """harbor env create for this site, with what its installed files tell (what create_prod.sh was rendered with)"""
    root = os.path.realpath(os.path.join(WEB_ROOT, site))
    projects = sorted(glob.glob(os.path.join(root, "*.csproj")))
    if not projects:
        abort(400, "Could not find this site's C# project")
    binding = conf_binding(site)
    if not binding:
        abort(400, "Could not find this site's Apache binding")
    service_text = read_text(os.path.join(SERVICE_DIR, service_name(site) + ".service"))
    working = re.search(r"^WorkingDirectory=(\S+)", service_text, re.M)
    publish_folder = os.path.basename(working.group(1).rstrip("/")) if working else ""
    if not FOLDER_RE.match(publish_folder):
        abort(400, "Could not determine this site's publish folder")
    project_text = read_text(projects[0])
    admin_match = re.search(r"^\s*ServerAdmin\s+(\S+)", read_text(os.path.join(APACHE_SITES, site + ".conf")), re.M)
    admin_mail = admin_match.group(1) if admin_match else ""
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", admin_mail):
        abort(400, "Could not determine this site's administrator email")
    umbraco = "umbraco" if "Umbraco.Cms" in project_text else "empty"

    # harbor env create with this site's values, as its create_prod.sh ran it
    return ["/usr/bin/python3", HARBOR_COMMAND, "env", "create", site, "--site-root", root, "--binding", binding,
            "--project", os.path.splitext(os.path.basename(projects[0]))[0], "--publish-folder", publish_folder,
            "--admin-mail", admin_mail, "--kind", umbraco]


def newest_package_version(package):
    """The newest stable version of a package on nuget.org, for a package added without one"""
    url = "https://api.nuget.org/v3-flatcontainer/%s/index.json" % urllib.parse.quote(package.lower())
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "VNS-Harbor"})
        with urllib.request.urlopen(request, timeout=15) as response:
            versions = json.loads(response.read(4 * 1024 * 1024).decode("utf-8")).get("versions", [])
    except (OSError, ValueError) as exc:
        app.logger.warning("Could not read the versions of %s on nuget.org: %s", package, exc)
        versions = []
    stable = [item for item in versions if isinstance(item, str) and VERSION_RE.fullmatch(item) and "-" not in item]
    if not stable:
        abort(400, "The newest version of %s was not found on nuget.org. Give the version to install." % package)
    return max(stable, key=lambda item: [int(part) if part.isdigit() else 0 for part in item.split("+")[0].split(".")])


def build_command(data):
    """Validate a terminal request and return (title, argv, cwd). User input is never shell code."""
    kind = data.get("kind")
    bash = "/bin/bash"
    # Updating a Python site's packages: what pip said was outdated is asked again afterwards
    if kind == "script" and data.get("script") == "update_packages.sh":
        PIP_OUTDATED.pop(data.get("site"), None)
    if kind in ("windows_move", "windows_move_refresh", "windows_move_refresh_all", "windows_move_take_over"):
        # Environments on Windows moved to a Linux server Harbor knows (harbor/environments/windows_move.py)
        harbor = ["/usr/bin/python3", HARBOR_COMMAND, "env", "windows-move"]
        if kind == "windows_move":
            server = data.get("server")
            if not isinstance(server, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", server):
                abort(400, "Choose the Linux server")
            if any(learnt_servers().get(server, {}).get(flag) for flag in ("no_web", "closed")):
                abort(400, f"{server} takes no new sites: choose another server")
            windows_moves(data)
            return "Copy from Windows", [*harbor, "move", "--server", server], "/"
        pause = ["--pause"] if data.get("pause") is True else []
        if kind == "windows_move_refresh_all":
            return "Refresh from Windows: all moved", [*harbor, "refresh", *pause], "/"
        site = site_arg(data)
        target = target_arg(site, data)
        if kind == "windows_move_refresh":
            return f"Refresh from Windows: {site}", [*harbor, "refresh", site, target, *pause], "/"
        return f"Take over from Windows: {site}", [*harbor, "take-over", site, target], "/"
    if kind in ("windows_import", "windows_refresh", "windows_refresh_all", "windows_take_over"):
        # Sites moved from a live Windows server (harbor/sites/windows_import.py): an administrator's alone
        harbor = ["/usr/bin/python3", HARBOR_COMMAND, "windows"]
        if kind == "windows_import":
            if has_members():
                abort(400, "This server is a master: it hosts nothing. Import the sites on a member.")
            takes_no_sites("import the sites")
            server = windows_server(data)
            if server:
                return "Import from Windows", [*harbor, "import-all", "--server", server], "/"
            share, user, domain = windows_login(data)
            return "Import from Windows", [*harbor, "import-all", share, "--user", user, *(["--domain", domain] if domain else [])], "/"
        if kind == "windows_refresh_all":
            return "Refresh from Windows: all", [*harbor, "refresh-all", *(["--pause"] if data.get("pause") is True else [])], "/"
        site = site_arg(data)
        if kind == "windows_refresh":
            return "Refresh from Windows: " + site, [*harbor, "refresh", site, *(["--pause"] if data.get("pause") is True else [])], "/"
        return "Take over from Windows: " + site, [*harbor, "take-over", site], "/"
    if kind in ("site_export", "export_restore"):
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        title, argv, _ = export_command(data)
        return title, argv, "/"
    if kind == "sql_transfer":
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        title, argv, _ = sql_transfer_command(data)
        return title, argv, "/"
    if kind in ("convert_site", "convert_delete", "convert_rollback", "convert_move"):
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        title, argv, _ = convert_command(data)
        return title, argv, "/"
    if kind == "servers":
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        title, argv, _ = servers_command(data)
        return title, argv, "/"
    if kind == "server_tool":
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        title, argv, _ = server_tool_command(data)
        return title, argv, "/"
    if kind == "install_sql_tools" and has_members():
        abort(400, "This server is a master: it hosts nothing itself. Install the tools on a member.")
    if kind == "install_sql_tools":
        if not os.path.isfile(HARBOR_COMMAND):
            abort(500, "Harbor is missing. Update VNS first.")
        return "Install the Microsoft SQL Server tools", ["/usr/bin/python3", HARBOR_COMMAND, "sql", "install-sql-tools"], "/"
    if kind == "sql_action":
        identifier = data.get("connection")
        if not isinstance(identifier, str):
            abort(400, "Select a SQL connection")
        try:
            with SQL_LOCK:
                _sql_module.connection(SQL_CONNECTIONS, identifier)
            if data.get("action") not in ("create", "backup", "backup_all", "copy", "restore", "drop", "delete_backup"):
                abort(400, "Unknown SQL action")
            if data["action"] != "backup_all":
                _sql_module.identifier(data.get("database", ""))
            if data["action"] in ("copy", "restore"):
                _sql_module.identifier(data.get("target", ""))
            if data.get("login") is not None:
                if data["action"] not in ("create", "restore", "copy"):
                    abort(400, "SQL login creation is only supported for create, restore or copy")
                _sql_module.validate_restore_login(data)
        except ValueError as exc:
            abort(400, str(exc))
        # Never one a site here runs on: it would stop working
        if data["action"] == "drop":
            users = sql_database_usage(identifier).get(str(data.get("database", "")).casefold())
            if users:
                abort(400, f"{data['database']} is used by {', '.join(item['label'] for item in users)}: convert or move it first")
        payload = {key: data[key] for key in ("action", "database", "target", "backup_id", "overwrite", "confirmation", "login", "delete_backups", "delete_login") if key in data}
        # A user is not told whether a database or login of a name is there: one not theirs is not theirs to learn of
        if not is_admin():
            payload["private_names"] = True
        return "SQL: " + data["action"], [sys.executable, SQL_HELPER, SQL_CONNECTIONS, identifier, json.dumps(payload)], "/"
    if kind == "update_system":
        if not ADMIN_SITE:
            abort(400, "The admin site is not configured")
        if not os.path.isfile(HARBOR_COMMAND):
            abort(400, "Harbor is missing: install it on the server")
        # The newest release; the release that is fetched installs itself, the admin site too (harbor self update)
        target = update_target()
        if not target:
            abort(400, "There is no release to update to yet.")
        return "Update to " + target, ["/usr/bin/python3", HARBOR_COMMAND, "self", "update", "--ref", target], "/"
    if kind == "update_package":
        site = site_arg(data)
        info = site_info(site, {})
        if info["kind"] not in ("csharp", "umbraco") or info.get("admin"):
            abort(400, "Package updates require a C# site")
        package = data.get("package")
        if package is not None and (not isinstance(package, str) or not PACKAGE_RE.fullmatch(package)):
            abort(400, "Invalid package name")
        available = info.get("package_updates", [])
        if info["updates"] != "available" or not available or (
                package is not None and not any(item["name"].lower() == package.lower() for item in available)):
            abort(400, "Run Check before updating this package")
        root = os.path.join(WEB_ROOT, site)
        return "Update packages: " + site, [
            sys.executable, os.path.join(LIB_DIR, "publish_status.py"),
            "upgrade", root, *([package] if package else []),
        ], root
    if kind == "bulk":
        sites = data.get("sites")
        action = data.get("action")
        if (not isinstance(sites, list) or not sites or len(sites) > 100
                or any(not isinstance(site, str) for site in sites)
                or len(set(sites)) != len(sites)
                or action not in ("check", "add", "remove", "local", "prod", "both", "outdated",
                                  "clear_logs", "clear_logs_prod", "clear_logs_all", "sync_settings", "sync_data", "sync_all")):
            abort(400, "Invalid multi action selection")
        tasks = []
        for name in sites:
            site = site_arg({"site": name})
            info = site_info(site, {})
            if info["kind"] not in ("csharp", "umbraco") or info.get("admin"):
                abort(400, "Multi actions require non-admin C# sites")
            if not any(os.path.isfile(path) for path in glob.glob(os.path.join(WEB_ROOT, site, "*.csproj"))):
                abort(400, "Multi actions require a C# project file in the site root")
            scripts = info["scripts"]
            targets = target_ids(os.path.join(WEB_ROOT, site))
            if action == "prod" and not targets:
                tasks.append({"site": site, "skip": "No production publish script"})
                continue
            # Publish all on a site with only its main site publishes the main site
            if action == "both" and not targets:
                action_here = "local"
            else:
                action_here = action
            if action.startswith("clear_logs"):
                # Umbraco's logs: of the main site, of every environment, or all of them, each with its own script.
                # Every environment is a task of its own, named after it in the output.
                if info["kind"] != "umbraco":
                    tasks.append({"site": site, "skip": "Not an Umbraco site"})
                    continue
                found = 0
                if action in ("clear_logs", "clear_logs_all"):
                    script = next((name for name in ("clear_logs_local.sh", "clear_logs.sh") if name in scripts), "")
                    if script:
                        _, argv, cwd = build_command({"kind": "script", "site": site, "script": script})
                        tasks.append({"site": site, "argv": argv, "cwd": cwd})
                        found += 1
                if action in ("clear_logs_prod", "clear_logs_all"):
                    root = os.path.join(WEB_ROOT, site)
                    for target in allowed_targets(site, publishable_targets(site, root), "clear_logs"):
                        if "clear_logs_prod.sh" not in target_scripts(site, target, info["kind"]):
                            continue
                        _, argv, cwd = build_command({"kind": "script", "site": site, "script": "clear_logs_prod.sh", "target": target})
                        tasks.append({"site": f"{site} · {target}", "argv": argv, "cwd": cwd})
                        found += 1
                if not found:
                    tasks.append({"site": site, "skip": "No logs to clear" if action == "clear_logs" else "No environment with a clear_logs script"})
                continue
            if action.startswith("sync_"):
                task = multi_sync_task(site, info, action)
                tasks.append({"site": site, **task})
                continue
            if action in ("add", "remove"):
                payload = {"kind": "dotnet_package", "site": site, "action": action,
                           "package": data.get("package"), "version": data.get("version", "")}
            elif action in ("check", "outdated"):
                projects = sorted(glob.glob(os.path.join(WEB_ROOT, site, "*.csproj")))
                if len(projects) != 1:
                    abort(400, "Expected exactly one C# project in this site")
                argv = [sys.executable, os.path.join(LIB_DIR, "publish_status.py"),
                        "check", os.path.dirname(projects[0])] if action == "check" else ["dotnet", "outdated", "--upgrade"]
                tasks.append({"site": site, "argv": argv,
                              "cwd": os.path.dirname(projects[0])})
                continue
            elif action_here == "both":
                payload = {"kind": "publish_both", "site": site}
            elif action == "prod" and targets != ["prod"]:
                # One environment at a time: they build in the same project folder
                root = os.path.join(WEB_ROOT, site)
                for target in targets:
                    build_command({"kind": "script", "site": site, "script": "publish_prod.sh", "target": target})
                active = allowed_targets(site, publishable_targets(site, root), "publish")
                if not active:
                    tasks.append({"site": site, "skip": "Its environments no longer exist on their servers, or you have no access to them"})
                    continue
                tasks.append({"site": site, "cwd": root, "argv": [
                    sys.executable, os.path.join(LIB_DIR, "publish_status.py"), "both", root,
                    *(publish_entry(site, target) for target in active)]})
                continue
            else:
                if action == "prod" and not allowed_targets(site, ["prod"], "publish"):
                    tasks.append({"site": site, "skip": "You have no access to its environment"})
                    continue
                script = "publish_prod.sh" if action_here == "prod" else next(
                    (name for name in ("publish_local.sh", "publish.sh") if name in scripts), "")
                payload = {"kind": "script", "site": site, "script": script}
            _, argv, cwd = build_command(payload)
            tasks.append({"site": site, "argv": argv, "cwd": cwd})
        # One site's package check (its Check button) is named as what it is; several sites are a multi action
        sites_given = [task["site"] for task in tasks]
        title = (f"Check the NuGet packages of {sites_given[0]}" if action == "check" and len(sites_given) == 1
                 else "Check the NuGet packages" if action == "check" else "Multi action: " + action)
        return title, [
            sys.executable, os.path.join(LIB_DIR, "bulk_tasks.py"), json.dumps(tasks),
        ], WEB_ROOT
    if kind == "remote_tool":
        site = site_arg(data)
        target = data.get("target") or "prod"
        remote_site(site, target)
        tool = data.get("tool")
        if tool not in ("check_service", "edit_service", "edit_conf", "shell"):
            abort(400, "Unknown remote tool")
        REMOTE_STATUS.pop(remote_key(site, target), None)
        return "Remote %s: %s" % (tool.replace("_", " "), site), with_target([*harbor_argv("remote_site.sh"), site, tool], target), WEB_ROOT
    if kind == "shell":
        site = data.get("site") or ""
        if site:
            site = site_arg(data)
            cwd = os.path.join(WEB_ROOT, site)
            title = "Shell: " + site
            if data.get("path"):
                cwd = safe_path(data["path"])
                if not os.path.isdir(cwd):
                    abort(400, "Unknown folder")
                title = "Shell: " + (data.get("label") or rel(cwd))[:60]
            if not is_admin():
                return title, user_shell(site, cwd), cwd
            return title, [bash, "-l"], cwd
        return "Shell: server", [bash, "-l"], "/"
    if kind == "publish_both":
        site = site_arg(data)
        local = publish_entry(site)
        # Publish all on the site page publishes the environments only. The multi action also publishes the main site.
        main_too = not data.get("environments_only")
        if main_too:
            if not local:
                abort(400, "This site has no test publish script")
            if not site_migrated(site):
                build_command({"kind": "script", "site": site, "script": os.path.basename(local)})
        root = os.path.join(WEB_ROOT, site)
        targets = target_ids(root)
        if not targets:
            abort(400, "Unknown script")
        for target in targets:
            build_command({"kind": "script", "site": site, "script": "publish_prod.sh", "target": target})
        # A user publishes to the environments they have access to; the others are left as they are. In the order
        # the work goes: the main site, then Development, Test, Staging and Production
        active = environment_order(root, allowed_targets(site, publishable_targets(site, root), "publish"))
        if not active and not main_too:
            abort(400, "No environment to publish to: they no longer exist on their servers")
        return ("Publish all: " if main_too else "Publish environments: ") + site, [
            sys.executable, os.path.join(LIB_DIR, "publish_status.py"),
            "both", root, *([local] if main_too else []),
            *(publish_entry(site, target) for target in active),
        ], root
    if kind == "env_script":
        # An environment's own custom script: here as root, from a folder only root may write; on another Linux server
        # over SSH, as Harbor's other actions there
        site = site_arg(data)
        target = target_arg(site, data)
        script = data.get("script", "")
        if not isinstance(script, str) or not CUSTOM_SCRIPT_RE.fullmatch(script):
            abort(400, "Unknown script")
        folder = env_scripts_dir(site, target)
        title = "%s · %s: %s" % (site, target, script)
        if folder is None:
            remote_site(site, target)
            return title, ["/usr/bin/env", "VNS_TARGET=" + target, "VNS_SCRIPT=" + script, *harbor_argv("remote_site.sh"), site, "run_script"], WEB_ROOT
        path = os.path.realpath(os.path.join(folder, script))
        if os.path.dirname(path) != os.path.realpath(folder) or not os.path.isfile(path):
            abort(400, "Unknown script")
        return title, [bash, runnable(path)], os.path.dirname(folder)
    if kind == "script" and site_migrated(site_arg(data)):
        site = site_arg(data)
        script = data.get("script", "")
        if not SCRIPT_RE.match(script):
            abort(400, "Unknown script")
        target = target_arg(site, data) if data.get("target") else ("prod" if script in HARBOR_TARGET_SCRIPTS else "")
        argv = harbor_script_command(site, script, target)
        root = os.path.join(WEB_ROOT, site)
        if script in ("publish.sh", "publish_local.sh", "publish_prod.sh") and glob.glob(os.path.join(root, "*.csproj")):
            argv = [sys.executable, os.path.join(LIB_DIR, "publish_status.py"),
                    "run", root, target_scope(target) if script == "publish_prod.sh" else "test", *argv]
        return "%s: %s" % (site, script), argv, root
    if kind == "script":
        site = site_arg(data)
        script = data.get("script", "")
        target = target_arg(site, data) if data.get("target") else "prod"
        folder = target_folder(os.path.join(WEB_ROOT, site), target)
        # A site published from a machine: its own scripts in Harbor's state, where only root can write
        if target == "prod" and os.path.isfile(os.path.join(own_scripts_dir(site), script)):
            folder = own_scripts_dir(site)
        path = os.path.realpath(os.path.join(folder, script))
        if not SCRIPT_RE.match(script) or os.path.dirname(path) != os.path.realpath(folder) or not os.path.isfile(path):
            abort(400, "Unknown script")
        argv = [bash, runnable(path)]
        folder = os.path.join(WEB_ROOT, site) if folder == own_scripts_dir(site) else folder
        root = os.path.join(WEB_ROOT, site)
        if script in ("publish.sh", "publish_local.sh", "publish_prod.sh") and glob.glob(os.path.join(root, "*.csproj")):
            argv = [sys.executable, os.path.join(LIB_DIR, "publish_status.py"),
                    "run", root, target_scope(target) if script == "publish_prod.sh" else "test", *argv]
        return "%s: %s" % (site, script), argv, folder
    if kind == "dotnet_package":
        site = site_arg(data)
        if site_info(site, {})["kind"] not in ("csharp", "umbraco"):
            abort(400, "NuGet packages can only be managed for C# sites")
        action = data.get("action", "")
        package = data.get("package", "")
        # Removing can take several at once: one task, one restore after them
        packages = data.get("packages") if action == "remove" and isinstance(data.get("packages"), list) else [package]
        if (action not in ("add", "remove") or not packages or len(packages) > 50
                or any(not isinstance(item, str) or not PACKAGE_RE.fullmatch(item) for item in packages)):
            abort(400, "Invalid package action or name")
        package = packages[0]
        projects = sorted(glob.glob(os.path.join(WEB_ROOT, site, "*.csproj")))
        if len(projects) != 1:
            abort(400, "Expected exactly one C# project in this site")
        version = data.get("version", "")
        if action == "add":
            if not isinstance(version, str) or len(version) > 128 or (version and not VERSION_RE.fullmatch(version)):
                abort(400, "Invalid package version")
            # Added without a restore (that runs as the build user), dotnet would write Version="*": the newest is named
            version = version or newest_package_version(package)
            argv = ["dotnet", "add", "package", package, *(["--version", version] if version else [])]
            title = "Add NuGet package: " + package
        else:
            argv = ["dotnet", "remove", "package", package]
            title = "Remove NuGet package" + ("s: " + ", ".join(packages) if len(packages) > 1 else ": " + package)
        # As root only the project file is changed; the restore, which can run build steps, runs as the build user
        if os.geteuid() == 0:
            restore = [sys.executable, os.path.join(LIB_DIR, "build_as.py"), "--", "dotnet", "restore"]
            changes = [argv + ["--no-restore"]] if argv[1] == "add" else [["dotnet", "remove", "package", item] for item in packages]
            argv = ["/bin/bash", "-c", " && ".join(" ".join(shlex.quote(item) for item in change) for change in [*changes, restore])]
        elif len(packages) > 1:
            argv = ["/bin/bash", "-c", " && ".join("dotnet remove package " + shlex.quote(item) for item in packages)]
        return title + " (" + site + ")", argv, os.path.dirname(projects[0])
    if kind == "create_prod":
        site = site_arg(data)
        # api_term replaces the command with harbor env create and this site's values
        scripts = os.path.join(WEB_ROOT, site, "scripts")
        # A migrated site has no scripts folder (phase 6): it runs in the site's folder
        return "Add environment: " + site, [bash], scripts if os.path.isdir(scripts) else os.path.join(WEB_ROOT, site)
    if kind == "move_env":
        site = site_arg(data)
        target = target_arg(site, {"target": data.get("environment_id")})
        prod = parse_prod(os.path.join(WEB_ROOT, site), target) or {}
        if prod.get("PROD_TARGET") == "windows" and not data.get("copy"):
            abort(400, "An environment on a Windows server cannot be migrated")
        return ("Copy" if data.get("copy") else "Migrate") + " environment: " + site, [*harbor_argv("move_env.sh"), site, target], HARBOR_DIR
    if kind == "move_site":
        site = site_arg(data)
        if site == ADMIN_SITE:
            abort(400, "The admin site cannot be moved")
        if managed_by(site):
            abort(400, "This site is an environment of a main site on another server. Move it from there.")
        return ("Copy" if data.get("copy") else "Migrate") + " site: " + site, [*harbor_argv("move_site.sh"), site], HARBOR_DIR
    if kind == "adopt_site":
        site = site_arg(data)
        main = data.get("main", "")
        if not isinstance(main, str) or main not in site_dirs() or main == site:
            abort(400, "Unknown main site")
        return "Move under %s: %s" % (main, site), [*harbor_argv("adopt_site.sh"), site, main], HARBOR_DIR
    if kind == "release_site":
        site = site_arg(data)
        return "Move out: " + site, [*harbor_argv("adopt_site.sh --release"), site, target_arg(site, data)], HARBOR_DIR
    if kind == "setup_site":
        site = site_arg(data)
        certificate = data.get("certificate", "y")
        if certificate not in ("y", "n"):
            abort(400, "Invalid certificate option")
        binding = data.get("binding", "")
        if not isinstance(binding, str):
            abort(400, "Invalid binding")
        if binding in ("", "*"):
            any_name_site()
            binding = "*"
        else:
            binding = dns_name(binding, "binding", fully_qualified=certificate == "y", short=certificate == "n")
        environment = data.get("environment", "")
        if environment not in ("", "Development", "Test", "Staging", "Production"):
            abort(400, "Invalid environment")
        return "Set up site: " + site, [*harbor_argv("setup_site.sh"), site, binding, certificate, environment], HARBOR_DIR
    if kind == "data_sync_settings":
        site = site_arg(data)
        site_id = data_sync_id(site, data.get("id"))
        return "Sync setup: " + site, [*harbor_argv("data_sync.sh"), site, "settings", site_id], HARBOR_DIR
    if kind == "data_sync":
        site = site_arg(data)
        source = data_sync_id(site, data.get("source"))
        destinations = destination_list(data.get("destination"))
        config = _data_sync_module.load(os.path.join(WEB_ROOT, site))
        for destination in destinations:
            data_sync_id(site, destination)
            try:
                _data_sync_module.kinds(config, source, destination)
            except ValueError as exc:
                abort(400, str(exc))
        # Sync everything: the settings first where they are behind, in the same task
        first = ["--settings-first"] if data.get("settings_first") is True else []
        return "Sync data: " + site, [*harbor_argv("data_sync.sh"), site, "sync", source, ",".join(destinations), *first], HARBOR_DIR
    if kind == "sync_env":
        site = site_arg(data)
        source = data.get("source", "")
        destination = data.get("destination", "")
        targets = target_ids(os.path.join(WEB_ROOT, site))
        if source != "main" and source not in targets:
            abort(400, "Unknown source environment")
        if destination != "all":
            destinations = destination_list(destination)
            if any(item not in targets or item == source for item in destinations):
                abort(400, "Unknown destination environment")
            config = _data_sync_module.load(os.path.join(WEB_ROOT, site))
            for item in destinations:
                try:
                    _data_sync_module.settings_allowed(config, source, item)
                except ValueError as exc:
                    abort(400, str(exc) + ". Change it in Sync setup.")
            destination = ",".join(destinations)
        return "Sync: " + site, [*harbor_argv("sync_env.sh"), site, source, destination], HARBOR_DIR
    if kind == "unit_tool":
        site = site_arg(data)
        unit = data.get("unit", "")
        tool = data.get("tool", "")
        if unit not in site_units(site):
            abort(400, "Unknown service")
        service = "/etc/systemd/system/%s.service" % service_name(unit)
        conf = "/etc/apache2/sites-available/%s.conf" % unit
        if tool == "check_service":
            return "Service: " + unit, [bash, "-c", 'clear; systemctl status "$1" --no-pager -l', "x", service_name(unit)], WEB_ROOT
        if tool == "edit_service":
            code = ('clear; nano "$1"; read -r -p "Reload daemon and restart service? [Y/n]: " a; '
                    'if [[ ! $a =~ ^[nN] ]]; then systemctl daemon-reload && systemctl restart "$2"; systemctl status "$2" --no-pager; fi')
            return "Edit service: " + unit, [bash, "-c", code, "x", service, service_name(unit)], WEB_ROOT
        if tool == "edit_conf":
            code = ('clear; nano "$1"; read -r -p "Test config and reload apache? [Y/n]: " a; '
                    'if [[ ! $a =~ ^[nN] ]]; then apache2ctl configtest && systemctl reload apache2; fi')
            return "Edit config: " + unit, [bash, "-c", code, "x", conf], WEB_ROOT
        abort(400, "Unknown tool")
    if kind == "apache":
        tool = data.get("tool", "")
        if tool == "restart":
            return "Restart Apache", [bash, "-c", 'clear; apache2ctl configtest && systemctl restart apache2 && echo "Apache restarted" ; systemctl status apache2 --no-pager | head -n 12'], WEB_ROOT
        if tool == "configtest":
            return "Apache config test", [bash, "-c", "clear; apache2ctl configtest"], WEB_ROOT
        abort(400, "Unknown tool")
    if kind == "register_env":
        return "Add existing site: " + site_arg(data), [*harbor_argv("register_env.sh"), site_arg(data)], HARBOR_DIR
    if kind == "copy_site":
        takes_no_sites("make the copy")
        source = site_arg(data)
        if managed_by(source):
            abort(400, "This site is an environment of a main site on another server. Make the copy from there.")
        # A site with environments is copied without them: the copy is a site of its own
        if source == ADMIN_SITE:
            abort(400, "The admin site cannot be copied.")
        target = dns_name(data.get("target", ""), "site name")
        binding = dns_name(data.get("binding", ""), "binding", fully_qualified=True)
        if any(service_name(name) == service_name(target) for name in site_dirs()):
            abort(400, "The destination site already exists.")
        database = data.get("database", "")
        if not isinstance(database, str) or len(database) > 128 or any(char in database for char in "\0\r\n;\"'"):
            abort(400, "Invalid database name")
        connection = data.get("connection")
        if connection is not None:
            if not isinstance(connection, dict) or set(connection) != {"server", "database", "username", "password"}:
                abort(400, "Enter SQL server, database, username and password.")
            for key, value in connection.items():
                if not isinstance(value, str) or not value.strip() or len(value) > (128 if key == "database" else 4096) or any(
                        char in value for char in "\0\r\n"):
                    abort(400, "Invalid SQL connection field: " + key)
        argv = [*harbor_argv("copy_site.sh"), source, target, binding]
        project = data.get("project", "")
        if project:
            if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project):
                abort(400, "Invalid project name")
            if len(glob.glob(os.path.join(glob.escape(os.path.join(WEB_ROOT, source)), "*.csproj"))) != 1:
                abort(400, "The site needs exactly one project to rename it")
        if database.strip():
            argv.append(database.strip())
        # A move: the copy takes over, and the site here is removed once it runs (only a site without environments)
        remove = data.get("remove_here")
        if remove:
            if target_ids(os.path.join(WEB_ROOT, source)):
                abort(400, "A site with environments cannot be moved this way: the copy leaves its environments out.")
            argv += ["--remove-source", "all" if data.get("delete_local") == "y" else "config"]
        elif data.get("stop_source"):
            argv.append("--stop-source")
        if project:
            argv = ["/usr/bin/env", "VNS_COPY_PROJECT=" + project, *argv]
        # Set up with HTTPS, as chosen in the dialog (offered when DNS points here)
        if data.get("https") in ("y", "n"):
            argv = ["/usr/bin/env", "VNS_COPY_HTTPS=" + data["https"], *argv]
        return "Copy site: " + source, argv, HARBOR_DIR
    if kind == "create_site":
        takes_no_sites("make the site")
        site = dns_name(data.get("site", ""), "site name")
        site_type = data.get("type", "")
        if site_type not in ("html", "python", "csharp"):
            abort(400, "Invalid site name, type or binding")
        if any(service_name(existing) == service_name(site) for existing in site_dirs()):
            abort(400, "The site already exists")
        certificate = data.get("certificate", "y")
        if certificate not in ("y", "n"):
            abort(400, "Invalid certificate option")
        # * is the site that answers any name. create_site.sh gives it this server's name, and refuses a
        # certificate when that name has no dot. A name without a dot is only for a site without a certificate.
        binding = data.get("binding", "")
        if not isinstance(binding, str):
            abort(400, "Invalid binding")
        if binding in ("", "*"):
            any_name_site()
            binding = "*"
        else:
            binding = dns_name(binding, "binding", fully_qualified=certificate == "y", short=certificate == "n")
            # The certificate is issued as the site is created: it fails while DNS points elsewhere
            if certificate == "y" and not points_here(binding):
                abort(400, display_dns_name(binding) + " does not point to this server yet, so no certificate can be issued. "
                      "Create the site with HTTP only, and use Issue certificate once DNS points here.")
        args = [site, site_type, "", "", "", binding, "", "", certificate]
        if site_type == "csharp" and data.get("external"):
            # Published from an external source: made now from an empty project's build, which its publishes replace
            project = data.get("project") or service_name(site)
            if project == data.get("site"):
                project = service_name(site)
            environment = data.get("environment") or "Production"
            if not isinstance(project, str) or not PROJECT_RE.match(project) or environment not in ("Development", "Test", "Staging", "Production"):
                abort(400, "Invalid project name or environment")
            return "Create: " + site, ["/usr/bin/python3", HARBOR_COMMAND, "site", "create-external", site, "--project", project,
                                       "--binding", binding, "--https", certificate, "--environment", environment], HARBOR_DIR
        if site_type == "csharp":
            legacy = "csharp" in data
            template = data.get("template") or ""
            version = data.get("version") or ""
            if not isinstance(template, str) or not isinstance(version, str):
                abort(400, "Invalid template name or version")
            template = template.strip()
            csharp = (data.get("csharp") or "umbraco") if legacy else ("umbraco" if template.lower() == "umbraco" else "empty")
            if csharp == "umbraco":
                template = ""
            project = data.get("project") or service_name(site)
            if project == data.get("site"):
                project = service_name(site)
            # A new site is where development happens: Development, published to _development
            publish = data.get("publish", "_development")
            if not isinstance(publish, str):
                abort(400, "Invalid publish folder")
            publish = publish.strip() or "."
            build = data.get("build") or "Debug"
            environment = data.get("environment") or ("Production" if build == "Release" else "Development")
            if environment not in ("Development", "Test", "Staging", "Production"):
                abort(400, "Invalid environment")
            version = version.strip()
            if csharp == "umbraco" and not version:
                version = default_umbraco_version()
                if not version:
                    abort(500, "The default Umbraco version could not be determined.")
            if legacy and csharp != "umbraco":
                version = ""
            if csharp not in ("umbraco", "empty") or build not in ("Debug", "Release"):
                abort(400, "Invalid C# options")
            if not PROJECT_RE.match(project) or (publish != "." and not FOLDER_RE.fullmatch(publish)):
                abort(400, "Invalid project name or publish folder")
            if version and not VERSION_RE.fullmatch(version):
                abort(400, "Invalid template version")
            if template and not PROJECT_RE.fullmatch(template):
                abort(400, "Invalid template name")
            if csharp == "empty":
                try:
                    match = next((item for item in installed_dotnet_templates()
                                  if item["short_name"].casefold() == (template or "web").casefold()), None)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    app.logger.error("Could not validate template: %s", exc)
                    abort(502, "Could not validate the template. Check server logs and try again.")
                if match is None:
                    abort(400, "Choose an installed web template by its exact short name.")
                if template:
                    template = match["short_name"]
            package = ""
            if csharp == "empty" and version:
                try:
                    metadata = installed_template_packages().get(template or "web", {})
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    app.logger.error("Could not resolve template package: %s", exc)
                    abort(502, "Could not resolve the installed template package. Check server logs.")
                package = metadata.get("package", "")
                if not package or not PACKAGE_RE.fullmatch(package):
                    abort(400, "A version requires a template from an installed NuGet template package. SDK templates use the installed SDK version.")
            args = [site, site_type, csharp, version, project, binding, publish, build, certificate, template, environment]
            if package:
                args.append(package)
        return "Create: " + site, [*harbor_argv("create_site.sh"), *args], HARBOR_DIR
    if kind == "fix_permissions":
        site = site_arg(data)
        scope = data.get("scope", "test")
        if scope not in ("test", "prod") or site == ADMIN_SITE:
            abort(400, "Invalid permissions target")
        argv = [*harbor_argv("fix_permissions.sh"), site, scope]
        if scope == "prod":
            argv = with_target(argv, target_arg(site, data))
        return "Fix permissions: " + site, argv, HARBOR_DIR
    if kind == "change_binding":
        site = site_arg(data)
        # A name without a dot is allowed here: the script refuses it for a site with a certificate.
        # An empty binding or * makes the site answer any name, which only a site on this server can.
        binding = data.get("binding", "")
        if not isinstance(binding, str):
            abort(400, "Invalid binding")
        if binding in ("", "*"):
            if data.get("scope") == "prod":
                abort(400, "An environment needs a binding")
            any_name_site(check_taken=False)
            binding = "*"
        else:
            binding = dns_name(binding, "binding", short=True)
        argv = [*harbor_argv("change_binding.sh"), site, binding]
        if "aliases" in data:
            aliases = data["aliases"]
            if not isinstance(aliases, str) or len(aliases) > 4096:
                abort(400, "Invalid aliases")
            alias_names = []
            for alias in re.split(r"[\s,]+", aliases.strip()):
                if not alias:
                    continue
                alias = dns_name(alias, "alias", short=True)
                if alias != binding and alias not in alias_names:
                    alias_names.append(alias)
            if len(alias_names) > 99:
                abort(400, "A certificate can include at most 99 aliases")
            aliases = " ".join(alias_names)
            argv.append(aliases)
            argv.append("--yes")
        if data.get("scope") == "prod":
            argv[2:4] = HARBOR_WRAPPERS["change_binding_prod.sh"]
            return "Prod bindings: " + site, with_target(argv, target_arg(site, data)), HARBOR_DIR
        return "Bindings: " + site, argv, HARBOR_DIR
    if kind == "prod_action":
        site = site_arg(data)
        action = data.get("action", "")
        if action not in ("issue_certificate", "change_env"):
            abort(400, "Unknown action")
        args = []
        if action == "change_env":
            environment = data.get("environment", "")
            if environment not in ("Development", "Test", "Staging", "Production"):
                abort(400, "Invalid environment")
            args.append(environment)
        else:
            args.append("--yes")
            certificate_names_point(site, data)
        if data.get("scope") == "test":
            if not os.path.isdir(os.path.join(WEB_ROOT, site)):
                abort(404, "Unknown site")
            return "Test %s: %s" % (action.replace("_", " "), site), [*harbor_argv("test_action.sh"), site, action, *args], HARBOR_DIR
        return "Prod %s: %s" % (action.replace("_", " "), site), with_target([*harbor_argv("prod_action.sh"), site, action, *args], target_arg(site, data)), HARBOR_DIR
    if kind == "delete_user_made":
        # What a deleted user made, deleted with them when the administrator chose so: each site whole, each database
        # with its own login, as their Delete buttons do; a step each
        steps = []
        for site in data.get("sites") or []:
            if site not in site_dirs() or site == ADMIN_SITE:
                abort(400, "Unknown site")
            steps.append({"title": f"Delete {site}", "argv": [*harbor_argv("delete_site.sh"), site, "n", "all"]})
        databases = data.get("databases") or {}
        if not isinstance(databases, dict):
            abort(400, "Expected databases by connection")
        for connection, names in databases.items():
            try:
                with SQL_LOCK:
                    _sql_module.connection(SQL_CONNECTIONS, connection)
                for name in names:
                    _sql_module.identifier(name)
            except (ValueError, TypeError) as exc:
                abort(400, str(exc))
            for name in names:
                payload = {"action": "drop", "database": name, "confirmation": name, "delete_login": True}
                steps.append({"title": f"Delete the database {name}", "argv": [sys.executable, SQL_HELPER, SQL_CONNECTIONS, connection, json.dumps(payload)]})
        if not steps:
            abort(400, "Nothing to delete")
        return "Delete what the user made", [sys.executable, os.path.join(LIB_DIR, "run_steps.py"), json.dumps(steps)], "/"
    if kind == "delete_site":
        site = site_arg(data)
        if site == ADMIN_SITE:
            abort(400, "The admin site cannot delete itself")
        certificates = "y" if data.get("certificates") else "n"
        scope = data.get("scope") or "all"
        if scope not in ("all", "test", "prod"):
            abort(400, "Invalid scope")
        argv = [*harbor_argv("delete_site.sh"), site, certificates, scope]
        if scope == "prod":
            argv = with_target(argv, target_arg(site, data))
            # An environment on another server Harbor cannot reach: only removed here, its site there not touched
            if data.get("only_here") is True:
                argv = ["/usr/bin/env", "VNS_DELETE_ONLY_HERE=1", *argv]
        return "Delete " + scope + ": " + site, argv, HARBOR_DIR
    abort(400, "Unknown task")


def multi_sync_task(site, info, action):
    """Sync from a main site to its environments, as its Sync setup allows and where the user may: the settings
    (sync_settings), content and media (sync_data), or both, the settings first (sync_all). {"argv", "cwd"} or {"skip"}"""
    if info["kind"] != "umbraco":
        return {"skip": "Not an Umbraco site"}
    root = os.path.join(WEB_ROOT, site)
    targets = environment_order(root, target_ids(root))
    if not targets:
        return {"skip": "No environments"}
    config = _data_sync_module.load(root)

    def allowed(check, verb):
        found = []
        for target in allowed_targets(site, targets, verb):
            try:
                check(config, "main", target)
                found.append(target)
            except ValueError:
                pass
        return found

    settings_to = allowed(_data_sync_module.settings_allowed, "sync_settings") if action != "sync_data" else []
    data_to = allowed(_data_sync_module.kinds, "sync_data") if action != "sync_settings" else []
    commands = []
    if data_to:
        commands.append(build_command({"kind": "data_sync", "site": site, "source": "main", "destination": ",".join(data_to),
                                       "settings_first": action == "sync_all"})[1])
    # Settings to the environments that take settings but not data (sync_all), or to all that take them (sync_settings)
    settings_only = [target for target in settings_to if action == "sync_settings" or target not in data_to]
    if settings_only:
        commands.append(build_command({"kind": "sync_env", "site": site, "source": "main", "destination": ",".join(settings_only)})[1])
    if not commands:
        return {"skip": {"sync_settings": "No environment takes settings from it (Sync setup)",
                         "sync_data": "No environment takes content or media from it (Sync setup)"}.get(action, "Sync setup allows nothing from it")}
    if len(commands) == 1:
        return {"argv": commands[0], "cwd": HARBOR_DIR}
    # One after the other in the same task: both use the main site's uSync export
    script = "; ".join(f"{shlex.join(argv)} || failed=1" for argv in commands) + "; exit ${failed:-0}"
    return {"argv": ["/bin/bash", "-c", script], "cwd": HARBOR_DIR}


def allowed_targets(site, targets, action):
    """The environments of a site the logged-in user may do action on"""
    return [target for target in targets if access.can(me(), site, target, action)]


def sees_session(s):
    """Administrators see every task; a user sees the ones they started"""
    return is_admin() or getattr(s, "owner", None) == me()["id"]


# Tasks that run as root by choice, or open a shell: only for administrators
ADMIN_TASKS = {"servers", "server_tool", "delete_user_made", "install_sql_tools", "update_system", "remote_tool", "move_env", "move_site", "adopt_site", "release_site",
               "setup_site", "unit_tool", "apache", "register_env", "windows_import", "windows_refresh", "windows_refresh_all", "windows_take_over",
               "windows_move", "windows_move_refresh", "windows_move_refresh_all", "windows_move_take_over", "convert_site", "convert_delete", "convert_rollback", "convert_move",
               "sql_transfer", "site_export", "export_restore"}


def env_of(site, data, scope_key="scope"):
    """The environment a task is about: "main", or the environment named in it"""
    return (data.get("target") or "prod") if data.get(scope_key) == "prod" else "main"


# ---- who made what (1.0.82): connections, databases and logins, by anyone (an administrator too), for administrators to
# see. What users made before is in their own record (made, and a connection they own); an administrator's before is
# not known.
def note_made(kind, name, connection=None, user_id=None):
    """name of kind (connections, databases, logins), on connection, made now by user_id (the one asking)"""
    if not name:
        return
    with CONFIG_LOCK:
        place = cfg.setdefault("made_by", {}).setdefault(kind, {})
        if connection:
            place = place.setdefault(connection, {})
        place[name] = {"by": user_id or (me() or {}).get("id", ""), "at": int(time.time())}
        save_config()


def forget_made_by(kind, name, connection=None):
    with CONFIG_LOCK:
        place = (cfg.get("made_by") or {}).get(kind) or {}
        if connection:
            place = place.get(connection) or {}
        if place.pop(name, None) is not None:
            save_config()


def maker(kind, name, connection=None):
    """Who made it: {"name", "user"} (user: not an administrator), from the register, else a user's own record of what
    they made; None when not known"""
    place = (cfg.get("made_by") or {}).get(kind) or {}
    if connection:
        place = place.get(connection) or {}
    found = place.get(name)
    if isinstance(found, dict) and found.get("by"):
        person = access.find(cfg, found["by"])
        return {"name": access.label(cfg, found["by"]), "user": bool(person) and not access.is_admin(person)}
    for user in cfg.get("users", []):
        mine = ((kind == "connections" and ((user.get("connections") or {}).get(name) or {}).get("owned"))
                or (kind in ("databases", "logins") and name in (((user.get("made") or {}).get(kind) or {}).get(connection) or []))
                or (kind == "sites" and name in ((user.get("made") or {}).get("sites") or [])))
        if mine:
            return {"name": access.label(cfg, user.get("id")), "user": not access.is_admin(user)}
    return None


def made_by(kind, name, connection=None):
    """Who made it, by name; "" when not known"""
    return (maker(kind, name, connection) or {}).get("name", "")


def given_to(allowed):
    """The users (not administrators, not locked) a thing is given to, by name, for an administrator to see"""
    return sorted((access.label(cfg, user.get("id")) for user in cfg.get("users", [])
                   if isinstance(user, dict) and not access.is_admin(user) and not user.get("disabled") and allowed(user)), key=str.lower)


def remember_maker(data):
    """What a task makes or deletes, kept in the register when it has gone well: a site made or copied, or deleted; a
    database (and its login) made, copied or restored to, or dropped"""
    uid = (me() or {}).get("id", "")
    if data.get("kind") in ("create_site", "copy_site"):
        # The sites there are after it that were not before
        before = set(site_dirs())
        return lambda status: status == 0 and [note_made("sites", made, None, uid) for made in set(site_dirs()) - before]
    if data.get("kind") == "delete_site" and (data.get("scope") or "all") == "all":
        return lambda status: status == 0 and not os.path.isdir(os.path.join(WEB_ROOT, data.get("site", ""))) and forget_made_by("sites", data.get("site", ""))
    if data.get("kind") != "sql_action":
        return None
    connection, action = data.get("connection"), data.get("action")
    if action in ("create", "copy", "restore"):
        made = data.get("database") if action == "create" else data.get("target")
        login = data.get("login") if isinstance(data.get("login"), str) else None
        def note(status):
            if status == 0:
                note_made("databases", made, connection, uid)
                if login:
                    note_made("logins", login, connection, uid)
        return note
    if action == "drop":
        return lambda status: status == 0 and forget_made_by("databases", data.get("database"), connection)
    return None


def authorize_task(data):
    """Stop a task the user may not start. Returns what to do when it has ended, when something the user creates
    should become theirs."""
    user = me()
    if access.is_admin(user):
        return None
    uid = user["id"]
    kind = data.get("kind")
    site = data.get("site", "")
    deny = lambda: abort(403, "You do not have access to do this.")
    need = lambda env, action: access.can(user, site, env, action) or deny()
    if kind in ADMIN_TASKS:
        deny()
    if kind == "sql_action":
        identifier, action, database = data.get("connection"), data.get("action"), data.get("database")
        mapped = {"create": "create", "backup": "backup", "backup_all": "backup", "copy": "copy", "restore": "restore",
                  "drop": "delete", "delete_backup": "backup"}.get(action)
        if not mapped:
            deny()
        if action == "create":
            access.can_sql(user, identifier, None, "create") or deny()
        elif action == "backup_all":
            access.can_sql(user, identifier, "*", "backup") or deny()
        else:
            access.can_sql(user, identifier, database, mapped) or deny()
        # A database the user makes, copies to or restores as is theirs
        made = data.get("database") if action == "create" else data.get("target") if action in ("copy", "restore") else None
        if made:
            if action == "restore" and not access.can_sql(user, identifier, made, "view"):
                # Restoring over a database the user cannot see would replace someone else's
                databases = []
                try:
                    with SQL_LOCK:
                        databases = [item.get("name") for item in _sql_module.databases(_sql_module.connection(SQL_CONNECTIONS, identifier))]
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    pass
                if made in databases:
                    deny()
            # One more of their own: within how many they may make
            if made not in (((user.get("made") or {}).get("databases") or {}).get(identifier) or []):
                refused = access.may_make(user, "databases", connection=identifier)
                if refused:
                    abort(403, refused)
            return lambda status: status == 0 and grant(uid, lambda target: access.grant_database(target, identifier, made)
                                                       | access.record_made(target, "databases", made, identifier))
        if action == "drop":
            # One of theirs gone: it no longer counts against how many they may make
            return lambda status: status == 0 and grant(uid, lambda target: access.forget_made(target, "databases", database, identifier))
        return None
    if not (user.get("web")):
        deny()
    if kind == "shell":
        # The server's shell is an administrator's; a site's runs as the user's own Linux user (phase 8c)
        if not site:
            deny()
        need(env_of(site, data), "terminal")
    elif kind == "script":
        script = data.get("script", "")
        env = (data.get("target") or "prod") if script.endswith("_prod.sh") or data.get("target") else "main"
        action = ("publish" if script.startswith("publish") else "clear_logs" if script.startswith("clear_logs")
                  else "sync_settings" if script == "sync_prod.sh" else "packages" if script == "outdated.sh" else "scripts")
        need(env, action)
    elif kind == "env_script":
        need(data.get("target") or "prod", "scripts")
    elif kind in ("dotnet_package", "update_package"):
        need("main", "packages")
    elif kind == "bulk":
        action = data.get("action")
        for name in data.get("sites") or []:
            site = name
            if action in ("check", "add", "remove", "outdated"):
                need("main", "packages")
            elif action in ("local", "clear_logs"):
                need("main", "publish" if action == "local" else "clear_logs")
            elif action.startswith("sync_"):
                # From the main site to the environments the user may sync to (the task leaves the rest out)
                verbs = {"sync_settings": ["sync_settings"], "sync_data": ["sync_data"]}.get(action, ["sync_settings", "sync_data"])
                any(access.can(user, name, "main", verb) for verb in verbs) or deny()
                any(access.can(user, name, env, verb) for env in target_ids(os.path.join(WEB_ROOT, name)) for verb in verbs) or deny()
            else:
                # Environments are published or cleared where the user has access (build_command leaves the rest out)
                verb = "publish" if action in ("prod", "both") else "clear_logs"
                if action in ("both", "clear_logs_all"):
                    need("main", verb)
                any(access.can(user, name, env, verb) for env in ["main", *target_ids(os.path.join(WEB_ROOT, name))]) or deny()
    elif kind == "publish_both":
        if not data.get("environments_only"):
            need("main", "publish")
        any(access.can(user, site, env, "publish") for env in target_ids(os.path.join(WEB_ROOT, site))) or deny()
    elif kind == "create_prod":
        any(access.can(user, site, env, "create_env") for env in ["main", *target_ids(os.path.join(WEB_ROOT, site))]) or deny()
        # Another server only one the master lets them make sites on (a master's user); a Windows server never a user's
        # A master's user: here only where this server hosts their environments, another server only one that does
        target = data.get("target", "local")
        if user.get("from_master"):
            if target == "windows" or target == "remote" and str(data.get("remote_server", "")) not in (user.get("envs_on") or []):
                deny()
            if target == "local" and not user.get("create_envs"):
                abort(403, "Their environments are not made on this server.")
        elif target in ("windows", "remote"):
            deny()
        before = set(target_ids(os.path.join(WEB_ROOT, site)))
        return lambda status: status == 0 and grant(uid, lambda target: [access.grant_environment(target, site, env)
                                                                    for env in set(target_ids(os.path.join(WEB_ROOT, site))) - before])
    elif kind in ("data_sync_settings",):
        need(data.get("id") or "main", "sync_setup")
    elif kind == "data_sync":
        need(data.get("source") or "main", "sync_data")
        for env in str(data.get("destination", "")).split(","):
            need(env.strip(), "sync_data")
            # The settings first: from the main site, as Sync settings would
            if data.get("settings_first") is True:
                need("main", "sync_settings")
                need(env.strip(), "sync_settings")
    elif kind == "sync_env":
        need(data.get("source") or "main", "sync_settings")
        if data.get("destination") == "all":
            # "All" is every environment the user may sync to
            data["destination"] = ",".join(env for env in target_ids(os.path.join(WEB_ROOT, site))
                                           if env != data.get("source") and access.can(user, site, env, "sync_settings")) or deny()
        else:
            for env in str(data.get("destination", "")).split(","):
                need(env.strip(), "sync_settings")
    elif kind in ("create_site", "copy_site"):
        user.get("create_sites") or deny()
        if kind == "copy_site":
            need("main", "files")
            if data.get("remove_here"):
                need("main", "delete")
        name = data.get("name") or data.get("folder") or data.get("new_site") or ""
        # One more of their own: within how many they may make (those still there count)
        refused = access.may_make(user, "sites", lambda made: os.path.isdir(os.path.join(WEB_ROOT, made)))
        if refused:
            abort(403, refused)
        before = set(site_dirs())
        return lambda status: status == 0 and grant(uid, lambda target: [access.grant_site(target, made) | access.record_made(target, "sites", made)
                                                                         for made in set(site_dirs()) - before])
    elif kind == "fix_permissions":
        need((data.get("target") or "prod") if data.get("scope") == "prod" else "main", "files")
    elif kind == "change_binding":
        need(env_of(site, data), "bindings")
    elif kind == "prod_action":
        need("main" if data.get("scope") == "test" else data.get("target") or "prod",
             "environment" if data.get("action") == "change_env" else "certificate")
    elif kind == "delete_site":
        scope = data.get("scope") or "all"
        if scope == "all":
            for env in ["main", *target_ids(os.path.join(WEB_ROOT, site))]:
                need(env, "delete")
            # Gone: it no longer counts against how many sites they may make
            return lambda status: status == 0 and not os.path.isdir(os.path.join(WEB_ROOT, site)) and grant(uid, lambda target: access.forget_made(target, "sites", site))
        else:
            need("main" if scope == "test" else data.get("target") or "prod", "delete")
    else:
        deny()
    return None


def grant(user_id, change):
    """Give a user something they made, and save it. Runs when a task has ended, so the user is found by id."""
    with CONFIG_LOCK:
        user = access.find(cfg, user_id)
        if user is None or access.is_admin(user):
            return
        if change(user):
            save_config()


def describe(s):
    return {"id": s.id, "title": s.title, "running": s.running, "exit": s.exit,
            "auto_close": s.auto_close, "output_problem": s.output_problem, "sites": s.sites,
            "scope": s.scope, "targets": getattr(s, "targets", []), "process": getattr(s, "process", False),
            "input": not getattr(s, "locked", False), "keep_output": getattr(s, "keep_output", False)}


@app.route("/api/term", methods=["GET", "POST"])
def api_term():
    if request.method == "GET":
        prune_sessions()
        with SESSIONS_LOCK:
            return jsonify(sessions=[describe(s) for s in SESSIONS.values() if sees_session(s)])
    payload = request.get_json(silent=True) or {}
    given, noted = authorize_task(payload), remember_maker(payload)
    # What the user made becomes theirs, and who made it is kept: both when it has ended
    after = (lambda status: [callback(status) for callback in (given, noted) if callback]) if (given or noted) else None
    title, argv, cwd = build_command(payload)
    extra_env = production_setup_env(payload, payload.get("site", "")) if payload.get("kind") == "create_prod" else None
    if payload.get("kind") == "server_tool":
        extra_env = server_tool_command(payload)[2]
    if payload.get("kind") == "servers":
        extra_env = servers_command(payload)[2]
    if payload.get("kind") in ("convert_site", "convert_delete", "convert_rollback", "convert_move"):
        extra_env = convert_command(payload)[2]
    if payload.get("kind") == "sql_transfer":
        extra_env = sql_transfer_command(payload)[2]
    if payload.get("kind") in ("site_export", "export_restore"):
        extra_env = export_command(payload)[2]
    if payload.get("kind") == "delete_site":
        extra_env = delete_databases_env(payload, site_arg(payload))
    if payload.get("kind") == "register_env":
        extra_env = production_setup_env(payload, payload.get("site", ""))
    if payload.get("kind") == "move_env":
        extra_env = production_setup_env(payload, payload.get("site", ""), moving=True)
        # On another Linux server it is set up with HTTP, and HTTPS, when chosen, comes once it runs there: if DNS does not
        # point there yet, it stays on HTTP and the move has still worked. IIS manages a Windows server's certificates.
        source = parse_prod(os.path.join(WEB_ROOT, payload["site"]), target_arg(payload["site"], {"target": payload.get("environment_id")})) or {}
        other_server = not (extra_env["VNS_PROD_TARGET"] == "same" or extra_env["VNS_PROD_TARGET"] == "local" and source.get("PROD_LOCAL_PATH"))
        if other_server and extra_env["VNS_PROD_TARGET"] != "windows":
            extra_env["VNS_MOVE_HTTPS"] = extra_env["VNS_PROD_HTTPS"]
            extra_env["VNS_PROD_HTTPS"] = "n"
        extra_env["VNS_MOVE_DELETE_LOCAL"] = "y" if payload.get("delete_local") == "y" else "n"
        if payload.get("copy"):
            extra_env["VNS_ENV_COPY"] = "1"
            extra_env["VNS_COPY_STOP"] = "y" if payload.get("stop_source") else "n"
            connection = payload.get("connection")
            if connection is not None:
                if (not isinstance(connection, dict) or set(connection) != {"server", "database", "username", "password"}
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(char in value for char in "\0\r\n")
                               for value in connection.values())):
                    abort(400, "Enter SQL server, database, username and password.")
                extra_env["VNS_COPY_SQL"] = json.dumps(connection)
    if payload.get("kind") == "windows_move":
        extra_env = {**(extra_env or {}), "VNS_WINDOWS_MOVES": json.dumps(windows_moves(payload))}
    if payload.get("kind") == "windows_import":
        sites = windows_sites(payload)
        extra_env = {**(extra_env or {}), "VNS_WINDOWS_SITES": json.dumps(sites),
                     **({} if payload.get("server") else {"VNS_SMB_PASSWORD": str(payload.get("password") or "")})}
    if payload.get("kind") in ("sync_env", "data_sync") or (
            payload.get("kind") == "bulk" and str(payload.get("action") or "").startswith("sync_")):
        # Who started it, for the sync history the dashboard in Umbraco shows. A multi sync from the overview too:
        # its syncs inherit it from bulk_tasks
        # By id too: the history shows the user's name as it is now, also after they change it in Profile
        user = me() or {}
        who = user.get("name") or user.get("username", "")
        extra_env = {**(extra_env or {}), "HARBOR_SYNC_WHO": f"{who} (Harbor)" if who else "Harbor",
                     "HARBOR_SYNC_WHO_ID": user.get("id", "")}
    if payload.get("kind") == "move_site":
        host, user, password, port = ssh_login(payload)
        # Its folder, binding and project name on the new server. Environments run the build it makes, so only a
        # site without them can get a new project name.
        site = payload["site"]
        folder = dns_name(payload.get("folder") or site, "folder name")
        binding = dns_name(payload.get("binding") or conf_binding(site), "binding", fully_qualified=True)
        project = payload.get("project") or ""
        if project:
            if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project):
                abort(400, "Invalid project name")
            projects = glob.glob(os.path.join(glob.escape(os.path.join(WEB_ROOT, site)), "*.csproj"))
            if len(projects) != 1:
                abort(400, "The site needs exactly one project to rename it")
            if os.path.splitext(os.path.basename(projects[0]))[0] == project:
                project = ""
            elif target_ids(os.path.join(WEB_ROOT, site)):
                abort(400, "A site with environments keeps its project name, as they run the build it makes")
        extra_env = {"VNS_MOVE_IP": host, "VNS_MOVE_USER": user, "VNS_MOVE_PASSWORD": password, "VNS_MOVE_SSH_PORT": port,
                     "VNS_MOVE_FOLDER": folder, "VNS_MOVE_BINDING": binding, "VNS_MOVE_PROJECT": project,
                     "VNS_MOVE_HTTPS": "y" if payload.get("https") == "y" else "n",
                     "VNS_MOVE_COPY": "1" if payload.get("copy") else "",
                     "VNS_MOVE_DELETE_LOCAL": "y" if not payload.get("copy") and payload.get("delete_local") == "y" else "n",
                     "VNS_COPY_STOP": "y" if payload.get("copy") and payload.get("stop_source") else "n"}
        # The environments that go (their ids), and for a migration what happens to those here that do not
        chosen = payload.get("environments")
        if chosen is not None:
            if not isinstance(chosen, list) or any(not isinstance(item, str) or not TARGET_RE.match(item) or item not in target_ids(os.path.join(WEB_ROOT, site))
                                                   for item in chosen):
                abort(400, "Unknown environment")
            extra_env["VNS_MOVE_ENVIRONMENTS"] = ",".join(chosen)
        bindings = payload.get("env_bindings") or {}
        if bindings:
            if not isinstance(bindings, dict) or any(key not in (chosen or []) or not isinstance(value, str) for key, value in bindings.items()):
                abort(400, "A new binding is for an environment that goes")
            extra_env["VNS_MOVE_ENV_BINDINGS"] = json.dumps({key: dns_name(value, "binding", fully_qualified=True) for key, value in bindings.items()})
        if payload.get("left") is not None:
            if payload.get("left") not in ("delete", "keep"):
                abort(400, "Choose what happens to the environments left here")
            extra_env["VNS_MOVE_LEFT"] = payload["left"]
    if payload.get("kind") == "sql_action" and payload.get("login") is not None:
        extra_env = {"VNS_SQL_LOGIN_PASSWORD": payload["login_password"]}
    if payload.get("kind") == "copy_site" and payload.get("connection") is not None:
        extra_env = {"VNS_COPY_SQL": json.dumps(payload["connection"])}
    if payload.get("kind") == "copy_site" and payload.get("environments"):
        # The environments here copied with it, each with its new binding: harbor checks the rest
        chosen = payload["environments"]
        known = target_ids(os.path.join(WEB_ROOT, site_arg(payload)))
        if (not isinstance(chosen, list) or len(chosen) > 20 or any(
                not isinstance(item, dict) or set(item) != {"id", "binding"} or item["id"] not in known or not isinstance(item["binding"], str)
                for item in chosen)):
            abort(400, "Choose the environments to copy, each with its binding")
        extra_env = {**(extra_env or {}), "VNS_COPY_ENVIRONMENTS": json.dumps([{"id": item["id"], "binding": dns_name(item["binding"], "binding", fully_qualified=True)}
                                                                              for item in chosen])}
    if payload.get("kind") == "create_prod":
        argv = create_prod_command(payload["site"])
    with SESSIONS_LOCK:
        if sum(1 for s in SESSIONS.values() if s.running) >= MAX_SESSIONS:
            return error("Too many running terminals. Close some first.")
        update_root = os.path.join(WEB_ROOT, payload["site"]) if payload.get("kind") == "script" and payload.get("script") == "outdated.sh" and glob.glob(os.path.join(WEB_ROOT, payload["site"], "*.csproj")) else None
        sites = terminal_activity_sites(payload)
        session_obj = TermSession(title, argv, cwd, extra_env, auto_close=terminal_auto_close(payload), update_root=update_root, sites=sites, scope=terminal_scope(payload), targets=terminal_targets(payload),
                                  # A user's tasks take no keys; their shell in a site does (it runs as their Linux user)
                                  owner=me()["id"], locked=not is_admin() and payload.get("kind") != "shell", after=after)
        session_obj.process = terminal_process(payload)
        # A custom script: its exit 10 is done, with its output kept open to read (KEEP_OPEN_EXIT in the page)
        session_obj.keep_output = payload.get("kind") in ("script", "env_script") and payload.get("keep_output") is True
        SESSIONS[session_obj.id] = session_obj
    audit("Started: " + title)
    return jsonify(describe(session_obj))


@app.route("/api/term/<sid>/close", methods=["POST"])
def api_term_close(sid):
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is not None and not sees_session(s):
            abort(404)
        s = SESSIONS.pop(sid, None)
    if s:
        s.kill()
    return jsonify(ok=True)


@sock.route("/ws/term/<sid>")
def ws_term(ws, sid):
    origin = request.headers.get("Origin", "")
    # The master's bridge (harbor master ws) is root here, as its calls are
    if me() is None or urlparse(origin).netloc != request.headers.get("Host", ""):
        return
    s = SESSIONS.get(sid)
    if s is None or not sees_session(s):
        return
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    q = queue.Queue()
    with s.lock:
        replay = bytes(s.buffer)
        s.subscribers.add(q)
        already_exited = not s.running
    try:
        if replay:
            ws.send(json.dumps({"o": decoder.decode(replay)}))
        if already_exited:
            ws.send(json.dumps({"x": s.exit, "output_problem": s.output_problem}))

        def pump():
            while True:
                data = q.get()
                if data is None:
                    ws.send(json.dumps({"x": s.exit, "output_problem": s.output_problem}))
                    return
                text = decoder.decode(data)
                if text:
                    ws.send(json.dumps({"o": text}))

        threading.Thread(target=pump, daemon=True).start()
        while True:
            message = json.loads(ws.receive())
            if message.get("t") == "i":
                s.write(str(message.get("d", "")))
            elif message.get("t") == "r":
                s.resize(int(message.get("rows", 30)), int(message.get("cols", 120)))
    except Exception:
        pass
    finally:
        with s.lock:
            s.subscribers.discard(q)


# ---------------------------------------------------------------- files API

MAX_EDIT_BYTES = 2 * 1024 * 1024


def destination_list(value):
    """One or more destinations, as a list or separated by commas, each once"""
    items = value if isinstance(value, list) else str(value or "").split(",")
    destinations = list(dict.fromkeys(item for item in items if isinstance(item, str) and item))
    if not destinations or len(destinations) > 50:
        abort(400, "Choose at least one destination")
    return destinations


def data_sync_id(site, value):
    """main, or one of the site's environments"""
    if value == "main":
        return value
    return target_arg(site, {"target": value})


@app.route("/api/sites/<site>/custom-scripts", methods=["GET", "POST"])
def custom_script_file(site):
    """Read, save, rename or delete a custom script of the main site or of an environment (?target=). An administrator's
    alone: a script runs as root. Saved as root's, in a folder only root may write, so it may be run."""
    require_admin()
    if site not in site_dirs():
        abort(400, "Unknown site")
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else {"action": "read", "path": request.args.get("path", "")}
    if not isinstance(data, dict) or data.get("action") not in ("read", "save", "delete", "rename", "secure"):
        abort(400, "Expected read, save, rename, delete or secure")
    # secure: the whole folder made root's, no script named
    script = data.get("path") if data["action"] != "secure" else "folder.sh"
    names = [script, data.get("name")] if data["action"] == "rename" else [script]
    if any(not isinstance(name, str) or not CUSTOM_SCRIPT_RE.fullmatch(name) for name in names):
        abort(400, "Use letters, numbers, . _ and - for the script's name, ending in .sh")
    target = request.args.get("target") or "main"
    if target == "main":
        folder = custom_scripts_dir(site, script)
    else:
        target = target_arg(site, {"target": target})
        folder = env_scripts_dir(site, target)
    if folder is None:
        remote_site(site, target)
        REMOTE_STATUS.pop(remote_key(site, target), None)
        result = remote_command(site, "scripts", timeout=60, target=target, input=json.dumps(data).encode("utf-8"),
                                env={**os.environ, "VNS_TARGET": target, "VNS_SCRIPT": script})
        try:
            response = json.loads(result.stdout)
        except ValueError:
            abort(502, "Invalid answer from the server")
        if isinstance(response, dict) and response.get("error"):
            abort(400, response["error"])
        if data["action"] != "read":
            audit("Made the scripts of %s · %s root's" % (site, target) if data["action"] == "secure" else
                  "%s the script %s of %s · %s" % ({"save": "Saved", "delete": "Deleted", "rename": "Renamed"}[data["action"]],
                                                   script + (" to " + data["name"] if data["action"] == "rename" else ""), site, target))
        return jsonify(response)
    if data["action"] == "secure":
        folder = os.path.join(WEB_ROOT, site, "scripts") if target == "main" else folder
        if os.path.isdir(folder):
            make_root_only(folder)
        audit("Made the scripts of %s · %s root's" % (site, target))
        return jsonify(ok=True)
    path = os.path.join(folder, script)
    if data["action"] == "read":
        try:
            with open(path, encoding="utf-8") as handle:
                return jsonify(content=handle.read())
        except FileNotFoundError:
            return jsonify(content="")
    if data["action"] == "delete":
        if os.path.isfile(path):
            os.remove(path)
        audit("Deleted the script %s of %s · %s" % (script, site, target))
        return jsonify(ok=True)
    if data["action"] == "rename":
        renamed = os.path.join(folder, data["name"])
        if not os.path.isfile(path):
            abort(404, "The script is gone.")
        if os.path.lexists(renamed):
            abort(400, data["name"] + " is there already.")
        os.rename(path, renamed)
        audit("Renamed the script %s of %s · %s to %s" % (script, site, target, data["name"]))
        return jsonify(ok=True)
    content = data.get("content", "")
    if not isinstance(content, str):
        abort(400, "Expected the script's text")
    # The script is saved as root's also in a folder someone else can change: the folder and the scripts not checked
    # are left as they are, and run only once an administrator makes them root's (Make them root's… in the menu)
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o755)
        os.chown(folder, 0, 0)
        os.chmod(folder, 0o755)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    os.chown(path, 0, 0)
    os.chmod(path, 0o755)
    audit("Saved the script %s of %s · %s" % (script, site, target))
    return jsonify(ok=True, safe=scripts_safe(folder))


@app.route("/api/sites/<site>/data-sync", methods=["POST"])
def save_data_sync(site):
    if site not in site_dirs():
        abort(400, "Unknown site")
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        abort(400, "Expected data sync settings")
    site_id = data_sync_id(site, data.get("id"))
    if not access.can(me(), site, site_id, "sync_setup"):
        abort(403, "You do not have access to do this.")
    try:
        settings = _data_sync_module.save(os.path.join(WEB_ROOT, site), site_id, data["settings"])
    except (OSError, ValueError) as exc:
        app.logger.error("Could not save data sync settings for %s: %s", site, exc)
        abort(400, str(exc))
    return jsonify(settings=settings)


# //host/share/folder. It is passed to rclone as an argument, never through a shell, so an admin share such as d$ is fine
WINDOWS_SHARE_RE = re.compile(r"//[A-Za-z0-9][A-Za-z0-9.-]{0,252}/[^\x00-\x1f\"<>|]{1,400}")


def windows_server(data):
    """A Windows server kept in Servers, when the request names one: harbor reads its share and login itself"""
    name = data.get("server")
    if not name:
        return ""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        abort(400, "Unknown Windows server")
    from harbor.core.system import System
    from harbor.server import servers
    if name not in servers.servers(System(), "windows"):
        abort(400, "Unknown Windows server")
    return name


def windows_login(data):
    """The share and login of a Windows server, checked: the share as //host/share/folder"""
    share = str(data.get("share") or "").strip().replace("\\", "/").rstrip("/")
    user = str(data.get("user") or "").strip()
    domain = str(data.get("domain") or "").strip()
    if not WINDOWS_SHARE_RE.fullmatch(share):
        abort(400, "Give the share as //server/share/folder")
    if not re.fullmatch(r"[A-Za-z0-9._@\\-]{1,128}", user) or not re.fullmatch(r"[A-Za-z0-9._-]{0,128}", domain):
        abort(400, "Give the Windows login (and its domain, if it has one)")
    if not isinstance(data.get("password"), str) or not data["password"] or len(data["password"]) > 256:
        abort(400, "Give the Windows password")
    return share, user, domain


def windows_sites(data):
    """The folders chosen to import, with their bindings and temporary aliases, checked"""
    sites = data.get("sites")
    if not isinstance(sites, list) or not sites or len(sites) > 200:
        abort(400, "Choose the sites to import")
    checked = []
    for item in sites:
        if not isinstance(item, dict):
            abort(400, "Invalid site")
        folder = str(item.get("folder") or "")
        if not folder or len(folder) > 200 or any(char in folder for char in "/\\\x00") or folder in (".", ".."):
            abort(400, "Invalid folder")
        binding = dns_name(item.get("binding") or folder, "binding", fully_qualified=True)
        aliases = [dns_name(alias, "alias", fully_qualified=True) for alias in str(item.get("aliases") or "").replace(",", " ").split()]
        checked.append({"folder": folder, "binding": binding, "aliases": ",".join(aliases)})
    return checked


def windows_moves(data):
    """The environments on Windows chosen to move, with their temporary aliases, checked"""
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        abort(400, "Choose the environments to move")
    checked = []
    for item in items:
        if not isinstance(item, dict) or item.get("site") not in site_dirs():
            abort(400, "Unknown site")
        target = target_arg(item["site"], {"target": item.get("target")})
        alias = str(item.get("alias") or "").strip()
        checked.append({"site": item["site"], "target": target, "alias": dns_name(alias, "alias", fully_qualified=True) if alias else ""})
    return checked


@app.route("/api/windows-move")
def api_windows_move():
    """Every site's environments on Windows, and the Linux servers Harbor knows: for Copy from Windows"""
    require_admin()
    from harbor.core.system import System
    from harbor.environments import windows_move
    from harbor.server import servers
    system = System()
    linux = {name: entry for name, entry in learnt_servers().items() if entry.get("kind") != "windows"}
    return jsonify(environments=windows_move.candidates(system), servers=sorted(linux),
                   names={name: alias_domain(entry) for name, entry in linux.items()})


def alias_domain(entry):
    """What a temporary alias on a server goes under: of the names its admin is reached by (admin.<name>), the one whose
    DNS points to the server now (also while it borrows another server's address); else the first, else its full name"""
    names = [item for item in entry.get("names") or [] if isinstance(item, str)]
    addresses = {entry.get("public", ""), entry.get("host", "")} - {""}
    for name in names:
        try:
            if socket.gethostbyname(f"admin.{name}") in addresses:
                return name
        except OSError:
            continue
    return names[0] if names else entry.get("fqdn", "")


@app.route("/api/windows/list", methods=["POST"])
def api_windows_list():
    """The folders in a Windows share, and what each holds (harbor windows list): for Import from Windows"""
    require_admin()
    data = request.get_json(silent=True) or {}
    server = windows_server(data)
    if server:
        argv, password = ["--server", server], ""
    else:
        share, user, domain = windows_login(data)
        argv, password = [share, "--user", user, *(["--domain", domain] if domain else [])], data["password"]
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "windows", "list", *argv],
                                capture_output=True, text=True, timeout=180, env={**os.environ, "VNS_SMB_PASSWORD": password})
    except subprocess.TimeoutExpired:
        return error("The Windows server did not answer in time.", 504)
    if result.returncode != 0:
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        return error(lines[-1] if lines else "The share could not be read.", 400)
    try:
        found = json.loads(result.stdout[result.stdout.index("{"):])
    except ValueError:
        return error("The share could not be read.", 400)
    return jsonify(folders=found.get("folders", []), projects=found.get("projects", []), imported=windows_imported())


# ---- Settings, Convert (phase 11): each site's database, converted between SQLite and Microsoft SQL Server or
# PostgreSQL by harbor site convert. An administrator's alone. A site here (also an environment managed from another
# server's main site) is converted here; an environment on another Linux server is converted by the Harbor there, over
# SSH (harbor site convert-remote). Its conversions are kept where it runs, so either Harbor rolls them back.
def convert_units():
    """Each site here and its environments in a folder here, with its service: (site, unit, label, binding)"""
    found = []
    names = site_dirs()
    adopted = adopted_sites(names)
    for name in names:
        if name in adopted or name == ADMIN_SITE:
            continue
        root = os.path.join(WEB_ROOT, name)
        if os.path.isfile(os.path.join(SERVICE_DIR, service_name(name) + ".service")):
            # The environment it runs as (DOTNET_ENVIRONMENT or ASPNETCORE_ENVIRONMENT), as the badge on its page says
            found.append((name, name, convert_environment(name, "Production"), conf_binding(name) or name))
        if managed_by(name):
            continue
        for target in environment_order(root, target_ids(root)):
            prod = parse_prod(root, target) or {}
            unit = prod.get("PROD_FOLDER") or ""
            if prod.get("PROD_LOCAL_PATH") and NAME_RE.fullmatch(unit) and os.path.isfile(os.path.join(SERVICE_DIR, service_name(unit) + ".service")):
                found.append((name, unit, convert_environment(unit, prod.get("PROD_ENVIRONMENT") or ("Production" if target == "prod" else target.capitalize())),
                              prod.get("PROD_BINDING") or conf_binding(unit) or unit))
    return found


def convert_remote_units():
    """The environments of the sites here that run on another Linux server: (site, target, unit, label, binding, where,
    reach), where is the server's name in Servers (else its address), reach the SSH it is reached by"""
    from harbor.core.system import System
    from harbor.server import servers
    try:
        named = {(entry.get("host") or "").lower(): name for name, entry in servers.servers(System(), "linux").items()}
    except (OSError, ValueError):
        named = {}
    found = []
    names = site_dirs()
    adopted = adopted_sites(names)
    for name in names:
        if name in adopted or name == ADMIN_SITE or managed_by(name):
            continue
        root = os.path.join(WEB_ROOT, name)
        for target in environment_order(root, target_ids(root)):
            prod = parse_prod(root, target) or {}
            unit = prod.get("PROD_FOLDER") or ""
            if prod.get("PROD_LOCAL_PATH") or prod.get("PROD_TARGET") == "windows" or not prod.get("PROD_IP") or not NAME_RE.fullmatch(unit):
                continue
            host = prod["PROD_IP"]
            found.append((name, target, unit, prod.get("PROD_ENVIRONMENT") or ("Production" if target == "prod" else target.capitalize()),
                          prod.get("PROD_BINDING") or unit, named.get(host.lower()) or host, (host.lower(), prod.get("PROD_USER"), prod.get("PROD_SSH_PORT"))))
    return found


def convert_environment(unit, fallback):
    """The environment a service runs as; ASP.NET Core's own default, Production, when it sets none"""
    found = service_environment(unit)
    return fallback if found in ("", "Unknown") else found


def convert_remote_info(site, target, units):
    """harbor site convert-info on the server where these environments run, by its own Harbor: (info, error)"""
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "site", "convert-remote", site, target, "convert-info", *units],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "The server did not answer in time."
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode != 0 or not lines:
        said = [line for line in (result.stderr + result.stdout).splitlines() if line.strip() and not line.startswith("Warning: Permanently added")]
        if any("invalid choice: 'convert-info'" in line or "convert-info" in line and "invalid choice" in line for line in said):
            return None, "Update Harbor on that server: it does not convert sites yet."
        return None, (said[-1] if said else "The server could not be reached.")
    try:
        found = json.loads(lines[-1])
    except ValueError:
        return None, "The server's answer could not be read."
    if "kept" not in found:
        return None, "Update Harbor on that server to convert its sites from here."
    return found, ""


CONVERT_LOCAL_HOSTS = {".", "localhost", "127.0.0.1", "::1", "(local)"}


def convert_host(host):
    """A host as compared: without brackets or a trailing dot (but . is SQL Server's own name for itself)"""
    host = (host or "").strip().strip("[]").casefold()
    return host if host == "." else host.rstrip(".")


# A database server's name looked up (DNS, /etc/hosts): {name: (until, addresses)}, kept five minutes
_HOST_ADDRESSES = {}


def host_addresses(host):
    """A database server's addresses: an address as it is, a name with what it resolves to (sql-01 and 172.23.3.21 are
    the same server). A name that does not resolve is itself"""
    host = convert_host(host)
    if not host or host in CONVERT_LOCAL_HOSTS or re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host):
        return {host}
    kept = _HOST_ADDRESSES.get(host)
    if kept and kept[0] > time.time():
        return kept[1]
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        found = set()
    found = {host, *found}
    _HOST_ADDRESSES[host] = (time.time() + 300, found)
    return found


def same_host(one, other):
    """Whether two hosts are the same database server: by name or address, either way"""
    one, other = convert_host(one), convert_host(other)
    return one == other or bool(host_addresses(one) & host_addresses(other))


def convert_own_addresses():
    try:
        return set(subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split())
    except (OSError, subprocess.SubprocessError):
        return set()


def convert_address_for(remote_host):
    """This server's address as another server reaches it: the one it sends from to that server (ip route get), else
    its first. A connection to this server's own SQL Server (localhost) is given to that server by it"""
    try:
        target = socket.gethostbyname(remote_host)
        found = re.search(r"\bsrc (\S+)", subprocess.run(["ip", "-o", "route", "get", target], capture_output=True, text=True, timeout=5).stdout)
        if found:
            return found[1]
    except (OSError, subprocess.SubprocessError):
        pass
    return next(iter(sorted(convert_own_addresses())), "")


def convert_connection(server, remote):
    """This admin's connection to the database server a conversion used: the same one by its id (made here), else one
    to the same engine, host and port. A server's own (localhost) is never another server's"""
    if not server:
        return None
    try:
        with SQL_LOCK:
            items = _sql_module.connections(SQL_CONNECTIONS)
    except (OSError, ValueError):
        return None
    host, engine, port = convert_host(server.get("host")), server.get("engine"), server.get("port")
    if not remote:
        for item in items:
            if server.get("connection") and item.get("id") == server["connection"]:
                return item
        # This server's own, by whichever of its names (., localhost, 127.0.0.1) or addresses
        if host in CONVERT_LOCAL_HOSTS or host in _sql_module.own_addresses():
            return next((item for item in items if _sql_module.engine(item) == engine and convert_host(item.get("host")) in CONVERT_LOCAL_HOSTS
                         and (not port or str(item.get("port") or "") == str(port))), None)
    if not host or remote and host in CONVERT_LOCAL_HOSTS:
        return None
    # On another server, this server's own SQL Server by this server's address: the localhost connection here, by it
    if remote and host in convert_own_addresses():
        for item in items:
            if _sql_module.engine(item) == engine and convert_host(item.get("host")) in CONVERT_LOCAL_HOSTS \
                    and (not port or str(item.get("port") or "") == str(port)):
                return {**item, "host": server.get("host")}
    for item in items:
        if _sql_module.engine(item) != engine or not same_host(item.get("host"), host):
            continue
        if port and str(item.get("port") or "") != str(port):
            continue
        if remote and convert_host(item.get("host")) in CONVERT_LOCAL_HOSTS:
            continue
        return item
    # None here: the database server's own, when it is a server in Servers (a member, say), lent by its admin
    return borrowed_connection(engine, host, port)


# Each server in Servers' own connections to its database servers, as its admin has them: {server: (until, [connection])},
# read again after a minute
_BORROWED = {}
_BORROWED_LOCK = threading.Lock()


def borrowed_connections():
    """The connections the Linux servers in Servers have to their own SQL Server and PostgreSQL (harbor sql
    own-connections over SSH, side by side, kept a minute): a site's database on one of them is converted with its
    connection, and needs no second one made here. Each by an id of its own (lent:<server>:<engine>:<port>), named
    as it is there, at the address this server reaches that server by"""
    from harbor.core.system import System
    from harbor.server import servers
    system = System()
    try:
        known = servers.servers(system, "linux")
    except (OSError, ValueError):
        return []

    def read(item):
        name, entry = item
        with _BORROWED_LOCK:
            kept = _BORROWED.get(name)
        if kept and kept[0] > time.time():
            return kept[1]
        found = []
        try:
            result = servers.remote(system, entry, REMOTE_HARBOR + "sql own-connections", check=False)
            lines = (result.stdout or "").strip().splitlines() if result.ok else []
            items = json.loads(lines[-1]) if lines else []
        except (OSError, ValueError, KeyError):
            items = []
        for connection in items if isinstance(items, list) else []:
            if not isinstance(connection, dict) or not connection.get("password"):
                continue
            engine = connection.get("engine") or "mssql"
            port = str(connection.get("port") or SQL_DEFAULT_PORTS.get(engine, ""))
            found.append({**connection, "id": f"lent:{name}:{engine}:{port}", "host": entry["host"], "lent_by": name})
        with _BORROWED_LOCK:
            _BORROWED[name] = (time.time() + 60, found)
        return found
    if not known:
        return []
    with ThreadPoolExecutor(max_workers=6) as executor:
        return [connection for found in executor.map(read, sorted(known.items())) for connection in found]


def borrowed_connection(engine, host, port, identifier=""):
    """The database server's own connection, lent by that server (by its id, or by the server's engine, host and port)"""
    if not identifier and (not host or host in CONVERT_LOCAL_HOSTS):
        return None
    for item in borrowed_connections():
        if identifier:
            if item["id"] == identifier:
                return item
            continue
        if (item.get("engine") or "mssql") != engine or not same_host(item.get("host"), host):
            continue
        if port and str(item.get("port") or "") != str(port):
            continue
        return item
    return None


# This server's SQL Server keeps its databases and takes no new one from Convert, Move or Copy (harbor server sql)
SQL_CLOSED = "/var/lib/harbor/no-new-databases"


def keeps_its_databases(connection):
    """Whether a connection's server takes no new database from Convert, Move or Copy: this server's own when it is so
    set, or a server in Servers marked so (one it lends, or one this admin connects to)"""
    host = convert_host((connection or {}).get("host"))
    if host in CONVERT_LOCAL_HOSTS or host in _sql_module.own_addresses():
        return os.path.exists(SQL_CLOSED)
    from harbor.core.system import System
    from harbor.server import servers
    try:
        known = servers.servers(System(), "linux")
    except (OSError, ValueError):
        return False
    if (connection or {}).get("lent_by") in known:
        return bool(known[connection["lent_by"]].get("sql_closed"))
    return any(entry.get("sql_closed") and same_host(entry.get("host"), host) for entry in known.values())


def refuse_closed(connection, what):
    """Refused where a database would end up on a server whose SQL Server keeps its own"""
    if keeps_its_databases(connection):
        abort(400, f"{connection.get('name') or connection.get('host')} keeps its databases: {what} to another server "
                   "(Settings, Servers: SQL Server…)")


def convert_settings(identifier):
    """A connection Convert uses, chosen by its id: one here, or one another server lends"""
    if not isinstance(identifier, str):
        return None
    if identifier.startswith("lent:"):
        return borrowed_connection("", "", "", identifier)
    try:
        with SQL_LOCK:
            return _sql_module.connection(SQL_CONNECTIONS, identifier)
    except (OSError, ValueError):
        return None


def convert_kept_ready(item, remote):
    """What this admin can do with a kept database: roll back to it and delete it, or why not (a connection missing)"""
    missing = [server for server in item.get("servers") or [] if not convert_connection(server, remote)]
    if missing:
        # The server a connection is missing to: the page offers to add it, its host and port filled in
        item["rollback_needs"] = {key: missing[0].get(key) for key in ("engine", "host", "port")}
    old = item.get("server")
    item["can_delete"] = not old or bool(convert_connection(old, remote))
    if not item["can_delete"]:
        item["delete_needs"] = {key: old.get(key) for key in ("engine", "host", "port")}
    item["can_rollback"] = bool(item.get("rollback") and item.get("exists") and not missing)
    item.pop("servers", None)
    item.pop("server", None)
    return item


# ---- Delete site: the databases on servers the site and its environments being deleted run on, offered to be deleted
# with them (never one another site here uses too), by this admin's connection to their server
def database_server(item):
    """The host and port a site's database is on, as its connection string names them (Server=host,port)"""
    server = (item.get("server") or "").strip()
    host, _, port = server.partition(",")
    host = host.split("\\")[0].removeprefix("tcp:")
    return host, (port.strip() if port.strip().isdigit() else "")


def site_databases(site, scope, target):
    """[{unit, label, engine, database, host, port, connection, shared}] of what Delete site deletes"""
    from harbor.core.system import System
    from harbor.sites import convert
    system = System()
    root = os.path.join(WEB_ROOT, site)
    here, there = [], []
    if scope in ("all", "test") and os.path.isfile(os.path.join(SERVICE_DIR, service_name(site) + ".service")):
        here.append((site, convert_environment(site, "Production")))
    if scope in ("all", "prod"):
        for each in target_ids(root) if scope == "all" else [target]:
            prod = parse_prod(root, each) or {}
            unit = prod.get("PROD_FOLDER") or ""
            label = prod.get("PROD_ENVIRONMENT") or ("Production" if each == "prod" else each.capitalize())
            if prod.get("PROD_TARGET") == "windows" or not NAME_RE.fullmatch(unit):
                continue
            if prod.get("PROD_LOCAL_PATH"):
                here.append((unit, label))
            elif prod.get("PROD_IP"):
                there.append((each, unit, label, prod["PROD_IP"]))
    found = []
    def add(item, label, remote, where=""):
        if item.get("engine") not in ("mssql", "postgres") or not item.get("database"):
            return
        host, port = database_server(item)
        # One on the other server's own SQL Server (localhost there) is not reached from here
        own_there = remote and convert_host(host) in CONVERT_LOCAL_HOSTS
        connection = None if own_there else convert_connection({"engine": item["engine"], "host": host, "port": port}, remote)
        found.append({"unit": item.get("unit"), "label": label, "engine": item["engine"], "database": item["database"],
                      "host": f"{where} itself" if own_there else host, "port": port, "connection": (connection or {}).get("id"),
                      "reachable": not own_there, "shared": []})
    for unit, label in here:
        add(convert.describe(system, unit), label, False)
    for each, unit, label, host in there:
        info, _why = convert_remote_info(site, each, [unit])
        for item in (info or {}).get("sites", []):
            add(item, label, True, host)
    # Never one another site here runs on too: it is kept, and said so
    if found:
        deleted = {item["unit"] for item in found} | {unit for unit, _label in here}
        for _site, unit, _label, _binding in convert_units():
            if unit in deleted:
                continue
            other = convert.describe(system, unit)
            host, _port = database_server(other)
            for item in found:
                if other.get("engine") == item["engine"] and (other.get("database") or "").lower() == item["database"].lower() \
                        and (convert_host(host) == convert_host(item["host"]) or {convert_host(host), convert_host(item["host"])} <= CONVERT_LOCAL_HOSTS):
                    item["shared"].append(unit)
    return found


@app.route("/api/sites/<site>/databases")
def api_site_databases(site):
    """What Delete site can delete of the site's databases on servers: an administrator's (it needs the connections)"""
    if not access.is_admin(me()):
        return jsonify(databases=[])
    if site not in site_dirs():
        abort(400, "Unknown site")
    scope = request.args.get("scope") or "all"
    if scope not in ("all", "test", "prod"):
        abort(400, "Invalid scope")
    target = target_arg(site, request.args) if scope == "prod" else ""
    return jsonify(databases=site_databases(site, scope, target))


def delete_databases_env(data, site):
    """The databases chosen to be deleted with a site, with this admin's connection to each (in the task's
    environment, never its command line): only those Delete site offers"""
    chosen = data.get("databases")
    if not chosen or not access.is_admin(me()):
        return None
    if not isinstance(chosen, list) or not all(isinstance(item, str) for item in chosen):
        abort(400, "Choose the databases to delete")
    scope = data.get("scope") or "all"
    target = target_arg(site, data) if scope == "prod" else ""
    wanted = []
    try:
        with SQL_LOCK:
            items = {item["id"]: item for item in _sql_module.connections(SQL_CONNECTIONS)}
    except (OSError, ValueError):
        items = {}
    for item in site_databases(site, scope, target):
        if item["database"] not in chosen or item["shared"]:
            continue
        settings = items.get(item.get("connection") or "")
        if not settings:
            abort(400, f"Add a connection to {item['host']} on the SQL Server page to delete {item['database']}")
        if convert_host(settings.get("host")) in CONVERT_LOCAL_HOSTS and convert_host(item["host"]) not in CONVERT_LOCAL_HOSTS:
            settings = {**settings, "host": item["host"]}
        wanted.append({"engine": item["engine"], "database": item["database"], "connection": settings, "unit": item["unit"]})
    return {"VNS_DELETE_DATABASES": json.dumps(wanted)} if wanted else None


def convert_source(data, remote):
    """This admin's connection to the server a site's database is on now, as harbor describes the site"""
    from harbor.core.system import System
    from harbor.sites import convert
    if remote:
        found, why = convert_remote_info(remote[0], remote[1], [remote[2]])
        item = next((item for item in (found or {}).get("sites", []) if item.get("unit") == remote[2]), None)
        if not item:
            abort(400, why or "The site is not found on its server")
    else:
        item = convert.describe(System(), data.get("unit"))
    if item.get("engine") not in ("mssql", "postgres"):
        abort(400, "Only a database on Microsoft SQL Server or PostgreSQL is moved")
    host, port = database_server(item)
    if remote and convert_host(host) in CONVERT_LOCAL_HOSTS:
        abort(400, f"Its database is on {remote[3]}'s own server: move it from the Harbor there")
    source = convert_connection({"engine": item["engine"], "host": host, "port": port}, bool(remote))
    return item, source, host, port


def convert_move_command(data, unit, remote, run, where):
    """harbor site convert-move: the database to another server of its kind, the connection to the old one beside it"""
    item, source, host, port = convert_source(data, remote)
    if not source:
        abort(400, f"Add a connection to {host}{':' + port if port else ''} on the SQL Server page to move its database from here")
    identifier = data.get("connection")
    try:
        with SQL_LOCK:
            target = _sql_module.connection(SQL_CONNECTIONS, identifier) if isinstance(identifier, str) else None
    except (OSError, ValueError):
        target = None
    if not target:
        abort(400, "Choose the server to move it to")
    refuse_closed(target, "move it")
    if _sql_module.engine(target) != item["engine"]:
        abort(400, "Move it to a server of its own kind: Convert changes its kind")
    if target.get("id") == source.get("id"):
        abort(400, "It is on that server already")
    if remote and convert_host(target.get("host")) in CONVERT_LOCAL_HOSTS:
        address = convert_address_for(next(entry for entry in convert_remote_units() if entry[5] == remote[3] and entry[2] == remote[2])[6][0])
        if not address:
            abort(400, "This server's address could not be found: add a connection to its SQL Server by its address")
        target = {**target, "host": address}
    database = data.get("database")
    if not isinstance(database, str) or not CONVERT_NAME.fullmatch(database):
        abort(400, "A database's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
    user = me() or {}
    argv = ["convert-move", unit, "--database", database, "--by", (user.get("name") or user.get("username") or "")[:120]]
    env = {"VNS_CONVERT_CONNECTION": json.dumps(target), "VNS_CONVERT_SOURCE": json.dumps(source)}
    if data.get("keep_login") is True:
        argv.append("--keep-login")
    else:
        login, password = data.get("login"), data.get("password")
        if not isinstance(login, str) or not CONVERT_NAME.fullmatch(login):
            abort(400, "A login's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
        if not isinstance(password, str) or not 8 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
            abort(400, "The login's password must be 8-128 characters without line breaks")
        argv += ["--login", login]
        env["VNS_CONVERT_LOGIN_PASSWORD"] = password
    return f"Move the database of {unit}{where} to {target.get('name') or target['host']}", run(*argv), env


@app.route("/api/convert/move")
def api_convert_move():
    """What Move needs to know first: whether this admin reaches the database's server, and whether its login is its
    alone (then it can go with it, by its name and password)"""
    require_admin()
    data = {"unit": request.args.get("unit", ""), "where": request.args.get("where", "")}
    remote = convert_remote_unit(data) if data["where"] else None
    if not remote and data["unit"] not in {unit for _site, unit, _label, _binding in convert_units()}:
        abort(400, "Choose a site here")
    if request.args.get("database"):
        # As Settings, Convert read it a moment ago: no second trip to the site's server. Only this admin's own connection
        # and its logins are looked at with it; the move itself reads the site again
        item = {"engine": request.args.get("engine"), "database": request.args.get("database"), "server": request.args.get("server", ""),
                "login": request.args.get("login", "")}
        if item["engine"] not in ("mssql", "postgres"):
            abort(400, "Only a database on Microsoft SQL Server or PostgreSQL is moved")
        host, port = database_server(item)
        own_there = bool(remote) and convert_host(host) in CONVERT_LOCAL_HOSTS
        source = None if own_there else convert_connection({"engine": item["engine"], "host": host, "port": port}, bool(remote))
    else:
        item, source, host, port = convert_source(data, remote)
    login = item.get("login") or ""
    alone = False
    if source and login:
        try:
            if item["engine"] == "mssql":
                alone = login in _sql_module.exclusive_database_logins(source, item["database"])
            else:
                alone = login in _sql_module.pg().exclusive_logins(source, item["database"])
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            alone = False
    return jsonify(source=(source or {}).get("id"), host=host, port=port, engine=item["engine"], database=item["database"], login=login, alone=alone,
                   umbraco=item.get("umbraco"), server=item.get("server", ""))


@app.route("/api/convert")
def api_convert():
    """The sites' databases here and on the servers its environments run on, the connections they can be converted to
    or from (never a password), and the databases kept after a conversion"""
    require_admin()
    from harbor.core.system import System
    from harbor.sites import convert
    system = System()
    sites, kept = [], []
    # ?site=: only that site and its environments (Convert… on a site's page)
    only = request.args.get("site") or ""
    here = [entry for entry in convert_units() if not only or entry[0] == only]
    for site, unit, label, binding in here:
        item = convert.describe(system, unit)
        item.pop("folder", None)
        managed = managed_by(site) if unit == site else None
        sites.append({**item, "site": site, "label": label, "binding": binding, "where": "",
                      **({"managed": (managed.get("host") or "").split(".")[0].lower()} if managed else {})})
    for item in convert.kept(system, {unit for _site, unit, _label, _binding in here} if only else None):
        kept.append({**convert_kept_ready(item, False), "where": ""})
    # Each other server once, over the SSH of one of its environments: in parallel, as the sites' statuses are read
    groups = {}
    for entry in convert_remote_units():
        if not only or entry[0] == only:
            groups.setdefault(entry[6], []).append(entry)
    def read(entries):
        site, target = entries[0][0], entries[0][1]
        return entries, *convert_remote_info(site, target, sorted({entry[2] for entry in entries}))
    if groups:
        with ThreadPoolExecutor(max_workers=6) as executor:
            answers = list(executor.map(read, groups.values()))
        for entries, found, why in answers:
            described = {item.get("unit"): item for item in (found or {}).get("sites", [])}
            for site, target, unit, label, binding, where, _reach in entries:
                # One the server did not answer for is shown with why: it is there, only not reached
                item = described.get(unit) or {"unit": unit, "convertible": False, "unreached": True, "reason": why or "Not found on that server."}
                if found and not item.get("engine") and item.get("reason") and "no service" in item["reason"]:
                    continue
                sites.append({**item, "site": site, "target": target, "label": label, "binding": binding, "where": where})
            for item in (found or {}).get("kept", []):
                kept.append({**convert_kept_ready(item, True), "where": entries[0][5]})
    # The connection here to the server each site's database is on: Convert to SQLite reads it from there, no other
    for item in sites:
        if item.get("engine") in ("mssql", "postgres") and item.get("database"):
            host, port = database_server(item)
            remote = bool(item.get("where"))
            if not (remote and convert_host(host) in CONVERT_LOCAL_HOSTS):
                found = convert_connection({"engine": item["engine"], "host": host, "port": port}, remote)
                if found:
                    item["source"] = found["id"]
    return jsonify(sites=sites, connections=convert_connections_public(), kept=kept)


def convert_connections_public():
    """The connections Convert and Move choose among (never a password), and whether each reaches its backup files
    (here, or over SSH): Move carries a Microsoft SQL Server database's backup between two servers that do"""
    found = []
    for item in sql_connections_public():
        public = {key: item[key] for key in ("id", "name", "host", "port", "engine") if key in item}
        try:
            public["files"] = item.get("engine") == "postgres" or bool(_sql_module.backup_file_access(item))
        except (KeyError, ValueError):
            public["files"] = False
        found.append(public)
    for public in found:
        # Whether its server keeps its databases: no place to convert, move or copy one to (it is still read from)
        public["closed"] = keeps_its_databases(public)
    # The database servers' own, lent by the servers in Servers (one this admin has a connection to is not twice): Move
    # does not carry backups with them
    for item in borrowed_connections():
        if any((entry.get("engine") or "mssql") == item.get("engine", "mssql") and same_host(entry.get("host"), item["host"])
               and str(entry.get("port") or "") == str(item.get("port") or "") for entry in found):
            continue
        found.append({**{key: item[key] for key in ("id", "name", "host", "port", "engine") if key in item}, "files": False, "lent_by": item["lent_by"],
                      "closed": keeps_its_databases(item)})
    return found


# ---- SQL Server page: which databases the sites run on, by their connection strings (here, and the environments on other
# servers as their status last said), so one in use is shown so and never deleted
SQL_DEFAULT_PORTS = {"mssql": "1433", "postgres": "5432"}


def sql_server_key(host, port, engine, there=""):
    """A database server as compared: this server's own (by any of its names or addresses) as one, another by its
    address, with its port (the engine's own when none is named)"""
    host = convert_host(host)
    if host in CONVERT_LOCAL_HOSTS:
        host = convert_host(there) if there else "this server"
    # A name by its address, so a connection to 172.23.3.21 and a site's sql-01 are the same server
    addresses = sorted((item for item in host_addresses(host) if item != host), key=lambda item: (":" in item, item))
    if addresses:
        host = addresses[0]
    if host in _sql_module.own_addresses():
        host = "this server"
    return host, str(port or SQL_DEFAULT_PORTS.get(engine, ""))


def sql_database_usage(connection_id, server=None):
    """{database (casefolded): [{label, site, unit, where}]} of the sites whose connection strings name a database on
    this connection's server; or on a server given as {host, port, engine} (a master asks each member so, for a
    database server on another member)"""
    from harbor.core.system import System
    from harbor.sites import convert
    if server:
        connection = server
    else:
        try:
            with SQL_LOCK:
                connection = _sql_module.connection(SQL_CONNECTIONS, connection_id)
        except (OSError, ValueError):
            return {}
    engine = _sql_module.engine(connection)
    key = sql_server_key(connection.get("host"), connection.get("port"), engine)
    found = {}
    def add(database, server, kind, label, site, unit, where="", there="", environment=""):
        if kind != engine or not database:
            return
        host, _, port = (server or "").partition(",")
        if sql_server_key(host.split("\\")[0].removeprefix("tcp:"), port.strip(), engine, there) == key:
            found.setdefault(database.casefold(), []).append({"label": label, "site": site, "unit": unit, "where": where, "environment": environment})
    system = System()
    for site, unit, label, _binding in convert_units():
        try:
            item = convert.describe(system, unit)
        except Exception:  # noqa: BLE001 - a site that cannot be read only shows nothing here
            continue
        add(item.get("database"), item.get("server"), item.get("engine"), f"{unit} ({label})", site, unit, environment=label)
    kinds = {"MS SQL": "mssql", "SQL Express": "mssql", "Azure SQL": "mssql", "PostgreSQL": "postgres"}
    remote = convert_remote_units()
    # Each environment on another server as its status says (read again when not read in the last 15 seconds), in parallel
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda entry: remote_status(entry[0], entry[1]), remote))
    for (site, target, unit, label, _binding, where, reach), status in zip(remote, statuses):
        add(status.get("database_name"), status.get("database_server"), kinds.get(status.get("database")), f"{unit} ({label}, on {where})",
            site, unit, where, reach[0], label)
    return found


def sql_transfer_command(data):
    """harbor site convert-transfer: a database no site runs on copied or moved (move) to another connection of its
    kind, with a login of its own. One a site runs on is moved with its site (Settings, Convert's Move)"""
    def settings_of(identifier):
        try:
            with SQL_LOCK:
                return _sql_module.connection(SQL_CONNECTIONS, identifier) if isinstance(identifier, str) else None
        except (OSError, ValueError):
            return None
    source, target = settings_of(data.get("connection")), settings_of(data.get("target"))
    if not source or not target:
        abort(400, "Choose the server to copy it to")
    if source["id"] == target["id"]:
        abort(400, "That is the server it is on")
    if _sql_module.engine(source) != _sql_module.engine(target):
        abort(400, "Copy it to a server of its own kind")
    refuse_closed(target, "copy it")
    database, name, login, password = data.get("database"), data.get("name"), data.get("login"), data.get("password")
    try:
        _sql_module.identifier(database or "")
    except ValueError as exc:
        abort(400, str(exc))
    if not isinstance(name, str) or not CONVERT_NAME.fullmatch(name):
        abort(400, "A database's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
    if not isinstance(login, str) or not CONVERT_NAME.fullmatch(login):
        abort(400, "A login's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
    if not isinstance(password, str) or not 8 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
        abort(400, "The login's password must be 8-128 characters without line breaks")
    for settings in (source, target):
        if _sql_module.engine(settings) == "mssql" and not _sql_module.backup_file_access(settings):
            abort(400, f"Turn on SSH for backup files in the connection to {settings['host']}: the backup is carried between the servers' backup folders")
    move = data.get("move") is True
    if move:
        users = sql_database_usage(source["id"]).get(database.casefold())
        if users:
            abort(400, f"{database} is used by {', '.join(item['label'] for item in users)}: move it with its site in Settings, Convert DB")
    argv = ["/usr/bin/python3", HARBOR_COMMAND, "site", "convert-transfer", database, "--name", name, "--login", login, *(["--move"] if move else [])]
    env = {"VNS_CONVERT_SOURCE": json.dumps(source), "VNS_CONVERT_CONNECTION": json.dumps(target), "VNS_CONVERT_LOGIN_PASSWORD": password}
    return f"{'Move' if move else 'Copy'} {database} to {target.get('name') or target['host']}", argv, env


# ---- Settings, Export: a site and its environments for a customer, without VNS Harbor (harbor site export)
EXPORTS_DIR = "/var/lib/harbor/exports"


def export_sites():
    """Each site here with the environments an export can take: [{site, managed, environments: [{target, unit, label,
    where, mode}]}]"""
    from harbor.core.system import System
    from harbor.server import servers
    try:
        named = {(entry.get("host") or "").lower(): server for server, entry in servers.servers(System(), "linux").items()}
    except (OSError, ValueError):
        named = {}
    found = []
    names = site_dirs()
    adopted = adopted_sites(names)
    for name in names:
        if name in adopted or name == ADMIN_SITE:
            continue
        root = os.path.join(WEB_ROOT, name)
        managed = managed_by(name)
        environments = [{"target": "main", "unit": name, "label": convert_environment(name, "Production") if unit_of(name) else "Site",
                         "where": "", "mode": "local"}]
        if not managed:
            for target in environment_order(root, target_ids(root)):
                prod = parse_prod(root, target) or {}
                unit = prod.get("PROD_FOLDER") or ""
                if not unit:
                    continue
                mode = "windows" if prod.get("PROD_TARGET") == "windows" else "local" if prod.get("PROD_LOCAL_PATH") else "remote"
                host = prod.get("PROD_IP", "")
                environments.append({"target": target, "unit": unit, "mode": mode, "where": named.get(host.lower()) or host if mode == "remote" else "",
                                     "label": prod.get("PROD_ENVIRONMENT") or ("Production" if target == "prod" else target.capitalize())})
        # One managed from another server is exported by the Harbor there (it has the project): only when that server is
        # in Servers, which reaches it
        manager = ""
        if managed:
            from harbor.sites.export import manager_server
            try:
                manager = (manager_server(System(), managed.get("host")) or ("",))[0]
            except (OSError, ValueError):
                manager = ""
        found.append({"site": name, "kind": site_kind(name), "managed": (managed.get("host") or "").split(".")[0].lower() if managed else "",
                      "manager": manager, "project": bool(glob.glob(os.path.join(glob.escape(root), "*.csproj"))), "environments": environments})
    return found


def manager_entry(server):
    """A Linux server in Servers by its name, for an export kept there: (entry, the ssh to it) or 404"""
    from harbor.core.system import System
    from harbor.server import servers
    entry = servers.servers(System(), "linux").get(server or "")
    if not entry:
        abort(404)
    return entry, [*servers.ssh(entry), f"{entry['user']}@{entry['host']}"], "" if entry["user"] == "root" else "sudo -n "


REMOTE_HARBOR = 'h=/usr/local/bin/harbor; [ -x "$h" ] || h=/opt/harbor/current/bin/harbor; exec "$h" '


def managed_export_items(sites):
    """The exports kept on other servers, each under its site here: of a site here managed from another server (made
    there), and of the environments of the sites here that run on servers in Servers (one made there before 1.0.85). One
    call a server, side by side"""
    from harbor.core.system import System
    from harbor.server import servers
    try:
        known = set(servers.servers(System(), "linux"))
    except (OSError, ValueError):
        known = set()
    groups = {}
    # Each environment's folder there, and the site here it is listed under
    under = {}
    for site in sites:
        if site.get("manager"):
            groups.setdefault(site["manager"], []).append(site["site"])
            under[(site["manager"], site["site"])] = site["site"]
        for env in site.get("environments") or []:
            if env.get("mode") == "remote" and env.get("where") in known:
                groups.setdefault(env["where"], []).append(env["unit"])
                under[(env["where"], env["unit"])] = site["site"]
    def read(server, units):
        try:
            _entry, ssh, sudo = manager_entry(server)
            result = subprocess.run([*ssh, sudo + "bash -c " + shlex.quote(REMOTE_HARBOR + "site export-list --units " + shlex.quote(",".join(units)))],
                                    capture_output=True, text=True, timeout=20)
            lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
            found = json.loads(lines[-1]).get("exports", []) if lines else []
        except (OSError, ValueError, subprocess.SubprocessError, HTTPException):
            return []
        items = []
        for item in found if isinstance(found, list) else []:
            unit = next((name for name in units if name in (item.get("environments") or [])), None) if isinstance(item, dict) else None
            if unit:
                # Named by the environment it is of (account.example.com), not the site it was made under there
                items.append({**item, "site": under[(server, unit)], "label": unit, "server": server, "deleted": False, "restorable": False})
        return items
    if not groups:
        return []
    with ThreadPoolExecutor(max_workers=4) as executor:
        return [item for found in executor.map(lambda pair: read(*pair), groups.items()) for item in found]


def site_kind(name):
    """What a site is, as its card says: umbraco, csharp, python or html (from its project, else what it runs)"""
    root = os.path.join(WEB_ROOT, name)
    projects = [path for path in glob.glob(os.path.join(glob.escape(root), "*.csproj")) if os.path.isfile(path)]
    if projects:
        return "umbraco" if any("Umbraco.Cms" in read_text(path) for path in projects) else "csharp"
    if os.path.isfile(os.path.join(root, "app", "app.py")):
        return "python"
    published = published_project_info(root, read_text(os.path.join(SERVICE_DIR, service_name(name) + ".service")))
    return published["kind"] if published else "html"


def export_items():
    """The exports kept here, as the admin shows them (with the link to send, never the server's paths)"""
    items = []
    for path in sorted(glob.glob(os.path.join(EXPORTS_DIR, "*", "meta.json"))):
        try:
            with open(path, encoding="utf-8") as stream:
                item = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict) or item.get("state") == "snapshot":
            continue
        keep = item.get("keep", 72 * 3600)
        items.append({key: item.get(key) for key in ("id", "site", "os", "at", "by", "environments", "size", "state", "deleted", "downloaded", "restored", "file")}
                     | {"link": request.host_url.rstrip("/") + "/download/" + item["token"] if item.get("token") and item.get("state") == "ready" else "",
                        "keep": keep, "expires": item["downloaded"] + keep if item.get("downloaded") and keep else None,
                        "restorable": bool(item.get("deleted") and os.path.isfile(os.path.join(os.path.dirname(path), "snapshot.tar.gz")))})
    return sorted(items, key=lambda item: item.get("at") or 0, reverse=True)


@app.route("/api/exports")
def api_exports():
    require_admin()
    sites = export_sites()
    exports = sorted(export_items() + managed_export_items(sites), key=lambda item: item.get("at") or 0, reverse=True)
    return jsonify(sites=sites, exports=exports)


@app.route("/api/exports/<identifier>", methods=["DELETE"])
def api_export_delete(identifier):
    """An export deleted now: its zip and its link, and its snapshot (Restore is not possible after it)"""
    require_admin()
    if not re.fullmatch(r"[0-9a-f]{16}", identifier):
        abort(400, "That is not an export")
    if request.args.get("server"):
        # One kept on the server an environment here is managed from: deleted there
        _entry, ssh, sudo = manager_entry(request.args["server"])
        result = subprocess.run([*ssh, sudo + "bash -c " + shlex.quote(REMOTE_HARBOR + "site export-forget " + identifier)],
                                capture_output=True, text=True, timeout=300)
    else:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "site", "export-forget", identifier], capture_output=True, text=True, timeout=300)
    if result.returncode:
        lines = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
        return error(lines[-1] if lines else "The export could not be deleted.", 400)
    return jsonify(ok=True)


@app.route("/api/exports/<identifier>/zip")
def api_export_zip(identifier):
    """An export's zip for an administrator here: it is not the customer's download, so its time does not start"""
    require_admin()
    if not re.fullmatch(r"[0-9a-f]{16}", identifier):
        abort(400, "That is not an export")
    if request.args.get("server"):
        # One kept on the server an environment here is managed from: read from there as it is sent, never kept here
        name = request.args.get("file", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.zip", name):
            abort(400, "That is not an export")
        _entry, ssh, sudo = manager_entry(request.args["server"])
        process = subprocess.Popen([*ssh, f"{sudo}cat {shlex.quote(os.path.join(EXPORTS_DIR, identifier, name))}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        first = process.stdout.read(65536)
        if not first:
            process.wait()
            abort(404)
        def stream():
            try:
                yield first
                yield from iter(lambda: process.stdout.read(1024 * 1024), b"")
            finally:
                process.stdout.close()
                process.wait()
        return Response(stream(), mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
    folder = os.path.join(EXPORTS_DIR, identifier)
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as stream:
            item = json.load(stream)
    except (OSError, ValueError):
        abort(404)
    archive = os.path.join(folder, item.get("file") or "")
    if item.get("state") != "ready" or not item.get("file") or not os.path.isfile(archive):
        abort(404)
    return send_file(archive, as_attachment=True, download_name=item["file"], mimetype="application/zip", conditional=True)


@app.route("/download/<token>", methods=["GET", "HEAD"])
def export_download(token):
    """An export's zip, by its link (the customer's): no login. Its first download starts its time (3 hours to 7 days, or never, as chosen), then it is
    deleted. An administrator's Download is /api/exports/<id>/zip, which does not"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token):
        abort(404)
    for path in glob.glob(os.path.join(EXPORTS_DIR, "*", "meta.json")):
        try:
            with open(path, encoding="utf-8") as stream:
                item = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict) or not item.get("token") or not hmac.compare_digest(item["token"], token):
            continue
        archive = os.path.join(os.path.dirname(path), item.get("file") or "")
        if item.get("state") != "ready" or not item.get("file") or not os.path.isfile(archive):
            break
        # A link previewed (HEAD) does not start the countdown; a download does
        if request.method == "GET" and not item.get("downloaded"):
            subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "site", "export-downloaded", item["id"]], capture_output=True, timeout=60)
        return send_file(archive, as_attachment=True, download_name=item["file"], mimetype="application/zip", conditional=True)
    return Response("This export is not here any more: it was deleted some time after it was first downloaded.", status=410, mimetype="text/plain")


def export_databases_env(site, labels):
    """The databases of the environments exported, with this admin's connection to each (in the task's environment,
    never its command line): {environment folder: {database, connection}}"""
    try:
        with SQL_LOCK:
            items = {item["id"]: item for item in _sql_module.connections(SQL_CONNECTIONS)}
    except (OSError, ValueError):
        items = {}
    wanted = {}
    for item in site_databases(site, "all", ""):
        if item["unit"] not in labels or not item.get("connection") or item["unit"] in wanted:
            continue
        settings = items.get(item["connection"])
        if not settings:
            continue
        if convert_host(settings.get("host")) in CONVERT_LOCAL_HOSTS and convert_host(item["host"]) not in CONVERT_LOCAL_HOSTS:
            settings = {**settings, "host": item["host"]}
        wanted[item["unit"]] = {"database": item["database"], "connection": settings}
    return wanted


def export_command(data):
    """harbor site export (with the databases' connections in its environment), or export-restore"""
    harbor = ["/usr/bin/python3", HARBOR_COMMAND, "site"]
    if data.get("kind") == "export_restore":
        identifier = data.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{16}", identifier):
            abort(400, "That is not an export")
        return "Restore the site of an export", [*harbor, "export-restore", identifier], None
    site = site_arg(data)
    entry = next((item for item in export_sites() if item["site"] == site), None)
    if not entry:
        abort(400, "Choose a site here")
    # Managed from another server: exported by the Harbor there, which this one reaches only when it is in Servers
    if entry["managed"] and not entry["manager"]:
        abort(400, f"{site} is managed from {entry['managed']}: add that server under Servers to export it from here")
    if entry["managed"] and data.get("delete") is True:
        abort(400, f"{site} is managed from {entry['managed']}: delete it from there")
    # A .NET site is built for the OS chosen; one that is not (HTML, Python) runs on either
    built = entry["kind"] in ("csharp", "umbraco")
    if built and data.get("os") not in ("windows", "linux"):
        abort(400, "Choose Windows or Linux")
    if not built:
        data = {**data, "os": "any"}
    targets = data.get("targets")
    known = {item["target"]: item for item in entry["environments"] if item["mode"] != "windows"}
    if not isinstance(targets, list) or not targets or any(target not in known for target in targets):
        abort(400, "Choose what to export")
    user = me() or {}
    keep = data.get("keep", 72)
    if keep not in (3, 6, 12, 24, 72, 168, 0):
        abort(400, "Choose how long it lives")
    argv = [*harbor, "export", site, "--os", data["os"], "--targets", ",".join(targets), "--by", (user.get("name") or user.get("username") or "")[:120],
            "--keep", str(keep), *(["--delete"] if data.get("delete") is True else [])]
    labels = {known[target]["unit"] for target in targets}
    databases = export_databases_env(site, labels)
    env = {"VNS_EXPORT_DATABASES": json.dumps(databases)} if databases else {}
    # Deleted with it: the databases chosen too (kept for Restore first, as Delete site takes them)
    if data.get("delete") is True and data.get("databases"):
        env.update(delete_databases_env({"databases": data["databases"], "scope": "all"}, site) or {})
    env = env or None
    return f"Export {site}" + ("" if data["os"] == "any" else f" for {'Windows' if data['os'] == 'windows' else 'Linux'}"), argv, env


@app.route("/api/sql/usage")
def api_sql_usage():
    """Which databases of a connection the sites run on: shown on the SQL Server page, an administrator's"""
    if not is_admin():
        return jsonify(usage={})
    identifier = request.args.get("connection", "")
    # A database server by its address (asked by a master for one on another member)
    host = request.args.get("host", "")
    if host:
        engine = request.args.get("engine", "mssql")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host) or engine not in ("mssql", "postgres") or not re.fullmatch(r"[0-9]{0,5}", request.args.get("port", "")):
            return error("Give the database server's address, port and engine.")
        return jsonify(usage=sql_database_usage("", {"host": host, "port": request.args.get("port", ""), "engine": engine}))
    return jsonify(usage=sql_database_usage(identifier))


@app.route("/api/master/sql-usage")
def api_master_sql_usage():
    """(On a master) which sites on any member run on the databases of a member's connection: that member's own, and
    each other member's whose connection strings name its server (by the address the master reaches it at, which the
    members were given too). Each site said by its member, so its page opens there"""
    require_admin()
    server, identifier = request.args.get("server", ""), request.args.get("connection", "")
    if server not in member_names() or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        return error("Name a member and its connection.")
    from urllib.parse import quote, urlencode
    from harbor.core.system import Stop, System
    from harbor.server import master
    system = System()
    members = master.members(system)
    found = {}

    def merge(usage, member):
        for database, users in (usage or {}).items():
            found.setdefault(database, []).extend({**item, "server": member, "label": f"{item.get('label')} · {member}"} for item in users if isinstance(item, dict))
    try:
        own = master.ask(system, server, members[server], "GET", f"/api/sql/usage?connection={quote(identifier)}")
        merge((own.get("body") or {}).get("usage"), server)
        listed = master.ask(system, server, members[server], "GET", "/api/sql/connections")
    except Stop as exc:
        return error(str(exc), 502)
    connection = next((item for item in (listed.get("body") or {}).get("connections") or [] if item.get("id") == identifier), None)
    if connection:
        # Its own server (localhost there) is, to the others, the address the master reaches it at
        host = str(connection.get("host") or "").strip("[]")
        if host.lower() in ("localhost", "127.0.0.1", "::1", ".", "(local)") or host.lower() == server.lower():
            host = master.reached_at(members[server]["host"])
        query = urlencode({"host": host, "port": connection.get("port") or "", "engine": connection.get("engine") or "mssql"})

        def ask_other(name):
            try:
                return name, (master.ask(system, name, members[name], "GET", f"/api/sql/usage?{query}").get("body") or {}).get("usage")
            except Stop:
                return name, None
        with ThreadPoolExecutor(max_workers=8) as executor:
            for name, usage in executor.map(ask_other, [name for name in members if name != server]):
                merge(usage, name)
    # An environment on one member managed from a site on another is told by both: once, as its site's (where its page
    # is on the master), not again as the member's own
    for database, users in found.items():
        kept = {}
        for item in users:
            key = (item.get("unit"), item.get("where") or item.get("server"))
            if key not in kept or (item.get("where") and not kept[key].get("where")):
                kept[key] = item
        found[database] = list(kept.values())
    return jsonify(usage=found)


@app.route("/api/convert/connections")
def api_convert_connections():
    require_admin()
    return jsonify(connections=convert_connections_public())


# A database's and login's name as a site's: letters, digits, . _ and - (a PostgreSQL name has 63 bytes at most)
CONVERT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


def convert_remote_unit(data):
    """The environment on another server a task is for, by its server and service: (site, target, unit, where)"""
    where, unit = data.get("where"), data.get("unit")
    for site, target, found, _label, _binding, name, _reach in convert_remote_units():
        if name == where and found == unit:
            return site, target, unit, where
    abort(400, "Choose a site")


def convert_kept_item(identifier, remote):
    """A kept database as harbor tells it, here or on the environment's server"""
    from harbor.core.system import System
    from harbor.sites import convert
    if remote:
        site, target, unit, _where = remote
        found, why = convert_remote_info(site, target, [unit])
        if not found:
            abort(400, why)
        items = found["kept"]
    else:
        items = convert.kept(System())
    item = next((item for item in items if item.get("id") == identifier), None)
    if not item:
        abort(400, "That database is not kept after a conversion")
    return item


def convert_command(data):
    """harbor site convert, convert-rollback or convert-delete, with the connections and the new login's password in its
    environment; for an environment on another server, run there by its Harbor (convert-remote)"""
    harbor = ["/usr/bin/python3", HARBOR_COMMAND, "site"]
    remote = convert_remote_unit(data) if data.get("where") else None
    run = (lambda action, *rest: [*harbor, "convert-remote", remote[0], remote[1], action, *rest]) if remote \
        else (lambda action, *rest: [*harbor, action, *rest])
    where = f" on {remote[3]}" if remote else ""
    if data.get("kind") in ("convert_delete", "convert_rollback"):
        identifier = data.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            abort(400, "Choose a database to delete")
        item = convert_kept_item(identifier, remote)
        env = None
        if data.get("kind") == "convert_rollback":
            # The database servers it deletes from, by the ids harbor knows them by: this admin's connection to each
            env = {}
            for server in item.get("servers") or []:
                found = convert_connection(server, bool(remote))
                if not found:
                    abort(400, f"Add a connection to {server.get('host') or 'its server'} on the SQL Server page to roll back here")
                env[server.get("connection") or ""] = found
            return f"Roll back {item.get('unit')}{where}", run("convert-rollback", identifier), {"VNS_CONVERT_CONNECTIONS": json.dumps(env)}
        # Delete reaches the old database's server
        if item.get("server"):
            found = convert_connection(item["server"], bool(remote))
            if not found:
                abort(400, f"Add a connection to {item['server'].get('host') or 'its server'} on the SQL Server page to delete it here")
            env = {"VNS_CONVERT_CONNECTION": json.dumps(found)}
        name = item.get("database") or os.path.basename(item.get("file") or "")
        return f"Delete {name}{where}", run("convert-delete", identifier), env
    unit = data.get("unit")
    if not remote and unit not in {unit for _site, unit, _label, _binding in convert_units()}:
        abort(400, "Choose a site here")
    if data.get("kind") == "convert_move":
        return convert_move_command(data, unit, remote, run, where)
    to = data.get("to")
    if to not in ("mssql", "postgres", "sqlite"):
        abort(400, "Choose what to convert to")
    settings = convert_settings(data.get("connection"))
    if not settings:
        abort(400, "Choose a connection")
    if to != "sqlite" and _sql_module.engine(settings) != to:
        abort(400, "The connection is not the server chosen")
    if to != "sqlite":
        refuse_closed(settings, "convert it")
    if remote and convert_host(settings.get("host")) in CONVERT_LOCAL_HOSTS:
        # This server's own SQL Server: the other server reaches it by this server's address
        address = convert_address_for(next(entry for entry in convert_remote_units() if entry[5] == remote[3] and entry[2] == remote[2])[6][0])
        if not address:
            abort(400, "This server's address could not be found: add a connection to its SQL Server by its address")
        settings = {**settings, "host": address}
    user = me() or {}
    argv = ["convert", unit, to, "--by", (user.get("name") or user.get("username") or "")[:120]]
    env = {"VNS_CONVERT_CONNECTION": json.dumps(settings)}
    if to == "sqlite":
        name = data.get("file") or ""
        if name and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:sqlite\.db|sqlite|sqlite3|db)", name)):
            abort(400, "A SQLite file's name has letters, digits, . _ and -, and ends in .sqlite.db, .sqlite, .sqlite3 or .db")
        if name:
            argv += ["--file", name]
        title = f"Convert {unit}{where} to SQLite"
    else:
        database, login, password = data.get("database"), data.get("login"), data.get("password")
        if not isinstance(database, str) or not CONVERT_NAME.fullmatch(database):
            abort(400, "A database's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
        if not isinstance(login, str) or not CONVERT_NAME.fullmatch(login):
            abort(400, "A login's name has letters, digits, . _ and -, at most 63, and starts with a letter or digit")
        if not isinstance(password, str) or not 8 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
            abort(400, "The login's password must be 8-128 characters without line breaks")
        argv += ["--database", database, "--login", login]
        env["VNS_CONVERT_LOGIN_PASSWORD"] = password
        title = f"Convert {unit}{where} to {'Microsoft SQL Server' if to == 'mssql' else 'PostgreSQL'}"
    return title, run(*argv), env


# ---- A Windows server's sites listed on Web Server where they are (harbor/server/windows_sites.py): an administrator's.
# Where each is reached (its binding and backend) is a note kept here: nothing on Windows is changed but the files
WINDOWS_SITES_NOTES = os.path.join(DATA, "windows-sites.json")
# What each server listed last: {server: (until, answer)}, read again after two minutes or when asked
WINDOWS_SITES_SEEN = {}
WINDOWS_SITES_LOCK = threading.Lock()


def windows_folder_ok(folder):
    """A site's folder in a share by its name: never a path"""
    return isinstance(folder, str) and 0 < len(folder) <= 255 and bool(folder.strip()) and folder not in (".", "..") \
        and not any(char in '/\\:*?"<>|' or ord(char) < 32 for char in folder)


def windows_sites_notes():
    try:
        with open(WINDOWS_SITES_NOTES, encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def windows_listing(name, everything=False, fresh=False):
    """A Windows server's sites (harbor windows sites): ([{folder, excluded}], why not)"""
    kept = WINDOWS_SITES_SEEN.get(name)
    if not everything and not fresh and kept and kept[0] > time.time():
        return kept[1]
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "windows", "sites", "--server", name, *(["--all"] if everything else [])],
                                capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return [], "The Windows server did not answer in time."
    if result.returncode:
        lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        answer = ([], lines[-1] if lines else "The share could not be read.")
    else:
        try:
            answer = (json.loads(result.stdout[result.stdout.index("{"):]).get("sites", []), "")
        except ValueError:
            answer = ([], "The share could not be read.")
    if not everything:
        WINDOWS_SITES_SEEN[name] = (time.time() + 120, answer)
    return answer


@app.route("/api/windows-sites")
def api_windows_sites():
    """The Windows servers whose sites are listed, each with its sites and where each is reached (as noted here)"""
    require_admin()
    from harbor.core.system import System
    from harbor.server import servers
    listed = {name: entry for name, entry in servers.servers(System(), "windows").items() if entry.get("list_sites")}
    fresh = request.args.get("fresh") == "1"
    with ThreadPoolExecutor(max_workers=4) as executor:
        answers = dict(zip(listed, executor.map(lambda name: windows_listing(name, fresh=fresh), listed)))
    # Read after the shares have answered (that can take seconds): a binding saved meanwhile is in the answer
    notes = windows_sites_notes()
    found = []
    for name, entry in sorted(listed.items()):
        sites, why = answers[name]
        mine = notes.get(name) if isinstance(notes.get(name), dict) else {}
        found.append({"name": name, "share": entry.get("share", ""), "error": why,
                      "sites": [{"folder": item["folder"], "umbraco": bool(item.get("umbraco")),
                                 # What it is, read from its files: as the other sites' badges
                                 **{key: item.get(key, "") for key in ("kind", "umbraco_version", "dotnet", "framework")},
                                 "binding": (mine.get(item["folder"]) or {}).get("binding", ""),
                                 "backend": (mine.get(item["folder"]) or {}).get("backend", "")} for item in sites]})
    return jsonify(servers=found)


@app.route("/api/windows-sites/<name>/folders")
def api_windows_site_folders(name):
    """Every folder in its share, the ones left out marked: for choosing what is listed"""
    require_admin()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        abort(404)
    sites, why = windows_listing(name, everything=True)
    if why:
        return error(why, 400)
    return jsonify(folders=sites)


@app.route("/api/windows-sites/<name>/<path:folder>", methods=["PUT"])
def api_windows_site_note(name, folder):
    """Where a listed site is reached: its binding (a name) and its backend (an address), as noted here only"""
    require_admin()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) or not windows_folder_ok(folder):
        abort(404)
    data = request.get_json(silent=True) or {}
    binding = str(data.get("binding") or "").strip().lower()
    backend = str(data.get("backend") or "").strip()
    if binding and not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", binding):
        return error("Give the binding as a name, such as www.example.com", 400)
    if backend and (len(backend) > 500 or not re.fullmatch(r"https?://[^\s\"<>]+", backend)):
        return error("Give the backend as an address starting with https://", 400)
    with WINDOWS_SITES_LOCK:
        notes = windows_sites_notes()
        if not isinstance(notes.get(name), dict):
            notes[name] = {}
        mine = notes[name]
        if binding or backend:
            mine[folder] = {"binding": binding, "backend": backend}
        else:
            mine.pop(folder, None)
        temporary = WINDOWS_SITES_NOTES + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(notes, stream, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, WINDOWS_SITES_NOTES)
    audit(f"Windows site {folder} on {name}: binding {binding or 'none'}, backend {backend or 'none'}")
    return jsonify(binding=binding, backend=backend)


@app.route("/api/windows-fs/<name>/<path:folder>/<action>", methods=["GET", "POST"])
def windows_site_fs(name, folder, action):
    """Files of a listed Windows site, over SMB (lib/windows_files.py), answered as a Windows environment's are"""
    require_admin()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) or not windows_folder_ok(folder):
        abort(404)
    if action not in ("list", "read", "download", "save", "mkdir", "newfile", "rename", "delete", "upload"):
        abort(400, "Unknown file action")
    if request.method == "GET":
        if action not in ("list", "read", "download"):
            abort(405)
        data = {"path": request.args.get("path", "")}
    elif action == "upload":
        data = {"path": request.form.get("path", ""), "files": [
            {"name": upload.filename or "", "content": base64.b64encode(upload.read()).decode("ascii")}
            for upload in request.files.getlist("files")]}
    else:
        data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        abort(400, "Expected a file operation object")
    data["action"] = action
    try:
        result = subprocess.run(["/usr/bin/python3", HARBOR_COMMAND, "windows", "site-files", "--server", name, folder],
                                input=json.dumps(data).encode("utf-8"), capture_output=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        app.logger.error("Windows file %s failed for %s on %s: %s", action, folder, name, exc)
        abort(502, "Could not reach the Windows server")
    try:
        response = json.loads(result.stdout)
    except ValueError:
        response = {"error": result.stderr.decode("utf-8", errors="replace").strip() or "Invalid response from the Windows server"}
    if result.returncode or not isinstance(response, dict) or "error" in response:
        message = response.get("error") if isinstance(response, dict) else None
        abort(502, message or "The Windows server refused the request")
    if action in ("save", "mkdir", "newfile", "rename", "delete", "upload"):
        audit(f"Windows site {folder} on {name}: {action} {data.get('path', '')}")
    if action == "download":
        return file_response(io.BytesIO(base64.b64decode(response["content"])), os.path.basename(data["path"]))
    return jsonify(response)


@app.route("/api/windows/imported")
def api_windows_imported():
    """The sites imported and not taken over yet, and the Windows servers kept in Servers (never their passwords)"""
    require_admin()
    from harbor.core.system import System
    from harbor.server import servers
    kept = servers.servers(System(), "windows")
    return jsonify(imported=windows_imported(), name=socket.getfqdn().lower().rstrip("."),
                   servers=[{"name": name, "share": entry.get("share", ""), "user": entry.get("user", "")} for name, entry in sorted(kept.items())])


def windows_imported():
    """The sites here imported from Windows and not taken over yet"""
    try:
        return sorted(path.parent.name for path in Path(HARBOR_SITES).glob("*/windows-source.json"))
    except OSError:
        return []


@app.route("/api/sites/<site>/sync-status")
def sync_status(site):
    """For the Sync dialog: each destination's last syncs from the source, whether the source has changed since, and
    (for data) whether its settings are behind; and the newest syncs from or to the source. As the dashboard in
    Umbraco shows them (harbor/environments/history.py)."""
    if site not in site_dirs():
        abort(400, "Unknown site")
    source = data_sync_id(site, request.args.get("source") or "main")
    kind = request.args.get("kind") or "settings"
    if kind not in ("settings", "data"):
        abort(400, "Choose settings or data")
    if not access.can(me(), site, source, "sync_data" if kind == "data" else "sync_settings"):
        abort(403, "You do not have access to do this.")
    from harbor.core.system import System
    from harbor.environments import history
    system = System()
    root = os.path.join(WEB_ROOT, site)
    config = _data_sync_module.load(root)
    mine = current_prints(site, source)
    found = {}
    for destination in ["main", *target_ids(root)]:
        if destination == source:
            continue
        if kind == "data":
            try:
                kinds = _data_sync_module.kinds(config, source, destination)
            except ValueError:
                continue
            found[destination] = {"history": name_history(history.between(system, site, source, destination, kinds, mine)),
                                  "settings_behind": settings_behind(system, site, destination, config, source, kinds)}
        else:
            found[destination] = {"history": name_history(history.between(system, site, source, destination, ["settings"], mine))}
    try:
        recent = recent_syncs(site, source, kinds=("content", "media") if kind == "data" else ("settings",))
    except (OSError, ValueError):
        recent = []
    return jsonify(destinations=found, recent=recent)


@app.route("/api/sites/<site>/config/<kind>", methods=["GET", "POST"])
def managed_config(site, kind):
    # A service file or an Apache config can start programs as root: administrators only
    require_admin()
    if site not in site_dirs() or kind not in ("service", "conf"):
        abort(400, "Unknown site or configuration type")
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    if not isinstance(data, dict):
        abort(400, "Expected a configuration object")
    action = data.get("action") if request.method == "POST" else "read"
    if action not in ("read", "save", "apply"):
        abort(400, "Unknown configuration action")
    payload = {"kind": kind, "action": action, "content": data.get("content")}
    if action == "read" and "encoding" in request.args:
        payload["encoding"] = request.args["encoding"]
    scope = request.args.get("scope", "local")
    if scope not in ("local", "remote"):
        abort(400, "Unknown configuration scope")
    target = request.args.get("target") or "prod"
    if scope == "remote":
        remote_site(site, target)
        result = remote_command(site, "config", timeout=150, target=target, text=True, input=json.dumps(payload))
    else:
        unit = request.args.get("unit") or site
        if unit not in site_units(site):
            abort(400, "Unknown service")
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(LIB_DIR, "managed_config.py")],
                input=json.dumps(payload), text=True, capture_output=True, timeout=150,
                env={**os.environ, "SITE": unit})
        except (OSError, subprocess.TimeoutExpired) as exc:
            app.logger.error("Configuration %s failed for %s: %s", action, unit, exc)
            abort(502, "Configuration operation failed")
    try:
        response = json.loads(result.stdout)
    except ValueError:
        app.logger.error("Invalid configuration response for %s: %s", site, result.stderr)
        abort(502, "Invalid configuration response")
    if result.returncode:
        app.logger.error("Configuration %s failed for %s: %s", action, site, response)
        abort(502, response.get("error", "Configuration operation failed"))
    if response.get("encoding_required"):
        app.logger.warning("Configuration for %s is not UTF-8; requesting an explicit encoding", site)
    if action == "apply":
        REMOTE_STATUS.pop(remote_key(site, target), None)
    return jsonify(response)


APACHE_CONF = "/etc/apache2/apache2.conf"


@app.route("/api/server/apache-configtest", methods=["POST"])
def apache_configtest():
    """apache2ctl configtest, answered as it is: whether Apache would start with the configs as they are, and what it
    said (Syntax OK, its warnings, or the error with the file and line)"""
    require_admin()
    try:
        test = subprocess.run(["apache2ctl", "configtest"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return error(f"apache2ctl could not be run: {exc}", 502)
    lines = [line for line in (test.stdout + test.stderr).splitlines() if line.strip()]
    return jsonify(ok=test.returncode == 0, lines=lines[-60:],
                   warnings=[line for line in lines if re.match(r"^(AH\d+|\[[^\]]*warn)", line) or "Warning" in line])


@app.route("/api/server/apache-config", methods=["GET", "POST"])
def apache_config():
    """Apache's own config (apache2.conf) in the editor, as a site's config: read, saved, and applied only when
    apache2ctl configtest passes. Administrators only: it can start programs as root."""
    require_admin()
    data = (request.get_json(silent=True) or {}) if request.method == "POST" else {"action": "read"}
    action = data.get("action") if isinstance(data, dict) else None
    if action == "read":
        try:
            with open(APACHE_CONF, encoding="utf-8") as stream:
                return jsonify(path=APACHE_CONF, content=stream.read(), encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return error(f"{APACHE_CONF} could not be read: {exc}", 502)
    if action == "save":
        content = data.get("content")
        if not isinstance(content, str) or "\0" in content:
            return error("Expected the config as text")
        # In place, so its owner and rights stay
        with open(APACHE_CONF, "w", encoding="utf-8") as stream:
            stream.write(content)
        audit("Saved " + APACHE_CONF)
        return jsonify(ok=True)
    if action == "apply":
        test = subprocess.run(["apache2ctl", "configtest"], capture_output=True, text=True, timeout=60)
        if test.returncode:
            return error("The config test failed, so Apache was not reloaded: " + (test.stderr or test.stdout).strip(), 502)
        subprocess.run(["systemctl", "reload", "apache2"], capture_output=True, text=True, timeout=60, check=False)
        return jsonify(ok=True, output=test.stdout + test.stderr)
    return error("Unknown configuration action")


def windows_files(site, target, data):
    """A file operation on an environment on a Windows server, over SMB, answered like a remote Linux one"""
    try:
        result = subprocess.run([*harbor_argv("windows_files.sh"), site],
                                input=json.dumps(data).encode("utf-8"), capture_output=True, timeout=150,
                                env={**os.environ, "VNS_TARGET": target})
    except (OSError, subprocess.TimeoutExpired) as exc:
        app.logger.error("Windows file %s failed for %s: %s", data.get("action"), site, exc)
        abort(502, "Could not reach the Windows server")
    try:
        response = json.loads(result.stdout)
    except ValueError:
        response = {"error": result.stderr.decode("utf-8", errors="replace").strip() or "Invalid response from the Windows server"}
    if result.returncode or not isinstance(response, dict) or "error" in response:
        message = response.get("error") if isinstance(response, dict) else None
        app.logger.error("Windows file %s failed for %s: %s", data.get("action"), site, message)
        abort(502, message or "The Windows server refused the request")
    return response


@app.route("/api/remote-fs/<site>/<action>", methods=["GET", "POST"])
def remote_fs(site, action):
    # The file manager addresses an environment as site@id
    site, _, target = site.partition("@")
    target = target or "prod"
    body = request.get_json(silent=True) if request.method != "GET" and action != "upload" else None
    file_access(site, target, action, request.args.get("path") if request.method == "GET" else request.form.get("path") if action == "upload"
                else body.get("path") if isinstance(body, dict) else "")
    windows = site in site_dirs() and TARGET_RE.match(target) and (parse_prod(os.path.join(WEB_ROOT, site), target) or {}).get("PROD_TARGET") == "windows"
    if not windows:
        remote_site(site, target)
    if action not in ("list", "read", "download", "save", "mkdir", "newfile", "rename", "delete", "upload"):
        abort(400, "Unknown file action")
    if request.method == "GET":
        if action not in ("list", "read", "download"):
            abort(405)
        data = {"path": request.args.get("path", "")}
    elif action == "upload":
        data = {"path": request.form.get("path", ""), "files": [
            {"name": upload.filename or "", "content": base64.b64encode(upload.read()).decode("ascii")}
            for upload in request.files.getlist("files")]}
    else:
        data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        abort(400, "Expected a file operation object")
    data["action"] = action
    if windows:
        response = windows_files(site, target, data)
    else:
        result = remote_command(site, "files", timeout=120, target=target, input=json.dumps(data).encode("utf-8"))
        try:
            response = json.loads(result.stdout)
        except ValueError:
            app.logger.error("Invalid remote file response for %s", site)
            abort(502, "Invalid remote file response")
    if action == "download":
        return file_response(io.BytesIO(base64.b64decode(response["content"])), os.path.basename(data["path"]))
    if action == "list" and isinstance(response.get("items"), list):
        folder = str(data.get("path") or "").replace("\\", "/").strip("/")
        response["items"] = [item for item in response["items"] if not hidden_from_user(folder + "/" + str(item.get("name", "")))]
    return jsonify(response)


@app.route("/api/fs/list")
@app.route("/api/site-fs/<path:root>/list")
@app.route("/api/server-fs/list")
def fs_list(root=None):
    full = fs_path(request.args.get("path", ""))
    if not os.path.isdir(full):
        return error("Not a folder", 404)
    items = []
    site, env = (None, None) if server_files() or is_admin() else file_env(full)
    for entry in os.scandir(full):
        if site and hidden_from_user(os.path.relpath(entry.path, file_env_root(site, env))):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        is_dir = entry.is_dir(follow_symlinks=server_files())
        try:
            editable = not is_dir and not entry.is_symlink() and entry.is_file() and editable_file(Path(entry.path), info.st_size)
            has_editable_files = (True if server_files() else contains_editable_files(Path(entry.path))) if is_dir else False
        except OSError as exc:
            app.logger.warning("Could not check file editability for %s: %s", entry.path, exc)
            editable = False
            has_editable_files = is_dir
        items.append({"name": entry.name, "dir": is_dir, "size": 0 if is_dir else info.st_size,
                      "modified": int(info.st_mtime), "editable": editable, "has_editable_files": has_editable_files,
                      **file_metadata(info)})
    items.sort(key=lambda i: (i["name"].lower(), i["name"]))
    return jsonify(path=fs_relative(full), items=items)


@app.route("/api/fs/read")
@app.route("/api/site-fs/<path:root>/read")
@app.route("/api/server-fs/read")
def fs_read(root=None):
    full = fs_path(request.args.get("path", ""))
    if not os.path.isfile(full):
        return error("Not a file", 404)
    if os.path.getsize(full) > MAX_EDIT_BYTES:
        return error("The file is too large to edit here. Download it instead.")
    with open(full, "rb") as f:
        data = f.read()
    if b"\0" in data:
        return error("Binary file. Download it instead.")
    return jsonify(content=data.decode("utf-8", errors="replace"))


@app.route("/api/fs/save", methods=["POST"])
@app.route("/api/site-fs/<path:root>/save", methods=["POST"])
@app.route("/api/server-fs/save", methods=["POST"])
def fs_save(root=None):
    data = request.get_json(silent=True) or {}
    full = fs_path(data.get("path", ""))
    if os.path.isdir(full) or not os.path.isdir(os.path.dirname(full)):
        return error("Invalid path")
    existed = os.path.exists(full)
    # Written in place, so owner and permissions of an existing file are kept
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(data.get("content", ""))
    if not existed:
        own_like_parent(full)
    return jsonify(ok=True)


def valid_name(name):
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


@app.route("/api/fs/mkdir", methods=["POST"])
@app.route("/api/fs/newfile", methods=["POST"])
@app.route("/api/site-fs/<path:root>/mkdir", methods=["POST"])
@app.route("/api/site-fs/<path:root>/newfile", methods=["POST"])
@app.route("/api/server-fs/mkdir", methods=["POST"])
@app.route("/api/server-fs/newfile", methods=["POST"])
def fs_create(root=None):
    data = request.get_json(silent=True) or {}
    parent = fs_path(data.get("path", ""))
    name = data.get("name", "")
    if not valid_name(name) or not os.path.isdir(parent):
        return error("Invalid name")
    full = os.path.join(parent, name)
    if os.path.lexists(full):
        return error("It already exists")
    if request.path.endswith("mkdir"):
        os.mkdir(full)
    else:
        open(full, "x").close()
    own_like_parent(full)
    return jsonify(ok=True)


@app.route("/api/fs/rename", methods=["POST"])
@app.route("/api/site-fs/<path:root>/rename", methods=["POST"])
@app.route("/api/server-fs/rename", methods=["POST"])
def fs_rename(root=None):
    data = request.get_json(silent=True) or {}
    full = fs_path(data.get("path", ""))
    name = data.get("name", "")
    if full == "/":
        return error("The filesystem root cannot be renamed.")
    if is_site_root(full):
        return error("Sites cannot be renamed here. Use change binding or create a new site.")
    if not valid_name(name) or not os.path.lexists(full):
        return error("Invalid name")
    target = os.path.join(os.path.dirname(full), name)
    if os.path.lexists(target):
        return error("It already exists")
    os.rename(full, target)
    return jsonify(ok=True)


@app.route("/api/fs/delete", methods=["POST"])
@app.route("/api/site-fs/<path:root>/delete", methods=["POST"])
@app.route("/api/server-fs/delete", methods=["POST"])
def fs_delete(root=None):
    full = fs_path((request.get_json(silent=True) or {}).get("path", ""))
    if full == "/":
        return error("The filesystem root cannot be deleted.")
    if is_site_root(full):
        return error("Sites cannot be deleted here. Use Delete site.")
    if os.path.islink(full) or os.path.isfile(full):
        os.remove(full)
    elif os.path.isdir(full):
        shutil.rmtree(full)
    else:
        return error("Not found", 404)
    return jsonify(ok=True)


@app.route("/api/fs/upload", methods=["POST"])
@app.route("/api/site-fs/<path:root>/upload", methods=["POST"])
@app.route("/api/server-fs/upload", methods=["POST"])
def fs_upload(root=None):
    parent = fs_path(request.form.get("path", ""))
    if not os.path.isdir(parent):
        return error("Not a folder")
    count = 0
    for upload in request.files.getlist("files"):
        name = os.path.basename(upload.filename or "")
        if not valid_name(name):
            continue
        target = fs_path(os.path.join(fs_relative(parent), name))
        existed = os.path.exists(target)
        upload.save(target)
        if not existed:
            own_like_parent(target)
        count += 1
    return jsonify(ok=True, count=count)


@app.route("/api/fs/download")
@app.route("/api/site-fs/<path:root>/download")
@app.route("/api/server-fs/download")
def fs_download(root=None):
    full = fs_path(request.args.get("path", ""))
    if not os.path.isfile(full):
        return error("Not a file", 404)
    return file_response(full, os.path.basename(full))


# What the file browser shows itself (?inline=1): pictures and PDFs, never SVG or HTML, which could run in the admin
PREVIEW_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
                 ".avif": "image/avif", ".bmp": "image/bmp", ".ico": "image/x-icon", ".pdf": "application/pdf"}


def file_response(source, name):
    """A file to download, or with ?inline=1 one of PREVIEW_TYPES to show; source is a path or a file object"""
    kind = PREVIEW_TYPES.get(os.path.splitext(name)[1].lower())
    if request.args.get("inline") != "1":
        return send_file(source, as_attachment=True, download_name=name)
    if not kind:
        abort(400, "Only pictures and PDFs are shown here: download it")
    response = send_file(source, mimetype=kind, as_attachment=False, download_name=name)
    response.headers["X-Harbor-Preview"] = "1"
    return response


def configure_sql_startup_on_admin_start():
    try:
        subprocess.run([sys.executable, os.path.join(LIB_DIR, "sql_startup.py"),
                        "--scan", WEB_ROOT, "--exclude", ADMIN_SITE], check=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        app.logger.exception("Automatic SQL startup configuration failed; inspect the admin service log.")


threading.Thread(target=configure_sql_startup_on_admin_start, daemon=True).start()
threading.Thread(target=sync_linux_users, daemon=True).start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True)
