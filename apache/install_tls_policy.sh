#!/bin/bash
# Install the shared Apache server-side TLS policy; run as root.
set -euo pipefail

if [[ ${1:-} != "" && ${1:-} != "--start-if-stopped" ]] || (( $# > 1 )); then
	echo "Usage: $0 [--start-if-stopped]" >&2
	exit 2
fi

if ! command -v apache2ctl >/dev/null 2>&1 && [[ ! -d /etc/apache2 ]]; then
	if [[ ${1:-} == "--start-if-stopped" ]]; then
		echo "Apache is not installed; cannot install TLS policy." >&2
		exit 1
	fi
	echo "Apache not installed; skipping shared TLS policy."
	exit 0
fi
if ! command -v apache2ctl >/dev/null 2>&1 || [[ ! -d /etc/apache2 ]]; then
	echo "Incomplete Apache installation; cannot install TLS policy." >&2
	exit 1
fi

source_conf="$(dirname "$0")/conf/vns-tls.conf"
available=/etc/apache2/conf-available/vns-tls.conf
enabled=/etc/apache2/conf-enabled/vns-tls.conf
backup="${available}.vns-backup-$$"
[[ -f ${source_conf} && -d /etc/apache2/conf-available && -d /etc/apache2/conf-enabled ]] || {
	echo "Apache TLS policy source or configuration directories are missing." >&2; exit 1;
}
if [[ -L ${available} || ( -e ${available} && ! -f ${available} ) ]]; then
	echo "Refusing to replace non-regular TLS policy: ${available}" >&2; exit 1
fi
if [[ -e ${enabled} || -L ${enabled} ]]; then
	if [[ ! -L ${enabled} || $(readlink "${enabled}") != ../conf-available/vns-tls.conf ]]; then
		echo "Refusing to replace unexpected enabled TLS policy: ${enabled}" >&2; exit 1
	fi
fi
if [[ -e ${backup} || -L ${backup} ]]; then
	echo "TLS policy backup already exists: ${backup}" >&2; exit 1
fi

had_config=0
had_enabled=0
enabled_ssl=0
reload_attempted=0
[[ -e ${available} ]] && had_config=1
[[ -L ${enabled} ]] && had_enabled=1
if (( had_config )); then cp -p -- "${available}" "${backup}"; fi

rollback() {
	local status=$?
	trap - EXIT
	if (( status != 0 )); then
		echo "TLS policy installation failed; restoring previous Apache configuration." >&2
		if (( had_config )); then
			mv -f -- "${backup}" "${available}" || echo "Could not restore ${available}; backup: ${backup}" >&2
		else
			rm -f -- "${available}"
		fi
		if (( ! had_enabled )); then rm -f -- "${enabled}"; fi
		if (( enabled_ssl )); then a2dismod ssl >/dev/null || echo "Could not disable ssl module after failure." >&2; fi
		if (( reload_attempted )); then
			if apache2ctl configtest && systemctl reload apache2; then
				echo "Restored Apache configuration reloaded." >&2
			else
				echo "Could not reload restored Apache configuration; check Apache manually." >&2
			fi
		fi
	else
		rm -f -- "${backup}"
	fi
	exit "${status}"
}
trap rollback EXIT

if ! a2query -m ssl >/dev/null 2>&1; then
	a2enmod ssl
	enabled_ssl=1
fi
install -m 644 -- "${source_conf}" "${available}"
if (( ! had_enabled )); then ln -s ../conf-available/vns-tls.conf "${enabled}"; fi

echo "Testing Apache TLS policy (TLS 1.2 and TLS 1.3 required)"
apache2ctl configtest
if systemctl is-active --quiet apache2; then
	reload_attempted=1
	systemctl reload apache2
elif [[ ${1:-} == "--start-if-stopped" ]]; then
	systemctl start apache2
else
	echo "Apache is stopped; TLS policy will be used when it starts."
fi
echo "Shared Apache TLS policy installed."
