"""Forgotten passwords (the sign-in page's Forgot password): a link mailed to the user, good for one use within
TRY_MINUTES. Only a hash of each link's secret is kept, so the file cannot be used to sign in; a newer link for the
same user replaces the one before, and a used or expired link is gone."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time

TRY_MINUTES = 30
# A link mailed with a user's login, to choose their own password: they may not read it at once
WELCOME_MINUTES = 7 * 24 * 60
LOCK = threading.Lock()


def _key(token):
	return hashlib.sha256(str(token).encode()).hexdigest()


def _load(path, now):
	try:
		found = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	if not isinstance(found, dict):
		return {}
	return {key: item for key, item in found.items() if isinstance(item, dict) and item.get("expires", 0) > now}


def _save(path, links):
	path = Path(path)
	temporary = path.with_name(f".{path.name}.{os.getpid()}")
	descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
		json.dump(links, stream)
	os.replace(temporary, path)


def make(path, user_id, now=None, minutes=TRY_MINUTES):
	"""A new link's secret for the user, good for minutes: the one before it no longer works"""
	now = now or time.time()
	token = secrets.token_urlsafe(32)
	with LOCK:
		links = {key: item for key, item in _load(path, now).items() if item.get("user") != user_id}
		links[_key(token)] = {"user": user_id, "expires": now + minutes * 60}
		_save(path, links)
	return token


def peek(path, token, now=None):
	"""The user a link is for while it works, without using it; None when it does not"""
	if not isinstance(token, str) or not 20 <= len(token) <= 100:
		return None
	return (_load(path, now or time.time()).get(_key(token)) or {}).get("user")


def use(path, token, now=None):
	"""The user a link is for, and the link gone: it works once. None when it does not work"""
	if not isinstance(token, str) or not 20 <= len(token) <= 100:
		return None
	now = now or time.time()
	with LOCK:
		links = _load(path, now)
		found = links.pop(_key(token), None)
		_save(path, links)
	return (found or {}).get("user")
