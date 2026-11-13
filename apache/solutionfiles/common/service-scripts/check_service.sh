#!/bin/bash
# The service's status.
# It is a harbor command now; this script only calls it.
exec "@APACHE_DIR@/../bin/harbor" site check-service @SITE@
