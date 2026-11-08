"""Manage local SQL Server startup ordering for .NET site services."""
import argparse
from pathlib import Path
import shlex
import subprocess

from database_info import database_details
from domain_names import service_name


DROP_IN = "vns-local-sql.conf"
ORDERING = "[Unit]\nWants=mssql-server.service\nAfter=network-online.target mssql-server.service\n"


def configure(service, root, variables):
    text = service.read_text(encoding="utf-8")
    if not any(line.startswith("ExecStart=") and "/dotnet " in line for line in text.splitlines()):
        raise ValueError("SQL startup ordering is only supported for .NET services")
    environment = variables.get("DOTNET_ENVIRONMENT") or variables.get("ASPNETCORE_ENVIRONMENT", "")
    details = database_details(root, environment, variables)
    local_sql = details["database"] == "MS SQL" and details["database_server"] == "."
    path = service.parent / (service.name + ".d") / DROP_IN
    if local_sql:
        if path.exists() and path.read_text(encoding="utf-8") == ORDERING:
            print("Local SQL startup ordering is already configured.")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ORDERING, encoding="utf-8")
        print("Configured startup after local SQL Server (ordering only, not database readiness).")
        return True
    if path.exists():
        path.unlink()
        print("Removed managed local SQL startup ordering: the active datasource is not MS SQL (.).")
        return True
    print("No change: the active datasource is not MS SQL (.).")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit")
    args = parser.parse_args()
    unit = service_name(args.unit) + ".service"
    service = Path("/etc/systemd/system") / unit
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    root = subprocess.run(["systemctl", "show", unit, "--property=WorkingDirectory", "--value"],
                          check=True, capture_output=True, text=True).stdout.strip()
    if not root or not Path(root).is_dir():
        raise ValueError("The service WorkingDirectory is missing or invalid")
    settings = subprocess.run(["systemctl", "show", unit, "--property=Environment", "--value"],
                              check=True, capture_output=True, text=True).stdout
    variables = dict(item.split("=", 1) for item in shlex.split(settings) if "=" in item)
    if configure(service, root, variables):
        subprocess.run(["systemctl", "daemon-reload"], check=True)


if __name__ == "__main__":
    main()
