#!/bin/bash
# Usage: install_server.sh. It is now `harbor sql install-server`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql install-server "$@"
