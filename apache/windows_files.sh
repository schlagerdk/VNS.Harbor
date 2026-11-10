#!/bin/bash
# Usage: windows_files.sh <site> < operation.json, with the environment in VNS_TARGET
# The file manager for an environment on a Windows server: lists, reads, saves, creates, renames, deletes,
# uploads and downloads files in the site's folder on the share, over SMB with rclone. lib/windows_files.py
# does the work and answers in JSON on stdout.
set -euo pipefail
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh
source lib/windows_prod.sh

_site=${1:-}
validate_site_name "${_site}"
set_site_vars
load_prod_settings
[[ ${PROD_TARGET} == windows ]] || { echo '{"error": "This is not an environment on a Windows server."}'; exit 1; }
# In this shell, as it sets up the connection in the environment. Its message becomes the JSON error.
_errors=$(mktemp)
trap 'rm -f "${_errors}"' EXIT
if ! windows_smb_connect >/dev/null 2>"${_errors}"; then
	python3 -c 'import json, sys; print(json.dumps({"error": open(sys.argv[1]).read().strip() or "Could not open the Windows share"}))' "${_errors}"
	exit 1
fi
rm -f "${_errors}"
trap - EXIT
exec python3 lib/windows_files.py "${_windowsTarget}"
