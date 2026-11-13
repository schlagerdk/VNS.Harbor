#!/bin/bash
# The app folder owned by www-data: folders 755, files 644.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site fix-app-permissions @SITE@
