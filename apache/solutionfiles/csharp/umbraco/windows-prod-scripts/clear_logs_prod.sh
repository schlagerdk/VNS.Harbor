#!/bin/bash
# Clear the Umbraco logs where this environment is. Its settings are the environment's own (publish_prod.sh).
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
_self=$(realpath "$0")
_folder=$(dirname "${_self}")
_site=${_folder%/scripts*}
_site=${_site##*/}
_id=prod
[[ ${_folder} != */scripts/targets/* ]] || _id=${_folder##*/}
VNS_TARGET=${_id} exec "@APACHE_DIR@/../bin/harbor" env clear-logs "${_site}"
