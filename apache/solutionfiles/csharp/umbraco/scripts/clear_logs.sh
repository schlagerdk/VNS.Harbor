#!/bin/bash
# Delete Umbraco's logs, with the site stopped.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site clear-logs @SITE@
