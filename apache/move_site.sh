#!/bin/bash
# Usage: move_site.sh <site>, with the new server in VNS_MOVE_IP, _USER, _PASSWORD (empty = SSH key) and _SSH_PORT
# Migrates a site to another Linux server, or copies it there with VNS_MOVE_COPY=1, together with its environments
# on this server. Environments on other servers or on Windows stay where they are: the site keeps its settings for them.
# Everything is set up with HTTP on the new server, as DNS may not point there yet. Issue certificate on each site
# when it does. The whole folders go, also the builds, so nothing is built and the sites run the same version.
# A SQL Server on this machine stays here: the copies' connections to it are pointed at this server's address.
# A migration stops the sites here and removes their services and Apache configs once the new server runs them, and
# VNS_MOVE_DELETE_LOCAL=y also deletes their files. A copy leaves them as they are; VNS_COPY_STOP=y stops them while
# they are copied. If a step fails, the sites here run as before and what was made on the new server is removed.
# The new server needs the same VNS version: move_site.sh --receive <folder> <SQL server> sets the sites up there.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_rootsFolder=/var/lib/vns-roots
_systemdFolder=/etc/systemd/system
_apacheSites=/etc/apache2/sites-available
_incomingFolder=/var/lib/vns-incoming

# ---- on the new server: one <name>.conf, and <name>.service when it has one, for every site that arrives
receive() {
	local _incoming=$1 _sqlServer=${2:-} _conf _name _binding _working _folder _units=() _unit _script
	[[ -d ${_incoming} ]] || { echo "${_incoming} was not found." >&2; exit 1; }
	trap release_port_reservation EXIT
	for _conf in "${_incoming}"/*.conf; do
		[[ -f ${_conf} ]] || continue
		_name=$(basename "${_conf}" .conf)
		validate_site_name "${_name}"
		_site=${_name}
		set_site_vars
		if [[ -e ${_configPath} || -e ${_servicePath} ]]; then
			echo "${_name} is already set up on this server." >&2
			exit 1
		fi
		echo "Set up ${_name} with HTTP"
		python3 lib/move_site.py http-conf "${_conf}"
		_binding=$(awk '$1 == "ServerName" { print $2; exit }' "${_conf}")
		_folders=("${_siteRoot}")
		if [[ -f ${_incoming}/${_name}.service ]]; then
			# The port it had may be taken here
			allocate_ports
			python3 lib/move_site.py port "${_portHttp}" "${_incoming}/${_name}.service" "${_conf}"
			_working=$(sed -n 's/^WorkingDirectory=//p' "${_incoming}/${_name}.service" | head -n 1)
			[[ -z ${_working} || ${_working%/} == "${_siteRoot}" ]] || _folders+=("${_working%/}")
		fi
		for _folder in "${_folders[@]}"; do
			[[ -d ${_folder} ]] || continue
			[[ -z ${_binding} ]] || python3 lib/move_site.py http-settings "${_folder}" "${_binding}" >/dev/null
			[[ -n ${_sqlServer} ]] || continue
			_repointed=$(python3 lib/copy_site.py repoint-sql "${_folder}" "${_sqlServer}")
			if [[ ${_repointed} != 0 ]]; then
				echo "The SQL connection in ${_folder} now points to ${_sqlServer}, which must accept connections from this server."
			fi
		done
		cp "${_conf}" "${_configPath}"
		a2ensite "${_name}.conf" >/dev/null
		if [[ -f ${_incoming}/${_name}.service ]]; then
			cp "${_incoming}/${_name}.service" "${_servicePath}"
			mkdir -p "${_rootsFolder}/${_name}"
			systemctl daemon-reload
			if grep -q '^ExecStart=.*/dotnet ' "${_servicePath}"; then
				python3 lib/sql_startup.py "${_serviceName}" || echo "Could not make ${_serviceName} wait for SQL Server at boot." >&2
			fi
			systemctl enable "${_serviceName}" >/dev/null 2>&1
			systemctl start "${_serviceName}"
			_units+=("${_serviceName}")
		fi
		release_port_reservation
	done
	# Environments on this server are published with HTTP from now on, until a certificate is issued
	if [[ -f ${_incoming}/http-scripts ]]; then
		while IFS= read -r _script; do
			[[ -f ${_script} ]] || continue
			perl -pi -e "s/^PROD_HTTPS=\\S*/PROD_HTTPS='n'/" "${_script}"
		done <"${_incoming}/http-scripts"
	fi
	apache2ctl configtest
	systemctl reload apache2
	for _unit in ${_units[@]+"${_units[@]}"}; do
		systemctl is-active --quiet "${_unit}" || { echo "${_unit} did not start." >&2; exit 1; }
	done
	rm -rf "${_incoming:?}"
}

if [[ ${1:-} == --receive ]]; then
	receive "${2:-}" "${3:-}"
	exit 0
fi

# ---- on this server
_site=${1:-}
validate_site_name "${_site}"
set_site_vars
_copy=${VNS_MOVE_COPY:-}
_verb=Migrate
[[ -z ${_copy} ]] || _verb=Copy
[[ -d ${_siteRoot} ]] || { echo "The folder ${_siteRoot} does not exist." >&2; exit 1; }
[[ -f ${_configPath} ]] || { echo "${_site} has no Apache config." >&2; exit 1; }
if [[ -f /var/lib/vns-managed/${_site} ]]; then
	echo "${_site} is an environment of a main site on $(sed -n 2p "/var/lib/vns-managed/${_site}"). Move it from there." >&2
	exit 1
fi

_host=${VNS_MOVE_IP:-}
_user=${VNS_MOVE_USER:-root}
_password=${VNS_MOVE_PASSWORD:-}
_sshPort=${VNS_MOVE_SSH_PORT:-22}
unset VNS_MOVE_PASSWORD
[[ -n ${_host} ]] || { echo "Enter the new server." >&2; exit 1; }
[[ ${_sshPort} =~ ^[0-9]+$ ]] || { echo "The SSH port must be a number." >&2; exit 1; }

# The sites that go: the site, and its environments on this server. Their names are their folders, services and configs.
_names=("${_site}")
_folders=("${_siteRoot}")
_httpScripts=()
_kept=()
for _id in $(prod_targets); do
	VNS_TARGET=${_id} load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		_kept+=("${PROD_BINDING} on Windows (${PROD_WINDOWS_SHARE})")
		continue
	elif [[ -z ${PROD_LOCAL_PATH} ]]; then
		_kept+=("${PROD_BINDING} on ${PROD_IP}")
		continue
	fi
	_httpScripts+=("$(dirname "${_prodScript}")/publish_prod.sh")
	[[ ! -f ${_apacheSites}/${PROD_FOLDER}.conf ]] || _names+=("${PROD_FOLDER}")
	# One in the site's folder goes with it
	if [[ ${_root} != "${_siteRoot}"/* && -d ${_root} ]]; then
		_folders+=("${_root}")
	fi
done

_ssh=(ssh -p "${_sshPort}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2)
if [[ -n ${_password} ]]; then
	command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
	export SSHPASS=${_password}
	_ssh=(sshpass -e "${_ssh[@]}")
else
	_ssh+=(-o BatchMode=yes)
fi
_sudo=""
[[ ${_user} == root ]] || _sudo="sudo -n"
on_new_server() {
	"${_ssh[@]}" "${_user}@${_host}" "${_sudo} $1"
}
copy_to_new_server() {
	rsync -aHz -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "$1" "${_user}@${_host}:$2"
}

# ---- before anything is stopped: the new server must be ready, and have none of the sites
echo "Check ${_host}"
_check="set -e; test -f /vns/apache/move_site.sh || { echo 'VNS on ${_host} must be updated first: /vns/update.sh' >&2; exit 1; }"
_check+="; test \"\$(uname -m)\" = $(printf %q "$(uname -m)") || { echo '${_host} has another CPU architecture than this server, so the builds cannot run there.' >&2; exit 1; }"
for _name in "${_names[@]}"; do
	_unit=$(normalize_dns_name "${_name}" unit)
	_check+="; ! test -e $(printf %q "${_systemdFolder}/${_unit}.service") -o -e $(printf %q "${_apacheSites}/${_name}.conf") || { echo $(printf %q "${_name} already exists on ${_host}.") >&2; exit 1; }"
	if grep -qs '^ExecStart=.*/usr/lib/dotnet/dotnet ' "${_systemdFolder}/${_unit}.service"; then
		_check+="; test -x /usr/lib/dotnet/dotnet || { echo '.NET is not installed on ${_host}.' >&2; exit 1; }"
	fi
done
for _folder in "${_folders[@]}"; do
	_check+="; ! test -e $(printf %q "${_folder}") || { echo $(printf %q "${_folder} already exists on ${_host}.") >&2; exit 1; }"
done
on_new_server "bash -c $(printf %q "${_check}")"

# The source's own SQL Server stays: the copies are pointed at this server
_sqlServer=$(hostname -I 2>/dev/null | awk '{ print $1 }' || true)
_sqlServer=${_sqlServer:-$(hostname -f 2>/dev/null || hostname)}

_staging=$(mktemp -d)
_incoming=${_incomingFolder}/${_site}-$$
_stopped=()
_started=0
_done=0
undo() {
	local _status=$? _unit _cleanup=""
	if ((!_done)); then
		echo "The ${_verb,,} failed, so ${_site} runs here as before." >&2
		if ((_started)); then
			echo "Remove what was made on ${_host}" >&2
			for _name in "${_names[@]}"; do
				_unit=$(normalize_dns_name "${_name}" unit)
				_cleanup+="systemctl stop $(printf %q "${_unit}") 2>/dev/null; systemctl disable $(printf %q "${_unit}") 2>/dev/null; "
				_cleanup+="rm -f $(printf %q "${_systemdFolder}/${_unit}.service") $(printf %q "/etc/apache2/sites-enabled/${_name}.conf") $(printf %q "${_apacheSites}/${_name}.conf"); "
				_cleanup+="rm -rf $(printf %q "${_rootsFolder}/${_name}"); "
			done
			for _folder in "${_folders[@]}"; do _cleanup+="rm -rf $(printf %q "${_folder}"); "; done
			_cleanup+="rm -rf $(printf %q "${_incoming}"); systemctl daemon-reload; apache2ctl configtest >/dev/null 2>&1 && systemctl reload apache2; true"
			on_new_server "bash -c $(printf %q "${_cleanup}")" || echo "Could not clean up ${_host}. Remove the sites there by hand." >&2
		fi
		for _unit in ${_stopped[@]+"${_stopped[@]}"}; do
			systemctl start "${_unit}" || echo "WARNING: could not start ${_unit} again." >&2
		done
	fi
	rm -rf "${_staging}"
	exit "${_status}"
}
trap undo EXIT

for _name in "${_names[@]}"; do
	_unit=$(normalize_dns_name "${_name}" unit)
	cp "${_apacheSites}/${_name}.conf" "${_staging}/${_name}.conf"
	[[ ! -f ${_systemdFolder}/${_unit}.service ]] || cp "${_systemdFolder}/${_unit}.service" "${_staging}/${_name}.service"
done
((${#_httpScripts[@]} == 0)) || printf '%s\n' "${_httpScripts[@]}" >"${_staging}/http-scripts"

if [[ -z ${_copy} || ${VNS_COPY_STOP:-} == y ]]; then
	echo "Stop the sites here, so their databases and files stay consistent while they are copied"
	for _name in "${_names[@]}"; do
		_unit=$(normalize_dns_name "${_name}" unit)
		[[ -f ${_systemdFolder}/${_unit}.service ]] || continue
		systemctl is-active --quiet "${_unit}" || continue
		_stopped+=("${_unit}")
		systemctl stop "${_unit}"
	done
fi

_started=1
for _folder in "${_folders[@]}"; do
	echo "Copy ${_folder} to ${_host}"
	on_new_server "mkdir -p $(printf %q "${_folder}")"
	copy_to_new_server "${_folder}/" "${_folder}/"
done
on_new_server "mkdir -p $(printf %q "${_incoming}")"
copy_to_new_server "${_staging}/" "${_incoming}/"
echo "Set up the sites on ${_host}"
on_new_server "bash /vns/apache/move_site.sh --receive $(printf %q "${_incoming}") $(printf %q "${_sqlServer}")"
_done=1
unset SSHPASS

if [[ -n ${_copy} ]]; then
	for _unit in ${_stopped[@]+"${_stopped[@]}"}; do systemctl start "${_unit}"; done
	echo "${_site} is copied to ${_host}. It is not changed here."
else
	echo "Remove the sites here"
	for _name in "${_names[@]}"; do
		_unit=$(normalize_dns_name "${_name}" unit)
		systemctl disable "${_unit}" >/dev/null 2>&1 || true
		rm -f "${_systemdFolder}/${_unit}.service"
		a2dissite "${_name}.conf" >/dev/null 2>&1 || true
		rm -f "${_apacheSites}/${_name}.conf"
		rm -rf "${_rootsFolder:?}/${_name:?}"
	done
	systemctl daemon-reload
	apache2ctl configtest
	systemctl reload apache2
	if [[ ${VNS_MOVE_DELETE_LOCAL:-} == y ]]; then
		for _folder in "${_folders[@]}"; do
			echo "Delete ${_folder}"
			rm -rf "${_folder:?}"
		done
	else
		echo "The files are kept here: ${_folders[*]}"
	fi
	echo "${_site} has moved to ${_host}. A certificate on this server is not removed."
fi
echo "On ${_host} the sites answer on HTTP: ${_names[*]}. When DNS points there, use Issue certificate on each of them."
if ((${#_kept[@]})); then
	echo "Environments on other servers are not moved; ${_site} still publishes to them: ${_kept[*]}"
fi
