#!/bin/bash
# Usage: change_binding_prod.sh [site] [new-domain] [aliases] [--yes]. It is now `harbor env binding` (VNS_TARGET chooses the environment).
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env binding "$@"
