#!/bin/bash
# Usage: move_env.sh <site> <environment>, with the destination in VNS_PROD_TARGET:
#   local    VNS_PROD_LOCAL_PATH=/var/www/<name>. The folder is moved, and the service and Apache config follow it.
#   remote   VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT, _FOLDER, _BINDING, _ENVIRONMENT, _HTTPS
#   windows  VNS_PROD_SMB_SHARE, _SMB_USER, _SMB_PASSWORD, _SMB_DOMAIN, _BINDING, _ENVIRONMENT, _HTTPS
# To a server, settings and content are copied and the main site's project is published there. The local service and
# Apache config are removed only when that has worked. VNS_MOVE_DELETE_LOCAL=y then also deletes the local files.
# If a step fails, the environment runs here as before. Files already copied to the server are left there.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_rootsFolder=/var/lib/vns-roots
_site=${1:-}
validate_site_name "${_site}"
set_site_vars
export VNS_TARGET=${2:-}
[[ -n ${VNS_TARGET} ]] || { echo "Choose the environment to move." >&2; exit 1; }
load_prod_settings
if [[ -z ${PROD_LOCAL_PATH} || ${PROD_TARGET} == windows ]]; then
	echo "Only an environment on this server can be moved." >&2
	exit 1
fi

_oldId=${VNS_TARGET}
_oldRoot=${PROD_LOCAL_PATH%/}
_oldName=${PROD_FOLDER}
_oldUnit=$(normalize_dns_name "${_oldName}" unit)
_oldService=/etc/systemd/system/${_oldUnit}.service
_oldConf=/etc/apache2/sites-available/${_oldName}.conf
_oldScripts=$(dirname "${_prodScript}")
_destination=${VNS_PROD_TARGET:-}
[[ -d ${_oldRoot} ]] || { echo "The folder ${_oldRoot} does not exist." >&2; exit 1; }
[[ -f ${_oldService} ]] || { echo "The service of ${_oldName} does not exist." >&2; exit 1; }
[[ -f ${_oldConf} ]] || { echo "The Apache config of ${_oldName} does not exist." >&2; exit 1; }

remove_local_service() {
	systemctl stop "${_oldUnit}" || true
	systemctl disable "${_oldUnit}" || true
	rm -f "${_oldService}"
	a2dissite "${_oldName}.conf" >/dev/null 2>&1 || true
	rm -f "${_oldConf}"
	rm -rf "${_rootsFolder:?}/${_oldName:?}"
	systemctl daemon-reload
	apache2ctl configtest
	systemctl reload apache2
}

# ---- to another folder on this server
move_local() {
	# Not local: undo_local_move runs when the script exits and needs them
	_newRoot=${VNS_PROD_LOCAL_PATH%/}
	_newName=$(basename "${_newRoot}")
	if [[ ${_newRoot} != "${_rootPath}/${_newName}" ]]; then
		echo "The new folder must be ${_rootPath}/<name>." >&2
		exit 1
	fi
	validate_site_name "${_newName}"
	_newUnit=$(normalize_dns_name "${_newName}" unit)
	_newService=/etc/systemd/system/${_newUnit}.service
	_newConf=/etc/apache2/sites-available/${_newName}.conf
	if [[ -e ${_newRoot} || -e ${_newService} || -e ${_newConf} ]]; then
		echo "${_newName} already exists on this server." >&2
		exit 1
	fi

	_backup=$(mktemp -d)
	cp "${_oldService}" "${_backup}/service"
	cp "${_oldConf}" "${_backup}/conf"
	cp -a "${_oldScripts}" "${_backup}/scripts"

	# Runs on every exit: only an unfinished move is undone
	_moved=0
	undo_local_move() {
		local _status=$?
		if ((!_moved)); then
			echo "The move failed. Restoring ${_oldName}." >&2
			systemctl stop "${_newUnit}" 2>/dev/null || true
			systemctl disable "${_newUnit}" 2>/dev/null || true
			rm -f "${_newService}"
			a2dissite "${_newName}.conf" >/dev/null 2>&1 || true
			rm -f "${_newConf}"
			[[ ! -d ${_newRoot} || -e ${_oldRoot} ]] || mv "${_newRoot}" "${_oldRoot}"
			[[ ! -d ${_rootsFolder}/${_newName} || -e ${_rootsFolder}/${_oldName} ]] || mv "${_rootsFolder}/${_newName}" "${_rootsFolder}/${_oldName}"
			cp "${_backup}/service" "${_oldService}"
			cp "${_backup}/conf" "${_oldConf}"
			cp -a "${_backup}/scripts/." "${_oldScripts}/"
			systemctl daemon-reload || true
			systemctl enable "${_oldUnit}" >/dev/null 2>&1 || true
			a2ensite "${_oldName}.conf" >/dev/null 2>&1 || true
			apache2ctl configtest >/dev/null 2>&1 && systemctl reload apache2 || true
			systemctl start "${_oldUnit}" || true
		fi
		rm -rf "${_backup}"
		exit "${_status}"
	}
	trap undo_local_move EXIT

	echo "Stop ${_oldName}"
	systemctl stop "${_oldUnit}"
	echo "Move ${_oldRoot} to ${_newRoot}"
	mv "${_oldRoot}" "${_newRoot}"
	[[ ! -d ${_rootsFolder}/${_oldName} ]] || mv "${_rootsFolder}/${_oldName}" "${_rootsFolder}/${_newName}"

	echo "Update the service and the Apache config"
	systemctl disable "${_oldUnit}" >/dev/null 2>&1 || true
	a2dissite "${_oldName}.conf" >/dev/null 2>&1 || true
	# Only the folder and the name used for roots and logs change. The binding is left alone, also if it contains the name.
	OLD_ROOT=${_oldRoot} NEW_ROOT=${_newRoot} OLD_NAME=${_oldName} NEW_NAME=${_newName} perl -pe '
		s/\Q$ENV{OLD_ROOT}\E/$ENV{NEW_ROOT}/g;
		s/^(Description=.*\[)\Q$ENV{OLD_NAME}\E\]/$1$ENV{NEW_NAME}]/;
		s/^(SyslogIdentifier=)\Q$ENV{OLD_NAME}\E$/$1$ENV{NEW_NAME}/;
		s{vns-roots/\Q$ENV{OLD_NAME}\E\b}{vns-roots/$ENV{NEW_NAME}}g;' "${_backup}/service" >"${_newService}"
	OLD_ROOT=${_oldRoot} NEW_ROOT=${_newRoot} OLD_NAME=${_oldName} NEW_NAME=${_newName} perl -pe '
		s/\Q$ENV{OLD_ROOT}\E/$ENV{NEW_ROOT}/g;
		s{(/var/log/apache2/)\Q$ENV{OLD_NAME}\E-}{$1$ENV{NEW_NAME}-}g;' "${_backup}/conf" >"${_newConf}"
	rm -f "${_oldService}" "${_oldConf}"

	systemctl daemon-reload
	systemctl enable "${_newUnit}" >/dev/null
	a2ensite "${_newName}.conf" >/dev/null
	apache2ctl configtest
	systemctl reload apache2
	systemctl start "${_newUnit}"

	echo "Update the environment's settings"
	for _file in "${_oldScripts}"/publish_prod.sh "${_oldScripts}"/sync_prod.sh "${_oldScripts}"/clear_logs_prod.sh; do
		[[ -f ${_file} ]] || continue
		NEW_ROOT=${_newRoot} NEW_NAME=${_newName} perl -pi -e '
			s/^PROD_LOCAL_PATH=\S*/PROD_LOCAL_PATH=\x27$ENV{NEW_ROOT}\x27/;
			s/^PROD_FOLDER=\S*/PROD_FOLDER=\x27$ENV{NEW_NAME}\x27/;' "${_file}"
	done
	_moved=1
	echo "${_oldName} now runs as ${_newName} from ${_newRoot}."
	echo "The port, binding and certificate are unchanged."
}

# ---- to a remote Linux or Windows server
copy_content() (
	export VNS_TARGET=$1
	load_prod_settings
	local_folders=()
	for _folder in umbraco uSync Views wwwroot App_Data; do
		[[ -d ${_oldRoot}/${_folder} ]] && local_folders+=("${_folder}")
	done
	if [[ ${PROD_TARGET} == windows ]]; then
		source lib/windows_prod.sh
		windows_smb_connect
		rclone copy "${_oldRoot}" "${_windowsTarget}" --include '/appsettings*.json' --exclude '/appsettings-schema*.json' --modify-window 2s --stats 0
		for _folder in ${local_folders[@]+"${local_folders[@]}"}; do
			echo "Copy ${_folder}"
			rclone copy "${_oldRoot}/${_folder}" "${_windowsTarget}/${_folder}" --create-empty-src-dirs --modify-window 2s --stats 0
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
		"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} mkdir -p '${_root}'"
		_rsync=(rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync")
		shopt -s nullglob
		for _file in "${_oldRoot}"/appsettings*.json; do
			[[ ${_file##*/} == appsettings-schema* ]] || "${_rsync[@]}" "${_file}" "${PROD_USER}@${PROD_IP}:${_root}/"
		done
		for _folder in ${local_folders[@]+"${local_folders[@]}"}; do
			echo "Copy ${_folder}"
			"${_rsync[@]}" "${_oldRoot}/${_folder}" "${PROD_USER}@${PROD_IP}:${_root}/"
		done
	fi
)

move_to_server() {
	# Not local: undo_server_move runs when the script exits and needs them
	_moving=${_oldId}-moving
	_newScripts=${_siteRoot}/scripts/targets/${_moving}
	_finalId=${_oldId}
	local _number=2
	if [[ ${_oldId} == prod ]]; then
		# The older single environment moves into its own folder
		_finalId=$(printf '%s' "${VNS_PROD_ENVIRONMENT:-production}" | tr '[:upper:]' '[:lower:]')
		while [[ -e ${_siteRoot}/scripts/targets/${_finalId} ]]; do
			_finalId=$(printf '%s' "${VNS_PROD_ENVIRONMENT:-production}" | tr '[:upper:]' '[:lower:]')-${_number}
			_number=$((_number + 1))
		done
	fi
	if [[ -e ${_newScripts} ]]; then
		echo "An earlier move left ${_newScripts}. Remove it first." >&2
		exit 1
	fi

	# Runs on every exit: only an unfinished move is undone
	_moved=0
	undo_server_move() {
		local _status=$?
		if ((!_moved)); then
			echo "The move failed, so ${_oldName} keeps running on this server." >&2
			echo "Files already copied to the destination are left there." >&2
			rm -rf "${_newScripts}"
			rm -f "${_siteRoot}/.vns-publish/target-${_moving}.json"
			systemctl start "${_oldUnit}" || true
		fi
		exit "${_status}"
	}
	trap undo_server_move EXIT

	# Also checks the login, before anything is stopped
	load_main_site
	VNS_PROD_ID=${_moving} VNS_PROD_NO_PUBLISH=1 VNS_PROD_MODAL=1 run_create_prod
	unset VNS_PROD_PASSWORD VNS_PROD_SMB_PASSWORD

	echo "Stop ${_oldName}, so its database and files stay consistent while they are copied"
	systemctl stop "${_oldUnit}"
	copy_content "${_moving}"

	echo "Publish to the new server"
	VNS_MOVING=1 bash "${_newScripts}/publish_prod.sh"

	echo "Remove ${_oldName} from this server"
	_moved=1
	remove_local_service
	if [[ ${VNS_MOVE_DELETE_LOCAL:-} == y ]]; then
		echo "Delete the local files in ${_oldRoot}"
		rm -rf "${_oldRoot:?}"
	else
		echo "The local files are kept in ${_oldRoot}."
	fi

	if [[ ${_oldId} == prod ]]; then
		rm -f "${_siteRoot}/scripts/publish_prod.sh" "${_siteRoot}/scripts/sync_prod.sh" "${_siteRoot}/scripts/clear_logs_prod.sh"
		mv -f "${_siteRoot}/.vns-publish/prod.json" "${_siteRoot}/.vns-publish/target-${_finalId}.json" 2>/dev/null || true
	else
		rm -rf "${_oldScripts:?}"
	fi
	mv "${_newScripts}" "${_siteRoot}/scripts/targets/${_finalId}"
	if [[ -f ${_siteRoot}/.vns-publish/target-${_moving}.json ]]; then
		mv -f "${_siteRoot}/.vns-publish/target-${_moving}.json" "${_siteRoot}/.vns-publish/target-${_finalId}.json"
	fi
	echo "${_oldName} has moved. Point DNS for ${VNS_PROD_BINDING:-the binding} to the new server if you have not done so."
	echo "A certificate on this server for the old binding is not removed."
}

case "${_destination}" in
	local) move_local ;;
	remote | windows) move_to_server ;;
	*) echo "The destination must be local, remote or windows." >&2; exit 1 ;;
esac
