#!/bin/bash
# Usage: drop_db.sh [db name] [y = remove backup]. It is now `harbor sql drop`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/drop_db.sql
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql drop "$@"
