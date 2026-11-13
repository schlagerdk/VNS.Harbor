#!/bin/bash
# Usage: move_site.sh <site>, with the new server in VNS_MOVE_IP, _USER, _PASSWORD (empty = SSH key) and _SSH_PORT
#        move_site.sh --receive <folder> <SQL server>   on the new server: set up the sites that arrived
# It is now `harbor site move` and `harbor site receive`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
_harbor="$(dirname "$(readlink -f "$0")")/../bin/harbor"
if [[ ${1:-} == --receive ]]; then
	shift
	exec "${_harbor}" site receive "$@"
fi
exec "${_harbor}" site move "$@"
