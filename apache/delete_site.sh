#!/bin/bash
# Usage: delete_site.sh [site] [y|n (delete certificates)] [all|test|prod], the environment in VNS_TARGET. It is now `harbor site delete`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site delete "$@"
