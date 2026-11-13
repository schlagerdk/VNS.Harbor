#!/bin/bash
# Usage: setup_site.sh <folder> <binding|*> <y|n certificate> [Development|Test|Staging|Production]. It is now `harbor site setup`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site setup "$@"
