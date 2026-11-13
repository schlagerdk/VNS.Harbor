#!/bin/bash
# Usage: shrink_logs.sh. It is now `harbor sql shrink-logs`.
# The sa password, as set_sa_password.sh and updates set it here: sqlcmd gets it in SQLCMDPASSWORD
sa() { export SQLCMDPASSWORD=$4; }
sa -U sa -P '@SA_PASSWORD@' -i scripts/shrink_logs.sql
exec "$(dirname "$(readlink -f "$0")")/../../bin/harbor" sql shrink-logs "$@"
