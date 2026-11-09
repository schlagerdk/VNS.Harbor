#!/bin/bash
# Mirror Umbraco content folders (uSync, Views, css, scripts, media) from this site's publish folder to the Windows server.
# Each folder is mirrored (files missing here are deleted on the server), like robocopy /mir.
#
# Usage:
#   sync_prod.sh              Run the sync
#   sync_prod.sh --dry-run    Show what would change, without changing anything
#
# This file contains a password: keep it readable by root only (chmod 700).
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
cd ..
set -euo pipefail
source "@APACHE_DIR@/lib/windows_prod.sh"

# ---- Production settings (filled in by create_prod.sh, edit if needed) -----
PROD_WINDOWS_SHARE=@PROD_WINDOWS_SHARE@   # The IIS site's folder, e.g. //server/share/site-folder
PROD_SMB_USER=@PROD_SMB_USER@
PROD_SMB_PASSWORD=@PROD_SMB_PASSWORD@
PROD_SMB_DOMAIN=@PROD_SMB_DOMAIN@         # Empty if the server is not on a domain
# ---------------------------------------------------------------------------

_publishFolder=@PUBLISH_FOLDER@
_folders=(uSync Views wwwroot/css wwwroot/scripts wwwroot/media)
_rcloneOpts=(--create-empty-src-dirs --modify-window 2s --stats 0 -v)

case "${1:-}" in
	"") ;;
	--dry-run) _rcloneOpts+=(--dry-run); echo "DRY RUN: no files will be changed." ;;
	*) echo "Usage: $0 [--dry-run]" >&2; exit 1 ;;
esac

windows_smb_connect
cd "${_publishFolder}"

_failed=0
for _folder in "${_folders[@]}"; do
	# A missing source folder must never be treated as an empty one,
	# as the sync would then wipe the folder on the server.
	if [[ ! -d ${_folder} ]]; then
		echo "${_folder}: SKIPPED (not found in the publish folder)"
		continue
	fi
	if rclone sync "${_folder}" "${_windowsTarget}/${_folder}" "${_rcloneOpts[@]}"; then
		echo "${_folder}: OK"
	else
		echo "${_folder}: FAILED"
		_failed=$((_failed + 1))
	fi
done

if ((_failed)); then
	echo "Sync finished with errors in ${_failed} folder(s)." >&2
	exit 1
fi
echo "Sync completed."
