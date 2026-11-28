"""Mail in Harbor, from mailcow (phase 15 of dev/HARBOR.md): the mailcow server Harbor uses, its state, domains,
mailboxes and aliases, and the DNS records a domain needs.

The server is kept as Harbor's other secrets (/etc/harbor/secrets/mailcow.json, root only): its host and an API key
with read and write access (mailcow, Configuration, Access, API). Never sent to a page: the admin shows only the host.
mailcow answers 200 to a write that failed too: its messages say which (type success, or danger with the reason).
"""
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

SETTINGS = Path("/etc/harbor/secrets/mailcow.json")
HOST = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
KEY = re.compile(r"[A-Za-z0-9-]{16,128}")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")
LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
# The DKIM key mailcow makes for a new domain, and its selector
DKIM_SELECTOR = "dkim"
DKIM_BITS = 2048
# What a domain's mail policy says when Harbor makes it: mail from elsewhere quarantined, as a start
SPF = "v=spf1 mx ~all"
DMARC = "v=DMARC1; p=quarantine"
# mailcow's messages, in words; the rest as mailcow says them (domain_exists → domain exists)
MESSAGES = {
	"domain_exists": "The domain is in mailcow already.", "domain_not_empty": "The domain still has mailboxes: delete them first.",
	"domain_not_found": "mailcow has no such domain.", "object_exists": "That address is taken.",
	"max_mailbox_exceeded": "The domain has as many mailboxes as it may.", "max_alias_exceeded": "The domain has as many aliases as it may.",
	"mailbox_quota_exceeded": "That is more than the domain's quota leaves.", "password_complexity": "The password is too weak for mailcow's password policy.",
	"password_mismatch": "The passwords do not match.", "is_alias_or_mailbox": "That address is a mailbox or an alias already.",
	"goto_invalid": "A destination is not a valid address.", "access_denied": "mailcow refused it: the API key may not do that.",
	"domain_invalid": "That is not a domain mailcow takes.", "mailbox_invalid": "That is not a valid address.",
	"alias_invalid": "That is not a valid alias.", "quota_not_0_not_numeric": "Give the quota in MB.",
}


class MailcowError(Exception):
	"""mailcow said no, or was not reached: its message, for the admin to show"""


def load(path=SETTINGS):
	try:
		found = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return found if isinstance(found, dict) and found.get("host") and found.get("key") else {}


def shown(settings):
	"""What a page may know of the server: whether there is one, and its host; never the key"""
	return {"configured": True, "host": settings["host"]} if settings else {"configured": False}


def validate(data, current=None):
	"""{host, key}; an empty key keeps the one saved for the same host"""
	current = current or {}
	if not isinstance(data, dict):
		raise ValueError("Enter the mailcow server and its API key.")
	host = str(data.get("host") or "").strip().lower().removeprefix("https://").rstrip("/")
	if not HOST.fullmatch(host):
		raise ValueError("Enter the mailcow server's name, such as mail.example.com.")
	key = str(data.get("key") or "").strip() or (current.get("key", "") if current.get("host") == host else "")
	if not KEY.fullmatch(key or ""):
		raise ValueError("Paste the API key from mailcow (Configuration, Access, API: read and write).")
	return {"host": host, "key": key}


def save(settings, path=SETTINGS):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{os.getpid()}")
	with open(temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
		json.dump(settings, stream)
	os.replace(temporary, path)


def remove(path=SETTINGS):
	Path(path).unlink(missing_ok=True)


def said(message):
	"""One of mailcow's messages in words: a key alone, or a key with its values"""
	parts = message if isinstance(message, list) else [message]
	key = str(parts[0]) if parts else ""
	words = MESSAGES.get(key) or key.replace("_", " ").capitalize()
	values = [str(value) for value in parts[1:] if isinstance(value, (str, int)) and str(value)]
	return f"{words} ({', '.join(values)})" if values and key not in MESSAGES else words


def call(settings, method, endpoint, body=None, opener=None):
	"""One call to mailcow's API: its answer, or a MailcowError with what it said (a write's messages are read: mailcow
	answers 200 to one that failed)"""
	if not settings:
		raise MailcowError("No mailcow server is set up (Settings, Mail).")
	request = urllib.request.Request(f"https://{settings['host']}/api/v1/{endpoint}", method=method,
	                                 headers={"X-API-Key": settings["key"], "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Harbor"},
	                                 data=json.dumps(body).encode() if body is not None else None)
	try:
		with (opener or urllib.request.urlopen)(request, timeout=30) as response:
			answer = json.loads(response.read().decode() or "{}")
	except urllib.error.HTTPError as error:
		try:
			answer = json.loads(error.read().decode() or "{}")
		except ValueError:
			answer = {}
		if error.code in (401, 403):
			raise MailcowError("mailcow did not take the API key, or not from this server's address (mailcow, Configuration, Access, API: Allow API access from).") from None
		raise MailcowError(answer.get("msg") if isinstance(answer, dict) and isinstance(answer.get("msg"), str) else f"mailcow answered {error.code}.") from None
	except (OSError, ValueError) as error:
		raise MailcowError(f"mailcow was not reached: {error}") from None
	# A refusal of the key itself comes as {"type": "error", "msg": "authentication failed"}
	if isinstance(answer, dict) and answer.get("type") == "error":
		message = str(answer.get("msg") or "")
		if "authentication" in message.lower() or "api" in message.lower():
			raise MailcowError("mailcow did not take the API key, or not from this server's address (mailcow, Configuration, Access, API: Allow API access from).")
		raise MailcowError(said(message) if message else "mailcow refused it.")
	if method == "POST" and isinstance(answer, list):
		failed = [said(item.get("msg")) for item in answer if isinstance(item, dict) and item.get("type") in ("danger", "error")]
		if failed:
			raise MailcowError(" ".join(dict.fromkeys(failed)))
	return answer


def listed(answer):
	"""A listing: mailcow answers {} where there is none"""
	return answer if isinstance(answer, list) else []


# ---- the server
def verify(settings, opener=None):
	"""Whether the server and key work, before they are kept: its version"""
	answer = call(settings, "GET", "get/status/version", opener=opener)
	if not isinstance(answer, dict) or "version" not in answer:
		raise MailcowError("That does not answer as mailcow does.")
	# A read-only key reads too: a write it may not do is told by a domain's own edit, so the key's access is asked
	return answer.get("version") or "unknown"


def status(settings, opener=None):
	"""Its state: {version, containers: [{name, state}], running, total, disk: {used, total, percent}}"""
	version = call(settings, "GET", "get/status/version", opener=opener).get("version") or ""
	containers = call(settings, "GET", "get/status/containers", opener=opener)
	found = []
	for item in (containers.values() if isinstance(containers, dict) else containers if isinstance(containers, list) else []):
		if isinstance(item, dict) and item.get("container"):
			found.append({"name": item["container"].removesuffix("-mailcow"), "state": item.get("state", ""), "image": item.get("image", "")})
	found.sort(key=lambda item: item["name"])
	disk = call(settings, "GET", "get/status/vmail", opener=opener)
	disk = disk if isinstance(disk, dict) else {}
	return {"version": version, "containers": found, "running": sum(item["state"] == "running" for item in found), "total": len(found),
	        "disk": {"used": disk.get("used", ""), "total": disk.get("total", ""), "percent": str(disk.get("used_percent", "")).rstrip("%")}}


# ---- domains
def domain_name(value):
	name = str(value or "").strip().lower().rstrip(".")
	try:
		name = name.encode("idna").decode("ascii")
	except (UnicodeError, ValueError):
		raise ValueError("That is not a domain.") from None
	if not DOMAIN.fullmatch(name):
		raise ValueError("That is not a domain.")
	return name


def mib(value):
	"""A size mailcow gives in bytes, in MB"""
	try:
		return int(int(value) / 1048576)
	except (TypeError, ValueError):
		return 0


# How a domain takes mail: its mailboxes here (""), or relayed to its own mail server (backup MX) for its known
# recipients, all of them, or those without a mailbox here, as mailcow's own badges say it
RELAYS = {"": "Mailboxes here", "relay": "Relay", "all": "Relay All", "nonlocal": "Relay Non-Local"}


def relay_of(item):
	flag = lambda key: str(item.get(key + "_int", item.get(key, 0)) or 0) == "1"
	if not flag("backupmx"):
		return ""
	return "nonlocal" if flag("relay_unknown_only") else "all" if flag("relay_all_recipients") else "relay"


def relay_fields(value):
	"""What mailcow is sent for a domain's relay: backupmx, relay_all_recipients and relay_unknown_only"""
	if value not in RELAYS:
		raise ValueError("Choose how the domain takes mail.")
	return {"backupmx": int(bool(value)), "relay_all_recipients": int(value in ("all", "nonlocal")), "relay_unknown_only": int(value == "nonlocal")}


def domain_item(item):
	return {"domain": item.get("domain_name", ""), "relay": relay_of(item), "name": item.get("domain_h_name") or item.get("domain_name", ""),
	        "description": item.get("description", ""), "active": bool(int(item.get("active_int", item.get("active", 1)) or 0)),
	        "mailboxes": int(item.get("mboxes_in_domain") or 0), "max_mailboxes": int(item.get("max_num_mboxes_for_domain") or 0),
	        "aliases": int(item.get("aliases_in_domain") or 0), "max_aliases": int(item.get("max_num_aliases_for_domain") or 0),
	        "quota_mb": mib(item.get("max_quota_for_domain")), "mailbox_quota_mb": mib(item.get("max_quota_for_mbox")),
	        "default_quota_mb": mib(item.get("def_quota_for_mbox")), "used_mb": mib(item.get("bytes_total")),
	        "assigned_mb": mib(item.get("quota_used_in_domain")), "messages": int(item.get("msgs_total") or 0)}


def domains(settings, opener=None):
	return sorted((domain_item(item) for item in listed(call(settings, "GET", "get/domain/all", opener=opener)) if isinstance(item, dict)),
	              key=lambda item: item["domain"])


def domain(settings, name, opener=None):
	answer = call(settings, "GET", f"get/domain/{urllib.parse.quote(domain_name(name))}", opener=opener)
	if not isinstance(answer, dict) or not answer.get("domain_name"):
		raise MailcowError("mailcow has no such domain.")
	return domain_item(answer)


def whole(value, low, high, what):
	try:
		number = int(value)
	except (TypeError, ValueError):
		raise ValueError(f"Give the {what} as a number.") from None
	if not low <= number <= high:
		raise ValueError(f"The {what} is {low} to {high}.")
	return number


def domain_limits(data):
	"""A domain's limits, checked: mailboxes, aliases, its total quota and each mailbox's (MB)"""
	limits = {"mailboxes": whole(data.get("mailboxes"), 1, 10000, "number of mailboxes"),
	          "aliases": whole(data.get("aliases"), 0, 100000, "number of aliases"),
	          "quota": whole(data.get("quota_mb"), 1, 100 * 1024 * 1024, "domain's quota"),
	          "maxquota": whole(data.get("mailbox_quota_mb"), 1, 100 * 1024 * 1024, "quota of a mailbox")}
	if limits["maxquota"] > limits["quota"]:
		raise ValueError("A mailbox's quota cannot be more than the domain's.")
	limits["defquota"] = min(whole(data.get("default_quota_mb") or limits["maxquota"], 1, 100 * 1024 * 1024, "default quota"), limits["maxquota"])
	return limits


# A Relay All domain has no mailboxes or aliases here: its limits are the least mailcow takes (a quota is at least 1 MB)
RELAY_ALL_LIMITS = {"mailboxes": 0, "aliases": 0, "quota": 1, "maxquota": 1, "defquota": 1}


def limits_for(data):
	"""A domain's limits as sent, or the least for one that relays all its mail"""
	return dict(RELAY_ALL_LIMITS) if (data.get("relay") or "") == "all" else domain_limits(data)


def add_domain(settings, data, opener=None):
	"""A new domain with its limits and its DKIM key, made in one call"""
	name = domain_name(data.get("domain"))
	body = {"domain": name, "description": str(data.get("description") or name)[:255], **limits_for(data), **relay_fields(data.get("relay") or ""), "active": 1,
	        "dkim_selector": DKIM_SELECTOR, "key_size": DKIM_BITS, "restart_sogo": 1}
	call(settings, "POST", "add/domain", body, opener=opener)
	return name


def edit_domain(settings, name, data, opener=None):
	attr = {**limits_for(data), "active": 1 if data.get("active", True) else 0}
	if "relay" in data:
		attr.update(relay_fields(data.get("relay") or ""))
	call(settings, "POST", "edit/domain", {"items": [domain_name(name)], "attr": attr}, opener=opener)


def delete_domain(settings, name, opener=None):
	"""A domain with its mailboxes (and their mail) and DKIM key: mailcow deletes a domain only once it is empty"""
	name = domain_name(name)
	boxes = [item["address"] for item in mailboxes(settings, name, opener=opener)]
	if boxes:
		call(settings, "POST", "delete/mailbox", boxes, opener=opener)
	call(settings, "POST", "delete/domain", [name], opener=opener)
	try:
		call(settings, "POST", "delete/dkim", [name], opener=opener)
	except MailcowError:
		pass
	return len(boxes)


def dkim(settings, name, opener=None):
	"""{selector, txt}, or None without a key"""
	answer = call(settings, "GET", f"get/dkim/{urllib.parse.quote(domain_name(name))}", opener=opener)
	if not isinstance(answer, dict) or not answer.get("dkim_txt"):
		return None
	return {"selector": answer.get("dkim_selector") or DKIM_SELECTOR, "txt": answer["dkim_txt"]}


def add_dkim(settings, name, opener=None):
	call(settings, "POST", "add/dkim", {"domains": domain_name(name), "dkim_selector": DKIM_SELECTOR, "key_size": DKIM_BITS}, opener=opener)


# ---- mailboxes and aliases
def mailbox_item(item):
	return {"address": item.get("username", ""), "local_part": item.get("local_part", ""), "name": item.get("name", ""),
	        "active": int(item.get("active_int", item.get("active", 1)) or 0), "quota_mb": mib(item.get("quota")),
	        "used_mb": mib(item.get("quota_used")), "messages": int(item.get("messages") or 0),
	        "percent": item.get("percent_in_use") if isinstance(item.get("percent_in_use"), int) else None,
	        "last_login": max(int(item.get(key) or 0) for key in ("last_imap_login", "last_smtp_login", "last_pop3_login", "last_sso_login"))}


def mailboxes(settings, name, opener=None):
	found = listed(call(settings, "GET", f"get/mailbox/all/{urllib.parse.quote(domain_name(name))}", opener=opener))
	return sorted((mailbox_item(item) for item in found if isinstance(item, dict)), key=lambda item: item["address"])


def address(value, domain_of=None):
	"""An address checked, and in the domain asked for"""
	value = str(value or "").strip().lower()
	local, _, domain = value.rpartition("@")
	if not LOCAL_PART.fullmatch(local or "") or len(local) > 64:
		raise ValueError("That is not an address.")
	domain = domain_name(domain)
	if domain_of and domain != domain_name(domain_of):
		raise ValueError(f"The address is not in {domain_of}.")
	return f"{local}@{domain}"


def password(data, required=True):
	value = str(data.get("password") or "")
	if not value and not required:
		return ""
	if len(value) < 8 or len(value) > 256 or any(char in value for char in "\0\r\n"):
		raise ValueError("The password must be 8 to 256 characters.")
	return value


def add_mailbox(settings, domain, data, opener=None):
	name = domain_name(domain)
	local = str(data.get("local_part") or "").strip().lower()
	address(f"{local}@{name}")
	secret = password(data)
	call(settings, "POST", "add/mailbox", {"local_part": local, "domain": name, "name": str(data.get("name") or "").strip()[:255],
	                                       "password": secret, "password2": secret, "quota": whole(data.get("quota_mb"), 1, 100 * 1024 * 1024, "quota"),
	                                       "active": 1, "force_pw_update": 0}, opener=opener)
	return f"{local}@{name}"


def edit_mailbox(settings, mailbox, data, opener=None):
	attr = {}
	if "name" in data:
		attr["name"] = str(data.get("name") or "").strip()[:255]
	if data.get("quota_mb") not in (None, ""):
		attr["quota"] = whole(data.get("quota_mb"), 1, 100 * 1024 * 1024, "quota")
	if "active" in data:
		attr["active"] = 1 if data.get("active") else 0
	secret = password(data, required=False)
	if secret:
		attr.update(password=secret, password2=secret)
	if not attr:
		raise ValueError("Nothing to change.")
	call(settings, "POST", "edit/mailbox", {"items": [mailbox], "attr": attr}, opener=opener)


def delete_mailbox(settings, mailbox, opener=None):
	call(settings, "POST", "delete/mailbox", [mailbox], opener=opener)


def aliases(settings, name, opener=None):
	found = listed(call(settings, "GET", f"get/alias/all/{urllib.parse.quote(domain_name(name))}", opener=opener))
	return sorted(({"id": item.get("id"), "address": item.get("address", ""), "goto": [part for part in str(item.get("goto") or "").split(",") if part],
	                "active": int(item.get("active_int", item.get("active", 1)) or 0)} for item in found if isinstance(item, dict)),
	              key=lambda item: item["address"])


def add_alias(settings, domain, data, opener=None):
	name = domain_name(domain)
	local = str(data.get("local_part") or "").strip().lower()
	alias = f"@{name}" if local in ("", "*") else address(f"{local}@{name}")
	goto = [address(part) for part in re.split(r"[\s,;]+", str(data.get("goto") or "")) if part]
	if not goto:
		raise ValueError("Give the address mail to it goes to.")
	call(settings, "POST", "add/alias", {"address": alias, "goto": ",".join(goto), "active": 1, "sogo_visible": 1, "sender_allowed": 1}, opener=opener)
	return alias


def delete_alias(settings, alias_id, opener=None):
	call(settings, "POST", "delete/alias", [str(int(alias_id))], opener=opener)


# ---- the DNS a domain needs, as mailcow's own DNS check asks for it (data/web/inc/ajax/dns_diagnostics.php): with its
# standard ports only MX, autodiscover and autoconfig, the autodiscover SRV record, SPF, DMARC and DKIM
def wanted_records(host, name, key=None, relay=""):
	"""[{kind, type, name, content | data, priority, what}], what the domain needs on the mailcow server host. A relay
	domain's mail is delivered to its own mail server: its MX and autodiscover stay there, and it needs only what lets
	the mailcow server send its mail (SPF, DKIM and DMARC)"""
	found = [
		{"kind": "mx", "type": "MX", "name": name, "content": host, "priority": 10, "what": "Where mail to the domain is delivered"},
		{"kind": "autodiscover", "type": "CNAME", "name": f"autodiscover.{name}", "content": host, "what": "Outlook and phones find the server"},
		{"kind": "autoconfig", "type": "CNAME", "name": f"autoconfig.{name}", "content": host, "what": "Thunderbird finds the server"},
		{"kind": "srv", "type": "SRV", "name": f"_autodiscover._tcp.{name}", "data": {"priority": 0, "weight": 1, "port": 443, "target": host},
		 "content": f"0 1 443 {host}", "what": "Autodiscover's service record"},
		# SPF lets the mailcow server send: by mx where the domain's mail comes here, by its name for a relay domain (whose
		# MX is its own mail server)
		{"kind": "spf", "type": "TXT", "name": name, "content": SPF if not relay else f"v=spf1 a:{host} ~all", "allow": "mx" if not relay else f"a:{host}",
		 "host": host, "what": "SPF: which servers send the domain's mail"},
		{"kind": "dmarc", "type": "TXT", "name": f"_dmarc.{name}", "content": DMARC, "what": "DMARC: what a receiver does with mail that fails SPF and DKIM"},
	]
	if relay:
		found = [item for item in found if item["kind"] in ("spf", "dmarc")]
	if key:
		found.append({"kind": "dkim", "type": "TXT", "name": f"{key['selector']}._domainkey.{name}", "content": key["txt"], "what": "DKIM: the key the domain's mail is signed with"})
	return found


def flat(text):
	"""A TXT record's text as compared: without its quotes and the spaces Cloudflare puts between its parts"""
	return re.sub(r'"\s*"', "", str(text or "")).strip('"').replace(" ", "").lower()


class Resolver:
	"""What SPF needs looked up: the mailcow server's addresses, and the SPF record of a domain it includes (dig, through
	this server's resolver)"""

	def __init__(self, run=None):
		self.run = run

	def addresses(self, host):
		import socket
		try:
			return {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
		except (OSError, UnicodeError):
			return set()

	def txt(self, name):
		import subprocess
		try:
			result = (self.run or subprocess.run)(["dig", "+short", "+time=3", "+tries=1", "TXT", name], capture_output=True, text=True, timeout=10)
		except (OSError, subprocess.SubprocessError):
			return []
		return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def spf_allows(text, item, resolver, seen=None):
	"""Whether an SPF record lets the mailcow server send: by mx (where the domain's mail comes to it), by its name, by
	an ip4 or ip6 range its address is in, or through an include or redirect that does (followed, at most ten lookups as
	SPF itself allows)"""
	import ipaddress
	seen = seen if seen is not None else set()
	host = item["host"]
	addresses = getattr(resolver, "_found", None)
	if addresses is None and resolver:
		addresses = resolver._found = {ipaddress.ip_address(address) for address in resolver.addresses(host)}
	addresses = addresses or set()
	for word in flat_text(text).lower().split()[1:]:
		qualifier, word = (word[0], word[1:]) if word[:1] in "+-~?" else ("+", word)
		if qualifier != "+":
			continue
		mechanism, _, value = word.partition(":") if ":" in word else word.partition("=")
		if mechanism == "mx" and not value and item["allow"] == "mx":
			return True
		if mechanism == "a" and value.rstrip(".") == host:
			return True
		if mechanism in ("ip4", "ip6") and value:
			try:
				network = ipaddress.ip_network(value, strict=False)
			except ValueError:
				continue
			if any(address in network for address in addresses):
				return True
		if mechanism in ("include", "redirect") and value and resolver and len(seen) < 10 and value not in seen:
			seen.add(value)
			for record in resolver.txt(value):
				if flat(record).startswith("v=spf1") and spf_allows(record, item, resolver, seen):
					return True
	return False


def flat_text(text):
	"""A TXT record's text as it reads: its parts joined, without the quotes"""
	return re.sub(r'"\s*"', "", str(text or "")).strip().strip('"')


def dkim_key(text):
	"""What makes two DKIM records the same: their key (p) and its type (k, rsa when not said). Tags such as t=s and
	s=email, and the spaces and quotes between the parts, do not"""
	tags = {}
	for part in flat_text(text).split(";"):
		name, _, value = part.partition("=")
		tags[name.strip().lower()] = re.sub(r"\s+", "", value)
	return (tags.get("k") or "rsa").lower(), tags.get("p", "")


def check_records(wanted, current, resolver=None):
	"""Each wanted record with its state against the domain's records now: ok, missing or differs (with what is there).
	An SPF record that has other senders keeps them: ok when it lets the mailcow server send (spf_allows)"""
	checked = []
	for item in wanted:
		same = [record for record in current if record["name"].lower() == item["name"].lower()]
		if item["kind"] == "mx":
			there = [record for record in same if record["type"] == "MX"]
			ok = any(record["content"].rstrip(".").lower() == item["content"] for record in there)
		elif item["type"] == "CNAME":
			there = [record for record in same if record["type"] in ("CNAME", "A", "AAAA")]
			ok = any(record["type"] == "CNAME" and record["content"].rstrip(".").lower() == item["content"] for record in there)
		elif item["kind"] == "srv":
			there = [record for record in same if record["type"] == "SRV"]
			ok = any((record.get("data") or {}).get("target", "").rstrip(".").lower() == item["data"]["target"]
			         and int((record.get("data") or {}).get("port") or 0) == 443 for record in there)
		elif item["kind"] == "spf":
			there = [record for record in same if record["type"] == "TXT" and flat(record["content"]).startswith("v=spf1")]
			ok = any(spf_allows(record["content"], item, resolver) for record in there)
		elif item["kind"] == "dmarc":
			there = [record for record in same if record["type"] == "TXT" and flat(record["content"]).startswith("v=dmarc1")]
			ok = bool(there)
		else:
			there = [record for record in same if record["type"] == "TXT" and flat(record["content"]).startswith("v=dkim1")]
			ok = any(dkim_key(record["content"]) == dkim_key(item["content"]) for record in there)
		checked.append({**item, "state": "ok" if ok else "differs" if there else "missing",
		                "current": [{"id": record["id"], "type": record["type"], "content": record["content"]} for record in there]})
	return checked


def record_body(item, current=None):
	"""What Cloudflare is sent to make (or, with the record there, change) a wanted record: DNS only, TTL auto. An SPF
	record there gets mx added and keeps its other senders"""
	if item["kind"] == "srv":
		return {"type": "SRV", "name": item["name"], "ttl": 1, "data": item["data"]}
	content = item["content"]
	if item["kind"] == "spf" and current:
		# One there keeps its other senders: what lets this server send is put in after v=spf1
		words = current["content"].replace('" "', "").strip('"').split()
		content = " ".join([words[0], item["allow"], *words[1:]])
	body = {"type": item["type"], "name": item["name"], "content": f'"{content}"' if item["type"] == "TXT" else content, "ttl": 1}
	if item["type"] == "MX":
		body["priority"] = item["priority"]
	if item["type"] == "CNAME":
		body["proxied"] = False
	return body


# ---- Rspamd, mailcow's spam filter: what it has scanned and done, and the latest messages it scanned
RSPAMD_ACTIONS = ("no action", "greylist", "add header", "rewrite subject", "soft reject", "reject")


def rspamd(settings, count=100, opener=None):
	"""{version, uptime, scanned, spam, ham, learned, actions: {action: n}, history: [message]}, newest first"""
	stats = call(settings, "GET", "get/logs/rspamd-stats", opener=opener)
	stats = stats if isinstance(stats, dict) else {}
	actions = stats.get("actions") if isinstance(stats.get("actions"), dict) else {}
	history = []
	for item in listed(call(settings, "GET", f"get/logs/rspamd-history/{int(count)}", opener=opener)):
		if not isinstance(item, dict):
			continue
		symbols = item.get("symbols") if isinstance(item.get("symbols"), dict) else {}
		# What weighed most, either way: the symbols with the largest scores
		weighed = sorted(({"name": name, "score": round(float((value or {}).get("score") or 0), 2)} for name, value in symbols.items()),
		                 key=lambda symbol: -abs(symbol["score"]))
		rcpt = item.get("rcpt_mime") or item.get("rcpt_smtp") or []
		history.append({"time": int(item.get("unix_time") or 0), "action": str(item.get("action") or ""),
		                "score": round(float(item.get("score") or 0), 2), "required": round(float(item.get("required_score") or 0), 2),
		                "from": str(item.get("sender_mime") or item.get("sender_smtp") or ""), "to": rcpt if isinstance(rcpt, list) else [str(rcpt)],
		                "subject": str(item.get("subject") or ""), "ip": str(item.get("ip") or ""), "user": str(item.get("user") or ""),
		                "size": int(item.get("size") or 0), "symbols": [symbol for symbol in weighed if symbol["score"]][:8]})
	return {"version": stats.get("version", ""), "uptime": int(stats.get("uptime") or 0), "scanned": int(stats.get("scanned") or 0),
	        "spam": int(stats.get("spam_count") or 0), "ham": int(stats.get("ham_count") or 0), "learned": int(stats.get("learned") or 0),
	        "actions": {action: int(actions.get(action) or 0) for action in RSPAMD_ACTIONS}, "history": history}


def public_records(wanted, run=None):
	"""What public DNS says now of the names wanted (dig, through this server's resolver), as Cloudflare's records are
	given: a domain whose DNS is kept elsewhere is checked the same way. None where dig cannot be run"""
	import subprocess

	def ask(name, kind):
		try:
			result = (run or subprocess.run)(["dig", "+short", "+time=3", "+tries=1", kind, name], capture_output=True, text=True, timeout=10)
		except (OSError, subprocess.SubprocessError):
			return None
		return [line.strip() for line in result.stdout.splitlines() if line.strip() and not line.startswith(";")]
	found = []
	asked = set()
	for item in wanted:
		kinds = ("CNAME", "A") if item["type"] == "CNAME" else (item["type"],)
		for kind in kinds:
			if (item["name"], kind) in asked:
				continue
			asked.add((item["name"], kind))
			lines = ask(item["name"], kind)
			if lines is None:
				return None
			for line in lines:
				# dig follows a CNAME when asked for A: only an address is the A record's own
				if kind == "A" and not re.fullmatch(r"[0-9.]+", line):
					continue
				record = {"id": "", "type": kind, "name": item["name"], "content": line.rstrip(".")}
				if kind == "MX":
					record["content"] = line.split()[-1].rstrip(".")
				if kind == "SRV":
					parts = line.split()
					if len(parts) != 4:
						continue
					record["data"] = {"priority": int(parts[0]), "weight": int(parts[1]), "port": int(parts[2]), "target": parts[3].rstrip(".")}
				found.append(record)
	return found
