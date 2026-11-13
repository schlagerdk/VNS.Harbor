#!/bin/bash
# Usage: move_env.sh <site> <environment>, with the destination in VNS_PROD_TARGET and the other VNS_PROD_* settings.
# It is now `harbor env move`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env move "$@"
