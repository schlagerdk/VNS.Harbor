#!/bin/bash
# Usage: move_env.sh <site> <environment>, with the destination in VNS_PROD_TARGET:
#   local    VNS_PROD_LOCAL_PATH=/var/www/<name>, _BINDING, _ENVIRONMENT, _HTTPS
#   remote   VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT, _FOLDER, _BINDING, _ENVIRONMENT, _HTTPS
#   windows  VNS_PROD_SMB_SHARE, _SMB_USER, _SMB_PASSWORD, _SMB_DOMAIN, _BINDING, _ENVIRONMENT, _HTTPS
# The environment can be on this server or on a remote Linux server.
# On this server to this server, the folder is moved and the service and Apache config follow it.
# Otherwise the whole folder is copied, also the build, so the site runs the same version afterwards. It is only built
# for Windows and for another CPU architecture. A database in SQL Server stays where it is: a connection to the source's
# own machine is pointed at that machine. A SQLite database is part of the files.
# The source's service and Apache config are removed only when that has worked. VNS_MOVE_DELETE_LOCAL=y then also
# deletes its files. If a step fails, the source runs as before. Files already copied to the destination are left there.
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
if [[ ${PROD_TARGET} == windows ]]; then
	echo "An environment on a Windows server cannot be migrated." >&2
	exit 1
fi
_sourceRemote=0
[[ -n ${PROD_LOCAL_PATH} ]] || _sourceRemote=1

_oldId=${VNS_TARGET}
_oldRoot=${_root%/}
_oldName=${PROD_FOLDER}
_oldUnit=$(normalize_dns_name "${_oldName}" unit)
_oldService=/etc/systemd/system/${_oldUnit}.service
_oldConf=/etc/apache2/sites-available/${_oldName}.conf
_oldScripts=$(dirname "${_prodScript}")
_destination=${VNS_PROD_TARGET:-}
if ((_sourceRemote)); then
	prod_exec "test -d $(printf %q "${_oldRoot}")" || { echo "${_oldRoot} was not found on ${PROD_IP}." >&2; exit 1; }
else
	[[ -d ${_oldRoot} ]] || { echo "The folder ${_oldRoot} does not exist." >&2; exit 1; }
	[[ -f ${_oldService} ]] || { echo "The service of ${_oldName} does not exist." >&2; exit 1; }
	[[ -f ${_oldConf} ]] || { echo "The Apache config of ${_oldName} does not exist." >&2; exit 1; }
fi

source_control() {
	if ((_sourceRemote)); then
		prod_exec "systemctl $1 $(printf %q "${_oldUnit}")"
	else
		systemctl "$1" "${_oldUnit}"
	fi
}

remove_remote_source() {
	printf 'UNIT=%q\nNAME=%q\nROOT=%q\nDELETE_FILES=%q\n' "${_oldUnit}" "${_oldName}" "${_oldRoot}" "${VNS_MOVE_DELETE_LOCAL:-}" | cat - <(cat <<'REMOTE'
systemctl stop "${UNIT}" || true
systemctl disable "${UNIT}" || true
rm -f "/etc/systemd/system/${UNIT}.service" "/etc/apache2/sites-enabled/${NAME}.conf" "/etc/apache2/sites-available/${NAME}.conf"
rm -rf "/var/lib/vns-roots/${NAME}"
rm -f "/var/lib/vns-managed/${NAME}"
[[ ${DELETE_FILES} != y ]] || rm -rf "${ROOT:?}"
systemctl daemon-reload
apache2ctl configtest
systemctl reload apache2
REMOTE
) | prod_exec "bash -s"
}

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
	echo "${_oldName} now runs as ${_newName} from ${_newRoot}. The port is unchanged."

	local _oldBinding _newBinding=${VNS_PROD_BINDING:-} _aliases
	_oldBinding=$(awk '$1 == "ServerName" { print $2; exit }' "${_newConf}")
	if [[ -n ${_newBinding} && ${_newBinding,,} != "${_oldBinding,,}" ]]; then
		echo "Change the binding to ${_newBinding}"
		_aliases=$(server_aliases_from_config "${_newConf}" "${_oldBinding}")
		if ! bash ./change_binding_prod.sh "${_site}" "${_newBinding}" "${_aliases}" --yes; then
			echo "The site was moved, but its binding could not be changed. Use Change bindings on the environment." >&2
			exit 1
		fi
		# change_binding_prod.sh changes appsettings.json
		_scheme=http
		if grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${_newConf}"; then _scheme=https; fi
		for _file in "${_newRoot}"/appsettings*.json; do
			[[ -f ${_file} ]] || continue
			sed -i.vns-backup -E "s|(\"UmbracoApplicationUrl\"[[:space:]]*:[[:space:]]*\")[^\"]*|\1${_scheme}://${_newBinding}|" "${_file}"
			rm -f "${_file}.vns-backup"
		done
		systemctl restart "${_newUnit}"
	fi
}

# ---- to a remote Linux or Windows server
# The source's folder as rsync sees it
fetch_source() {
	_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
	if [[ -n ${PROD_PASSWORD} ]]; then
		command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
		export SSHPASS=${PROD_PASSWORD}
		_ssh=(sshpass -e "${_ssh[@]}")
	fi
	_sudo=""
	[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
	rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "${PROD_USER}@${PROD_IP}:${_oldRoot}/" "${_contentRoot}/"
	unset SSHPASS
}

# The whole folder, to this server or a remote Linux server
copy_everything() (
	export VNS_TARGET=$1
	load_prod_settings
	if [[ -n ${PROD_LOCAL_PATH} ]]; then
		mkdir -p "${_root}"
		rsync -a "${_contentRoot}/" "${_root}/"
		exit 0
	fi
	_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
	if [[ -n ${PROD_PASSWORD} ]]; then
		command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
		export SSHPASS=${PROD_PASSWORD}
		_ssh=(sshpass -e "${_ssh[@]}")
	fi
	_sudo=""
	[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
	"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} mkdir -p $(printf %q "${_root}")"
	rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "${_contentRoot}/" "${PROD_USER}@${PROD_IP}:${_root}/"
)

# A SQL Server on the source's machine stays there: the settings at the destination are pointed at that machine.
# Only the copy changes. The server must accept connections from the destination.
repoint_destination_sql() (
	export VNS_TARGET=$1
	_sourceServer=${_sourceHost}
	load_prod_settings
	if [[ ${PROD_TARGET} != windows ]]; then
		if [[ -n ${PROD_LOCAL_PATH} && ${_sourceRemote} == 0 ]] || [[ -z ${PROD_LOCAL_PATH} && ${PROD_IP} == "${_sourceServer}" ]]; then
			exit 0
		fi
	fi
	_settings=$(mktemp -d)
	trap 'rm -rf "${_settings}"' EXIT
	cp "${_contentRoot}"/appsettings*.json "${_settings}/" 2>/dev/null || exit 0
	_changed=$(python3 lib/copy_site.py repoint-sql "${_settings}" "${_sourceServer}")
	[[ ${_changed} == 0 ]] || echo "The SQL connection now points to ${_sourceServer}. That SQL Server must accept connections from the new server."
	if [[ ${PROD_TARGET} != windows && ${_changed} == 0 ]]; then
		exit 0
	fi
	if [[ ${PROD_TARGET} == windows ]]; then
		# Publishing to Linux sets the URL on the server. On Windows it is set here, as a publish keeps existing settings.
		_scheme=https
		[[ ${PROD_HTTPS} != n ]] || _scheme=http
		echo "Set UmbracoApplicationUrl to ${_scheme}://${PROD_BINDING}"
		for _file in "${_settings}"/appsettings*.json; do
			python3 lib/windows_prod.py set-url "${_file}" "${_scheme}://${PROD_BINDING}" "${PROD_HTTPS}"
		done
		source lib/windows_prod.sh
		windows_smb_connect
		rclone copy "${_settings}" "${_windowsTarget}" --modify-window 2s --stats 0
	elif [[ -n ${PROD_LOCAL_PATH} ]]; then
		cp "${_settings}"/appsettings*.json "${_root}/"
	else
		_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
		if [[ -n ${PROD_PASSWORD} ]]; then
			export SSHPASS=${PROD_PASSWORD}
			_ssh=(sshpass -e "${_ssh[@]}")
		fi
		_sudo=""
		[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
		rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" "${_settings}/" "${PROD_USER}@${PROD_IP}:${_root}/"
	fi
)

server_architecture() (
	export VNS_TARGET=$1
	load_prod_settings
	if [[ -n ${PROD_LOCAL_PATH} ]]; then uname -m; else prod_exec "uname -m"; fi
)

copy_content() (
	export VNS_TARGET=$1
	load_prod_settings
	local_folders=()
	for _folder in umbraco uSync Views wwwroot App_Data; do
		[[ -d ${_contentRoot}/${_folder} ]] && local_folders+=("${_folder}")
	done
	if [[ ${PROD_TARGET} == windows ]]; then
		source lib/windows_prod.sh
		windows_smb_connect
		rclone copy "${_contentRoot}" "${_windowsTarget}" --include '/appsettings*.json' --exclude '/appsettings-schema*.json' --modify-window 2s --stats 0
		for _folder in ${local_folders[@]+"${local_folders[@]}"}; do
			echo "Copy ${_folder}"
			rclone copy "${_contentRoot}/${_folder}" "${_windowsTarget}/${_folder}" --create-empty-src-dirs --modify-window 2s --stats 0
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
		for _file in "${_contentRoot}"/appsettings*.json; do
			[[ ${_file##*/} == appsettings-schema* ]] || "${_rsync[@]}" "${_file}" "${PROD_USER}@${PROD_IP}:${_root}/"
		done
		for _folder in ${local_folders[@]+"${local_folders[@]}"}; do
			echo "Copy ${_folder}"
			"${_rsync[@]}" "${_contentRoot}/${_folder}" "${PROD_USER}@${PROD_IP}:${_root}/"
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
	if [[ ${_destination} == local ]]; then
		local _newName _newLocalRoot=${VNS_PROD_LOCAL_PATH%/}
		_newName=$(basename "${_newLocalRoot}")
		if [[ ${_newLocalRoot} != "${_rootPath}/${_newName}" || -e ${_newLocalRoot} ]]; then
			echo "The new folder must be a new folder directly under ${_rootPath}." >&2
			exit 1
		fi
	elif [[ ${_destination} == remote && ${_sourceRemote} == 1 && ${VNS_PROD_IP:-} == "${PROD_IP}" && ${VNS_PROD_FOLDER:-} == "${_oldName}" ]]; then
		echo "The environment is already in that folder on that server." >&2
		exit 1
	fi
	_contentRoot=${_oldRoot}
	_fetchFolder=""
	if ((_sourceRemote)); then
		_sourceHost=${PROD_IP}
	else
		_sourceHost=$(hostname -I 2>/dev/null | awk '{ print $1 }' || true)
		_sourceHost=${_sourceHost:-$(hostname -f 2>/dev/null || hostname)}
	fi

	# Runs on every exit: only an unfinished move is undone
	_moved=0
	undo_server_move() {
		local _status=$?
		if ((!_moved)); then
			echo "The migration failed, so ${_oldName} keeps running where it was." >&2
			echo "Files already copied to the destination are left there." >&2
			rm -rf "${_newScripts}"
			rm -f "${_siteRoot}/.vns-publish/target-${_moving}.json"
			source_control start || echo "WARNING: could not start ${_oldName} again." >&2
		fi
		[[ -z ${_fetchFolder} ]] || rm -rf "${_fetchFolder}"
		exit "${_status}"
	}
	trap undo_server_move EXIT

	# Also checks the login, before anything is stopped
	load_main_site
	VNS_PROD_ID=${_moving} VNS_PROD_NO_PUBLISH=1 VNS_PROD_MODAL=1 run_create_prod
	unset VNS_PROD_PASSWORD VNS_PROD_SMB_PASSWORD

	echo "Stop ${_oldName}, so its database and files stay consistent while they are copied"
	source_control stop
	if ((_sourceRemote)); then
		# The servers have no access to each other, so the files go through this server
		_fetchFolder=$(mktemp -d)
		_contentRoot=${_fetchFolder}
		echo "Fetch ${_oldRoot} from ${PROD_IP}"
		fetch_source
	fi

	if [[ ${_destination} == windows ]]; then
		# Windows runs another build, so only settings and content are copied
		copy_content "${_moving}"
		repoint_destination_sql "${_moving}"
		echo "Publish to the new server"
		VNS_MOVING=1 bash "${_newScripts}/publish_prod.sh"
	else
		echo "Copy everything to the destination"
		copy_everything "${_moving}"
		repoint_destination_sql "${_moving}"
		_sourceArch=$(if ((_sourceRemote)); then prod_exec "uname -m"; else uname -m; fi)
		if [[ ${_sourceArch} == "$(server_architecture "${_moving}")" ]]; then
			echo "Create the service and Apache config"
			VNS_PUBLISH_TRACKED=target-${_moving} VNS_MIGRATE_ONLY=1 bash "${_newScripts}/publish_prod.sh"
		else
			echo "The destination has another CPU architecture, so the project is built for it"
			VNS_MOVING=1 bash "${_newScripts}/publish_prod.sh"
		fi
	fi

	echo "Remove ${_oldName} from where it was"
	_moved=1
	if ((_sourceRemote)); then
		remove_remote_source
		[[ ${VNS_MOVE_DELETE_LOCAL:-} == y ]] || echo "The files are kept in ${_oldRoot} on ${PROD_IP}."
	else
		remove_local_service
		if [[ ${VNS_MOVE_DELETE_LOCAL:-} == y ]]; then
			echo "Delete the local files in ${_oldRoot}"
			rm -rf "${_oldRoot:?}"
		else
			echo "The local files are kept in ${_oldRoot}."
		fi
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
	[[ -z ${_fetchFolder} ]] || rm -rf "${_fetchFolder}"
	echo "${_oldName} has moved. Point DNS for ${VNS_PROD_BINDING:-the binding} to the new server if you have not done so."
	echo "A certificate on this server for the old binding is not removed."
}

case "${_destination}" in
	local) if ((_sourceRemote)); then move_to_server; else move_local; fi ;;
	remote | windows) move_to_server ;;
	*) echo "The destination must be local, remote or windows." >&2; exit 1 ;;
esac
