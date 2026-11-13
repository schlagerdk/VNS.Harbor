#!/bin/bash
# Usage: data_sync.sh <site> settings <main|environment>
#        data_sync.sh <site> sync <main|environment> <destination[,destination...]> [--dry-run]
# It is now `harbor env data-sync`.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env data-sync "$@"
