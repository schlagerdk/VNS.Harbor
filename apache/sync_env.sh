#!/bin/bash
# Usage: sync_env.sh <site> <main|environment> <environment[,environment...]|all> [--dry-run]
#        sync_env.sh <site> <main|environment> <main|environment> --data <content,media> [--dry-run]
# Sync settings from the main site, or from one environment, to one or all of the site's other environments.
# With --data it syncs data instead: in each uSync version folder Content and Dictionary for content, Media and
# wwwroot/media for media. Data is merged: files are added and updated, never deleted. data_sync.sh checks first
# that the two sites may sync data, and it is what the admin runs.
# Only settings are synced: in each uSync version folder (v9, v17, v18, Root ...) the folders listed in
# _settingsFolders and usync.config, plus Views, wwwroot/css and wwwroot/scripts. Each is mirrored on its own.
# Content, Media, Dictionary, Domains and any folder not listed are never touched, so data on a destination is kept.
# A source folder that does not exist is skipped: it never empties the destination.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_site=${1:-}
_from=${2:-}
_to=${3:-}
_dryRun=()
_data=""
shift 3 || true
while (($#)); do
	case "$1" in
		--dry-run) _dryRun=(--dry-run); echo "DRY RUN: nothing is changed on the destinations." ;;
		--data)
			_data=${2:-}
			[[ ${_data} =~ ^(content|media)(,(content|media))?$ ]] || { echo "--data takes content, media or content,media" >&2; exit 1; }
			shift
			;;
		*) echo "Usage: $0 <site> <main|environment> <environment|all> [--data <content,media>] [--dry-run]" >&2; exit 1 ;;
	esac
	shift
done
validate_site_name "${_site}"
set_site_vars
[[ -n ${_from} && -n ${_to} ]] || { echo "Usage: $0 <site> <main|environment> <environment|all>" >&2; exit 1; }
[[ ${_from} != "${_to}" ]] || { echo "The source and destination are the same." >&2; exit 1; }

_settingsFolders=(ContentTypes DataTypes Languages MediaTypes MemberTypes RelationTypes Templates)
_codeFolders=(Views wwwroot/css wwwroot/scripts)
_targets=()
while IFS= read -r _id; do _targets+=("${_id}"); done < <(prod_targets)
_known=" main ${_targets[*]:-} "
[[ ${_known} == *" ${_from} "* ]] || { echo "Unknown source: ${_from}" >&2; exit 1; }

_destinations=()
if [[ -n ${_data} ]]; then
	# Data goes to one site at a time, and may also go to the main site
	[[ ${_to} != all ]] || { echo "Data is synced to one site at a time." >&2; exit 1; }
	[[ ${_known} == *" ${_to} "* ]] || { echo "Unknown destination: ${_to}" >&2; exit 1; }
	_destinations=("${_to}")
else
	for _id in ${_targets[@]+"${_targets[@]}"}; do
		[[ ${_id} != "${_from}" ]] || continue
		[[ ${_to} == all || ",${_to}," == *",${_id},"* ]] && _destinations+=("${_id}")
	done
	if [[ ${_to} != all ]]; then
		for _id in ${_to//,/ }; do
			[[ ${_id} != main && ${_known} == *" ${_id} "* ]] || { echo "Unknown destination: ${_id}" >&2; exit 1; }
		done
	fi
fi
((${#_destinations[@]})) || { echo "No environment to sync to." >&2; exit 1; }

_source=""
_workDir=""
trap '[[ -z ${_workDir} ]] || rm -rf "${_workDir}"' EXIT

# The main site's files are in the folder its service runs from
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

ssh_for_target() {
	_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
	if [[ -n ${PROD_PASSWORD} ]]; then
		command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
		export SSHPASS=${PROD_PASSWORD}
		_ssh=(sshpass -e "${_ssh[@]}")
	fi
	_sudo=""
	[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
}

_what=settings
[[ -z ${_data} ]] || _what="data (${_data//,/ and })"

# What is copied from an environment: uSync, plus the code folders for settings or the media files for data
_pullFolders=(uSync "${_codeFolders[@]}")
if [[ -n ${_data} ]]; then
	_pullFolders=(uSync)
	[[ ${_data} != *media* ]] || _pullFolders+=(wwwroot/media)
fi

# Copy an environment's uSync and code folders to _source. Any error stops the sync: a partial copy must not be mirrored.
pull_environment() (
	export VNS_TARGET=$1
	load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		source lib/windows_prod.sh
		windows_smb_connect
		for _folder in "${_pullFolders[@]}"; do
			rclone lsf "${_windowsTarget}/${_folder}" --max-depth 1 >/dev/null 2>&1 || continue
			rclone copy "${_windowsTarget}/${_folder}" "${_source}/${_folder}" --create-empty-src-dirs --modify-window 2s --stats 0
		done
	elif [[ -n ${PROD_LOCAL_PATH} ]]; then
		for _folder in "${_pullFolders[@]}"; do
			[[ -d ${_root}/${_folder} ]] || continue
			mkdir -p "${_source}/${_folder}"
			rsync -a "${_root}/${_folder}/" "${_source}/${_folder}/"
		done
	else
		ssh_for_target
		for _folder in "${_pullFolders[@]}"; do
			"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} test -d '${_root}/${_folder}'" || continue
			mkdir -p "${_source}/${_folder}"
			rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "${PROD_USER}@${PROD_IP}:${_root}/${_folder}/" "${_source}/${_folder}/"
		done
	fi
)

if [[ ${_from} == main ]]; then
	_source=$(main_folder)
else
	_workDir=$(mktemp -d)
	_source=${_workDir}
	echo "Copy ${_what} from ${_from}"
	pull_environment "${_from}"
fi

# What is synced: only what exists at the source
_mirror=()
_files=()
_versionFolders=("${_settingsFolders[@]}")
if [[ -n ${_data} ]]; then
	_versionFolders=()
	[[ ${_data} != *content* ]] || _versionFolders+=(Content Dictionary)
	[[ ${_data} != *media* ]] || _versionFolders+=(Media)
	_codeFolders=()
	[[ ${_data} != *media* ]] || _codeFolders=(wwwroot/media)
fi
for _version in "${_source}"/uSync/*/; do
	[[ -d ${_version} ]] || continue
	_version=$(basename "${_version}")
	[[ ${_version} =~ ^(v[0-9]+|Root)$ ]] || continue
	for _folder in "${_versionFolders[@]}"; do
		[[ ! -d ${_source}/uSync/${_version}/${_folder} ]] || _mirror+=("uSync/${_version}/${_folder}")
	done
	[[ -n ${_data} || ! -f ${_source}/uSync/${_version}/usync.config ]] || _files+=("uSync/${_version}/usync.config")
done
for _folder in ${_codeFolders[@]+"${_codeFolders[@]}"}; do
	[[ ! -d ${_source}/${_folder} ]] || _mirror+=("${_folder}")
done
# Settings are mirrored within each folder. Data is merged: nothing is deleted on the destination.
_rcloneMode=sync
_rsyncDelete=(--delete)
if [[ -n ${_data} ]]; then
	_rcloneMode=copy
	_rsyncDelete=()
fi
((${#_mirror[@]})) || { echo "Nothing to sync in ${_source}." >&2; exit 1; }

# Mirror each listed folder on its own, so nothing outside them is deleted.
# It runs inside an if, where set -e is off: every step stops it on its own.
push_environment() (
	local _path
	if [[ $1 == main ]]; then
		# The main site is on this server, in the folder its service runs from
		PROD_TARGET="" PROD_LOCAL_PATH=$(main_folder)
		_root=${PROD_LOCAL_PATH}
	else
		export VNS_TARGET=$1
		load_prod_settings
	fi
	if [[ ${PROD_TARGET} == windows ]]; then
		source lib/windows_prod.sh
		windows_smb_connect || exit 1
		for _path in "${_mirror[@]}"; do
			echo "${_path}"
			rclone "${_rcloneMode}" "${_source}/${_path}" "${_windowsTarget}/${_path}" --create-empty-src-dirs --modify-window 2s --stats 0 ${_dryRun[@]+"${_dryRun[@]}"} || exit 1
		done
		for _path in ${_files[@]+"${_files[@]}"}; do
			rclone copyto "${_source}/${_path}" "${_windowsTarget}/${_path}" --modify-window 2s --stats 0 ${_dryRun[@]+"${_dryRun[@]}"} || exit 1
		done
	elif [[ -n ${PROD_LOCAL_PATH} ]]; then
		for _path in "${_mirror[@]}"; do
			echo "${_path}"
			((${#_dryRun[@]})) || mkdir -p "${_root}/${_path}" || exit 1
			rsync -a ${_rsyncDelete[@]+"${_rsyncDelete[@]}"} ${_dryRun[@]+"${_dryRun[@]}"} "${_source}/${_path}/" "${_root}/${_path}/" || exit 1
		done
		for _path in ${_files[@]+"${_files[@]}"}; do
			rsync -a ${_dryRun[@]+"${_dryRun[@]}"} "${_source}/${_path}" "${_root}/${_path}" || exit 1
		done
		chown -R www-data:www-data "${_root}/uSync" "${_root}/Views" "${_root}/wwwroot" 2>/dev/null || true
	else
		ssh_for_target
		local _rsync=(rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" ${_dryRun[@]+"${_dryRun[@]}"})
		for _path in "${_mirror[@]}"; do
			echo "${_path}"
			"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} mkdir -p $(printf %q "${_root}/${_path}")" || exit 1
			"${_rsync[@]}" ${_rsyncDelete[@]+"${_rsyncDelete[@]}"} "${_source}/${_path}/" "${PROD_USER}@${PROD_IP}:${_root}/${_path}/" || exit 1
		done
		for _path in ${_files[@]+"${_files[@]}"}; do
			"${_rsync[@]}" "${_source}/${_path}" "${PROD_USER}@${PROD_IP}:${_root}/${_path}" || exit 1
		done
		"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} chown -R www-data:www-data $(printf %q "${_root}/uSync") $(printf %q "${_root}/Views") $(printf %q "${_root}/wwwroot") 2>/dev/null || true"
	fi
)

# True only when the server answers that the folder is gone. An unreachable server fails in the sync instead.
destination_missing() (
	[[ $1 != main ]] || exit 1
	export VNS_TARGET=$1
	load_prod_settings
	[[ -z ${PROD_LOCAL_PATH} && ${PROD_TARGET} != windows ]] || exit 1
	prod_exec "test -d $(printf %q "${_root}")"
	[[ $? -eq 1 ]]
)

_failed=0
for _id in "${_destinations[@]}"; do
	if destination_missing "${_id}"; then
		echo "${_id}: SKIPPED (the site no longer exists on the server)"
		continue
	fi
	echo
	echo "== Sync ${_what} to ${_id} =="
	if ! push_environment "${_id}"; then
		echo "${_id}: FAILED" >&2
		_failed=$((_failed + 1))
	fi
done

if ((_failed)); then
	echo "Sync finished with errors in ${_failed} environment(s)." >&2
	exit 1
fi
[[ -n ${_data} ]] || echo "Sync completed. uSync imports the settings when it is set to import at startup, or from the backoffice."
