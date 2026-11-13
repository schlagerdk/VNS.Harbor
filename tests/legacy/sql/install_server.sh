#!/bin/bash
# Installs the latest SQL Server available for the server's Ubuntu version.
set -euo pipefail
clear

_sqlDir="$(cd "$(dirname "$0")" && pwd)"
source "${_sqlDir}/lib/microsoft_repo.sh"
source "${_sqlDir}/lib/install_tools.sh"
_baseUrl=https://packages.microsoft.com
# Newest first. The first version with a repository for this Ubuntu version is used.
_sqlVersions=(2025 2022 2019)

if [[ $(id -u) -eq 0 ]]; then _sudo=""; else _sudo="sudo"; fi

source /etc/os-release
if [[ ${ID} != ubuntu ]]; then
	echo "This script supports Ubuntu only (found: ${ID})" >&2
	exit 1
fi

_fallbackUbuntu=24.04

find_sql_version() {
	local _ubuntu=$1 _v
	for _v in "${_sqlVersions[@]}"; do
		if curl -fsSI "${_baseUrl}/config/ubuntu/${_ubuntu}/mssql-server-${_v}.list" >/dev/null 2>&1; then
			echo "${_v}"
			return 0
		fi
	done
	return 1
}

_repoUbuntu=${SQL_UBUNTU_VERSION:-${VERSION_ID}}

if ! command -v gpg >/dev/null 2>&1; then
	${_sudo} apt-get install -y gnupg
fi

echo "Add Microsoft signing keys"
install_microsoft_keys "${_sudo}"

install_server() {
	_sqlVersion=$(find_sql_version "${_repoUbuntu}" || true)

	if [[ -z ${_sqlVersion} ]]; then
		echo "Microsoft has no SQL Server repository for Ubuntu ${_repoUbuntu}." >&2
		echo "Supported: Ubuntu 22.04 and 24.04 LTS." >&2
		read -r -p "Try the Ubuntu ${_fallbackUbuntu} repository instead (NOT supported by Microsoft, may fail)? [y/N]: " _answer
		[[ ${_answer,,} == y || ${_answer,,} == yes ]] || exit 1
		_repoUbuntu=${_fallbackUbuntu}
		_sqlVersion=$(find_sql_version "${_repoUbuntu}") || { echo "No repository found for Ubuntu ${_repoUbuntu}" >&2; exit 1; }
	fi
	echo "Installing SQL Server ${_sqlVersion} using the Ubuntu ${_repoUbuntu} repository (server runs Ubuntu ${VERSION_ID})"

	# SQL Server 2025 requires libldap-2.5-0, which is not available in Ubuntu 24.04+
	if [[ ${_sqlVersion} == 2025 ]] && ! apt-cache show libldap-2.5-0 >/dev/null 2>&1 && ! dpkg -s libldap-2.5-0 >/dev/null 2>&1; then
		echo "Install libldap-2.5-0"
		_ldapDeb=$(mktemp --suffix=.deb)
		trap 'rm -f "${_ldapDeb}"' EXIT
		curl -fsSL https://deb.debian.org/debian/pool/main/o/openldap/libldap-2.5-0_2.5.13+dfsg-5_amd64.deb -o "${_ldapDeb}"
		${_sudo} apt-get install -y "${_ldapDeb}"
	fi

	echo "Add SQL Server repository"
	curl -fsSL "${_baseUrl}/config/ubuntu/${_repoUbuntu}/mssql-server-${_sqlVersion}.list" | ${_sudo} tee "/etc/apt/sources.list.d/mssql-server-${_sqlVersion}.list" >/dev/null

	echo "Install mssql-server"
	${_sudo} apt-get update
	${_sudo} apt-get install -y mssql-server

	# Runs interactively and prompts for the sa password, among other things
	${_sudo} /opt/mssql/bin/mssql-conf setup

	echo "Create backup folder"
	source lib/backup_permissions.sh
	prepare_backup_permissions

	${_sudo} systemctl status mssql-server --no-pager || true
}

_serverInstalled=0
if dpkg -s mssql-server >/dev/null 2>&1; then
	echo "mssql-server is already installed (use update_server.sh to update it) - skipping server install"
else
	install_server
	_serverInstalled=1
fi

if ((_serverInstalled)) || grep -rqF "'@SA_PASSWORD@'" "${_sqlDir}" --include='*.sh'; then
	source "${_sqlDir}/lib/sa_password.sh"
	sa_prompt_and_apply "${_sqlDir}"
fi

read -r -p "Install SQL command-line tools (sqlcmd)? [y/N]: " _answer
if [[ ${_answer} == [yY] || ${_answer} == [yY][eE][sS] ]]; then
	if dpkg -s mssql-tools18 >/dev/null 2>&1; then
		echo "mssql-tools18 is already installed (use update_server.sh to update it) - skipping tools install"
	else
		install_tools
	fi
else
	echo "SQL tools installation skipped"
fi
