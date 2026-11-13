#!/bin/bash
# Usage: add_to_crontab.sh. It is now `harbor sql add-to-crontab`.
exec "$(dirname "$(readlink -f "$0")")/../../bin/harbor" sql add-to-crontab "$@"
