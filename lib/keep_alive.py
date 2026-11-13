"""Send periodic HTTP requests to active dynamic sites on this server, and to the sites' environments on other servers."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_domains_spec = importlib.util.spec_from_file_location(
    "vns_keep_alive_domains", Path(__file__).with_name("domain_names.py"))
_domains = importlib.util.module_from_spec(_domains_spec)
_domains_spec.loader.exec_module(_domains)
service_name = _domains.service_name


CONFIG = Path("/etc/vns/keep-alive.json")
STATE = Path("/var/lib/vns/keep-alive")
DEFAULTS = {"enabled": True, "interval_minutes": 5}


def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise ValueError("Expected enabled and interval_minutes settings")
    if not isinstance(data["enabled"], bool):
        raise ValueError("Enabled must be a boolean")
    interval = data["interval_minutes"]
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
        raise ValueError("Interval must be a whole number from 1 to 1440 minutes")
    return data


def load(path=CONFIG):
    return validate(json.loads(path.read_text(encoding="utf-8")))


def save(data, path=CONFIG):
    validate(data)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".keep-alive-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def warm_sites(web_root=Path("/var/www"), services=Path("/etc/systemd/system"),
               configs=Path("/etc/apache2/sites-available")):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    failures = []
    for folder in sorted(web_root.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            unit = service_name(folder.name) + ".service"
            service = services / unit
            if not service.is_file():
                continue
            text = service.read_text(encoding="utf-8")
            command = re.search(r"(?m)^ExecStart=(.+)$", text)
            if not command or not re.search(r"(?:/dotnet\s|/gunicorn\s|/python3?\s)", command[1]):
                continue
            state = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                                   text=True, timeout=10)
            if state.returncode in (3, 4):
                continue
            if state.returncode != 0:
                raise RuntimeError("Could not read service state")
            ports = set(re.findall(r"(?:localhost|127\.0\.0\.1):(\d+)", command[1]))
            if len(ports) != 1 or not 1 <= int(next(iter(ports))) <= 65535:
                raise ValueError("Active webservice has no unambiguous loopback HTTP port")
            config = (configs / (folder.name + ".conf")).read_text(encoding="utf-8")
            binding = re.search(r"(?m)^\s*ServerName\s+(\S+)", config)
            if not binding:
                raise ValueError("Apache configuration has no binding")
            request = urllib.request.Request(
                "http://127.0.0.1:" + next(iter(ports)) + "/",
                headers={"Host": binding[1], "X-Forwarded-Proto": "https" if ":443>" in config else "http",
                         "User-Agent": "VNS-KeepAlive", "Cache-Control": "no-cache"})
            try:
                with opener.open(request, timeout=15) as response:
                    response.read(1)
            except urllib.error.HTTPError as exc:
                # A redirect or login challenge still proves the application received the request.
                if exc.code not in (301, 302, 303, 307, 308, 401, 403):
                    raise RuntimeError(f"HTTP {exc.code}") from None
                exc.close()
        except (OSError, ValueError, RuntimeError, urllib.error.URLError, subprocess.SubprocessError) as exc:
            print(f"Keep-alive failed for {folder.name}: {exc}", file=sys.stderr)
            failures.append(folder.name)
    return not failures


def environment_urls(web_root=Path("/var/www")):
    """The public address of every environment on another server, from the sites' saved settings."""
    urls = []
    for site in sorted(web_root.iterdir()) if web_root.is_dir() else []:
        if not site.is_dir() or site.is_symlink():
            continue
        for script in [site / "scripts/publish_prod.sh", *sorted(site.glob("scripts/targets/*/publish_prod.sh"))]:
            try:
                text = script.read_text(encoding="utf-8")
            except OSError:
                continue
            settings = {key: value.strip("'\"") for key, value in re.findall(r"(?m)^(PROD_[A-Z_]+)=(\S*)", text)}
            binding = settings.get("PROD_BINDING", "")
            if settings.get("PROD_LOCAL_PATH") or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", binding):
                continue
            scheme = "http" if settings.get("PROD_HTTPS") == "n" else "https"
            # The environment's id: the older single environment in scripts/ is prod
            target = "prod" if script.parent.name == "scripts" else script.parent.name
            urls.append((f"{site.name}/{target}", f"{scheme}://{binding}/"))
        # A migrated site (phase 6) has its environments' settings in Harbor's copy only
        copy = Path("/var/lib/harbor/sites") / site.name
        if (copy / "migrated.json").is_file():
            for path in sorted((copy / "environments").glob("*.json")):
                try:
                    found = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                binding = str(found.get("BINDING", ""))
                if found.get("LOCAL_PATH") or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", binding):
                    continue
                urls.append((f"{site.name}/{path.stem}", f"{'http' if found.get('HTTPS') == 'n' else 'https'}://{binding}/"))
    return urls


def warm_environments(web_root=Path("/var/www"), opener=None, results=None):
    """Request the public address of environments on other servers, Linux and Windows alike.
    How each one answered is saved in results, as site/id: {state, checked, error}, so the admin can show
    whether an environment without SSH, on IIS, is running."""
    opener = opener or urllib.request.build_opener(NoRedirect())
    failures = []
    answers = {}
    for name, url in environment_urls(web_root):
        request = urllib.request.Request(url, headers={"User-Agent": "VNS-KeepAlive", "Cache-Control": "no-cache"})
        error = ""
        try:
            with opener.open(request, timeout=30) as response:
                response.read(1)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308, 401, 403):
                error = f"HTTP {exc.code}"
            exc.close()
        except (OSError, urllib.error.URLError, ValueError) as exc:
            error = str(getattr(exc, "reason", exc))
        if error:
            print(f"Keep-alive failed for {name} ({url}): {error}", file=sys.stderr)
            failures.append(name)
        answers[name] = {"state": "failed" if error else "active", "checked": int(time.time()), "error": error}
    if results is not None:
        try:
            results.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=results.parent, prefix=".environments-")
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(answers, stream, indent=2)
            os.chmod(temporary, 0o644)
            os.replace(temporary, results)
        except OSError as exc:
            print(f"Keep-alive could not save the environment states: {exc}", file=sys.stderr)
    return not failures


def environment_states(state=STATE, max_age=1800):
    """What keep-alive last saw of each environment on another server, while it is recent"""
    try:
        data = json.loads((state / "environments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {name: value for name, value in data.items() if isinstance(value, dict)
            and isinstance(value.get("checked"), int) and 0 <= now - value["checked"] <= max_age}


def run(config=CONFIG, state=STATE):
    settings = load(config)
    if not settings["enabled"]:
        return True
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        previous = state / "last-run"
        now = time.time()
        last = float(previous.read_text()) if previous.exists() else 0
        if 0 <= now - last < settings["interval_minutes"] * 60:
            return True
        previous.write_text(str(now), encoding="utf-8")
        local = warm_sites()
        remote = warm_environments(results=state / "environments.json")
        return local and remote


def install(config=CONFIG, cron=Path("/etc/cron.d/vns-keep-alive")):
    script = Path(__file__).resolve()
    # From a Harbor release it is named through /opt/harbor/current, so the job runs the release in use
    release = re.fullmatch(r"/opt/harbor/releases/[^/]+/(.+)", str(script))
    if release:
        script = Path("/opt/harbor/current") / release[1]
    if re.search(r"\s|%", str(script)):
        raise ValueError("Cron installation requires a repository path without spaces or percent signs")
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        save(DEFAULTS, config)
    else:
        load(config)
    cron.write_text(
        "SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f'* * * * * root /usr/bin/python3 "{script}" 2>&1 | /usr/bin/logger -t vns-keep-alive\n',
        encoding="utf-8")
    cron.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    try:
        if args.install:
            install()
            return 0
        return 0 if run() else 1
    except (OSError, ValueError) as exc:
        print(f"Keep-alive failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
