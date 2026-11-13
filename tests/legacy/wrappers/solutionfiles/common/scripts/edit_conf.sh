#!/bin/bash
# Edit the Apache config, then test it and reload Apache.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site edit-conf @SITE@
