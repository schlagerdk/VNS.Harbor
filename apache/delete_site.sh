#!/bin/bash
# Usage: delete_site.sh [site] [y|n (delete certificates)] [all|test|prod]
#   all  = the site and all its environments (default)
#   test = only the site itself (environment folders inside the site folder are kept)
#   prod = only the environment selected by VNS_TARGET, including a remote one
# Environments on a Windows server are only removed here: their IIS site and files must be removed on the server.
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh
clear

_site=${1:-}
_deleteCertificate=${2:-}
_scope=${3:-all}

ask _site "Enter site name"
validate_site_name "${_site}" || exit 1
[[ ${_scope} =~ ^(all|test|prod)$ ]] || { echo "Scope must be all, test or prod." >&2; exit 1; }
set_site_vars
_binding=${_site}
if [[ -f ${_configPath} ]]; then
	_binding=$(awk '$1 == "ServerName" { print $2; exit }' "${_configPath}")
	_binding=${_binding:-${_site}}
fi

# Settings of an environment, read (never executed) from its publish_prod.sh
_prodVar() { sed -n "s/^$1=\([^ #	]*\).*/\1/p" "${_prodScript}" 2>/dev/null | head -n 1 | tr -d "'\\\\\""; }
_prodFolderOf() {
	if [[ $1 == prod ]]; then echo "${_siteRoot}/scripts"; else echo "${_siteRoot}/scripts/targets/$1"; fi
}
_allTargets=()
while IFS= read -r _id; do _allTargets+=("${_id}"); done < <(prod_targets)
_targets=()
if [[ ${_scope} == all ]]; then
	_targets=(${_allTargets[@]+"${_allTargets[@]}"})
elif [[ ${_scope} == prod ]]; then
	if [[ -n ${VNS_TARGET:-} ]]; then
		_targets=("${VNS_TARGET}")
	elif ((${#_allTargets[@]} == 1)); then
		_targets=("${_allTargets[0]}")
	else
		echo "Choose an environment with VNS_TARGET: ${_allTargets[*]:-this site has none}" >&2
		exit 1
	fi
	[[ -f $(_prodFolderOf "${_targets[0]}")/publish_prod.sh ]] || { echo "This site has no such environment." >&2; exit 1; }
fi

remove_service_and_config() {
	local _name=$1 _unit
	_unit=$(normalize_dns_name "${_name}" unit) || return 1
	if [[ -f /etc/systemd/system/${_unit}.service ]]; then
		echo "Stop, disable and delete service ${_name}"
		sudo systemctl stop "${_unit}" || true
		sudo systemctl disable "${_unit}" || true
		sudo rm -f "/etc/systemd/system/${_unit}.service"
	fi
	echo "Delete apache config and logs for ${_name}"
	sudo rm -f "/etc/apache2/sites-available/${_name}.conf" "/etc/apache2/sites-enabled/${_name}.conf"
	sudo rm -f "/var/log/apache2/${_name}-80-error.log" "/var/log/apache2/${_name}-443-error.log" "/var/log/apache2/${_name}-rewrite-error.log"
	sudo rm -rf "/var/lib/vns-roots/${_name:?}"
	sudo rm -f "/var/lib/vns-managed/${_name:?}"
}

remove_certificate() {
	local _domain=$1 _directory=$2 _conf
	[[ ${_deleteCertificate} =~ ^([yY]|[yY][eE][sS])$ ]] || return 0
	validate_binding "${_domain}" || return 1
	for _conf in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${_conf} ]] || continue
		if awk -v d="${_domain}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${_conf}"; then
			echo "Keep certificate for ${_domain}; another enabled site uses it."
			return 0
		fi
	done
	echo "Revoke and remove certificate for ${_domain}"
	sudo "${_acme}" --revoke -d "${_domain}" || true
	sudo "${_acme}" --remove -d "${_domain}" || true
	sudo rm -rf "${_directory:?}" || return 1
	# acme.sh keeps its files in a folder named after the binding, and --remove does not delete them
	clean_stale_acme "${_domain}" || return 1
	sudo rm -rf -- "/root/.acme.sh/${_domain}" "/root/.acme.sh/${_domain}_ecc"
}

if [[ -z ${_deleteCertificate} ]]; then
	read -r -p "Delete certificates? [y/N]: " _deleteCertificate
fi

# Folders of this site's local environments, kept when only the site itself is deleted
_keepFolders=()
if [[ ${_scope} == test ]]; then
	for _id in ${_allTargets[@]+"${_allTargets[@]}"}; do
		_prodScript=$(_prodFolderOf "${_id}")/publish_prod.sh
		_prodLocalPath=$(_prodVar PROD_LOCAL_PATH)
		[[ ${_prodLocalPath} == "${_siteRoot}"/* ]] || continue
		if [[ ${_prodLocalPath%/} == "${_siteRoot}"/*/* ]]; then
			echo "The folder ${_prodLocalPath} is nested too deep inside the site folder to keep it. Delete all instead." >&2
			exit 1
		fi
		_keepFolders+=("${_prodLocalPath#"${_siteRoot}"/}")
	done
fi

# ---- environments
for _id in ${_targets[@]+"${_targets[@]}"}; do
	export VNS_TARGET=${_id}
	_prodScript=$(_prodFolderOf "${_id}")/publish_prod.sh
	_prodLocalPath=$(_prodVar PROD_LOCAL_PATH)
	_prodFolder=$(_prodVar PROD_FOLDER)
	_prodBinding=$(_prodVar PROD_BINDING)
	_prodTarget=$(_prodVar PROD_TARGET)
	if [[ ${_prodTarget} == windows ]]; then
		# The IIS site is managed on the Windows server, and it holds the only copy of its settings and media.
		echo "The site on the Windows server is not touched: remove the IIS site and its folder at $(_prodVar PROD_WINDOWS_SHARE) yourself."
	elif [[ -n ${_prodLocalPath} ]]; then
		_prodLocalPath=${_prodLocalPath%/}
		validate_site_name "${_prodFolder}" && [[ ${_prodFolder} != "${_site}" ]] || { echo "Invalid production site name: ${_prodFolder}" >&2; exit 1; }
		case "${_prodLocalPath}" in
			"${_rootPath}"/?*) ;;
			*) echo "The production folder must be inside ${_rootPath}: ${_prodLocalPath}" >&2; exit 1 ;;
		esac
		[[ ${_prodLocalPath} != *..* ]] || { echo "Invalid production folder: ${_prodLocalPath}" >&2; exit 1; }
		remove_service_and_config "${_prodFolder}"
		echo "Delete production files ${_prodLocalPath}"
		sudo rm -rf "${_prodLocalPath:?}"
		[[ -z ${_prodBinding} ]] || remove_certificate "${_prodBinding}" "/etc/ssl/certs/${_prodBinding}_ecc" || exit 1
	else
		echo "Delete the remote production site (${_prodFolder})"
		# Use the saved production connection settings. Do not execute publish_prod.sh.
		load_prod_settings
		[[ -z ${PROD_LOCAL_PATH} ]] || { echo "Production target settings are inconsistent." >&2; exit 1; }
		validate_site_name "${PROD_FOLDER}" || exit 1
		validate_binding "${PROD_BINDING}" || exit 1
		[[ ${PROD_IP} =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || { echo "Invalid production host." >&2; exit 1; }
		[[ ${PROD_USER} =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,63}$ ]] || { echo "Invalid production SSH user." >&2; exit 1; }
		[[ ${PROD_SSH_PORT} =~ ^[0-9]+$ && ${PROD_SSH_PORT} -ge 1 && ${PROD_SSH_PORT} -le 65535 ]] || { echo "Invalid production SSH port." >&2; exit 1; }
		printf -v _remoteVars 'SITE=%q BINDING=%q DELETE_CERTIFICATE=%q' "${PROD_FOLDER}" "${PROD_BINDING}" "${_deleteCertificate}"
		prod_run "${_remoteVars}" <<'REMOTE'
set -euo pipefail
source /vns/apache/lib/common.sh
validate_site_name "${SITE}"
unit=$(normalize_dns_name "${SITE}" unit)
[[ ${BINDING} =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] || { echo "Invalid remote binding." >&2; exit 1; }
root="/var/www/${SITE}"
[[ ${root} == "/var/www/${SITE}" && ${root} != /var/www ]] || { echo "Unsafe remote site path." >&2; exit 1; }
if [[ -f /etc/systemd/system/${unit}.service ]]; then
	systemctl stop "${unit}.service"
	systemctl disable "${unit}.service"
fi
rm -f -- "/etc/systemd/system/${unit}.service"
rm -f -- "/etc/apache2/sites-available/${SITE}.conf" "/etc/apache2/sites-enabled/${SITE}.conf"
rm -f -- "/var/log/apache2/${SITE}-80-error.log" "/var/log/apache2/${SITE}-443-error.log" "/var/log/apache2/${SITE}-rewrite-error.log"
rm -rf -- "/var/lib/vns-roots/${SITE}"
rm -f -- "/var/lib/vns-managed/${SITE}"
rm -rf -- "${root:?}"
if [[ ${DELETE_CERTIFICATE} =~ ^([yY]|[yY][eE][sS])$ ]]; then
	domain_in_use=0
	for conf in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${conf} ]] || continue
		if awk -v d="${BINDING}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${conf}"; then
			domain_in_use=1
			break
		fi
	done
	if ((domain_in_use)); then
		echo "Keep certificate for ${BINDING}; another enabled site uses it."
	else
		if [[ -x /etc/acmesh/acme.sh ]]; then
			/etc/acmesh/acme.sh --revoke -d "${BINDING}" || true
			/etc/acmesh/acme.sh --remove -d "${BINDING}" || true
		fi
		rm -rf -- "/etc/ssl/certs/${BINDING}" "/etc/ssl/certs/${BINDING}_ecc"
		rm -rf -- "/root/.acme.sh/${BINDING}" "/root/.acme.sh/${BINDING}_ecc"
	fi
fi
systemctl daemon-reload
apache2ctl configtest
systemctl reload apache2
echo "Remote production site ${SITE} removed."
REMOTE
		_remoteResult=$?
		if ((_remoteResult != 0)); then
			echo "Remote production removal failed; local files and production settings have been kept." >&2
			exit "${_remoteResult}"
		fi
	fi
	if [[ ${_scope} == prod ]]; then
		if [[ ${_id} == prod ]]; then
			sudo rm -f "${_siteRoot}/scripts/publish_prod.sh" "${_siteRoot}/scripts/sync_prod.sh" "${_siteRoot}/scripts/clear_logs_prod.sh"
		else
			sudo rm -rf "${_siteRoot:?}/scripts/targets/${_id:?}"
			sudo rm -f "${_siteRoot}/.vns-publish/target-${_id}.json"
		fi
		# With no environments left, put the site back to a test-only site
		if [[ -z $(prod_targets) ]]; then
			for _name in publish clear_logs; do
				if [[ -f ${_siteRoot}/scripts/${_name}_local.sh && ! -e ${_siteRoot}/scripts/${_name}.sh ]]; then
					sudo mv "${_siteRoot}/scripts/${_name}_local.sh" "${_siteRoot}/scripts/${_name}.sh"
				fi
			done
		fi
	fi
done

# ---- the site itself
if [[ ${_scope} != prod ]]; then
	remove_service_and_config "${_site}"
	if [[ ${_scope} == test && ${#_keepFolders[@]} -gt 0 ]]; then
		echo "Delete the site files (environment folders ${_keepFolders[*]} and scripts are kept)"
		_findKeep=()
		for _keep in "${_keepFolders[@]}"; do _findKeep+=(! -name "${_keep%/}"); done
		sudo find "${_siteRoot}" -mindepth 1 -maxdepth 1 "${_findKeep[@]}" ! -name scripts -exec rm -rf {} +
	else
		echo "Delete site and files"
		sudo rm -rf "${_rootPath:?}/${_site:?}"
	fi
	remove_certificate "${_binding}" "${_certificateDirectory}" || exit 1
fi

echo "Reload daemon"
sudo systemctl daemon-reload
echo "Test and reload apache2"
sudo apache2ctl configtest
sudo systemctl reload apache2
echo "All is removed!"
