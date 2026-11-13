#!/bin/bash
# Usage: sync_env.sh <site> <main|environment> <environment[,environment...]|all> [--dry-run]
#        sync_env.sh <site> <main|environment> <main|environment> --data <content,media> [--dry-run]
# It is now `harbor env sync`.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env sync "$@"
