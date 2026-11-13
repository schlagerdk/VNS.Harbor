#!/bin/bash
# Usage: update_server.sh. It is now `harbor sql update-server`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql update-server "$@"
