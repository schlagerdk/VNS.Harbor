#!/bin/bash
# Prepare the standard local SQL backup folder and regular .bak files.
prepare_backup_permissions() {
	local _directory=/var/opt/mssql/backup _sudo=""
	[[ $EUID -eq 0 ]] || _sudo=sudo
	[[ ! -L ${_directory} ]] || { echo "Backup directory must not be a symbolic link." >&2; return 1; }
	${_sudo} mkdir -p "${_directory}" || return
	echo "Ensure SQL Server can read and overwrite backup files"
	${_sudo} chown mssql:mssql "${_directory}" || return
	${_sudo} chmod 750 "${_directory}" || return
	# Enumerate as root too: a normal caller may not be able to list this directory.
	${_sudo} /bin/bash -s -- "${_directory}" <<'BACKUP_PERMISSIONS'
set -euo pipefail
shopt -s nullglob nocaseglob
for file in "$1"/*.bak; do
	if [[ -L ${file} ]]; then
		echo "Backup files must not be symbolic links: ${file}" >&2
		exit 1
	fi
	[[ -f ${file} ]] || continue
	chown mssql:mssql "${file}"
	chmod 640 "${file}"
done
BACKUP_PERMISSIONS
}
