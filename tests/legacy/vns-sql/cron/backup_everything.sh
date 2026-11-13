#!/bin/bash
# Usage: backup_everything.sh. It is now `harbor sql backup-everything`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/backup_everything.sql
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../../bin/harbor" sql backup-everything "$@"
