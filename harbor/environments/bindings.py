"""Change the binding and aliases of an environment on Linux, here or on another server. From change_binding_prod.sh.

What runs where the environment is, is the script change_binding_prod.sh sent there, word for word: that server has the
VNS version it has (see harbor/environments/actions.py). PROD_BINDING in the environment's settings follows.
"""
from pathlib import Path

from harbor.core.bindings import server_aliases, site_binding
from harbor.core.site_config import ACME_SERVER
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.environments.actions import environment_of, set_setting

CHANGE_BINDING = r'''set -euo pipefail
source /opt/harbor/current/apache/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
unit=$(normalize_dns_name "${SITE}" unit)
conf=/etc/apache2/sites-available/${SITE}.conf
acme=/etc/acmesh/acme.sh
oldcert=/etc/ssl/certs/${OLD}_ecc
newcert=/etc/ssl/certs/${NEW}_ecc
settings=${ROOT}/appsettings.json

[[ -f ${conf} ]] || { echo "Apache config not found: ${conf}" >&2; exit 1; }
scheme=http
if grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${conf}"; then
	scheme=https
	[[ -x ${acme} ]] || { echo "acme.sh not found (${acme}). Run apache/init.sh there first." >&2; exit 1; }
fi
grep -qi "^[[:space:]]*ServerName[[:space:]]\+${OLD}[[:space:]]*$" "${conf}" || { echo "ServerName ${OLD} not found in ${conf}" >&2; exit 1; }

used_by_other() {
	local c
	for c in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${c} && ! ${c} -ef ${conf} ]] || continue
		awk -v d="$1" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${c}" && return 0
	done
	return 1
}
if used_by_other "${NEW}"; then
	echo "Binding ${NEW} is already used by another Apache site" >&2
	exit 1
fi
for alias in ${ALIASES}; do
	if used_by_other "${alias}"; then
		echo "Alias ${alias} is already used by another Apache site" >&2
		exit 1
	fi
done

backup=$(mktemp -d)
trap 'rm -rf "${backup}"' EXIT
cp "${conf}" "${backup}/site.conf"
[[ -f ${settings} ]] && cp "${settings}" "${backup}/appsettings.json"
app_restart_attempted=n
rollback() {
	echo "Restoring the previous Apache config" >&2
	cp "${backup}/site.conf" "${conf}"
	[[ -f ${backup}/appsettings.json ]] && cp "${backup}/appsettings.json" "${settings}"
	apache2ctl configtest && systemctl reload apache2 || true
	if [[ ${app_restart_attempted} == y ]]; then
		systemctl restart "${unit}" 2>/dev/null || true
	fi
}

# 1. Serve the new name on port 80 (the certificate still points to the old one)
sed -i -E "s/^([[:space:]]*ServerName[[:space:]]+)${OLD//./\\.}[[:space:]]*$/\1${NEW}/I" "${conf}"
replace_server_aliases "${conf}" "${ALIASES}"
apache2ctl configtest || { rollback; exit 1; }
systemctl reload apache2

# 2. Issue the certificate for the new name
if [[ ${scheme} == https ]]; then
	domains=("${NEW}")
	while IFS= read -r alias; do
		[[ -n ${alias} ]] || continue
		alias=${alias,,}
		[[ ${alias} =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ && ${#alias} -le 253 ]] || {
			echo "Invalid ServerAlias '${alias}' in ${conf}; certificate issuance stopped." >&2
			rollback
			exit 1
		}
		found=0
		for domain in "${domains[@]}"; do [[ ${domain} == "${alias}" ]] && found=1; done
		((found)) || domains+=("${alias}")
	done < <(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${conf}")
	domain_args=()
	for domain in "${domains[@]}"; do domain_args+=(-d "${domain}"); done
	printf 'Issue certificate for: %s\n' "${domains[*]}"
	if [[ ${NEW} != "${OLD}" ]] && ! used_by_other "${NEW}"; then
		"${acme}" --remove -d "${NEW}" >/dev/null 2>&1 || true
		rm -rf "/etc/ssl/certs/${NEW}" "${newcert}"
	fi
	rc=0
	"${acme}" --issue --apache --force --server "${ACME_SERVER}" "${domain_args[@]}" || rc=$?
	if [[ ${rc} -ne 0 && ${rc} -ne 2 ]]; then
		echo "Certificate issue failed. Check that DNS for ${NEW} points to this server." >&2
		rollback
		exit 1
	fi
	mkdir -p "${newcert}"
	"${acme}" --install-cert -d "${NEW}" --key-file "${newcert}/${NEW}.key" \
		--fullchain-file "${newcert}/fullchain.cer" --reloadcmd "systemctl reload apache2" || { rollback; exit 1; }

	# 3. Point the config to the new certificate
	sed -i "s|${oldcert}/fullchain.cer|${newcert}/fullchain.cer|g; s|${oldcert}/${OLD}.key|${newcert}/${NEW}.key|g" "${conf}"
fi
if [[ -f ${settings} ]] && grep -q "UmbracoApplicationUrl" "${settings}"; then
	sed -i -E "s|(\"UmbracoApplicationUrl\"[[:space:]]*:[[:space:]]*\")[^\"]*|\1${scheme}://${NEW}|" "${settings}"
fi
apache2ctl configtest || { rollback; exit 1; }
systemctl reload apache2
if [[ -f ${backup}/appsettings.json ]] && ! cmp -s "${backup}/appsettings.json" "${settings}"; then
	app_restart_attempted=y
	systemctl restart "${unit}" || { rollback; exit 1; }
fi

# 4. Remove the old certificate unless another site still uses that name
if [[ ${OLD} != "${NEW}" && ${scheme} == https ]]; then
	old_binding_in_use=n
	for alias in ${ALIASES}; do [[ ${alias} == "${OLD}" ]] && old_binding_in_use=y; done
	if [[ ${old_binding_in_use} == y ]] || used_by_other "${OLD}"; then
		echo "Previous binding ${OLD} is still used; keeping its certificate."
	else
		echo "Revoke and remove certificate for ${OLD}"
		"${acme}" --revoke -d "${OLD}" || true
		"${acme}" --remove -d "${OLD}" || true
		rm -rf "/etc/ssl/certs/${OLD}" "${oldcert}"
	fi
fi
echo "Bindings updated: ${scheme}://${NEW}/ (aliases: ${ALIASES:-none})"
'''


def change_binding(system, name="", new_binding="", aliases=None, yes=False, target="prod"):
    """aliases None asks for them, as the script did without a third argument"""
    if not name:
        name = system.ask("Enter folder name: ").strip()
    site, environment = environment_of(system, name, target)
    if environment.windows:
        raise Stop(f"The binding of a Windows production site is managed in IIS. Change it there, then update PROD_BINDING in {environment.script}.")
    # An environment on HTTP may use names without a dot
    http_only = environment.HTTPS == "n"
    try:
        old_binding = site_binding(environment.BINDING, http_only)
        if not new_binding:
            new_binding = system.ask(f"Enter new production binding [{old_binding}]: ").strip() or old_binding
        new_binding = site_binding(new_binding, http_only)
    except ValueError as error:
        raise Stop(str(error)) from None
    config = f"/etc/apache2/sites-available/{environment.FOLDER}.conf"
    try:
        old_aliases = settings.remote_exec(system, environment, f"bash -c 'source /opt/harbor/current/apache/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh; server_aliases_from_config {config} {old_binding}'",
                                           capture=True).stdout.strip()
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None
    system.say(f"Current aliases: {old_aliases or 'none'}")
    if aliases is None:
        given = system.ask("New aliases, comma-separated (Enter keeps current; - removes all): ")
        aliases = old_aliases if not given else "" if given == "-" else given
    try:
        new_aliases = server_aliases(new_binding, aliases, http_only)
        compared = server_aliases(old_binding, old_aliases, http_only)
    except ValueError as error:
        raise Stop(str(error)) from None
    if new_binding == old_binding and new_aliases == compared:
        system.say("The binding and aliases are unchanged.")
        return
    system.say(f"HTTP-only sites remain HTTP-only. For HTTPS sites the certificate will be updated; all names must resolve to "
               f"{environment.description} and be reachable on HTTP/port 80.")
    if not yes:
        answer = system.ask(f"Change production bindings to {new_binding} (aliases: {' '.join(new_aliases) or 'none'}) on {environment.description}? [y/N]: ")
        if answer.lower() not in ("y", "yes"):
            system.say("Aborted")
            raise Stop("", 1)
    umbraco = "1" if system.path(f"{Path(environment.script).parent}/sync_prod.sh").is_file() else "n"
    variables = (f"SITE='{environment.FOLDER}' OLD='{old_binding}' NEW='{new_binding}' ALIASES='{' '.join(new_aliases)}' "
                 f"ROOT='{environment.root}' ACME_SERVER='{ACME_SERVER}' MAIL='{environment.ADMIN_MAIL or site.admin_mail}' UMBRACO='{umbraco}'")
    system.step(f"Change the binding to {new_binding} on {environment.description}")
    result = settings.remote_run(system, environment, variables, CHANGE_BINDING, check=False)
    if not result.ok:
        raise Stop("", result.returncode)
    # Keep the settings of this site in sync (quotes and the trailing comment are preserved)
    set_setting(system, environment, "BINDING", new_binding, quoted=True)
    system.say(f"PROD_BINDING in {environment.script} is now {new_binding}")
