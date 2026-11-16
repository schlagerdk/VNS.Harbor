"""Actions on an environment, here or on another server: a new certificate, another environment, and the files of
one on Windows. From prod_action.sh, windows_file.sh and windows_files.sh.

What runs on another Linux server is the script prod_action.sh sent there, word for word: that server has the VNS
version it has, and harbor reaches it the same way until it has harbor too (phase 5).
"""
import json
from pathlib import Path
import re

from harbor.core.site_config import ACME_SERVER
from harbor.core.system import Stop
from harbor.environments import settings
from harbor.environments.windows import Share
from harbor.sites.actions import choose_environment, site_named
from harbor.core.paths import INSTALL, LIB

WINDOWS_FILES = LIB / "windows_files.py"
EDITABLE = re.compile(r"web\.config|appsettings(\.[A-Za-z0-9_-]+)?\.json")

ISSUE_CERTIFICATE = r'''set -euo pipefail
acme=/etc/acmesh/acme.sh
certdir=/etc/ssl/certs/${BINDING}_ecc
source /opt/harbor/current/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
unit=$(normalize_dns_name "${SITE}" unit)
conf=/etc/apache2/sites-available/${SITE}.conf
[[ -x ${acme} ]] || { echo "acme.sh not found (${acme}). Run harbor server init there first." >&2; exit 1; }
[[ -f ${conf} ]] || { echo "Apache config for ${SITE} not found. Publish to production first." >&2; exit 1; }
domains=("${BINDING}")
while IFS= read -r alias; do
	[[ -n ${alias} ]] || continue
	alias=${alias,,}
	[[ ${alias} =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ && ${#alias} -le 253 ]] || {
		echo "Invalid ServerAlias '${alias}' in ${conf}; certificate issuance stopped." >&2
		exit 1
	}
	found=0
	for domain in "${domains[@]}"; do [[ ${domain} == "${alias}" ]] && found=1; done
	((found)) || domains+=("${alias}")
done < <(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${conf}")
domain_args=()
for domain in "${domains[@]}"; do domain_args+=(-d "${domain}"); done
printf 'Issue certificate for: %s\n' "${domains[*]}"
rc=0
"${acme}" --issue --apache --force --server "${ACME_SERVER}" "${domain_args[@]}" || rc=$?
if [[ ${rc} -ne 0 && ${rc} -ne 2 ]]; then
	echo "Certificate issue failed. Check that DNS for ${BINDING} points to this server." >&2
	exit 1
fi
mkdir -p "${certdir}"
"${acme}" --install-cert -d "${BINDING}" --key-file "${certdir}/${BINDING}.key" \
	--fullchain-file "${certdir}/fullchain.cer" --reloadcmd "systemctl reload apache2"
ensure_production_tls_config "${conf}" "/etc/systemd/system/${unit}.service" "${SITE}" \
	"${BINDING}" "${ROOT}" "${certdir}" "${MAIL}" "${UMBRACO}"
apache2ctl configtest
systemctl reload apache2
echo "Certificate installed in ${certdir}"
'''

CHANGE_ENVIRONMENT = r'''set -euo pipefail
source /opt/harbor/current/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
unit=$(normalize_dns_name "${SITE}" unit)
service=/etc/systemd/system/${unit}.service
[[ -f ${service} ]] || { echo "Service file not found: ${service}. Publish to production first." >&2; exit 1; }
echo "Current:"
grep "_ENVIRONMENT" "${service}" || true
sed -i -E "s/^(Environment=(ASPNETCORE|DOTNET)_ENVIRONMENT=).*/\1${ENVIRONMENT}/" "${service}"
echo "Now:"
grep "_ENVIRONMENT" "${service}"
systemctl daemon-reload
systemctl restart "${unit}"
systemctl status "${unit}" --no-pager
'''


def environment_of(system, name, target):
    site = site_named(system, name)
    return site, settings.load(system, site.site_root, target)


def set_setting(system, environment, name, value, quoted):
    """A PROD_ setting in the environment's script, kept as it is written there; in Harbor's copy for an environment
    whose settings Harbor keeps (no script in the site folder, as one copied or moved from Windows)"""
    path = system.path(environment.script)
    if not path.is_file():
        suffix = settings.settings_script("", environment.id)
        site_root = environment.script[:-len(suffix)] if environment.script.endswith(suffix) else ""
        if not site_root:
            raise Stop(f"Cannot keep PROD_{name}: {environment.script} is missing.")
        settings.update(system, site_root, environment.id, {name: value})
        return
    text = path.read_text()
    if quoted:
        # As the script: the quote it had stays
        text = re.sub(rf"""(?m)^(PROD_{name}=)(["']?)[^"'\s]*\2""", lambda match: f"{match[1]}{match[2]}{value}{match[2]}", text)
    else:
        text = re.sub(rf"(?m)^PROD_{name}=\S*", f"PROD_{name}={value}", text)
    path.write_text(text)


def issue_certificate(system, name, target="prod", yes=False):
    site, environment = environment_of(system, name, target)
    if not yes:
        answer = system.ask(f"Issue a new certificate for {environment.BINDING} on {environment.description}? [y/N]: ")
        if answer.lower() not in ("y", "yes"):
            system.say("Aborted")
            raise Stop("", 1)
    if environment.windows:
        raise Stop("The certificate of a Windows production site is managed in IIS.")
    umbraco = "1" if system.path(f"{Path(environment.script).parent}/sync_prod.sh").is_file() else "n"
    variables = (f"SITE='{environment.FOLDER}' BINDING='{environment.BINDING.lower()}' ACME_SERVER='{ACME_SERVER}' "
                 f"ROOT='{environment.root}' MAIL='{environment.ADMIN_MAIL or site.admin_mail}' UMBRACO='{umbraco}'")
    system.step(f"Issue a certificate for {environment.BINDING} on {environment.description}")
    if not settings.remote_run(system, environment, variables, ISSUE_CERTIFICATE, check=False).ok:
        raise Stop("", 1)
    # The site now runs on HTTPS, also if it was created with plain HTTP
    set_setting(system, environment, "HTTPS", "y", quoted=False)


def change_environment(system, name, target="prod", given=""):
    site, environment = environment_of(system, name, target)
    system.say(f"Production environment of {environment.FOLDER} ({environment.description})")
    chosen = choose_environment(system, given)
    if environment.windows:
        share = Share(system, environment).connect()
        system.step(f"Set {chosen} in web.config")
        share.set_environment(chosen, str(INSTALL))
        system.say(f"Now: {chosen} (web.config). IIS restarts the app.")
    else:
        system.step(f"Restart {environment.FOLDER} as {chosen}")
        if not settings.remote_run(system, environment, f"SITE='{environment.FOLDER}' ENVIRONMENT='{chosen}'", CHANGE_ENVIRONMENT, check=False).ok:
            raise Stop("", 1)
    # publish_prod.sh only reads PROD_ENVIRONMENT when it creates a new service: kept in step
    set_setting(system, environment, "ENVIRONMENT", chosen, quoted=True)


def windows_environment(system, name, target):
    site, environment = environment_of(system, name, target)
    if not environment.windows:
        raise Stop("This is not an environment on a Windows server.")
    return environment


def windows_file(system, name, action, filename, target="prod"):
    """Read web.config or an appsettings file of a Windows environment to stdout, or save it from stdin, as it is"""
    environment = windows_environment(system, name, target)
    if not EDITABLE.fullmatch(filename or ""):
        raise Stop("Only web.config and appsettings*.json can be edited.")
    if action not in ("read", "save"):
        raise Stop(f"Unknown action: {action}")
    try:
        share = Share(system, environment).connect()
    except RuntimeError as error:
        raise Stop(str(error)) from None
    result = share.rclone("cat" if action == "read" else "rcat", f"{share.target}/{filename}", check=False)
    if not result.ok:
        raise Stop("", result.returncode)


def windows_files(system, name, target="prod"):
    """The file manager of a Windows environment: lib/windows_files.py answers one operation from stdin in JSON"""
    try:
        environment = windows_environment(system, name, target)
        share = Share(system, environment).connect()
    except Stop as error:
        system.say(json.dumps({"error": str(error) or "Could not open the Windows share"}))
        raise Stop("", 1) from None
    except RuntimeError as error:
        system.say(json.dumps({"error": str(error) or "Could not open the Windows share"}))
        raise Stop("", 1) from None
    result = system.run("python3", str(WINDOWS_FILES), share.target, env=share.env, check=False)
    if not result.ok:
        raise Stop("", result.returncode)
