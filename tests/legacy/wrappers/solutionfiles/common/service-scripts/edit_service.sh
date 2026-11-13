#!/bin/bash
# Edit the service, then reload and restart it.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site edit-service @SITE@
