#!/bin/bash
# Usage: copy_site.sh <source> <new-folder> <new-binding>
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"
source lib/common.sh
[[ $# -eq 3 || $# -eq 4 ]] || { echo "Usage: $0 <source> <new-folder> <new-binding> [new-database]" >&2; exit 1; }
_source=$(normalize_dns_name "$1" site)
_site=$(normalize_dns_name "$2" site)
_binding=$(normalize_dns_name "$3" binding)
validate_binding "${_binding}"
set_site_vars
trap release_port_reservation EXIT
trap 'echo "FAILED: Copy is incomplete. Inspect the new site before retrying; the source was not changed." >&2' ERR
if [[ -f /var/lib/vns-managed/${_source} ]]; then
	echo "${_source} is an environment of the main site $(head -n 1 "/var/lib/vns-managed/${_source}") on $(sed -n 2p "/var/lib/vns-managed/${_source}"). Make the copy from there." >&2
	exit 1
fi
_sourceUnit=$(normalize_dns_name "${_source}" unit)
_portHttp=""
if [[ -f /etc/systemd/system/${_sourceUnit}.service ]]; then allocate_ports; fi
_https=0
if grep -qiE '^[[:space:]]*SSLEngine[[:space:]]+on' "/etc/apache2/sites-available/${_source}.conf"; then _https=1; fi
_databaseArgs=()
if [[ -n ${4:-} ]]; then _databaseArgs=(--database "$4"); fi
# A renamed project has no dll with the new name yet: it is built instead of just started
_project=${VNS_COPY_PROJECT:-}
_build=0
if [[ -n ${_project} && ! -f ${_rootPath}/${_source}/${_project}.csproj ]]; then _build=1; fi
_databaseArgs+=(--project "${_project}")
python3 lib/copy_site.py "${_source}" "${_site}" "${_binding}" "${_portHttp}" "${_databaseArgs[@]}"
_changedConnection=${VNS_COPY_SQL:+y}
unset VNS_COPY_SQL
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
	if ((_build)); then
		echo "Build ${_project}"
		_publishScript=${_siteRoot}/scripts/publish.sh
		[[ -f ${_publishScript} ]] || _publishScript=${_siteRoot}/scripts/publish_local.sh
		if ! bash "${_publishScript}"; then
			echo "The copy could not be built. It is in ${_siteRoot}: fix the build and run its Publish." >&2
			exit 1
		fi
	else
		systemctl start "${_serviceName}"
	fi
	systemctl is-active --quiet "${_serviceName}"
fi
systemctl reload apache2
echo "Site copied: ${_source} -> ${_site} (${_binding})"
if [[ -n ${4:-} || -n ${_changedConnection} ]]; then
	echo "SQL connections were updated. The database and login must already exist; neither was created."
else
	echo "Database connections were copied unchanged; no database was duplicated."
fi
