#!/bin/bash
# Usage: uninstall_server.sh. It is now `harbor sql uninstall-server`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql uninstall-server "$@"
