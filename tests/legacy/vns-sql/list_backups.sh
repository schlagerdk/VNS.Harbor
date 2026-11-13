#!/bin/bash
# Usage: list_backups.sh. It is now `harbor sql list-backups`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql list-backups "$@"
