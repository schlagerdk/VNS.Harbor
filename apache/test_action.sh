#!/bin/bash
# Usage: test_action.sh <site> <issue_certificate|change_env> [environment|--yes]. It is now `harbor site action`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site action "$@"
