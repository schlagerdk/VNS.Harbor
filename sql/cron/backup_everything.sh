#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"

export TZ=":Europe/Copenhagen"
trap 'echo "SQL backup failed; inspect the preceding error. No successful completion was recorded." >&2' ERR

_backupDirectory=/var/opt/mssql/backup
[[ ! -L ${_backupDirectory} ]] || { echo "Backup directory must not be a symbolic link." >&2; exit 1; }
mkdir -p "${_backupDirectory}"
echo "Ensure SQL Server can write the backup directory and overwrite existing .bak files"
chown mssql:mssql "${_backupDirectory}"
chmod 750 "${_backupDirectory}"
shopt -s nullglob nocaseglob
for _backup in "${_backupDirectory}"/*.bak; do
	if [[ -L ${_backup} ]]; then
		echo "Backup files must not be symbolic links: ${_backup}" >&2
		exit 1
	fi
	[[ -f ${_backup} ]] || continue
	chown mssql:mssql "${_backup}"
	chmod 640 "${_backup}"
done

echo "----- $(date '+%d.%m.%Y %H:%M') -------------------------------------------------------------------------------"
/opt/mssql-tools18/bin/sqlcmd -b -C -S LOCALHOST -U sa -P '@SA_PASSWORD@' -i ../scripts/backup_everything.sql
echo "Backup of everything done! ----------------------------------------------------- $(date '+%d.%m.%Y %H:%M') -----"