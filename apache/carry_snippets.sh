#!/bin/bash
# Usage: carry_snippets.sh <site>, with the environment in VNS_TARGET (prod when it is not set)
# Make a Linux environment's Apache config carry the main site's config snippets (Allowed IPs, rewrites and the
# like), as the admin's config editor put them in the main site's config. publish_prod.sh runs it at every publish.
# They are written between VNS's BEGIN and END lines and made equal to the main site's: rules written by hand stay.
# The helper is sent along, so the server need not have this version of VNS. Apache tests the result before it is
# used; a config it does not accept is put back as it was. A Windows environment gets them in its own publish.
set -euo pipefail
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh
_site=${1:-}
validate_site_name "${_site}"
set_site_vars
load_prod_settings
[[ ${PROD_TARGET:-} != windows ]] || exit 0
validate_site_name "${PROD_FOLDER}"
_mainConf="/etc/apache2/sites-available/${_site}.conf"
[[ -f ${_mainConf} ]] || exit 0

echo "Carry the main site's config snippets"
_snippets=$(python3 lib/config_snippets.py read "${_mainConf}" | base64 -w0)
_code=$(base64 -w0 <lib/config_snippets.py)
prod_run "SITE=${PROD_FOLDER} SNIPPETS=${_snippets} SNIPPET_CODE=${_code}" <<'REMOTE'
set -euo pipefail
conf=/etc/apache2/sites-available/${SITE}.conf
[[ -f ${conf} ]] || exit 0
code=$(mktemp)
backup=$(mktemp)
trap 'rm -f "${code}" "${backup}"' EXIT
printf '%s' "${SNIPPET_CODE}" | base64 -d >"${code}"
cp -p "${conf}" "${backup}"
result=$(python3 "${code}" apache "${conf}" "$(printf '%s' "${SNIPPETS}" | base64 -d)")
if [[ ${result} != changed ]]; then
	echo "The config snippets are already the main site's"
elif apache2ctl configtest 2>/dev/null; then
	systemctl reload apache2
	echo "The config snippets are now the main site's"
else
	cp -p "${backup}" "${conf}"
	echo "Apache did not accept the config snippets, so the config is left as it was:" >&2
	apache2ctl configtest >&2 || true
fi
REMOTE
