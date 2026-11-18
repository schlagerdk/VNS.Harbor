"""Harbor's image signing key (Umbraco:CMS:Imaging:HMACSecretKey) taken off the services it was put on (1.0.46, in
the drop-in vns-security.conf). With it, Umbraco answers 400 to an image URL made by hand, and sites' images broke.

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
    """Harbor's image signing key taken off a service again; whether it changed. With a key, Umbraco answers 400 to an
    image URL a site makes by hand (media.Url() + "?width=400"), which many sites do: their images broke
    (test.poda.dk). A site that wants the key sets it itself, and makes its image URLs with GetCropUrl."""
    if not UNIT.fullmatch(unit):
        raise ValueError(f"Not a service: {unit}")
    path = SERVICES / f"{unit}.service.d" / DROP_IN
    if not path.is_file():
        return False
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    print(f"{unit}: Harbor's image signing key taken off. It counts from its next start.")
    return True


def reload():
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("unit", nargs="?", default="")
    parser.add_argument("--all", action="store_true", help="every .NET site's service here")
    parser.add_argument("--no-reload", action="store_true", help="no systemctl daemon-reload (the caller does it)")
    args = parser.parse_args(argv)
    units = [path.parent.name[:-len(".service.d")] for path in sorted(SERVICES.glob(f"*.service.d/{DROP_IN}"))] if args.all else [args.unit]
    changed = False
    for unit in units:
        try:
            changed = harden(unit) or changed
        except (OSError, ValueError) as error:
            print(f"{unit}: {error}", file=sys.stderr)
    if changed and not args.no_reload:
        reload()
    if args.all and not changed:
        print("No service here has Harbor's image signing key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
