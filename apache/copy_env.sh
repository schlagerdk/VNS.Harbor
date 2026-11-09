#!/bin/bash
# Usage: copy_env.sh <main-site> <source-folder> <new-folder> <new-binding> [new-database]
# Copy an environment that has its own folder to a new site, and make the copy an environment of the same main site.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"
[[ $# -eq 4 || $# -eq 5 ]] || { echo "Usage: $0 <main-site> <source-folder> <new-folder> <new-binding> [new-database]" >&2; exit 1; }
_main=$1
_source=$2
_new=$3
shift 2
bash ./copy_site.sh "${_source}" "$@"
if ! bash ./adopt_site.sh "${_new}" "${_main}"; then
	echo "The copy ${_new} works, but it could not be moved under ${_main}. It is listed as a separate site." >&2
	exit 1
fi
