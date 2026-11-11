#!/bin/bash
# Install the server-wide Apache configs every site has; run as root:
#   conf/vns-offline.conf  the page shown while a site's service does not answer
#   conf/vns-headers.conf  answers that do not tell others what the server runs
# If Apache does not accept them, the configs that were there before are put back.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d /etc/apache2/conf-available ]]; then
	echo "Apache not installed; skipping the server-wide configs."
	exit 0
fi
_backup=$(mktemp -d)
trap 'rm -rf "${_backup}"' EXIT
_names=(vns-offline vns-headers)
for _name in "${_names[@]}"; do
	[[ ! -f /etc/apache2/conf-available/${_name}.conf ]] || cp -p -- "/etc/apache2/conf-available/${_name}.conf" "${_backup}/"
done

mkdir -p /var/lib/vns/offline
install -m 644 -- lib/app_offline.htm /var/lib/vns/offline/app_offline.htm
a2enmod -q headers >/dev/null
for _name in "${_names[@]}"; do
	install -m 644 -- "conf/${_name}.conf" "/etc/apache2/conf-available/${_name}.conf"
	a2enconf -q "${_name}" >/dev/null
done
if ! apache2ctl configtest; then
	echo "Apache did not accept the server-wide configs; the previous ones are kept." >&2
	for _name in "${_names[@]}"; do
		if [[ -f ${_backup}/${_name}.conf ]]; then
			cp -p -- "${_backup}/${_name}.conf" "/etc/apache2/conf-available/${_name}.conf"
		else
			a2disconf -q "${_name}" >/dev/null || true
			rm -f -- "/etc/apache2/conf-available/${_name}.conf"
		fi
	done
	exit 1
fi
if systemctl is-active --quiet apache2; then
	systemctl reload apache2
fi
echo "Server-wide configs installed: the offline page, and headers that do not tell what the server runs."
