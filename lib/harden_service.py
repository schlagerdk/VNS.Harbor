"""An Umbraco 18 or newer site's service, in a drop-in of Harbor's own (vns-security.conf): a secret key of its own for
signing image URLs (Umbraco:CMS:Imaging:HMACSecretKey), kept for root only and never changed once made, as a new one
would make the signed URLs it gave out invalid. (Kestrel's Server header is not set here: Kestrel does not read it from
its settings. Apache takes it off, vns-headers.conf.)

Settings in the service, not in appsettings.json, so a publish never takes them away. The service is not restarted:
they count from its next start (a publish, Restart). Run by the update for every service, and by the site's
creation and publish for its own.

    python3 harden_service.py <unit> [--no-reload]
    python3 harden_service.py --all
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

SERVICES = Path(os.environ.get("VNS_SYSTEMD_DIR", "/etc/systemd/system"))
KEYS = Path(os.environ.get("VNS_SERVICE_SECRETS", "/etc/harbor/secrets/services"))
DROP_IN = "vns-security.conf"
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,250}")


def dll_of(text):
    """The dll a service runs with dotnet, or None"""
    match = re.search(r"(?m)^ExecStart=\S*dotnet\s+\"?([^\s\"]+?\.dll)\b", text)
    return Path(match.group(1)) if match else None


def umbraco_major(folder):
    """The major version of Umbraco in a published site (its .deps.json), or 0"""
    for path in sorted(folder.glob("*.deps.json")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'"Umbraco\.Cms\.Core/(\d+)\.', text)
        if match:
            return int(match.group(1))
    return 0


def key_file(unit):
    """The unit's image signing key, made once (64 random bytes, base64) and kept for root only"""
    path = KEYS / f"{unit}.env"
    if not path.is_file():
        KEYS.mkdir(parents=True, exist_ok=True)
        os.chmod(KEYS, 0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("Umbraco__CMS__Imaging__HMACSecretKey=" + base64.b64encode(secrets.token_bytes(64)).decode("ascii") + "\n")
    return path


def harden(unit):
    """The drop-in for an Umbraco 18+ site's service; whether it changed. Any other service: none (an earlier one goes)."""
    if not UNIT.fullmatch(unit):
        raise ValueError(f"Not a service: {unit}")
    service = SERVICES / f"{unit}.service"
    if not service.is_file():
        return False
    dll = dll_of(service.read_text(encoding="utf-8", errors="replace"))
    if not dll:
        return False
    umbraco = umbraco_major(dll.parent)
    path = SERVICES / f"{unit}.service.d" / DROP_IN
    if umbraco < 18:
        if path.is_file():
            path.unlink()
            return True
        return False
    text = f"# Set by Harbor: Umbraco's image URLs signed with a key of the site's own\n[Service]\nEnvironmentFile={key_file(unit)}\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"{unit}: Umbraco {umbraco}'s image signing key. It counts from its next start.")
    return True


def reload():
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("unit", nargs="?", default="")
    parser.add_argument("--all", action="store_true", help="every .NET site's service here")
    parser.add_argument("--no-reload", action="store_true", help="no systemctl daemon-reload (the caller does it)")
    args = parser.parse_args(argv)
    units = [path.name[:-len(".service")] for path in sorted(SERVICES.glob("*.service"))] if args.all else [args.unit]
    changed = False
    for unit in units:
        try:
            changed = harden(unit) or changed
        except (OSError, ValueError) as error:
            print(f"{unit}: {error}", file=sys.stderr)
    if changed and not args.no_reload:
        reload()
    if args.all and not changed:
        print("Every Umbraco 18+ site here has its image signing key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
