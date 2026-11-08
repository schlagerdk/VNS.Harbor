#!/bin/bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"

export TZ=":Europe/Copenhagen"
trap 'echo "SQL backup failed; inspect the preceding error. No successful completion was recorded." >&2' ERR

source ../lib/backup_permissions.sh
prepare_backup_permissions

echo "----- $(date '+%d.%m.%Y %H:%M') -------------------------------------------------------------------------------"
/opt/mssql-tools18/bin/sqlcmd -b -C -S LOCALHOST -U sa -P '@SA_PASSWORD@' -i ../scripts/backup_everything.sql
echo "Backup of everything done! ----------------------------------------------------- $(date '+%d.%m.%Y %H:%M') -----"