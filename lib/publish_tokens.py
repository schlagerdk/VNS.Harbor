"""Publish tokens: a Harbor user's own key for publishing a site from their machine without SSH
(POST /api/publish/<site> with the token as Bearer). One token a user: making a new one ends the one before.

Only a hash of each token is kept, readable by root alone; the token itself is shown once, when it is made.

  /etc/harbor/secrets/publish-tokens.json   {user id: {hash, made, used}}
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile

PATH = Path("/etc/harbor/secrets/publish-tokens.json")
PREFIX = "hpt_"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def load(path=PATH):
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def save(data, path=PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-tokens-")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def make(user_id, path=PATH):
    """A new token for the user, in place of the one they had: returned once, only its hash kept"""
    token = PREFIX + secrets.token_hex(32)
    data = load(path)
    data[user_id] = {"hash": digest(token), "made": now(), "used": ""}
    save(data, path)
    return token


def info(user_id, path=PATH):
    entry = load(path).get(user_id)
    if not isinstance(entry, dict) or not entry.get("hash"):
        return {"exists": False}
    return {"exists": True, "made": entry.get("made", ""), "used": entry.get("used", "")}


def remove(user_id, path=PATH):
    data = load(path)
    if data.pop(user_id, None) is not None:
        save(data, path)


def user_for(header, path=PATH):
    """The id of the user whose token an Authorization header carries (Bearer hpt_…), or None"""
    if not isinstance(header, str) or not header.startswith("Bearer ") or len(header) > 200:
        return None
    token = header[len("Bearer "):].strip()
    if not token.startswith(PREFIX):
        return None
    wanted = digest(token)
    data = load(path)
    for user_id, entry in data.items():
        if isinstance(entry, dict) and hmac.compare_digest(str(entry.get("hash", "")), wanted):
            entry["used"] = now()
            try:
                save(data, path)
            except OSError:
                pass
            return user_id
    return None
