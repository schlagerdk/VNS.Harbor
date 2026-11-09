#!/bin/bash
# Usage: copy_env.sh <main-site> <source-folder> <new-folder> <new-binding> [new-database]
# Copy an environment that has its own folder to a new site, and make the copy an environment of the same main site.
# With VNS_COPY_REMOTE=1 the source is the remote Linux environment in VNS_TARGET: the copy is made on that server.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo /bin/bash "$(realpath "$0")" "$@"
cd "$(dirname "$0")"
[[ $# -eq 4 || $# -eq 5 ]] || { echo "Usage: $0 <main-site> <source-folder> <new-folder> <new-binding> [new-database]" >&2; exit 1; }
_main=$1
_source=$2
_new=$3
shift 2

if [[ ${VNS_COPY_REMOTE:-} == 1 ]]; then
	source lib/common.sh
	source lib/prod.sh
	_site=${_main}
	set_site_vars
	load_prod_settings
	if [[ -n ${PROD_LOCAL_PATH} || ${PROD_TARGET} == windows ]]; then
		echo "Only an environment on a remote Linux server is copied on that server." >&2
		exit 1
	fi
	# On stdin, so a new SQL password stays out of the server's process list
	{
		echo 'set -e'
		echo 'export VNS_COPY_MANAGED=1'
		[[ -z ${VNS_COPY_SQL:-} ]] || printf 'export VNS_COPY_SQL=%q\n' "${VNS_COPY_SQL}"
		printf 'bash /vns/apache/copy_site.sh %q' "${_source}"
		printf ' %q' "$@"
		echo
	} | prod_exec "bash -s"
	unset VNS_COPY_SQL VNS_TARGET
	if ! VNS_PROD_IP=${PROD_IP} VNS_PROD_USER=${PROD_USER} VNS_PROD_PASSWORD=${PROD_PASSWORD} VNS_PROD_SSH_PORT=${PROD_SSH_PORT} \
		VNS_PROD_FOLDER=${_new} bash ./register_env.sh "${_main}"; then
		echo "The copy ${_new} works on ${PROD_IP}, but it could not be added under ${_main}. Add it with Add environment." >&2
		exit 1
	fi
	exit 0
fi

bash ./copy_site.sh "${_source}" "$@"
if ! bash ./adopt_site.sh "${_new}" "${_main}"; then
	echo "The copy ${_new} works, but it could not be moved under ${_main}. It is listed as a separate site." >&2
	exit 1
fi
