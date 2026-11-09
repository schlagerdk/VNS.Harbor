#!/bin/bash
# Usage: sync_env.sh <site> <main|environment> <environment|all>
# Sync content from the main site, or from one environment, to one or all of the site's other environments.
# With an environment as the source, its content is first copied to a temporary folder here.
# A source folder that does not exist is skipped by the sync: it never empties the destination.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_site=${1:-}
_from=${2:-}
_to=${3:-}
validate_site_name "${_site}"
set_site_vars
[[ -n ${_from} && -n ${_to} ]] || { echo "Usage: $0 <site> <main|environment> <environment|all>" >&2; exit 1; }
[[ ${_from} != "${_to}" ]] || { echo "The source and destination are the same." >&2; exit 1; }

_folders=(uSync Views wwwroot/css wwwroot/scripts)
_targets=()
while IFS= read -r _id; do _targets+=("${_id}"); done < <(prod_targets)
_known=" main ${_targets[*]:-} "
[[ ${_known} == *" ${_from} "* ]] || { echo "Unknown source: ${_from}" >&2; exit 1; }

_destinations=()
for _id in ${_targets[@]+"${_targets[@]}"}; do
	[[ ${_id} != "${_from}" ]] || continue
	[[ ${_to} == all || ${_to} == "${_id}" ]] && _destinations+=("${_id}")
done
((${#_destinations[@]})) || { echo "No environment to sync to." >&2; exit 1; }

_source=""
_workDir=""
trap '[[ -z ${_workDir} ]] || rm -rf "${_workDir}"' EXIT

# Copy the content of an environment to _source. Any error stops the sync: a partial copy must not be mirrored.
pull_environment() (
	export VNS_TARGET=$1
	load_prod_settings
	if [[ ${PROD_TARGET} == windows ]]; then
		source lib/windows_prod.sh
		windows_smb_connect
		for _folder in "${_folders[@]}"; do
			rclone lsf "${_windowsTarget}/${_folder}" --max-depth 1 >/dev/null 2>&1 || continue
			rclone copy "${_windowsTarget}/${_folder}" "${_source}/${_folder}" --create-empty-src-dirs --modify-window 2s --stats 0
		done
	elif [[ -n ${PROD_LOCAL_PATH} ]]; then
		for _folder in "${_folders[@]}"; do
			[[ -d ${_root}/${_folder} ]] || continue
			mkdir -p "${_source}/${_folder}"
			rsync -a "${_root}/${_folder}/" "${_source}/${_folder}/"
		done
	else
		_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
		if [[ -n ${PROD_PASSWORD} ]]; then
			command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
			export SSHPASS=${PROD_PASSWORD}
			_ssh=(sshpass -e "${_ssh[@]}")
		fi
		_sudo=""
		[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
		for _folder in "${_folders[@]}"; do
			"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} test -d '${_root}/${_folder}'" || continue
			mkdir -p "${_source}/${_folder}"
			rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "${PROD_USER}@${PROD_IP}:${_root}/${_folder}/" "${_source}/${_folder}/"
		done
	fi
)

if [[ ${_from} != main ]]; then
	_workDir=$(mktemp -d)
	_source=${_workDir}
	echo "Copy content from ${_from}"
	pull_environment "${_from}"
fi

# True only when the server answers that the folder is gone. An unreachable server fails in the sync instead.
destination_missing() (
	export VNS_TARGET=$1
	load_prod_settings
	[[ -z ${PROD_LOCAL_PATH} && ${PROD_TARGET} != windows ]] || exit 1
	prod_exec "test -d $(printf %q "${_root}")"
	[[ $? -eq 1 ]]
)

_failed=0
for _id in "${_destinations[@]}"; do
	_script=${_siteRoot}/scripts/targets/${_id}/sync_prod.sh
	[[ ${_id} != prod ]] || _script=${_siteRoot}/scripts/sync_prod.sh
	if [[ ! -f ${_script} ]]; then
		echo "${_id}: SKIPPED (no sync script)"
		continue
	fi
	if destination_missing "${_id}"; then
		echo "${_id}: SKIPPED (the site no longer exists on the server)"
		continue
	fi
	echo
	echo "== Sync to ${_id} =="
	if ! VNS_SYNC_SOURCE=${_source} bash "${_script}"; then
		echo "${_id}: FAILED" >&2
		_failed=$((_failed + 1))
	fi
done

if ((_failed)); then
	echo "Sync finished with errors in ${_failed} environment(s)." >&2
	exit 1
fi
echo "Sync completed."
