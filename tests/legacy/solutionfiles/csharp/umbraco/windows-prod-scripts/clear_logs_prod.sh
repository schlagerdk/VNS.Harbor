#!/bin/bash
# Clear the Umbraco logs on the Windows server. The site is offline briefly, as IIS keeps the log files open.
# This file contains a password: keep it readable by root only (chmod 700).
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
set -euo pipefail
source "@APACHE_DIR@/lib/windows_prod.sh"

# ---- Production settings (filled in by create_prod.sh, edit if needed) -----
PROD_WINDOWS_SHARE=@PROD_WINDOWS_SHARE@   # The IIS site's folder, e.g. //server/share/site-folder
PROD_SMB_USER=@PROD_SMB_USER@
PROD_SMB_PASSWORD=@PROD_SMB_PASSWORD@
PROD_SMB_DOMAIN=@PROD_SMB_DOMAIN@         # Empty if the server is not on a domain
PROD_OFFLINE_WAIT=5                       # Seconds IIS gets to release its files after app_offline.htm
# ---------------------------------------------------------------------------

on_exit() {
	local _status=$?
	# Always bring the site back up, also if deleting fails
	windows_bring_online || _status=1
	exit "${_status}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

windows_smb_connect
_logs=${_windowsTarget}/umbraco/Logs
if ! rclone lsf "${_logs}" --max-depth 1 >/dev/null 2>&1; then
	echo "Nothing to delete: umbraco/Logs not found"
	exit 0
fi

windows_take_offline
rclone purge "${_logs}"
echo "Deleted umbraco/Logs"
