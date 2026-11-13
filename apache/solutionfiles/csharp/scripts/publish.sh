#!/bin/bash
# Usage: publish.sh [Debug|Release]   (default: @BUILD_CONFIG@)
# Publish this site into its local publish folder and restart the service. After create_prod.sh has run this script
# is called publish_local.sh.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site publish @SITE@ "${1:-@BUILD_CONFIG@}"
