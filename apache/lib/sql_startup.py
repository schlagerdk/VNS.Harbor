"""Manage local SQL Server startup ordering for .NET site services."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from database_info import database_details, local_sql_server
from domain_names import service_name
from sql_ready import sqlcmd_path


DROP_IN = "vns-local-sql.conf"
READINESS_VERSION = 2
ORDERING = "[Unit]\nWants=mssql-server.service\nAfter=network-online.target mssql-server.service\n"


def configure(service, root, variables):
    text = service.read_text(encoding="utf-8")
    if not any(line.startswith("ExecStart=") and "/dotnet " in line for line in text.splitlines()):
        raise ValueError("SQL startup ordering is only supported for .NET services")
    environment = variables.get("DOTNET_ENVIRONMENT") or variables.get("ASPNETCORE_ENVIRONMENT", "")
    details = database_details(root, environment, variables)
    local_sql = details["database"] == "MS SQL" and local_sql_server(details["database_server"])
    path = service.parent / (service.name + ".d") / DROP_IN
    if path.exists() and path.read_text(encoding="utf-8") != ORDERING:
        raise ValueError("The legacy SQL drop-in has unexpected content; inspect it before migration")
    changed = False
    if local_sql:
        sqlcmd_path()
        section = re.search(r"(?m)^\[Unit\]\s*$", text)
        if section is None:
            raise ValueError("The service has no [Unit] section")
        start = section.end()
        next_section = re.search(r"(?m)^\[", text[start:])
        end = start + next_section.start() if next_section else len(text)
        block = text[start:end]
        for directive, required in (("Wants", ["mssql-server.service"]),
                                    ("After", ["network-online.target", "mssql-server.service"])):
            # Empty assignments reset earlier dependencies.
            active = []
            for match in re.finditer(r"(?m)^" + directive + r"=(.*)$", block):
                values = match[1].split()
                if not values:
                    active = []
                else:
                    active.extend(values)
            missing = [value for value in required if value not in active]
            if missing:
                block = block.rstrip("\n") + "\n" + directive + "=" + " ".join(missing) + "\n\n"
        updated = text[:start] + block + text[end:]
        service_section = re.search(r"(?m)^\[Service\]\s*$", updated)
        if service_section is None:
            raise ValueError("The service has no [Service] section")
        helper = Path(__file__).with_name("sql_ready.py").resolve()
        pre_start = f'ExecStartPre=/usr/bin/python3 "{helper}" "{Path(root)}"'
        if not any(line.startswith("ExecStartPre=") and "sql_ready.py" in line
                   for line in updated.splitlines()):
            if re.search(r"(?m)^RootDirectory=", updated) and re.search(
                    r"(?m)^Exec(?:StartPre|StartPost|Reload|ReloadPost|Stop|StopPost)=", updated):
                raise ValueError("Custom service hooks require manual review before enabling host-side SQL readiness.")
            additions = "\n" + pre_start + "\n"
            # Only the readiness pre-command runs outside chroot; the web process keeps its sandbox.
            if re.search(r"(?m)^RootDirectory=", updated):
                if re.search(r"(?m)^RootDirectoryStartOnly=", updated):
                    updated = re.sub(r"(?m)^RootDirectoryStartOnly=.*$", "RootDirectoryStartOnly=true", updated)
                    service_section = re.search(r"(?m)^\[Service\]\s*$", updated)
                else:
                    additions += "RootDirectoryStartOnly=true\n"
            additions += "TimeoutStartSec=150\nRestart=always\nRestartSec=10\n"
            # Append after existing Service directives so these values are effective.
            start = service_section.end()
            following = re.search(r"(?m)^\[", updated[start:])
            end = start + following.start() if following else len(updated)
            updated = updated[:end].rstrip("\n") + additions + "\n" + updated[end:]
        if updated != text:
            service.write_text(updated, encoding="utf-8")
            changed = True
    if path.exists():
        path.unlink()
        if not any(path.parent.iterdir()):
            path.parent.rmdir()
        changed = True
    print("Updated SQL startup ordering." if changed else "SQL startup ordering needs no change.")
    return changed


def service_configuration(unit):
    root = subprocess.run(["systemctl", "show", unit, "--property=WorkingDirectory", "--value"],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if not root or not Path(root).is_dir():
        raise ValueError("The service WorkingDirectory is missing or invalid")
    settings = subprocess.run(["systemctl", "show", unit, "--property=Environment", "--value"],
                              check=True, capture_output=True, text=True, timeout=30).stdout
    return root, dict(item.split("=", 1) for item in shlex.split(settings) if "=" in item)


def scan(web_root, services, state_path, excluded=""):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with (state_path.parent / "sql-startup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}

        def save():
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, state_path)

        failures = []
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
        for folder in sorted(web_root.iterdir()):
            if not folder.is_dir() or folder.is_symlink() or folder.name == excluded:
                continue
            try:
                unit = service_name(folder.name) + ".service"
                service = services / unit
                if not service.is_file() or "/dotnet " not in service.read_text():
                    continue
                digest = hashlib.sha256(service.read_bytes()).hexdigest()
                previous = state.get(unit, {})
                if previous.get("hash") == digest and previous.get("version") == READINESS_VERSION and not previous.get("pending"):
                    continue
                root, variables = service_configuration(unit)
                environment = variables.get("DOTNET_ENVIRONMENT") or variables.get("ASPNETCORE_ENVIRONMENT", "")
                details = database_details(root, environment, variables)
                eligible = details["database"] == "MS SQL" and local_sql_server(details["database_server"])
                changed = configure(service, root, variables)
                if changed or previous.get("pending"):
                    state[unit] = {"pending": True}
                    save()
                    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
                    subprocess.run(["systemctl", "restart", unit], check=True, timeout=180)
                if eligible:
                    state[unit] = {"hash": hashlib.sha256(service.read_bytes()).hexdigest(),
                                   "pending": False, "version": READINESS_VERSION}
                else:
                    state.pop(unit, None)
                save()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"SQL startup check failed for {folder.name}: {exc}", file=sys.stderr)
                failures.append(folder.name)
        if failures:
            raise RuntimeError("SQL startup checks failed for: " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit", nargs="?")
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--exclude", default="")
    args = parser.parse_args()
    if args.scan:
        scan(args.scan, Path("/etc/systemd/system"),
             Path("/var/lib/vns/sql-startup-checked.json"), args.exclude)
        return
    if not args.unit:
        parser.error("Specify a unit or --scan")
    unit = service_name(args.unit) + ".service"
    service = Path("/etc/systemd/system") / unit
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    root, variables = service_configuration(unit)
    if configure(service, root, variables):
        subprocess.run(["systemctl", "daemon-reload"], check=True)


if __name__ == "__main__":
    main()
