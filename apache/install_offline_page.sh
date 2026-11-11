#!/bin/bash
# Install the page Apache shows while a site's service does not answer (conf/vns-offline.conf); run as root.
# If Apache does not accept the config, the previous one is put back.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d /etc/apache2/conf-available ]]; then
	echo "Apache not installed; skipping the offline page."
	exit 0
fi
available=/etc/apache2/conf-available/vns-offline.conf
backup=$(mktemp)
had_config=0
if [[ -f ${available} ]]; then
	cp -p -- "${available}" "${backup}"
	had_config=1
fi

mkdir -p /var/lib/vns/offline
install -m 644 -- lib/app_offline.htm /var/lib/vns/offline/app_offline.htm
install -m 644 -- conf/vns-offline.conf "${available}"
a2enconf -q vns-offline >/dev/null
if ! apache2ctl configtest; then
	echo "Apache did not accept the offline page config; the previous config is kept." >&2
	if ((had_config)); then
		mv -f -- "${backup}" "${available}"
	else
		a2disconf -q vns-offline >/dev/null || true
		rm -f -- "${available}"
	fi
	exit 1
fi
rm -f -- "${backup}"
if systemctl is-active --quiet apache2; then
	systemctl reload apache2
fi
echo "Offline page installed: shown while a site's service does not answer."
