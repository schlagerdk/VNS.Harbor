#!/bin/bash
# Helpers for the environments of a site. Each has its settings in <site>/scripts/targets/<id>/publish_prod.sh,
# or in <site>/scripts/publish_prod.sh for an environment made before several were possible (id "prod").
# Needs _siteRoot (set_site_vars). Source after lib/common.sh. VNS_TARGET selects the environment.

# prod_targets: print the id of every environment of this site
prod_targets() {
	local _folder
	[[ ! -f ${_siteRoot}/scripts/publish_prod.sh ]] || echo prod
	for _folder in "${_siteRoot}"/scripts/targets/*/; do
		[[ -f ${_folder}publish_prod.sh ]] || continue
		basename "${_folder}"
	done
}

# load_main_site: read what create_prod.sh is rendered with from the site in _site
load_main_site() {
	local _mail
	_projects=("${_siteRoot}"/*.csproj)
	if [[ ! -f ${_projects[0]} || ${#_projects[@]} -ne 1 ]]; then
		echo "${_site} needs exactly one project." >&2
		exit 1
	fi
	_projectName=$(basename "${_projects[0]}" .csproj)
	_binding=$(awk '$1 == "ServerName" { print $2; exit }' "${_configPath}")
	[[ -n ${_binding} ]] || { echo "${_site} has no ServerName." >&2; exit 1; }
	_mail=$(awk '$1 == "ServerAdmin" { print $2; exit }' "${_configPath}")
	_adminMail=${_mail:-${_adminMail}}
	_publishFolder=$(sed -n 's/^WorkingDirectory=//p' "${_servicePath}" | head -n 1)
	_publishFolder=${_publishFolder%/}
	if [[ ${_publishFolder} == "${_siteRoot}" || -z ${_publishFolder} ]]; then
		_publishFolder=.
	else
		_publishFolder=$(basename "${_publishFolder}")
	fi
	_csharpKind=empty
	if grep -qs "Umbraco.Cms" "${_projects[0]}"; then _csharpKind=umbraco; fi
}

# run_create_prod: run the current create_prod.sh template with the VNS_PROD_* settings in the environment
run_create_prod() {
	local _script
	_script=$(mktemp)
	render "${_solutionFolder}/csharp/scripts/create_prod.sh" >"${_script}"
	bash "${_script}"
}

# load_prod_settings: read the PROD_* assignments (the rest of publish_prod.sh is not executed)
# and set _target (description) and _root (site folder on the production target)
load_prod_settings() {
	local _id=${VNS_TARGET:-prod} _script="${_siteRoot}/scripts/publish_prod.sh"
	if [[ ${_id} != prod ]]; then
		[[ ${_id} =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "Invalid environment: ${_id}" >&2; exit 1; }
		_script="${_siteRoot}/scripts/targets/${_id}/publish_prod.sh"
	fi
	if [[ ! -f ${_script} ]]; then
		echo "This site has no production site (missing ${_script})." >&2
		exit 1
	fi
	PROD_LOCAL_PATH="" PROD_IP="" PROD_USER="" PROD_PASSWORD="" PROD_SSH_PORT=22
	PROD_FOLDER="" PROD_BINDING="" PROD_PORT_HTTP="" PROD_ADMIN_MAIL="" PROD_HTTPS=""
	PROD_TARGET="" PROD_WINDOWS_SHARE="" PROD_SMB_USER="" PROD_SMB_PASSWORD="" PROD_SMB_DOMAIN=""
	# shellcheck disable=SC1090
	source <(grep -E '^PROD_[A-Z_]+=' "${_script}")
	if [[ -z ${PROD_FOLDER} || -z ${PROD_BINDING} ]]; then
		echo "PROD_FOLDER and PROD_BINDING must be set in ${_script}" >&2
		exit 1
	fi
	_prodScript=${_script}
	if [[ ${PROD_TARGET} == windows ]]; then
		_target=${PROD_WINDOWS_SHARE}
		_root=${PROD_WINDOWS_SHARE}
	elif [[ -n ${PROD_LOCAL_PATH} ]]; then
		_target="this server"
		_root=${PROD_LOCAL_PATH%/}
	else
		_target="${PROD_USER}@${PROD_IP}:${PROD_SSH_PORT}"
		_root=/var/www/${PROD_FOLDER}
	fi
}

prod_exec() {
	local _command=$1 _interactive=${2:-}
	if [[ ${PROD_TARGET:-} == windows ]]; then
		echo "This action is not available for a Windows production site. It has no SSH access: use IIS on the server." >&2
		exit 1
	fi
	if [[ -n ${PROD_LOCAL_PATH} ]]; then
		sudo bash -c "${_command}"
		return
	fi
	local _ssh=(ssh -p "${PROD_SSH_PORT}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2)
	[[ ${_interactive} != tty ]] || _ssh+=(-tt)
	if [[ -n ${PROD_PASSWORD} ]]; then
		command -v sshpass >/dev/null || { echo "sshpass is required for password login: sudo apt install sshpass" >&2; exit 1; }
		export SSHPASS=${PROD_PASSWORD}
		_ssh=(sshpass -e "${_ssh[@]}")
	else
		_ssh+=(-o BatchMode=yes)
	fi
	local _sudo=""
	[[ ${PROD_USER} == root ]] || _sudo="sudo -n"
	"${_ssh[@]}" "${PROD_USER}@${PROD_IP}" "${_sudo} ${_command}"
}

# prod_run "<VAR='x' ...>" runs the script on stdin as root on the production target
prod_run() {
	prod_exec "env $1 bash -s"
}
