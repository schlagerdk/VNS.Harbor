#!/bin/bash
# Usage: set_servername.sh (as root)
# Apache warns "Could not reliably determine the server's fully qualified domain name" unless a ServerName is
# set globally. This sets it to this server's own name. Nothing changes when one is already set.
set -euo pipefail
if grep -rqsE '^[[:space:]]*ServerName[[:space:]]' /etc/apache2/apache2.conf /etc/apache2/conf-enabled/; then
	exit 0
fi
_name=$(hostname -f 2>/dev/null || true)
[[ ${_name} =~ ^[A-Za-z0-9.-]+$ ]] || _name=$(hostname)
[[ ${_name} =~ ^[A-Za-z0-9.-]+$ ]] || _name=localhost
echo "Set Apache's global ServerName to ${_name}"
printf "# This server's own name, so Apache does not warn that it cannot determine it\nServerName %s\n" "${_name}" \
	>/etc/apache2/conf-available/vns-servername.conf
a2enconf -q vns-servername
