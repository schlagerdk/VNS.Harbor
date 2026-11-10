#!/bin/bash
# Usage: data_sync.sh <site> settings <main|environment>
#        data_sync.sh <site> sync <main|environment> <main|environment> [--dry-run]
# settings: set uSync in the appsettings.json of the main site or one environment by its data sync settings
#   (scripts/data_sync.json, saved by the admin), then restart it so uSync reads them.
# sync: sync data from one site to another, when both have data sync on, the source may send and the destination
#   may receive. Settings are synced from the main site first, so the destination has the same document types.
#   Data is merged: content and media are added and updated, never deleted. The destination is restarted, and
#   uSync imports the data at startup.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_site=${1:-}
_action=${2:-}
validate_site_name "${_site}"
set_site_vars
[[ -d ${_siteRoot}/scripts ]] || { echo "Unknown site: ${_site}" >&2; exit 1; }

valid_id() {
	[[ $1 == main || ( $1 =~ ^[a-z0-9][a-z0-9-]{0,62}$ && -n $(prod_targets | grep -Fx -- "$1") ) ]] || { echo "Unknown site or environment: $1" >&2; exit 1; }
}

main_folder() {
	local _working
	_working=$(sed -n 's/^WorkingDirectory=//p' "${_servicePath}" 2>/dev/null | head -n 1)
	_working=${_working%/}
	if [[ -n ${_working} ]]; then
		echo "${_working}"
	elif [[ -d ${_siteRoot}/_publish ]]; then
		echo "${_siteRoot}/_publish"
	else
		echo "${_siteRoot}"
	fi
}

_workDir=$(mktemp -d)
trap 'rm -rf "${_workDir}"' EXIT

# Edit the appsettings.json of one site in place, by its saved settings
apply_settings() {
	local _id=$1 _file=${_workDir}/appsettings.json
	if [[ ${_id} == main ]]; then
		# The project's copy too, as publishing the main site copies it to the folder the site runs from
		local _files=() _path
		for _path in "${_siteRoot}/appsettings.json" "$(main_folder)/appsettings.json"; do
			[[ -f ${_path} ]] && [[ " ${_files[*]:-} " != *" ${_path} "* ]] && _files+=("${_path}")
		done
		((${#_files[@]})) || { echo "The main site has no appsettings.json." >&2; exit 1; }
		python3 lib/data_sync.py appsettings "${_siteRoot}" main "${_files[@]}"
		chown www-data:www-data "${_files[@]}" 2>/dev/null || true
		echo "Restart ${_serviceName}"
		systemctl restart "${_serviceName}"
		return
	fi
	export VNS_TARGET=${_id}
	load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		bash windows_file.sh "${_site}" read appsettings.json > "${_file}"
		python3 lib/data_sync.py appsettings "${_siteRoot}" "${_id}" "${_file}"
		bash windows_file.sh "${_site}" save appsettings.json < "${_file}"
		# IIS restarts the app when web.config changes
		source lib/windows_prod.sh
		windows_smb_connect >/dev/null
		echo "Restart the IIS app"
		rclone touch "${_windowsTarget}/web.config"
	else
		local _path=${_root}/appsettings.json _unit
		prod_exec "cat $(printf %q "${_path}")" > "${_file}"
		python3 lib/data_sync.py appsettings "${_siteRoot}" "${_id}" "${_file}"
		prod_exec "tee $(printf %q "${_path}") >/dev/null" < "${_file}"
		prod_exec "chown www-data:www-data $(printf %q "${_path}")"
		_unit=$(normalize_dns_name "${PROD_FOLDER}" unit)
		echo "Restart ${_unit} on ${_target}"
		prod_exec "systemctl restart $(printf %q "${_unit}")"
	fi
}

case "${_action}" in
	settings)
		_id=${3:-}
		valid_id "${_id}"
		echo "== Data sync settings for ${_id} =="
		apply_settings "${_id}"
		echo "Done."
		;;
	sync)
		_from=${3:-}
		_to=${4:-}
		_dryRun=()
		case "${5:-}" in
			"") ;;
			--dry-run) _dryRun=(--dry-run) ;;
			*) echo "Unknown option: $5" >&2; exit 1 ;;
		esac
		valid_id "${_from}"
		valid_id "${_to}"
		_kinds=$(python3 lib/data_sync.py kinds "${_siteRoot}" "${_from}" "${_to}") || exit 1
		echo "Sync ${_kinds// / and } from ${_from} to ${_to}. Nothing is deleted on ${_to}."
		if [[ ${_to} != main ]]; then
			echo
			echo "== 1. Settings from the main site to ${_to} =="
			bash sync_env.sh "${_site}" main "${_to}" ${_dryRun[@]+"${_dryRun[@]}"}
		fi
		echo
		echo "== Data from ${_from} to ${_to} =="
		bash sync_env.sh "${_site}" "${_from}" "${_to}" --data "${_kinds// /,}" ${_dryRun[@]+"${_dryRun[@]}"}
		if ((${#_dryRun[@]})); then
			echo "DRY RUN: ${_to} is not restarted."
			exit 0
		fi
		# The destination imports what it received at startup. Its settings are applied again on the way,
		# so a destination edited by hand still imports.
		echo
		echo "== Import on ${_to} =="
		apply_settings "${_to}"
		echo "Done. uSync on ${_to} imports the data as it starts. Its log in the backoffice shows the result."
		;;
	*)
		echo "Usage: $0 <site> settings <main|environment> | sync <from> <to> [--dry-run]" >&2
		exit 1
		;;
esac
