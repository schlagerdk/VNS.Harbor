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
