#!/bin/bash
# Removes SQL Server, then offers to remove SQL client tools. Data is preserved unless explicitly deleted.
set -euo pipefail
clear

if [[ $(id -u) -eq 0 ]]; then _sudo=""; else _sudo="sudo"; fi
_sqlDir="$(cd "$(dirname "$0")" && pwd)"

_hasServer=0
dpkg -s mssql-server >/dev/null 2>&1 && _hasServer=1

if ((_hasServer)); then
	echo "This will remove SQL Server (mssql-server). SQL tools are optional and can be kept."
	read -r -p "Continue? [y/N]: " _answer
	[[ ${_answer} == [yY] || ${_answer} == [yY][eE][sS] ]] || { echo "Aborted"; exit 1; }

	echo "Stop and disable service"
	${_sudo} systemctl stop mssql-server || true
	${_sudo} systemctl disable mssql-server || true

	echo "Remove mssql-server"
	${_sudo} apt-get remove --purge -y mssql-server
	echo "Remove SQL Server apt repositories and unused keys"
	${_sudo} rm -f /etc/apt/sources.list.d/mssql-server-*.list
	# Remove the keys only if no other repository uses them
	if ! grep -qs 'microsoft-prod.gpg' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null; then
		${_sudo} rm -f /usr/share/keyrings/microsoft-prod.gpg
	fi
	${_sudo} apt-get update
	${_sudo} apt-get autoremove -y

	echo
	echo "Databases, logs and backups are stored in /var/opt/mssql"
	echo "Type DELETE to permanently delete this folder, or press Enter to keep it."
	read -r -p "> " _answer
	if [[ ${_answer} == DELETE ]]; then
		${_sudo} rm -rf /var/opt/mssql
		echo "/var/opt/mssql deleted"
	else
		echo "/var/opt/mssql kept"
	fi

	echo "SQL Server removed"
else
	echo "mssql-server is not installed"
fi

bash "${_sqlDir}/uninstall_tools.sh"
