#!/bin/bash
# Usage: copy_site.sh <source> <new-folder> <new-binding> [new-database]. It is now `harbor site copy`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site copy "$@"
