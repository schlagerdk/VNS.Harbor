#!/bin/bash
# Usage: set_servername.sh (as root)
# Gives Apache a global ServerName: this server's own name. It is now `harbor server set-servername`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" server set-servername "$@"
