#!/bin/bash
# Usage: create_db.sh [db name]. It is now `harbor sql create`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/create_db.sql
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql create "$@"
