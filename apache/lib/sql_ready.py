"""Wait for the site's local SQL databases before starting its web process."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

from database_info import configuration_values, connection_fields, connection_type, local_sql_server


_spec = importlib.util.spec_from_file_location(
    "vns_sql_ready_admin", Path(__file__).resolve().parents[2] / "sql/lib/admin.py")
_sql_admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sql_admin)


def sqlcmd_path():
    path = _sql_admin.sqlcmd_path()
    if path:
        return path
    raise ValueError("SQL tools are required for database readiness. Install SQL tools on this server.")


def probes(root, environment, variables, executable):
    values = configuration_values(root, environment, variables)
    commands = []
    for key, connection in values.items():
        if not key.startswith("connectionstrings__") or key.endswith("_providername"):
            continue
        if not isinstance(connection, str) or not connection.strip():
            continue
        provider = str(values.get(key + "_providername", "")).lower()
        if connection_type(connection, provider) != "MS SQL":
            continue
        fields = connection_fields(connection)
        server = next((fields[name] for name in (
            "server", "data source", "datasource", "address", "addr", "network address")
            if fields.get(name)), "")
        if not local_sql_server(server):
            continue
        database = fields.get("initial catalog") or fields.get("database")
        if not database:
            raise ValueError("The local SQL connection must specify a database for readiness checks.")
        # sqlcmd does not accept '.' as a Linux loopback server name.
        if server == "." or server.startswith(".,"):
            server = "127.0.0.1" + server[1:]
        elif server.lower() == "tcp:." or server.lower().startswith("tcp:.,"):
            server = "tcp:127.0.0.1" + server[5:]
        command = [executable, "-S", server, "-d", database, "-b", "-l", "5", "-t", "5",
                   "-h", "-1", "-W", "-Q", "SET NOCOUNT ON; SELECT 1"]
        env = os.environ.copy()
        username = fields.get("user id") or fields.get("uid") or fields.get("user")
        password = fields.get("password") or fields.get("pwd")
        if username and password:
            command.extend(["-U", username])
            env["SQLCMDPASSWORD"] = password
        elif (fields.get("integrated security") or fields.get("trusted_connection", "")).lower() in (
                "true", "yes", "sspi"):
            command.append("-E")
            env.pop("SQLCMDPASSWORD", None)
        else:
            raise ValueError("The local SQL connection has no supported authentication configuration.")
        if (fields.get("trustservercertificate") or fields.get("trust server certificate", "")).lower() in (
                "true", "yes"):
            command.append("-C")
        commands.append((command, env))
    if not commands:
        raise ValueError("No local MS SQL database connection was found for readiness checks.")
    return commands


def wait_ready(commands, timeout=120):
    deadline = time.monotonic() + timeout
    print("Waiting for the site's local SQL databases (up to 120 seconds)...", flush=True)
    while time.monotonic() < deadline:
        ready = True
        for command, env in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ready = False
                break
            try:
                result = subprocess.run(command, env=env, capture_output=True, text=True,
                                        timeout=min(12, remaining))
                if result.returncode != 0 or result.stdout.strip() != "1":
                    ready = False
                    break
            except subprocess.TimeoutExpired:
                ready = False
                break
        if ready:
            print("Local SQL databases are ready.", flush=True)
            return
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise TimeoutError("Local SQL database readiness timed out. The web process was not started; systemd will retry.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    environment = os.environ.get("DOTNET_ENVIRONMENT") or os.environ.get("ASPNETCORE_ENVIRONMENT", "")
    try:
        wait_ready(probes(args.root, environment, os.environ, sqlcmd_path()))
    except ValueError as exc:
        # Do not emit connection strings, sqlcmd output, or credential-bearing argv.
        print(f"SQL readiness failed: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("SQL readiness timed out after 120 seconds; systemd will retry.", file=sys.stderr)
        return 1
    except OSError:
        print("SQL readiness failed: could not read configuration or execute SQL tools.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
