#!/bin/bash
# Usage: uninstall_tools.sh. It is now `harbor sql uninstall-sql-tools`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql uninstall-sql-tools "$@"
