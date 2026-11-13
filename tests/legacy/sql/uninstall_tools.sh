#!/bin/bash
# Remove SQL client tools without removing SQL Server or shared ODBC packages.
set -euo pipefail
if [[ $(id -u) -eq 0 ]]; then _sudo=""; else _sudo="sudo"; fi

_packages=()
for _package in mssql-tools18 mssql-tools; do
	if dpkg -s "${_package}" >/dev/null 2>&1; then
		_packages+=("${_package}")
	fi
done
if ((${#_packages[@]} == 0)); then
	echo "SQL command-line tools are not installed"
	exit 0
fi

read -r -p "Remove SQL command-line tools (sqlcmd)? [y/N]: " _answer
if [[ ${_answer} != [yY] && ${_answer} != [yY][eE][sS] ]]; then
	echo "SQL tools kept"
	exit 0
fi

echo "Remove SQL command-line tools"
${_sudo} apt-get remove --purge -y "${_packages[@]}"
${_sudo} rm -f /etc/profile.d/mssql-tools.sh
echo "SQL tools removed. SQL Server, shared ODBC packages and Microsoft repositories are unchanged."
