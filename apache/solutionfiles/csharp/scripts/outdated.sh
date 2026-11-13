#!/bin/bash
# The project's outdated packages, and an upgrade if wanted.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site outdated @SITE@
