#!/bin/bash
# Usage: install_tools.sh. It is now `harbor sql install-sql-tools`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql install-sql-tools "$@"
