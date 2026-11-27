"""DNS in Harbor, from Cloudflare (phase 14 of dev/HARBOR.md): the account Harbor uses, its zones and their records.

The account is kept as Harbor's other secrets (/etc/harbor/secrets/cloudflare.json, root only): an API token (Bearer,
limited to DNS: recommended), or an auth email with the Global API Key (X-Auth-Email and X-Auth-Key). Never sent to a
page: the admin shows only which kind is kept and its email.
"""
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

SETTINGS = Path("/etc/harbor/secrets/cloudflare.json")
API = "https://api.cloudflare.com/client/v4"
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SECRET = re.compile(r"[A-Za-z0-9_\-]{20,200}")
ZONE_ID = re.compile(r"[0-9a-f]{32}")
RECORD_ID = re.compile(r"[0-9a-f]{32}")
TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA", "PTR")
CAA_TAGS = ("issue", "issuewild", "iodef")
# Auto (1), then as account.vns.dk offers them
TTLS = (1, 60, 120, 300, 600, 900, 1800, 3600, 7200, 18000, 43200, 86400)
# What may be proxied through Cloudflare
PROXIABLE = ("A", "AAAA", "CNAME")


class CloudflareError(Exception):
    """Cloudflare said no, or was not reached: its message, for the admin to show"""


def load(path=SETTINGS):
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) and (found.get("token") or (found.get("email") and found.get("key"))) else {}


def shown(settings):
    """What a page may know of the account: whether there is one, its kind and its email, never the key or token"""
    if not settings:
        return {"configured": False}
    return {"configured": True, "auth": "token" if settings.get("token") else "key", "email": settings.get("email", "")}


def validate(data, current=None):
    """{auth: "token", token} or {auth: "key", email, key}; an empty token or key keeps the one saved (same kind)"""
    current = current or {}
    if not isinstance(data, dict) or data.get("auth") not in ("token", "key"):
        raise ValueError("Choose an API token, or an email with the Global API Key.")
    if data["auth"] == "token":
        token = str(data.get("token") or "").strip() or current.get("token", "")
        if not SECRET.fullmatch(token or ""):
            raise ValueError("Paste the API token from Cloudflare (My Profile, API Tokens).")
        return {"token": token}
    email = str(data.get("email") or "").strip()
    key = str(data.get("key") or "").strip() or (current.get("key", "") if current.get("email") else "")
    if not EMAIL.fullmatch(email):
        raise ValueError("Enter the email the Cloudflare account logs in with.")
    if not SECRET.fullmatch(key or ""):
        raise ValueError("Paste the Global API Key from Cloudflare (My Profile, API Tokens).")
    return {"email": email, "key": key}


def save(settings, path=SETTINGS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(settings, stream)
    os.replace(temporary, path)


def remove(path=SETTINGS):
    Path(path).unlink(missing_ok=True)


def call(settings, method, endpoint, body=None, query=None, opener=None):
    """One call to Cloudflare's API: its result, or a CloudflareError with what it said"""
    if not settings:
        raise CloudflareError("No Cloudflare account is set up (Settings, DNS).")
    headers = {"Content-Type": "application/json", "User-Agent": "Harbor"}
    if settings.get("token"):
        headers["Authorization"] = f"Bearer {settings['token']}"
    else:
        headers["X-Auth-Email"] = settings["email"]
        headers["X-Auth-Key"] = settings["key"]
    url = API + endpoint + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=20) as response:
            answer = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        try:
            answer = json.loads(error.read().decode() or "{}")
        except ValueError:
            raise CloudflareError(f"Cloudflare answered {error.code}.") from None
    except (OSError, ValueError) as error:
        raise CloudflareError(f"Cloudflare was not reached: {error}") from None
    if not answer.get("success"):
        messages = [item.get("message", "") for item in answer.get("errors") or [] if isinstance(item, dict)]
        raise CloudflareError("; ".join(filter(None, messages)) or "Cloudflare refused it.")
    return answer


def pages(settings, endpoint, query=None, opener=None):
    """Every item of a listing, page by page"""
    found, page = [], 1
    while True:
        answer = call(settings, "GET", endpoint, query={**(query or {}), "page": page, "per_page": 50}, opener=opener)
        found += answer.get("result") or []
        info = answer.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            return found
        page += 1


def verify(settings, opener=None):
    """Whether the account works, before it is kept: the zones it reaches, counted"""
    if settings.get("token"):
        call(settings, "GET", "/user/tokens/verify", opener=opener)
    return len(zones(settings, opener=opener))


def zones(settings, opener=None):
    """[{id, name, status}], by name"""
    return sorted(({"id": item["id"], "name": item["name"], "status": item.get("status", "")}
                   for item in pages(settings, "/zones", opener=opener)), key=lambda item: item["name"])


def _account_of(settings):
    """Which account the settings log in to, without the key or token: the domains kept are of that one only"""
    import hashlib
    said = json.dumps([settings.get("auth"), settings.get("email"), settings.get("token") or settings.get("key")])
    return hashlib.sha256(said.encode()).hexdigest()[:16]


def kept_zones(settings, path, fresh=False, opener=None):
    """The account's domains as kept (they seldom change): read from Cloudflare the first time, when the account is
    another, or when asked fresh (Settings, Cloudflare, Refresh domains). {"zones": [...], "read": when}"""
    import time
    account = _account_of(settings)
    if not fresh:
        try:
            kept = json.loads(Path(path).read_text(encoding="utf-8"))
            if kept.get("account") == account and isinstance(kept.get("zones"), list):
                return {"zones": kept["zones"], "read": kept.get("read")}
        except (OSError, ValueError):
            pass
    found = zones(settings, opener=opener)
    read = int(time.time())
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps({"account": account, "read": read, "zones": found}), encoding="utf-8")
    os.replace(temporary, path)
    return {"zones": found, "read": read}


def forget_zones(path):
    """The kept domains gone: the account was removed"""
    try:
        Path(path).unlink()
    except OSError:
        pass


def records(settings, zone, opener=None):
    """A zone's records: [{id, type, name, content, priority, ttl, proxied, proxiable}], by type and name"""
    if not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown zone")
    found = [{"id": item["id"], "type": item.get("type", ""), "name": item.get("name", ""), "content": item.get("content", ""),
              "priority": item.get("priority"), "ttl": item.get("ttl", 1), "proxied": bool(item.get("proxied")),
              "proxiable": bool(item.get("proxiable")), "data": item.get("data") if isinstance(item.get("data"), dict) else None}
             for item in pages(settings, f"/zones/{zone}/dns_records", opener=opener)]
    return sorted(found, key=lambda item: (item["type"], item["name"]))


def record_body(data, zone_name, admin, current=None):
    """A record from what was sent, checked: type, name (@ for the zone itself), content, priority (MX), TTL, proxied.
    Proxied is an administrator's: a user's record keeps what it has (off for a new one)"""
    if not isinstance(data, dict):
        raise ValueError("Expected a DNS record")
    kind = current["type"] if current else str(data.get("type") or "")
    if kind not in TYPES:
        raise ValueError("Choose the record's type.")
    name = str(data.get("name") or "").strip().rstrip(".")
    if name in ("", "@"):
        name = zone_name
    elif not name.lower().endswith("." + zone_name.lower()) and name.lower() != zone_name.lower():
        name = f"{name}.{zone_name}"
    if not re.fullmatch(r"[A-Za-z0-9*_.\-]{1,253}", name):
        raise ValueError("The name may have letters, digits, - _ . and * (for a wildcard).")
    # SRV and CAA are kept by Cloudflare as their parts (data), not as one line of content
    if kind in ("SRV", "CAA"):
        return structured_body(kind, name, data)
    content = str(data.get("content") or "").strip()
    if not content or len(content) > 4096:
        raise ValueError("Enter the record's content.")
    # TXT in quotes, as Cloudflare and account.vns.dk keep it
    if kind == "TXT" and not (content.startswith('"') and content.endswith('"')):
        content = f'"{content}"'
    try:
        ttl = int(data.get("ttl") or 1)
    except (TypeError, ValueError):
        ttl = 0
    if ttl not in TTLS:
        raise ValueError("Choose the TTL.")
    body = {"type": kind, "name": name, "content": content, "ttl": ttl}
    if kind == "MX":
        try:
            priority = int(data.get("priority"))
        except (TypeError, ValueError):
            priority = -1
        if not 0 <= priority <= 65535:
            raise ValueError("The priority is 0 to 65535.")
        body["priority"] = priority
    if kind in PROXIABLE:
        proxied = bool(data.get("proxied")) if admin else bool((current or {}).get("proxied"))
        body["proxied"] = proxied
    return body


def whole(value, low, high, what):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low - 1
    if not low <= number <= high:
        raise ValueError(f"The {what} is {low} to {high}.")
    return number


def structured_body(kind, name, data):
    """SRV (_service._proto.name: priority, weight, port, target) and CAA (flags, tag, value), as Cloudflare takes them"""
    try:
        ttl = int(data.get("ttl") or 1)
    except (TypeError, ValueError):
        ttl = 0
    if ttl not in TTLS:
        raise ValueError("Choose the TTL.")
    if kind == "SRV":
        if not re.match(r"_[A-Za-z0-9-]+\._(tcp|udp|tls)\.", name + "."):
            raise ValueError("An SRV record's name starts with the service and protocol, such as _sip._tcp.")
        target = str(data.get("target") or "").strip().rstrip(".")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,253}", target):
            raise ValueError("Enter the target, the name of the server that gives the service.")
        return {"type": kind, "name": name, "ttl": ttl, "data": {"priority": whole(data.get("priority"), 0, 65535, "priority"),
                "weight": whole(data.get("weight"), 0, 65535, "weight"), "port": whole(data.get("port"), 0, 65535, "port"), "target": target}}
    tag = str(data.get("tag") or "")
    if tag not in CAA_TAGS:
        raise ValueError("Choose what the CAA record says: issue, issuewild or iodef.")
    value = str(data.get("value") or "").strip()
    if not value or len(value) > 1024:
        raise ValueError("Enter the CAA record's value, such as letsencrypt.org.")
    return {"type": kind, "name": name, "ttl": ttl, "data": {"flags": whole(data.get("flags") or 0, 0, 255, "flag"), "tag": tag, "value": value}}


def create(settings, zone, body, opener=None):
    return call(settings, "POST", f"/zones/{zone}/dns_records", body, opener=opener)["result"]


def update(settings, zone, record, body, opener=None):
    if not RECORD_ID.fullmatch(record or ""):
        raise ValueError("Unknown record")
    return call(settings, "PUT", f"/zones/{zone}/dns_records/{record}", body, opener=opener)["result"]


def delete(settings, zone, record, opener=None):
    if not RECORD_ID.fullmatch(record or ""):
        raise ValueError("Unknown record")
    call(settings, "DELETE", f"/zones/{zone}/dns_records/{record}", opener=opener)


def record(settings, zone, record_id, opener=None):
    if not RECORD_ID.fullmatch(record_id or ""):
        raise ValueError("Unknown record")
    item = call(settings, "GET", f"/zones/{zone}/dns_records/{record_id}", opener=opener)["result"]
    return {"id": item["id"], "type": item.get("type", ""), "name": item.get("name", ""), "proxied": bool(item.get("proxied"))}


def purge(settings, zone, opener=None):
    call(settings, "POST", f"/zones/{zone}/purge_cache", {"purge_everything": True}, opener=opener)


# ---- the parts of a zone beside DNS (Cloudflare in Harbor's menu): Cache, SSL/TLS, Rules and Security
# A part and what a user given it may do there. SSL/TLS and Security change how every visitor reaches the site: an
# administrator's, unless given
# In the order of the domain's tabs: what it is doing first
PARTS = ("dns", "analytics", "cache", "speed", "rules", "ssl", "security")
PART_NAMES = {"dns": "DNS", "analytics": "Analytics", "cache": "Cache", "speed": "Speed", "ssl": "SSL/TLS", "rules": "Rules", "security": "Security"}
# The zone settings each part shows and changes, with the values Cloudflare takes
SETTINGS_OF = {
    "cache": {"development_mode": ("on", "off"), "cache_level": ("basic", "simplified", "aggressive"),
              "browser_cache_ttl": (0, 30, 60, 120, 300, 1200, 1800, 3600, 7200, 10800, 14400, 18000, 28800, 43200, 57600, 72000, 86400,
                                    172800, 259200, 345600, 432000, 691200, 1382400, 2073600, 2678400, 5356800, 16070400, 31536000),
              "always_online": ("on", "off")},
    "ssl": {"ssl": ("off", "flexible", "full", "strict"), "always_use_https": ("on", "off"), "min_tls_version": ("1.0", "1.1", "1.2", "1.3"),
            "automatic_https_rewrites": ("on", "off"), "tls_1_3": ("on", "off", "zrt")},
    "speed": {"brotli": ("on", "off"), "early_hints": ("on", "off"), "http3": ("on", "off"), "0rtt": ("on", "off"),
              "rocket_loader": ("on", "off")},
    "security": {"security_level": ("essentially_off", "low", "medium", "high", "under_attack"), "browser_check": ("on", "off"),
                 "challenge_ttl": (300, 900, 1800, 2700, 3600, 7200, 10800, 14400, 28800, 57600, 86400, 604800, 2592000, 31536000)},
}


def zone_settings(settings, zone, part, opener=None):
    """The zone settings of a part, as {id: value}; one the token may not read is left out"""
    if part not in SETTINGS_OF or not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown part")
    found = {}
    for item in call(settings, "GET", f"/zones/{zone}/settings", opener=opener).get("result") or []:
        if isinstance(item, dict) and item.get("id") in SETTINGS_OF[part]:
            found[item["id"]] = {"value": item.get("value"), "editable": bool(item.get("editable", True))}
    return found


def change_setting(settings, zone, part, setting, value, opener=None):
    """One zone setting changed, to one of the values Cloudflare takes for it"""
    allowed = SETTINGS_OF.get(part, {}).get(setting)
    if allowed is None or not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown setting")
    if isinstance(allowed[0], int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = None
    if value not in allowed:
        raise ValueError("Not a value this setting takes.")
    return call(settings, "PATCH", f"/zones/{zone}/settings/{setting}", {"value": value}, opener=opener)["result"]


URL = re.compile(r"https?://[^\s\"'<>]{1,2000}")


def purge_urls(settings, zone, urls, opener=None):
    """Only these files forgotten by Cloudflare's cache (at most 30 a call, as Cloudflare takes them)"""
    urls = [url.strip() for url in urls if str(url).strip()]
    if not urls or len(urls) > 30 or not all(URL.fullmatch(url) for url in urls):
        raise ValueError("Give one to 30 addresses, each starting with https://")
    call(settings, "POST", f"/zones/{zone}/purge_cache", {"files": urls}, opener=opener)


# Redirect rules (Cloudflare's Single Redirects: the phase http_request_dynamic_redirect), the rules a site most often
# needs: from a host or path to another address, 301 or 302
REDIRECT_PHASE = "http_request_dynamic_redirect"
HOSTNAME = re.compile(r"(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")


def redirect_rules(settings, zone, opener=None):
    """The zone's redirect rules: [{id, description, enabled, mode, pattern, expression, target, target_expression,
    status, keep_query}]. mode is how a rule matches: "wildcard" (a pattern of the whole address, * for any part, its
    parts in the target as ${1}, ${2}), "all" (every request) or "custom" (Cloudflare's own expression, as it is);
    none when the zone has no such ruleset yet"""
    if not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown zone")
    try:
        ruleset = call(settings, "GET", f"/zones/{zone}/rulesets/phases/{REDIRECT_PHASE}/entrypoint", opener=opener)["result"]
    except CloudflareError as error:
        if "not found" in str(error).lower() or "could not find" in str(error).lower():
            return []
        raise
    return [redirect_read(rule) for rule in ruleset.get("rules") or []]


WILDCARD = re.compile(r'^http\.request\.full_uri (?:strict_)?wildcard r?"((?:[^"\\]|\\.)*)"$')
REPLACE = re.compile(r'^wildcard_replace\(\s*http\.request\.full_uri\s*,\s*r?"((?:[^"\\]|\\.)*)"\s*,\s*r?"((?:[^"\\]|\\.)*)"\s*(?:,[^)]*)?\)$')


def redirect_read(rule):
    """A redirect rule as Harbor shows and changes it"""
    expression = (rule.get("expression") or "").strip()
    redirect = (rule.get("action_parameters") or {}).get("from_value") or {}
    target_url = redirect.get("target_url") or {}
    found = {"id": rule.get("id", ""), "description": rule.get("description", ""), "enabled": rule.get("enabled", True),
             "mode": "custom", "pattern": "", "expression": expression, "target": target_url.get("value") or "", "target_expression": "",
             "status": redirect.get("status_code", 301), "keep_query": bool(redirect.get("preserve_query_string"))}
    if expression == "true":
        found["mode"] = "all"
    elif WILDCARD.match(expression):
        found.update(mode="wildcard", pattern=WILDCARD.match(expression).group(1))
    if target_url.get("expression"):
        replaced = REPLACE.match(target_url["expression"].strip())
        if replaced and found["mode"] == "wildcard" and replaced.group(1) == found["pattern"]:
            found["target"] = replaced.group(2)
        else:
            # Cloudflare's own target expression: kept as it is
            found["target_expression"] = target_url["expression"]
    return found


def wildcard_parts(pattern):
    return pattern.count("*")


def redirect_expression(source_host, source_path=""):
    """A rule's expression from what is typed: a host (and a path, which then begins with it)"""
    host = str(source_host or "").strip().lower().rstrip(".")
    if not HOSTNAME.fullmatch(host):
        raise ValueError("Give the host it is from, such as old.example.com.")
    path = str(source_path or "").strip()
    if path and (not path.startswith("/") or '"' in path or len(path) > 500):
        raise ValueError("The path starts with /, such as /old.")
    if host.startswith("*."):
        expression = f'ends_with(http.host, "{host[1:]}")'
    else:
        expression = f'http.host eq "{host}"'
    if path:
        expression += f' and starts_with(http.request.uri.path, "{path}")'
    return expression


def redirect_build(rule):
    """A rule for Cloudflare from one Harbor shows: how it matches (a wildcard pattern, every request, or Cloudflare's
    own expression), where it goes (${1}, ${2} for what each * of the pattern matched), 301, 302, 307 or 308"""
    status = int(rule.get("status") or 301)
    if status not in (301, 302, 307, 308):
        raise ValueError("A redirect is 301, 302, 307 or 308.")
    mode = rule.get("mode") or ("custom" if rule.get("expression") else "host")
    target = str(rule.get("target") or "").strip()
    target_expression = str(rule.get("target_expression") or "").strip()
    if mode == "host":
        # Harbor's first redirects, by host and path
        expression = redirect_expression(rule.get("source_host"), rule.get("source_path"))
    elif mode == "all":
        expression = "true"
    elif mode == "wildcard":
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern or len(pattern) > 500 or '"' in pattern or "\n" in pattern:
            raise ValueError("Give the pattern, such as https://old.example.com/*")
        expression = f'http.request.full_uri wildcard r"{pattern}"'
    elif mode == "custom":
        expression = str(rule.get("expression") or "").strip()
        if not expression or len(expression) > 4096:
            raise ValueError("Give Cloudflare's expression for the rule.")
    else:
        raise ValueError("Unknown kind of match")
    if target_expression and not target:
        to = {"expression": target_expression}
    else:
        if not URL.fullmatch(target) or '"' in target:
            raise ValueError("The address it goes to starts with https://")
        used = [int(number) for number in re.findall(r"\$\{(\d+)\}", target)]
        if used and mode != "wildcard":
            raise ValueError("${1}, ${2} take what the * of a wildcard pattern matched: choose Wildcard pattern for them.")
        if used and max(used) > wildcard_parts(pattern):
            raise ValueError(f"The pattern has {wildcard_parts(pattern)} *: ${{{max(used)}}} has nothing to take.")
        to = {"expression": f'wildcard_replace(http.request.full_uri, r"{pattern}", r"{target}")'} if used else {"value": target}
    return {"expression": expression, "description": str(rule.get("description") or "")[:500], "enabled": rule.get("enabled", True) is not False,
            "action": "redirect", "action_parameters": {"from_value": {"status_code": status, "target_url": to,
                                                                        "preserve_query_string": bool(rule.get("keep_query"))}}}


def save_redirects(settings, zone, rules, opener=None):
    """The zone's redirect rules as given, put in place of those there were: Cloudflare keeps a phase's rules as one list"""
    built = [redirect_build(rule) for rule in rules]
    call(settings, "PUT", f"/zones/{zone}/rulesets/phases/{REDIRECT_PHASE}/entrypoint", {"rules": built}, opener=opener)


# ---- which parts a token may use: what Settings says is missing
TOKEN_NEEDS = {"dns": "Zone, DNS, Edit", "analytics": "Zone, Analytics, Read", "speed": "Zone, Zone Settings, Edit", "cache": "Zone, Cache Purge, Purge and Zone, Zone Settings, Edit",
               "ssl": "Zone, Zone Settings, Edit (and Zone, SSL and Certificates, Edit)", "rules": "Zone, Single Redirect, Edit (or Zone, Dynamic Redirect, Edit), Zone, Cache Rules, Edit and Zone, Config Rules, Edit",
               "security": "Zone, Zone Settings, Edit"}


# ---- Page Rules (Cloudflare's older rules, still in use on many zones): a URL pattern and its actions
PAGE_RULE_ID = re.compile(r"[0-9a-f]{32}")
PATTERN = re.compile(r"[^\s\"'<>]{1,500}")


def page_rules(settings, zone, opener=None):
    """The zone's page rules, by priority: [{id, pattern, actions: [{id, value}], forward: {url, status} or None, enabled, priority}]"""
    if not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown zone")
    found = []
    for rule in call(settings, "GET", f"/zones/{zone}/pagerules", opener=opener).get("result") or []:
        target = ((rule.get("targets") or [{}])[0].get("constraint") or {}).get("value", "")
        actions = [{"id": action.get("id", ""), "value": action.get("value")} for action in rule.get("actions") or []]
        forward = next(({"url": (action["value"] or {}).get("url", ""), "status": (action["value"] or {}).get("status_code", 301)}
                        for action in actions if action["id"] == "forwarding_url" and isinstance(action["value"], dict)), None)
        found.append({"id": rule.get("id", ""), "pattern": target, "actions": actions, "forward": forward,
                      "enabled": rule.get("status") == "active", "priority": rule.get("priority", 1)})
    return sorted(found, key=lambda item: -item["priority"])


ON_OFF = ("on", "off")
# What a page rule can do, as Cloudflare names it: its values (None for one without, "forward" for Forwarding URL)
PAGE_ACTIONS = {
    "always_use_https": None,
    "automatic_https_rewrites": ON_OFF,
    "browser_cache_ttl": (1800, 3600, 7200, 14400, 28800, 43200, 86400, 172800, 604800, 2678400, 31536000),
    "browser_check": ON_OFF,
    "cache_deception_armor": ON_OFF,
    "cache_level": ("bypass", "basic", "simplified", "aggressive", "cache_everything"),
    "disable_zaraz": None,
    "edge_cache_ttl": (7200, 14400, 28800, 43200, 86400, 172800, 604800, 1209600, 2419200),
    "email_obfuscation": ON_OFF,
    "forwarding_url": "forward",
    "ip_geolocation": ON_OFF,
    "opportunistic_encryption": ON_OFF,
    "explicit_cache_control": ON_OFF,
    "rocket_loader": ON_OFF,
    "security_level": ("essentially_off", "low", "medium", "high", "under_attack"),
    "ssl": ("off", "flexible", "full", "strict"),
}
# These two are a page rule's only action when it has one of them
PAGE_ALONE = ("forwarding_url", "always_use_https")
ACTION_ID = re.compile(r"[a-z0-9_]{1,40}")


def page_action(item, pattern):
    """One action of a page rule, checked: one Harbor knows by its values; another, kept from Cloudflare, as it was"""
    kind = str((item or {}).get("id") or "")
    value = (item or {}).get("value")
    if kind not in PAGE_ACTIONS:
        if not ACTION_ID.fullmatch(kind) or len(json.dumps(value)) > 2000:
            raise ValueError("Unknown setting")
        return {"id": kind, **({"value": value} if value is not None else {})}
    allowed = PAGE_ACTIONS[kind]
    if allowed is None:
        return {"id": kind}
    if allowed == "forward":
        value = value if isinstance(value, dict) else {}
        url = str(value.get("url") or "").strip()
        if not URL.fullmatch(url):
            raise ValueError("The address it forwards to starts with https://")
        used = [int(number) for number in re.findall(r"\$(\d+)", url)]
        if used and max(used) > pattern.count("*"):
            raise ValueError(f"The pattern has {pattern.count('*')} *: ${max(used)} has nothing to take.")
        status = int(value.get("status_code") or 301)
        if status not in (301, 302):
            raise ValueError("A page rule forwards with 301 or 302.")
        return {"id": kind, "value": {"url": url, "status_code": status}}
    if value not in allowed:
        raise ValueError(f"Choose a value for {kind.replace('_', ' ')}.")
    return {"id": kind, "value": value}


def page_rule_body(data):
    """A page rule from what is chosen: its URL pattern (* for any part), what it does there (one or more settings, or
    Forwarding URL or Always use HTTPS alone), on or off. One sent as Harbor's first form ({url, status}) forwards"""
    pattern = str(data.get("pattern") or "").strip()
    if not PATTERN.fullmatch(pattern) or "." not in pattern:
        raise ValueError("Give the URL pattern, such as www.example.com/*")
    given = data.get("actions")
    if given is None and data.get("url"):
        given = [{"id": "forwarding_url", "value": {"url": data.get("url"), "status_code": data.get("status") or 301}}]
    if not isinstance(given, list) or not given:
        raise ValueError("Choose what the rule does.")
    actions = [page_action(item, pattern) for item in given]
    kinds = [action["id"] for action in actions]
    if len(set(kinds)) != len(kinds):
        raise ValueError("Each setting once in a rule.")
    alone = [kind for kind in kinds if kind in PAGE_ALONE]
    if alone and len(kinds) > 1:
        raise ValueError(f"{'Forwarding URL' if alone[0] == 'forwarding_url' else 'Always use HTTPS'} is a rule's only setting.")
    return {"targets": [{"target": "url", "constraint": {"operator": "matches", "value": pattern}}], "actions": actions,
            "status": "active" if data.get("enabled", True) is not False else "disabled"}


def save_page_rule(settings, zone, rule_id, data, opener=None):
    """A forwarding page rule made (rule_id None) or changed"""
    body = page_rule_body(data)
    if rule_id is None:
        return call(settings, "POST", f"/zones/{zone}/pagerules", body, opener=opener)["result"]
    if not PAGE_RULE_ID.fullmatch(rule_id or ""):
        raise ValueError("Unknown page rule")
    return call(settings, "PUT", f"/zones/{zone}/pagerules/{rule_id}", body, opener=opener)["result"]


def page_rule_state(settings, zone, rule_id, enabled, opener=None):
    """A page rule on or off, whatever its actions"""
    if not PAGE_RULE_ID.fullmatch(rule_id or ""):
        raise ValueError("Unknown page rule")
    return call(settings, "PATCH", f"/zones/{zone}/pagerules/{rule_id}", {"status": "active" if enabled else "disabled"}, opener=opener)["result"]


def delete_page_rule(settings, zone, rule_id, opener=None):
    if not PAGE_RULE_ID.fullmatch(rule_id or ""):
        raise ValueError("Unknown page rule")
    call(settings, "DELETE", f"/zones/{zone}/pagerules/{rule_id}", opener=opener)


# ---- Cache Rules and Configuration Rules: what page rules set for a part of the site, as Cloudflare's newer rules.
# Each a phase of its own, its rules a list put in place as a whole, as the redirects are. A rule's settings are said
# with the page rules' names and values, so they are shown and changed as those are; what Harbor does not know of a
# rule's parameters is kept as it was (extra)
SETTINGS_PHASES = {"cache": ("http_request_cache_settings", "set_cache_settings"), "config": ("http_config_settings", "set_config")}
# Configuration Rules: a page rule's setting, its parameter, and its values (bool: on and off as true and false)
CONFIG_SETTINGS = {"automatic_https_rewrites": ("automatic_https_rewrites", bool), "browser_check": ("bic", bool),
                   "email_obfuscation": ("email_obfuscation", bool), "opportunistic_encryption": ("opportunistic_encryption", bool),
                   "rocket_loader": ("rocket_loader", bool), "disable_zaraz": ("disable_zaraz", None),
                   "security_level": ("security_level", ("off", "essentially_off", "low", "medium", "high", "under_attack")),
                   "ssl": ("ssl", ("off", "flexible", "full", "strict", "origin_pull"))}
CACHE_SETTINGS = ("cache_level", "edge_cache_ttl", "browser_cache_ttl", "cache_deception_armor", "explicit_cache_control")
# A cache rule's eligibility: Cache everything is eligible, Bypass is not; Cloudflare's other levels have no rule
CACHE_LEVELS = ("bypass", "cache_everything")


def match_read(expression):
    """How a rule matches: {mode, pattern, expression}, mode "wildcard" (a pattern of the whole address), "all" or "custom\""""
    expression = (expression or "").strip()
    if expression == "true":
        return {"mode": "all", "pattern": "", "expression": expression}
    found = WILDCARD.match(expression)
    if found:
        return {"mode": "wildcard", "pattern": found.group(1), "expression": expression}
    return {"mode": "custom", "pattern": "", "expression": expression}


def match_build(rule):
    """A rule's expression from how it matches: a wildcard pattern of the whole address, every request, or Cloudflare's own"""
    mode = rule.get("mode") or "wildcard"
    if mode == "all":
        return "true"
    if mode == "wildcard":
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern or len(pattern) > 500 or '"' in pattern or "\n" in pattern:
            raise ValueError("Give the pattern, such as https://www.example.com/images/*")
        return f'http.request.full_uri wildcard r"{pattern}"'
    if mode == "custom":
        expression = str(rule.get("expression") or "").strip()
        if not expression or len(expression) > 4096:
            raise ValueError("Give Cloudflare's expression for the rule.")
        return expression
    raise ValueError("Unknown kind of match")


def _ttl(value):
    return isinstance(value, dict) and value.get("mode") == "override_origin" and isinstance(value.get("default"), int) and set(value) <= {"mode", "default"}


def settings_read(kind, rule):
    """A cache or configuration rule as Harbor shows it: [{id, value}] with the page rules' names, and the rest kept"""
    params = json.loads(json.dumps(rule.get("action_parameters") or {}))
    actions = []
    if kind == "cache":
        if isinstance(params.get("cache"), bool):
            actions.append({"id": "cache_level", "value": "cache_everything" if params.pop("cache") else "bypass"})
        for key, setting in (("edge_ttl", "edge_cache_ttl"), ("browser_ttl", "browser_cache_ttl")):
            if _ttl(params.get(key)):
                actions.append({"id": setting, "value": params.pop(key)["default"]})
        key = params.get("cache_key")
        if isinstance(key, dict) and isinstance(key.get("cache_deception_armor"), bool):
            actions.append({"id": "cache_deception_armor", "value": "on" if key.pop("cache_deception_armor") else "off"})
            if not key:
                params.pop("cache_key")
        if isinstance(params.get("origin_cache_control"), bool):
            actions.append({"id": "explicit_cache_control", "value": "on" if params.pop("origin_cache_control") else "off"})
    else:
        for setting, (key, values) in CONFIG_SETTINGS.items():
            value = params.get(key)
            if values is bool and isinstance(value, bool):
                actions.append({"id": setting, "value": "on" if params.pop(key) else "off"})
            elif values is None and value is True:
                params.pop(key)
                actions.append({"id": setting, "value": None})
            elif isinstance(values, tuple) and value in values:
                actions.append({"id": setting, "value": params.pop(key)})
    return {"description": rule.get("description", ""), "enabled": rule.get("enabled", True), **match_read(rule.get("expression")),
            "actions": actions, "extra": params}


def settings_build(kind, rule):
    """A cache or configuration rule for Cloudflare from what Harbor shows: its settings, with what was kept of it"""
    if kind not in SETTINGS_PHASES:
        raise ValueError("Unknown kind of rule")
    extra = rule.get("extra") if isinstance(rule.get("extra"), dict) else {}
    if len(json.dumps(extra)) > 8000:
        raise ValueError("The rule is too large.")
    params = json.loads(json.dumps(extra))
    given = rule.get("actions") if isinstance(rule.get("actions"), list) else []
    kinds = [str((item or {}).get("id") or "") for item in given]
    if len(set(kinds)) != len(kinds):
        raise ValueError("Each setting once in a rule.")
    for item in given:
        setting, value = str(item.get("id") or ""), item.get("value")
        if kind == "cache":
            if setting == "cache_level":
                if value not in CACHE_LEVELS:
                    raise ValueError("A cache rule caches everything there, or bypasses the cache.")
                params["cache"] = value == "cache_everything"
            elif setting in ("edge_cache_ttl", "browser_cache_ttl"):
                if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 31536000:
                    raise ValueError("Choose how long it is kept.")
                params["edge_ttl" if setting == "edge_cache_ttl" else "browser_ttl"] = {"mode": "override_origin", "default": value}
            elif setting == "cache_deception_armor" and value in ON_OFF:
                params.setdefault("cache_key", {})["cache_deception_armor"] = value == "on"
            elif setting == "explicit_cache_control" and value in ON_OFF:
                params["origin_cache_control"] = value == "on"
            else:
                raise ValueError(f"A cache rule does not set {setting.replace('_', ' ')}.")
        else:
            key, values = CONFIG_SETTINGS.get(setting, (None, ()))
            if key is None:
                raise ValueError(f"A configuration rule does not set {setting.replace('_', ' ')}.")
            if values is bool and value in ON_OFF:
                params[key] = value == "on"
            elif values is None:
                params[key] = True
            elif isinstance(values, tuple) and value in values:
                params[key] = value
            else:
                raise ValueError(f"Choose a value for {setting.replace('_', ' ')}.")
    if not params:
        raise ValueError("Choose what the rule does.")
    return {"expression": match_build(rule), "description": str(rule.get("description") or "")[:500], "enabled": rule.get("enabled", True) is not False,
            "action": SETTINGS_PHASES[kind][1], "action_parameters": params}


def settings_rules(settings, zone, kind, opener=None):
    """A zone's cache or configuration rules, in order: none when it has no such ruleset yet"""
    if not ZONE_ID.fullmatch(zone or "") or kind not in SETTINGS_PHASES:
        raise ValueError("Unknown zone")
    try:
        ruleset = call(settings, "GET", f"/zones/{zone}/rulesets/phases/{SETTINGS_PHASES[kind][0]}/entrypoint", opener=opener)["result"]
    except CloudflareError as error:
        if "not found" in str(error).lower() or "could not find" in str(error).lower():
            return []
        raise
    return [settings_read(kind, rule) for rule in ruleset.get("rules") or []]


def save_settings_rules(settings, zone, kind, rules, opener=None):
    """A zone's cache or configuration rules as given, in place of those there were"""
    if not ZONE_ID.fullmatch(zone or "") or kind not in SETTINGS_PHASES:
        raise ValueError("Unknown zone")
    built = [settings_build(kind, rule) for rule in rules]
    call(settings, "PUT", f"/zones/{zone}/rulesets/phases/{SETTINGS_PHASES[kind][0]}/entrypoint", {"rules": built}, opener=opener)


# ---- A page rule made into the newer rules, as Cloudflare's own guide does it: Forwarding URL and Always Use HTTPS a
# redirect, the cache settings a cache rule, the rest a configuration rule. Its pattern becomes one of the whole address
# (http*:// before it where it has no scheme, which is then the first *, and a * after it for the query string)
def page_rule_address(pattern):
    """A page rule's pattern as a wildcard of the whole address, and how many * were put before its own"""
    pattern = pattern.strip()
    if pattern.lower().startswith(("http://", "https://")):
        return pattern, 0
    return "http*://" + pattern, 1


def page_rule_match(full):
    """How the newer rule matches: a pattern that ends in * as the wildcard it is; one that does not (a page rule's
    exact address, its query string not looked at) that address alone or with a query string, not every address after it"""
    if full.endswith("*"):
        return {"mode": "wildcard", "pattern": full, "expression": ""}
    return {"mode": "custom", "pattern": "", "expression": f'(http.request.full_uri wildcard r"{full}" or http.request.full_uri wildcard r"{full}?*")'}


def page_rule_redirect(named, enabled, full, target, status):
    """A redirect from a page rule's address: where an exact address gives its * on, the target is Cloudflare's own
    expression, its last * (the query string) not used, as the query string goes with it anyway"""
    redirect = {"description": named, "enabled": enabled, "status": status, "keep_query": True, **page_rule_match(full), "target": target, "target_expression": ""}
    if redirect["mode"] == "custom" and "${" in target:
        redirect.update(target="", target_expression=f'wildcard_replace(http.request.full_uri, r"{full}*", r"{target}")')
    return redirect


def page_rule_plan(rule):
    """What a page rule becomes: {redirect, cache, config} (each a rule as Harbor shows it, or None), what Cloudflare's
    default already does (dropped: [{id, why}]) and what has no newer rule (left: [{id, why}]). With anything left the
    page rule is kept as it is"""
    pattern = str(rule.get("pattern") or "")
    full, before = page_rule_address(pattern)
    named = f"From page rule {pattern}"[:500]
    enabled = rule.get("enabled", True) is not False
    plan = {"redirect": None, "cache": None, "config": None, "dropped": [], "left": []}
    cache, config = [], []
    for action in rule.get("actions") or []:
        kind, value = action.get("id"), action.get("value")
        if kind == "forwarding_url" and isinstance(value, dict):
            target = re.sub(r"\$(\d+)", lambda found: "${%d}" % (int(found.group(1)) + before), str(value.get("url") or ""))
            plan["redirect"] = page_rule_redirect(named, enabled, full, target, value.get("status_code") or 301)
        elif kind == "always_use_https":
            # From http:// to the same address at https://, each * of it given on
            rest = re.sub(r"^https?://", "", pattern.strip(), flags=re.I)
            at = iter(range(1, rest.count("*") + 1))
            plan["redirect"] = page_rule_redirect(named, enabled, "http://" + rest, "https://" + re.sub(r"\*", lambda found: "${%d}" % next(at), rest), 301)
        elif kind == "cache_level" and value in CACHE_LEVELS:
            cache.append({"id": kind, "value": value})
        elif kind == "cache_level" and value == "aggressive":
            plan["dropped"].append({"id": kind, "why": "Standard is how Cloudflare caches anyway"})
        elif kind == "cache_level":
            plan["left"].append({"id": kind, "why": "No newer rule caches by these query string levels"})
        elif kind in CACHE_SETTINGS:
            cache.append({"id": kind, "value": value})
        elif kind in CONFIG_SETTINGS:
            config.append({"id": kind, "value": value})
        elif kind == "ip_geolocation":
            plan["left"].append({"id": kind, "why": "Only for the whole domain now: Managed Transforms, Add visitor location headers"})
        else:
            plan["left"].append({"id": kind, "why": "No newer rule Harbor knows of"})
    for key, actions in (("cache", cache), ("config", config)):
        if actions:
            plan[key] = {"description": named, "enabled": enabled, **page_rule_match(full), "actions": actions, "extra": {}}
    # Each is checked now, so nothing is sent before all of it can be
    if plan["redirect"]:
        redirect_build(plan["redirect"])
    for key in ("cache", "config"):
        if plan[key]:
            settings_build(key, plan[key])
    return plan


def convert_page_rule(settings, zone, rule_id, opener=None):
    """A page rule made into the newer rules, and turned off (not deleted: on again, it is as it was). A redirect goes
    last of the redirects, where the first that matches wins; a cache or configuration rule first of its kind, where
    the last wins: page rules converted from the top keep the order they had"""
    if not PAGE_RULE_ID.fullmatch(rule_id or ""):
        raise ValueError("Unknown page rule")
    rule = next((item for item in page_rules(settings, zone, opener=opener) if item["id"] == rule_id), None)
    if rule is None:
        raise ValueError("Unknown page rule")
    plan = page_rule_plan(rule)
    if plan["left"]:
        raise ValueError("Some of its settings have no newer rule: the page rule is kept.")
    if plan["redirect"]:
        save_redirects(settings, zone, redirect_rules(settings, zone, opener=opener) + [plan["redirect"]], opener=opener)
    for kind in ("cache", "config"):
        if plan[kind]:
            save_settings_rules(settings, zone, kind, [plan[kind]] + settings_rules(settings, zone, kind, opener=opener), opener=opener)
    if rule["enabled"]:
        page_rule_state(settings, zone, rule_id, False, opener=opener)
    return plan


# ---- Analytics: the zone's traffic a day, from Cloudflare's GraphQL Analytics API (on every plan, the last 30 days)
ANALYTICS_QUERY = """query ($zone: String!, $since: Date!, $until: Date!) { viewer { zones(filter: {zoneTag: $zone}) {
  httpRequests1dGroups(limit: 31, filter: {date_geq: $since, date_leq: $until}, orderBy: [date_ASC]) {
    dimensions { date } sum { requests cachedRequests bytes cachedBytes threats pageViews } uniq { uniques } } } } }"""


# What the traffic was made of over the period: countries, answers, content, browsers, HTTP and TLS. Asked on its own,
# so the days are still shown where a plan or token does not give it
BREAKDOWN_QUERY = """query ($zone: String!, $since: Date!, $until: Date!) { viewer { zones(filter: {zoneTag: $zone}) {
  httpRequests1dGroups(limit: 31, filter: {date_geq: $since, date_leq: $until}) { sum {
    encryptedRequests
    countryMap { clientCountryName requests threats bytes }
    responseStatusMap { edgeResponseStatus requests }
    contentTypeMap { edgeResponseContentTypeName requests bytes }
    browserMap { uaBrowserFamily pageViews }
    clientHTTPVersionMap { clientHTTPProtocol requests }
    clientSSLMap { clientSSLProtocol requests }
  } } } } }"""
# Each list of the breakdown: Cloudflare's map, its name and the numbers kept, how many are shown
BREAKDOWN_MAPS = {
    "countries": ("countryMap", "clientCountryName", ("requests", "threats", "bytes"), 12),
    "statuses": ("responseStatusMap", "edgeResponseStatus", ("requests",), 12),
    "content": ("contentTypeMap", "edgeResponseContentTypeName", ("requests", "bytes"), 8),
    "browsers": ("browserMap", "uaBrowserFamily", ("pageViews",), 8),
    "http": ("clientHTTPVersionMap", "clientHTTPProtocol", ("requests",), 6),
    "tls": ("clientSSLMap", "clientSSLProtocol", ("requests",), 6),
}


def _graphql(settings, query, variables, opener=None):
    headers = {"Content-Type": "application/json", "User-Agent": "Harbor"}
    if settings.get("token"):
        headers["Authorization"] = f"Bearer {settings['token']}"
    else:
        headers["X-Auth-Email"], headers["X-Auth-Key"] = settings["email"], settings["key"]
    request = urllib.request.Request(API + "/graphql", method="POST", headers=headers, data=json.dumps({"query": query, "variables": variables}).encode())
    try:
        with (opener or urllib.request.urlopen)(request, timeout=30) as response:
            answer = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        raise CloudflareError(f"Cloudflare answered {error.code}.") from None
    except (OSError, ValueError) as error:
        raise CloudflareError(f"Cloudflare was not reached: {error}") from None
    if answer.get("errors"):
        raise CloudflareError("; ".join(item.get("message", "") for item in answer["errors"] if isinstance(item, dict)) or "Cloudflare refused it.")
    zones = (((answer.get("data") or {}).get("viewer") or {}).get("zones") or [{}])
    return zones[0].get("httpRequests1dGroups") or [] if zones else []


def _period(zone, days, today):
    import datetime
    if not ZONE_ID.fullmatch(zone or "") or days not in (1, 7, 30):
        raise ValueError("Unknown zone or period")
    until = today or datetime.date.today()
    return until - datetime.timedelta(days=days - 1), until


def breakdown(settings, zone, days=7, today=None, opener=None):
    """What the period's traffic was made of, each list summed over its days and largest first:
    {encrypted, countries: [{name, requests, threats, bytes}], statuses: [{name, requests}], content: [{name, requests,
    bytes}], browsers: [{name, views}], http: [{name, requests}], tls: [{name, requests}]}"""
    since, until = _period(zone, days, today)
    groups = _graphql(settings, BREAKDOWN_QUERY, {"zone": zone, "since": since.isoformat(), "until": until.isoformat()}, opener)
    found = {"encrypted": 0}
    for key, (field, name, numbers, shown) in BREAKDOWN_MAPS.items():
        summed = {}
        for group in groups:
            for item in (group.get("sum") or {}).get(field) or []:
                label = str(item.get(name) if item.get(name) not in (None, "") else "Unknown")
                into = summed.setdefault(label, dict.fromkeys(numbers, 0))
                for number in numbers:
                    into[number] += int(item.get(number) or 0)
        rows = [{"name": label, **{("views" if number == "pageViews" else number): value for number, value in values.items()}}
                for label, values in summed.items()]
        rows.sort(key=lambda row: -(row.get("requests", row.get("views", 0))))
        found[key] = rows[:shown]
    found["encrypted"] = sum(int((group.get("sum") or {}).get("encryptedRequests") or 0) for group in groups)
    return found


def analytics(settings, zone, days=7, today=None, opener=None):
    """The zone's traffic a day over the last days (7 or 30): [{date, requests, cached, bytes, cached_bytes, threats,
    views, visitors}], oldest first"""
    import datetime
    since, until = _period(zone, days, today)
    groups = _graphql(settings, ANALYTICS_QUERY, {"zone": zone, "since": since.isoformat(), "until": until.isoformat()}, opener)
    found = {}
    for group in groups:
        total, uniq = group.get("sum") or {}, group.get("uniq") or {}
        found[(group.get("dimensions") or {}).get("date", "")] = {"requests": total.get("requests", 0), "cached": total.get("cachedRequests", 0),
                                                                  "bytes": total.get("bytes", 0), "cached_bytes": total.get("cachedBytes", 0),
                                                                  "threats": total.get("threats", 0), "views": total.get("pageViews", 0),
                                                                  "visitors": uniq.get("uniques", 0)}
    # Every day of the period, a day with nothing as nothing
    empty = {"requests": 0, "cached": 0, "bytes": 0, "cached_bytes": 0, "threats": 0, "views": 0, "visitors": 0}
    return [{"date": (since + datetime.timedelta(days=at)).isoformat(), **found.get((since + datetime.timedelta(days=at)).isoformat(), empty)}
            for at in range(days)]


# ---- which records are locked for users: kept by Harbor (not in Cloudflare), set by an administrator a record at a time.
# Not set, a proxied record is locked (as it was before there was a choice) and any other is not
# ---- how many rules a domain may have: Cloudflare's own numbers, its plan's where those are not given
REDIRECTS_BY_PLAN = {"free": 10, "pro": 25, "business": 50, "enterprise": 300}


def rule_limits(settings, zone, opener=None):
    """{"redirects", "page_rules", "cache", "config"}: how many redirect rules, page rules, cache rules and configuration
    rules, from the domain's entitlements, else the domain itself (page rules) and its plan (the rest, as many of each);
    None where it cannot be told"""
    if not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown zone")
    found = call(settings, "GET", f"/zones/{zone}", opener=opener).get("result") or {}
    plan = str(((found.get("plan") or {}).get("legacy_id")) or "").lower()
    limits = {"redirects": REDIRECTS_BY_PLAN.get(plan), "page_rules": (found.get("meta") or {}).get("page_rule_quota"),
              "cache": REDIRECTS_BY_PLAN.get(plan), "config": REDIRECTS_BY_PLAN.get(plan)}
    # The entitlements are exact, but not every token may read them
    try:
        for item in call(settings, "GET", f"/zones/{zone}/entitlements", opener=opener).get("result") or []:
            if not isinstance(item, dict):
                continue
            value = (item.get("allocation") or {}).get("value")
            if item.get("id") == "rulesets.dynamic_redirect_rule_max" and isinstance(value, int):
                limits["redirects"] = value
            elif item.get("id") == "page_rules" and isinstance(value, int):
                limits["page_rules"] = value
    except CloudflareError:
        pass
    return {key: value if isinstance(value, int) and not isinstance(value, bool) else None for key, value in limits.items()}


# ---- Managed Transforms: headers Cloudflare adds to or takes from each request and answer, turned on and off
MANAGED_HEADERS = {
    "add_client_certificate_headers": ("Add TLS client auth headers", "The server is told the visitor's client certificate, where mutual TLS (mTLS) is used"),
    "add_visitor_location_headers": ("Add visitor location headers", "The server is told the visitor's city, country, continent, longitude and latitude"),
    "remove_visitor_ip_headers": ("Remove visitor IP headers", "Headers that may carry the visitor's IP address are taken off before the server sees them"),
    "add_true_client_ip_headers": ("Add True-Client-IP header", "The server is told the visitor's IP address in a True-Client-IP header"),
    "add_waf_credential_check_status_header": ("Add leaked credentials checks header", "The server is told in Exposed-Credential-Check when a login uses a password known to have leaked"),
    "remove_x-powered-by_header": ("Remove X-Powered-By headers", "The answer no longer says what software the server runs"),
    "add_security_headers": ("Add security headers", "Answers get headers against cross-site scripting and being framed by other sites"),
}


def managed_headers(settings, zone, opener=None):
    """{"request": [...], "response": [...]}: each {id, title, text, enabled, conflicts: [ids it cannot be on with]}"""
    if not ZONE_ID.fullmatch(zone or ""):
        raise ValueError("Unknown zone")
    found = call(settings, "GET", f"/zones/{zone}/managed_headers", opener=opener).get("result") or {}
    said = lambda item: {"id": item.get("id"), "title": MANAGED_HEADERS.get(item.get("id"), (item.get("id"), ""))[0],
                         "text": MANAGED_HEADERS.get(item.get("id"), ("", ""))[1], "enabled": bool(item.get("enabled")),
                         "conflicts": [other for other in item.get("conflicts_with") or [] if isinstance(other, str)] if item.get("has_conflict") else []}
    return {"request": [said(item) for item in found.get("managed_request_headers") or [] if isinstance(item, dict)],
            "response": [said(item) for item in found.get("managed_response_headers") or [] if isinstance(item, dict)]}


def set_managed_header(settings, zone, header, enabled, opener=None):
    """One managed header turned on or off: only one Cloudflare has for the domain"""
    current = managed_headers(settings, zone, opener)
    side = next((side for side in ("request", "response") if any(item["id"] == header for item in current[side])), None)
    if not side:
        raise ValueError("Unknown header")
    body = {"managed_request_headers": [], "managed_response_headers": []}
    body[f"managed_{side}_headers"] = [{"id": header, "enabled": bool(enabled)}]
    call(settings, "PATCH", f"/zones/{zone}/managed_headers", body, opener=opener)
    return managed_headers(settings, zone, opener)


def load_locks(path):
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def is_locked(locks, zone, record):
    """Whether a record ({id, proxied}) is locked for users"""
    chosen = (locks.get(zone) or {}).get(record.get("id"))
    return bool(record.get("proxied")) if chosen is None else bool(chosen)


def keep_open(path, zone, record_id, admin):
    """A record a user made or changed stays open to them, also when they proxied it (a proxied record is otherwise
    locked until opened). An administrator's choice is left as it is"""
    if admin:
        return
    if (load_locks(path).get(zone) or {}).get(record_id) is None:
        set_lock(path, zone, record_id, False)


def set_lock(path, zone, record_id, locked):
    """A record locked (True) or opened (False) for users, as chosen; None forgets the choice (a deleted record)"""
    if not ZONE_ID.fullmatch(zone or "") or not RECORD_ID.fullmatch(record_id or ""):
        raise ValueError("Unknown record")
    locks = load_locks(path)
    zone_locks = locks.setdefault(zone, {})
    if locked is None:
        zone_locks.pop(record_id, None)
    else:
        zone_locks[record_id] = bool(locked)
    if not zone_locks:
        locks.pop(zone, None)
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(locks), encoding="utf-8")
    os.replace(temporary, path)
