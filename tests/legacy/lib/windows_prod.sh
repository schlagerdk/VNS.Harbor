#!/bin/bash
# Helpers for a Windows (IIS) production target, reached over SMB with rclone 1.60 or newer. No mount is needed.
# Needs PROD_WINDOWS_SHARE, PROD_SMB_USER, PROD_SMB_PASSWORD and PROD_SMB_DOMAIN.

_windowsLib=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_windowsTarget=""
_windowsOffline=0

# Log in and set _windowsTarget to the rclone path of the site folder. The folder must already exist.
windows_smb_connect() {
	command -v rclone >/dev/null || { echo "rclone is not installed. Run apache/init.sh, or see https://rclone.org/install/" >&2; return 1; }
	local _host=${PROD_WINDOWS_SHARE#//}
	_host=${_host%%/*}
	_windowsTarget=":smb:${PROD_WINDOWS_SHARE#"//${_host}/"}"

	# Settings go in environment variables, so the password stays out of the process list.
	# /dev/null makes rclone keep its config in memory instead of looking for a file.
	export RCLONE_CONFIG=/dev/null RCLONE_SMB_HOST="${_host}" RCLONE_SMB_USER="${PROD_SMB_USER}"
	if [[ -n ${PROD_SMB_DOMAIN} ]]; then
		export RCLONE_SMB_DOMAIN="${PROD_SMB_DOMAIN}"
	fi
	# rclone requires the password in its obscured form. This is encoding, not encryption.
	RCLONE_SMB_PASS=$(printf '%s' "${PROD_SMB_PASSWORD}" | rclone obscure -) || return 1
	export RCLONE_SMB_PASS

	# rclone creates a missing destination, so a typo in the share would otherwise create a new folder.
	# This also stops early on a failed login.
	if ! rclone lsf "${_windowsTarget}" --max-depth 1 >/dev/null; then
		echo "Could not open ${PROD_WINDOWS_SHARE}. Check the share, the login and that the IIS site folder exists." >&2
		return 1
	fi
}

windows_take_offline() {
	local _page _design
	_page=$(mktemp)
	# The page visitors see while the site is updated. It reloads by itself as soon as the site answers again.
	_design="$(dirname "${BASH_SOURCE[0]}")/app_offline.htm"
	if [[ -f ${_design} ]]; then
		cp "${_design}" "${_page}"
	else
		echo '<!doctype html><html><head><meta charset="utf-8"><title>Updating</title></head><body><h1>Updating, back shortly.</h1></body></html>' >"${_page}"
	fi
	echo "Take site offline"
	_windowsOffline=1
	if ! rclone copyto "${_page}" "${_windowsTarget}/app_offline.htm"; then
		rm -f "${_page}"
		return 1
	fi
	rm -f "${_page}"
	# IIS shuts the app down when it sees app_offline.htm, and answers with the page and 503 when it has.
	# Wait for that, so the app's files are released before they are replaced, then a little more.
	if [[ -n ${1:-} ]]; then
		local _waited=0 _code=""
		while ((_waited < 60)); do
			_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || true)
			[[ ${_code} != 503 ]] || break
			sleep 2
			_waited=$((_waited + 2))
		done
		if [[ ${_code} == 503 ]]; then
			echo "The site shows the update page"
		else
			echo "The site did not answer with the update page within 60 seconds (last answer: ${_code:-none}). Going on."
		fi
	fi
	sleep "${PROD_OFFLINE_WAIT:-5}"
}

windows_bring_online() {
	((_windowsOffline)) || return 0
	echo "Remove app_offline.htm"
	if rclone deletefile "${_windowsTarget}/app_offline.htm"; then
		_windowsOffline=0
	else
		echo "FAILED: could not remove app_offline.htm. The site stays offline until it is deleted from ${PROD_WINDOWS_SHARE}." >&2
		return 1
	fi
}

# Change the environment in the web.config on the server. IIS restarts the app when the file changes.
windows_set_environment() {
	local _environment=$1 _dir _status=0
	_dir=$(mktemp -d)
	if ! rclone copyto "${_windowsTarget}/web.config" "${_dir}/web.config"; then
		echo "Could not read web.config in ${PROD_WINDOWS_SHARE}. Publish to production first." >&2
		_status=1
	elif ! python3 "${_windowsLib}/windows_prod.py" set-environment "${_dir}/web.config" "${_environment}" ||
		! rclone copyto "${_dir}/web.config" "${_windowsTarget}/web.config"; then
		_status=1
	fi
	rm -rf "${_dir}"
	return "${_status}"
}
