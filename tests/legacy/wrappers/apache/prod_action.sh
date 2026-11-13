#!/bin/bash
# Usage: prod_action.sh <site> <issue_certificate|change_env> [environment|--yes], the environment in VNS_TARGET. It is now `harbor env action`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env action "$@"
