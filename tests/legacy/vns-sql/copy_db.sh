#!/bin/bash
# Usage: copy_db.sh [old db name] [new db name]. It is now `harbor sql copy`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/copy_db.sql
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql copy "$@"
