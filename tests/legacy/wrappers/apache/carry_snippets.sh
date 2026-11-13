#!/bin/bash
# Usage: carry_snippets.sh <site>, with the environment in VNS_TARGET (prod when it is not set)
# It is now `harbor env carry-snippets`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env carry-snippets "$@"
