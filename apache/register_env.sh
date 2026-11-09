#!/bin/bash
# Usage: register_env.sh <main-site>, with VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT and _FOLDER in the environment
# Make a site that already runs on a remote Linux server an environment of a main site on this server.
# Its binding, protocol and environment are read from the server. Nothing is published, and the only thing written
# on the server is the note that tells its admin that the site is managed from here.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

_site=${1:-}
validate_site_name "${_site}"
set_site_vars
load_main_site

PROD_TARGET="" PROD_LOCAL_PATH=""
PROD_IP=${VNS_PROD_IP:-}
PROD_USER=${VNS_PROD_USER:-}
PROD_PASSWORD=${VNS_PROD_PASSWORD:-}
PROD_SSH_PORT=${VNS_PROD_SSH_PORT:-22}
_folder=${VNS_PROD_FOLDER:-}
validate_site_name "${_folder}"
[[ ${PROD_IP} =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || { echo "Invalid server host." >&2; exit 1; }
[[ ${PROD_USER} =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,63}$ ]] || { echo "Invalid SSH username." >&2; exit 1; }
_unit=$(normalize_dns_name "${_folder}" unit)

# Read what is needed from the server. The answer is parsed, never executed.
_answer=$(printf 'FOLDER=%q\nUNIT=%q\n' "${_folder}" "${_unit}" | cat - <(cat <<'REMOTE'
root=/var/www/${FOLDER}
service=/etc/systemd/system/${UNIT}.service
conf=/etc/apache2/sites-available/${FOLDER}.conf
[[ -d ${root} ]] && echo "folder=yes"
compgen -G "${root}/*.csproj" >/dev/null && echo "project=yes"
[[ -e /var/lib/vns-managed/${FOLDER} ]] && echo "managed=$(tr '\n' ' ' </var/lib/vns-managed/"${FOLDER}")"
if [[ -f ${service} ]]; then
	echo "working=$(sed -n 's/^WorkingDirectory=//p' "${service}" | head -n 1)"
	echo "dll=$(sed -n 's/^ExecStart=//p' "${service}" | grep -oE '[^ "]+\.dll' | head -n 1)"
	echo "environment=$(sed -n 's/^Environment=ASPNETCORE_ENVIRONMENT=//p' "${service}" | head -n 1)"
fi
if [[ -f ${conf} ]]; then
	echo "binding=$(awk '$1 == "ServerName" { print $2; exit }' "${conf}")"
	grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${conf}" && echo "https=y"
fi
exit 0
REMOTE
) | prod_exec "bash -s") || { echo "Could not read ${_folder} on ${PROD_IP}." >&2; exit 1; }

_found="" _project="" _managed="" _working="" _dll="" _environment="" _binding2="" _https=n
while IFS='=' read -r _key _value; do
	case "${_key}" in
		folder) _found=${_value} ;;
		project) _project=${_value} ;;
		managed) _managed=${_value} ;;
		working) _working=${_value%/} ;;
		dll) _dll=$(basename "${_value}" .dll) ;;
		environment) _environment=${_value} ;;
		binding) _binding2=${_value} ;;
		https) _https=${_value} ;;
	esac
done <<<"${_answer}"

[[ -n ${_found} ]] || { echo "${_folder} was not found on ${PROD_IP}." >&2; exit 1; }
[[ -z ${_project} ]] || { echo "${_folder} has its own project on ${PROD_IP}, so it is a main site." >&2; exit 1; }
[[ -z ${_managed} ]] || { echo "${_folder} is already an environment of another main site: ${_managed}" >&2; exit 1; }
[[ -n ${_dll} ]] || { echo "${_folder} has no service that runs a dll on ${PROD_IP}." >&2; exit 1; }
[[ -n ${_binding2} ]] || { echo "${_folder} has no Apache config with a ServerName on ${PROD_IP}." >&2; exit 1; }
if [[ ${_working} != "/var/www/${_folder}" ]]; then
	echo "${_folder} runs from ${_working}. An environment on a server must run from /var/www/${_folder}." >&2
	exit 1
fi
_assembly=$(sed -n 's:.*<AssemblyName>\(.*\)</AssemblyName>.*:\1:p' "${_projects[0]}" | head -n 1)
_assembly=${_assembly:-${_projectName}}
if [[ ${_assembly} != "${_dll}" ]]; then
	echo "${_folder} runs ${_dll}.dll, but ${_site} builds ${_assembly}.dll. A publish would break the site." >&2
	exit 1
fi
case "${_environment}" in
	Development | Test | Staging | Production) ;;
	*) _environment=Production ;;
esac

VNS_PROD_MODAL=1 VNS_PROD_NO_PUBLISH=1 VNS_PROD_TARGET=remote VNS_PROD_IP=${PROD_IP} VNS_PROD_USER=${PROD_USER} \
	VNS_PROD_PASSWORD=${PROD_PASSWORD} VNS_PROD_SSH_PORT=${PROD_SSH_PORT} VNS_PROD_FOLDER=${_folder} \
	VNS_PROD_BINDING=${_binding2} VNS_PROD_ENVIRONMENT=${_environment} VNS_PROD_HTTPS=${_https} run_create_prod

if ! printf '%s\n%s\n' "${_site}" "$(hostname -f 2>/dev/null || hostname)" |
	prod_exec "bash -c 'mkdir -p /var/lib/vns-managed && cat >\"/var/lib/vns-managed/\$0\"' $(printf %q "${_folder}")"; then
	echo "WARNING: could not write the note on ${PROD_IP}. It is written when the admin here next reads the environment's status." >&2
fi
echo "${_folder} on ${PROD_IP} is now an environment of ${_site}. Nothing was published."
