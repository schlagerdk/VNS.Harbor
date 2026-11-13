#!/bin/bash
# Usage: adopt_site.sh <site> <main-site>                    Make a published site an environment of its main site
#        adopt_site.sh --release <main-site> <environment>   Make it a separate site again
# It is now `harbor site adopt` and `harbor site release`.
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
_harbor="$(dirname "$(readlink -f "$0")")/../bin/harbor"
if [[ ${1:-} == --release ]]; then
	shift
	exec "${_harbor}" site release "$@"
fi
exec "${_harbor}" site adopt "$@"
