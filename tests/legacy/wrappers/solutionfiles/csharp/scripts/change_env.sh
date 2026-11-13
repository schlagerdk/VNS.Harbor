#!/bin/bash
# Usage: change_env.sh [Development|Test|Staging|Production]. The environment the service runs as.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site action @SITE@ change_env "${1:-}"
