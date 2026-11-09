#!/bin/bash
# Usage: copy_env.sh <main-site> <source-folder> <new-folder> <new-binding> [new-database]
# Copy an environment that has its own folder to a new site, and make the copy an environment of the same main site.
# With VNS_COPY_REMOTE=1 the source is the remote Linux environment in VNS_TARGET: the copy is made on that server.
# With VNS_COPY_TO_LOCAL=1 as well, the copy is made on this server instead: settings and content are fetched from the
# server, and the main site's project is published to the copy. VNS_COPY_STOP=y stops the source during the fetch.
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
	if [[ ${VNS_COPY_TO_LOCAL:-} == 1 ]]; then
		_newBinding=$2
		_newDatabase=${3:-}
		_sourceUnit=$(normalize_dns_name "${_source}" unit)
		_sourceRoot=${_root}
		_sourceEnvironment=${PROD_ENVIRONMENT:-Production}
		_sourceHttps=${PROD_HTTPS:-y}
		_site=${_new}
		set_site_vars
		_newRoot=${_siteRoot}
		if [[ -e ${_newRoot} || -e ${_servicePath} || -e ${_configPath} ]]; then
			echo "${_new} already exists on this server." >&2
			exit 1
		fi
		_site=${_main}
		set_site_vars
		load_main_site
		_before=$(prod_targets)
		VNS_PROD_MODAL=1 VNS_PROD_NO_PUBLISH=1 VNS_PROD_TARGET=local VNS_PROD_LOCAL_PATH=${_newRoot} VNS_PROD_FOLDER=${_new} \
			VNS_PROD_BINDING=${_newBinding} VNS_PROD_ENVIRONMENT=${_sourceEnvironment} VNS_PROD_HTTPS=${_sourceHttps} run_create_prod
		_newId=""
		while IFS= read -r _id; do
			grep -qx "${_id}" <<<"${_before}" || _newId=${_id}
		done < <(prod_targets)
		[[ -n ${_newId} ]] || { echo "The new environment was not created." >&2; exit 1; }

		_ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new)
		if [[ -n ${PROD_PASSWORD} ]]; then
			command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
			export SSHPASS=${PROD_PASSWORD}
			_ssh=(sshpass -e "${_ssh[@]}")
		fi
		_sudo=""
		[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
		_rsync=(rsync -az -e "${_ssh[*]}" --rsync-path="${_sudo:+${_sudo} }rsync" --exclude '/umbraco/Logs/')
		_host=${PROD_USER}@${PROD_IP}

		# The source starts again also if the copy fails, and an unfinished copy is removed so it can be tried again
		_fetched=0
		_stopped=0
		finish_fetch() {
			local _status=$?
			start_source
			if ((!_fetched)); then
				echo "The copy failed. ${_source} is not changed, and the unfinished copy was removed." >&2
				rm -rf "${_siteRoot:?}/scripts/targets/${_newId:?}" "${_newRoot:?}"
			fi
			exit "${_status}"
		}
		start_source() {
			if ((_stopped)); then
				_stopped=0
				prod_exec "systemctl start $(printf %q "${_sourceUnit}")" || echo "WARNING: could not start ${_source} on ${PROD_IP} again." >&2
			fi
		}
		trap finish_fetch EXIT
		if [[ ${VNS_COPY_STOP:-} == y ]]; then
			echo "Stop ${_source} while its files are copied"
			_stopped=1
			prod_exec "systemctl stop $(printf %q "${_sourceUnit}")"
		fi
		mkdir -p "${_newRoot}"
		echo "Copy settings"
		"${_rsync[@]}" --exclude '/appsettings-schema*.json' --include '/appsettings*.json' --exclude '*' "${_host}:${_sourceRoot}/" "${_newRoot}/"
		for _folder in umbraco uSync Views wwwroot App_Data; do
			"${_ssh[@]}" "${_host}" "${_sudo} test -d $(printf %q "${_sourceRoot}/${_folder}")" || continue
			echo "Copy ${_folder}"
			"${_rsync[@]}" "${_host}:${_sourceRoot}/${_folder}" "${_newRoot}/"
		done
		start_source
		if [[ -n ${_newDatabase} || -n ${VNS_COPY_SQL:-} ]]; then
			python3 lib/copy_site.py update-connections "${_newRoot}" "${_newDatabase}"
		fi
		# A SQL Server on the source's machine stays there, unless a new connection was entered
		if [[ -z ${VNS_COPY_SQL:-} && $(python3 lib/copy_site.py repoint-sql "${_newRoot}" "${PROD_IP}") != 0 ]]; then
			echo "The SQL connection now points to ${PROD_IP}. That SQL Server must accept connections from this server."
		fi
		_fetched=1
		trap - EXIT

		unset VNS_COPY_SQL VNS_TARGET SSHPASS
		echo "Publish to ${_new}"
		if ! bash "${_siteRoot}/scripts/targets/${_newId}/publish_prod.sh"; then
			echo "The copy is in ${_newRoot}, but publishing failed. Fix the problem and publish environment ${_newId}, or delete it." >&2
			exit 1
		fi
		echo "${_source} is copied to ${_newRoot} as the environment ${_newId}."
		exit 0
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
