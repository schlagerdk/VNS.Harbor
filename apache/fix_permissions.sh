#!/bin/bash
# Usage: fix_permissions.sh <site> [test|prod]. It is now `harbor site fix-permissions`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site fix-permissions "$@"
