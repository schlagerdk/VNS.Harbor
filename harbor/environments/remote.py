"""What the admin does with an environment on another Linux server: its status, start, stop, logs, editing its
service or Apache config, a shell, its files and its config. From remote_site.sh.

What runs on the server is sent word for word as the script sent it. The values sent with it are quoted for any POSIX
shell (the script used bash's printf %q, which the server's bash reads the same).
"""
from pathlib import Path
import re
import shlex

from harbor.core.names import normalize_site_name, service_name
from harbor.core.system import Stop
from harbor.environments import settings
from harbor.core.paths import LIB
from harbor.sites.actions import site_named

HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")

STATUS = r'''set -euo pipefail
python3 - <<'PY'
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
site = os.environ["SITE"]
unit = os.environ["UNIT"]
service = Path("/etc/systemd/system", unit + ".service")
conf = Path("/etc/apache2/sites-available", site + ".conf")
text = conf.read_text() if conf.is_file() else ""
service_text = service.read_text() if service.is_file() else ""
state = subprocess.run(["systemctl", "is-active", unit + ".service"], capture_output=True, text=True)
if state.returncode not in (0, 3, 4):
    raise RuntimeError(state.stderr or "Could not read remote service status")
secure = bool(re.search(r"<VirtualHost\s+[^>]*:443>", text))
certificate = re.search(r'^\s*SSLCertificateFile\s+"?([^"\s]+)', text, re.M)
environment = ""
variables = {}
if service.is_file():
    settings = subprocess.run(["systemctl", "show", unit + ".service", "--property=Environment", "--value"],
                              capture_output=True, text=True, check=True)
    variables = dict(item.split("=", 1) for item in shlex.split(settings.stdout) if "=" in item)
    environment = variables.get("DOTNET_ENVIRONMENT") or variables.get("ASPNETCORE_ENVIRONMENT", "")
port = re.search(r"(?:localhost|127\.0\.0\.1):(\d+)", service_text)
binding = re.search(r"^\s*ServerName\s+(\S+)", text, re.M)
aliases = []
seen = {binding.group(1).lower()} if binding else set()
for line in text.splitlines():
    fields = line.split("#", 1)[0].split()
    if len(fields) > 1 and fields[0].lower() == "serveralias":
        for alias in fields[1:]:
            if alias.lower() not in seen:
                aliases.append(alias)
                seen.add(alias.lower())
exec(os.environ.pop("DATABASE_CODE"))
database = {"database": "Unknown", "database_name": ""}
# The .NET the site runs, from the folder and dll its service starts
working = re.search(r"^WorkingDirectory=(.+)$", service_text, re.M)
started = re.search(r"([^\s/]+)\.dll\b", service_text)
try:
    dotnet = dotnet_runtime(working.group(1).strip().strip('"') if working else Path("/var/www", site), started.group(1) if started else "")
except OSError:
    dotnet = ""
try:
    database = database_details(Path("/var/www", site), environment, variables)
except (OSError, ValueError):
    print("Could not read remote database configuration", file=__import__("sys").stderr)
try:
    note = Path("/var/lib/vns-managed", site)
    managed = os.environ.get("MAIN_SITE", "") + "\n" + os.environ.get("MAIN_HOST", "") + "\n"
    if os.environ.get("MAIN_SITE") and Path("/var/www", site).is_dir() and (not note.is_file() or note.read_text() != managed):
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(managed)
except OSError as exc:
    print("Could not write the management note: %s" % exc, file=__import__("sys").stderr)
print(json.dumps({
    "unit": site if service.is_file() else "",
    "state": state.stdout.strip() if service.is_file() else "",
    "exists": Path("/var/www", site).is_dir(),
    "scheme": "https" if secure else "http",
    "https": secure and bool(certificate and Path(certificate.group(1)).is_file()),
    "environment": environment,
    "dotnet": dotnet,
    **database,
    "binding": binding.group(1) if binding else "",
    "aliases": aliases,
    "port": port.group(1) if port else "",
}))
PY
'''

SERVICE = r'''set -euo pipefail
systemctl "${ACTION}" "${UNIT}.service"
'''

LOGS = r'''set -euo pipefail
if [[ ${ACTION} == logs ]]; then
	journalctl -u "${UNIT}.service" -n 300 --no-pager -o short-iso
else
	rc=0
	TERM=xterm-256color SYSTEMD_COLORS=1 systemctl status "${UNIT}.service" --no-pager -l || rc=$?
	[[ ${rc} -eq 0 || ${rc} -eq 3 ]]
fi
'''

INTERACTIVE = {
    'edit_service': 'set -e; nano "/etc/systemd/system/$UNIT.service"; read -r -p "Reload daemon and restart service? [Y/n]: " answer; if [[ ! $answer =~ ^[nN] ]]; then systemctl daemon-reload; systemctl restart "$UNIT.service"; systemctl status "$UNIT.service" --no-pager; fi',
    'edit_conf': 'set -e; nano "/etc/apache2/sites-available/$SITE.conf"; read -r -p "Test config and reload Apache? [Y/n]: " answer; if [[ ! $answer =~ ^[nN] ]]; then apache2ctl configtest; systemctl reload apache2; fi',
    'shell': 'cd "/var/www/$SITE" && export force_color_prompt=yes && exec bash -il',
}


def remote_environment(system, name, target="prod"):
    site = site_named(system, name)
    environment = settings.load(system, site.site_root, target)
    if environment.LOCAL_PATH or environment.windows:
        raise Stop("This is not a remote production target.")
    try:
        normalize_site_name(environment.FOLDER)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    if not HOST.fullmatch(environment.IP):
        raise Stop("Invalid remote host.")
    if not USER.fullmatch(environment.USER):
        raise Stop("Invalid SSH username.")
    if not environment.SSH_PORT.isdigit() or not 1 <= int(environment.SSH_PORT) <= 65535:
        raise Stop("Invalid SSH port.")
    return site, environment


def assignments(**values):
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def remote_action(system, name, action, target="prod"):
    site, environment = remote_environment(system, name, target)
    values = {"SITE": environment.FOLDER, "UNIT": service_name(environment.FOLDER)}
    if action == "status":
        here = system.output("hostname", "-f", check=False) or system.output("hostname", check=False)
        # Tells the admin on the server that this site is managed from here, so it cannot be copied there
        variables = assignments(**values, DATABASE_CODE=(LIB / "database_info.py").read_text().rstrip("\n"), MAIN_SITE=site.name, MAIN_HOST=here)
        result = settings.remote_run(system, environment, variables, STATUS, check=False)
    elif action in ("start", "stop", "restart"):
        result = settings.remote_run(system, environment, assignments(**values, ACTION=action), SERVICE, check=False)
    elif action in ("logs", "check_service"):
        result = settings.remote_run(system, environment, assignments(**values, ACTION=action), LOGS, check=False)
    elif action in INTERACTIVE:
        command = f"env TERM=xterm-256color COLORTERM=truecolor {assignments(**values)} bash -c {shlex.quote(INTERACTIVE[action])}"
        result = settings.remote_exec(system, environment, command, tty=True, check=False)
    elif action in ("files", "config"):
        from harbor.core.names import standalone
        code = standalone((LIB / ("managed_config.py" if action == "config" else "remote_files.py")).read_text()).rstrip("\n")
        result = settings.remote_exec(system, environment, f"env {assignments(**values)} python3 -c {shlex.quote(code)}", check=False)
    else:
        raise Stop(f"Unknown remote action: {action}")
    if not result.ok:
        raise Stop("", result.returncode)
