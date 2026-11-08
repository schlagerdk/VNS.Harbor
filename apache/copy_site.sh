#!/bin/bash
# Usage: copy_site.sh <source> <new-folder> <new-binding>
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"
source lib/common.sh
[[ $# -eq 3 ]] || { echo "Usage: $0 <source> <new-folder> <new-binding>" >&2; exit 1; }
_source=$(normalize_dns_name "$1" site)
_site=$(normalize_dns_name "$2" site)
_binding=$(normalize_dns_name "$3" binding)
validate_binding "${_binding}"
set_site_vars
trap release_port_reservation EXIT
trap 'echo "FAILED: Copy is incomplete. Inspect the new site before retrying; the source was not changed." >&2' ERR
_sourceUnit=$(normalize_dns_name "${_source}" unit)
_portHttp=""
if [[ -f /etc/systemd/system/${_sourceUnit}.service ]]; then allocate_ports; fi
_https=0
if grep -qiE '^[[:space:]]*SSLEngine[[:space:]]+on' "/etc/apache2/sites-available/${_source}.conf"; then _https=1; fi
python3 lib/copy_site.py "${_source}" "${_site}" "${_binding}" "${_portHttp}"
mkdir -p "/var/lib/vns-roots/${_site}"
if ((_https)); then
	_final=$(mktemp)
	cp "${_configPath}" "${_final}"
	_docRoot="${_siteRoot}"
	issue_certificate
	cp "${_final}" "${_configPath}"
	rm -f "${_final}"
fi
apache2ctl configtest
a2ensite "${_site}.conf"
if [[ -f ${_servicePath} ]]; then
	systemctl daemon-reload
	if grep -q '^ExecStart=.*/dotnet ' "${_servicePath}"; then
		python3 "${_apacheDir}/lib/sql_startup.py" "${_serviceName}"
	fi
	systemctl enable "${_serviceName}"
	systemctl start "${_serviceName}"
	systemctl is-active --quiet "${_serviceName}"
fi
systemctl reload apache2
echo "Site copied: ${_source} -> ${_site} (${_binding})"
echo "Database connections were copied unchanged; no database was duplicated."
