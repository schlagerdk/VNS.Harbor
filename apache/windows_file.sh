#!/bin/bash
# Usage: windows_file.sh <site> <read|save> <file>, with the environment in VNS_TARGET
# Read a settings file of a Windows environment to stdout, or save it from stdin. Only web.config and
# appsettings*.json in the site's folder can be read or saved. IIS restarts the app when web.config changes.
set -euo pipefail
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh
source lib/windows_prod.sh

_site=${1:-}
_action=${2:-}
_file=${3:-}
validate_site_name "${_site}"
set_site_vars
load_prod_settings
[[ ${PROD_TARGET} == windows ]] || { echo "This is not an environment on a Windows server." >&2; exit 1; }
if [[ ! ${_file} =~ ^(web\.config|appsettings(\.[A-Za-z0-9_-]+)?\.json)$ ]]; then
	echo "Only web.config and appsettings*.json can be edited." >&2
	exit 1
fi
windows_smb_connect >/dev/null
case "${_action}" in
	read) rclone cat "${_windowsTarget}/${_file}" ;;
	save) rclone rcat "${_windowsTarget}/${_file}" ;;
	*) echo "Unknown action: ${_action}" >&2; exit 1 ;;
esac
