#!/bin/bash
# Issue the certificate again, for the Apache config as it is.
# It is a harbor command now; this script only calls it.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
exec "@APACHE_DIR@/../bin/harbor" site reissue-certificate @SITE@
