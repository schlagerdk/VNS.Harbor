#!/bin/bash
# Usage: data_sync.sh <site> settings <main|environment>
#        data_sync.sh <site> sync <main|environment> <destination[,destination...]> [--dry-run]
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

# Edit the appsettings.json of one site in place, by its saved settings, and restart the site when it changed.
# With force it is restarted anyway, so uSync imports what it just received.
apply_settings() {
	local _id=$1 _force=${2:-} _file=${_workDir}/appsettings.json _result
	if [[ ${_id} == main ]]; then
		# The project's copy too, as publishing the main site copies it to the folder the site runs from
		local _files=() _path
		for _path in "${_siteRoot}/appsettings.json" "$(main_folder)/appsettings.json"; do
			[[ -f ${_path} ]] && [[ " ${_files[*]:-} " != *" ${_path} "* ]] && _files+=("${_path}")
		done
		((${#_files[@]})) || { echo "The main site has no appsettings.json." >&2; exit 1; }
		_result=$(python3 lib/data_sync.py appsettings "${_siteRoot}" main "${_files[@]}") || exit 1
		echo "${_result}"
		chown www-data:www-data "${_files[@]}" 2>/dev/null || true
		[[ -n ${_force} || ${_result} == *Updated* ]] || { echo "Nothing changed, so ${_serviceName} is not restarted."; return; }
		echo "Restart ${_serviceName}"
		systemctl restart "${_serviceName}"
		return
	fi
	export VNS_TARGET=${_id}
	load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		bash windows_file.sh "${_site}" read appsettings.json > "${_file}"
		_result=$(python3 lib/data_sync.py appsettings "${_siteRoot}" "${_id}" "${_file}") || exit 1
		echo "${_result}"
		[[ -n ${_force} || ${_result} == *Updated* ]] || { echo "Nothing changed, so the IIS app is not restarted."; return; }
		bash windows_file.sh "${_site}" save appsettings.json < "${_file}"
		# IIS restarts the app when web.config changes
		source lib/windows_prod.sh
		windows_smb_connect >/dev/null
		echo "Restart the IIS app"
		rclone touch "${_windowsTarget}/web.config"
	else
		local _path=${_root}/appsettings.json _unit
		prod_exec "cat $(printf %q "${_path}")" > "${_file}"
		_result=$(python3 lib/data_sync.py appsettings "${_siteRoot}" "${_id}" "${_file}") || exit 1
		echo "${_result}"
		[[ -n ${_force} || ${_result} == *Updated* ]] || { echo "Nothing changed, so the site is not restarted."; return; }
		prod_exec "tee $(printf %q "${_path}") >/dev/null" < "${_file}"
		prod_exec "chown www-data:www-data $(printf %q "${_path}")"
		_unit=$(normalize_dns_name "${PROD_FOLDER}" unit)
		echo "Restart ${_unit} on ${_target}"
		prod_exec "systemctl restart $(printf %q "${_unit}")"
	fi
}

# Whether uSync has written content in a site's folder, where data sync takes it from. uSync writes content when it
# is saved, and everything at startup on a site that may send, so content made before has to be written once.
has_exported_content() (
	local _id=$1 _folder
	if [[ ${_id} == main ]]; then
		_folder=$(main_folder)
		compgen -G "${_folder}/uSync/*/Content/*" >/dev/null
		return
	fi
	export VNS_TARGET=${_id}
	load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		return 0
	fi
	prod_exec "compgen -G $(printf %q "${_root}")'/uSync/*/Content/*' >/dev/null" 2>/dev/null
)

case "${_action}" in
	settings)
		_id=${3:-}
		valid_id "${_id}"
		echo "== Sync setup for ${_id} =="
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
		[[ -n ${_to} ]] || { echo "Choose a destination." >&2; exit 1; }
		# Every destination is checked before anything is synced
		_wantsContent=0
		for _id in ${_to//,/ }; do
			valid_id "${_id}"
			_kinds=$(python3 lib/data_sync.py kinds "${_siteRoot}" "${_from}" "${_id}") || exit 1
			[[ " ${_kinds} " != *" content "* ]] || _wantsContent=1
		done
		# Content is synced from what uSync has written: without it, only media would go, and that would look like a sync
		if ((_wantsContent)) && ! has_exported_content "${_from}"; then
			echo "${_from} has no content written by uSync yet, so there is no content to sync." >&2
			echo "uSync writes content when it is saved, and all of it when a site that may send data starts." >&2
			echo "Save Sync setup on ${_from}, which restarts it, or restart it, then sync again." >&2
			exit 1
		fi
		# One destination: settings from the main site, then the data merged in, then a restart to import it.
		# Every step stops it on its own, as set -e is off in the if that runs it.
		sync_one() (
			local _id=$1 _kinds
			_kinds=$(python3 lib/data_sync.py kinds "${_siteRoot}" "${_from}" "${_id}") || exit 1
			echo
			echo "######## ${_id}: ${_kinds// / and } from ${_from}. Nothing is deleted on ${_id}."
			# Settings first, so the document types match, unless Sync setup says the destination does not take them
			if [[ ${_id} != main ]] && _reason=$(python3 lib/data_sync.py settings "${_siteRoot}" main "${_id}" 2>&1); then
				echo
				echo "== Settings from the main site to ${_id} =="
				bash sync_env.sh "${_site}" main "${_id}" ${_dryRun[@]+"${_dryRun[@]}"} || exit 1
			elif [[ ${_id} != main ]]; then
				echo
				echo "Settings are not synced first: ${_reason}. Its document types must already match."
			fi
			echo
			echo "== Data from ${_from} to ${_id} =="
			bash sync_env.sh "${_site}" "${_from}" "${_id}" --data "${_kinds// /,}" ${_dryRun[@]+"${_dryRun[@]}"} || exit 1
			if ((${#_dryRun[@]})); then
				echo "DRY RUN: ${_id} is not restarted."
				exit 0
			fi
			# The destination imports what it received at startup. Its settings are applied again on the way,
			# so a destination edited by hand still imports.
			echo
			echo "== Import on ${_id} =="
			apply_settings "${_id}" force || exit 1
			echo "uSync on ${_id} imports the data as it starts. Its log in the backoffice shows the result."
		)
		_failed=()
		for _id in ${_to//,/ }; do
			sync_one "${_id}" || { echo "${_id}: FAILED" >&2; _failed+=("${_id}"); }
		done
		if ((${#_failed[@]})); then
			echo "Data sync failed for: ${_failed[*]}" >&2
			exit 1
		fi
		echo
		echo "Done."
		;;
	*)
		echo "Usage: $0 <site> settings <main|environment> | sync <from> <to> [--dry-run]" >&2
		exit 1
		;;
esac
