#!/bin/bash
# Usage: change_binding.sh [folder] [new-domain] [aliases] [--yes]. It is now `harbor site binding`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site binding "$@"
