#!/bin/bash
# Usage: list_dbs.sh. It is now `harbor sql list-databases`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/list_dbs.sql
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql list-databases "$@"
