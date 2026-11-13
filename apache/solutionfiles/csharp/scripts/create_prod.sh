#!/bin/bash
# Add an environment to this site: a remote Linux server (SSH), a local folder, or an IIS site on a Windows server (SMB).
# Asks for its settings (or takes the admin's), makes scripts/targets/<environment>/ and publishes it. A destination
# that already holds a site is adopted as it is. Run it again to add another environment.
# It is now `harbor env create`, with the values this script was made with.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
	exec sudo "$0" "$@"
fi

# The admin runs a copy made for this run: it goes, as the old script removed itself
[[ ${VNS_PROD_MODAL:-} != 1 ]] || rm -f -- "$0"
R_APACHE_DIR="@APACHE_DIR@" exec "@APACHE_DIR@/../bin/harbor" env create @SITE@ --site-root "@SITE_ROOT@" --binding "@BINDING@" \
	--project "@PROJECT_NAME@" --publish-folder "@PUBLISH_FOLDER@" --admin-mail "@ADMIN_MAIL@" --kind "@CSHARP_KIND@" "$@"
