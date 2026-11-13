#!/bin/bash
# Shared SQL client installation; the caller supplies _sudo and VERSION_ID.
install_tools() {
	echo "Add Microsoft tools repository"
	curl -fsSL "https://packages.microsoft.com/config/ubuntu/${VERSION_ID}/prod.list" | ${_sudo} tee /etc/apt/sources.list.d/mssql-release.list >/dev/null
	echo "Install mssql-tools18"
	${_sudo} apt-get update
	${_sudo} env ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev
	echo "Add sqlcmd to PATH for all users"
	echo 'export PATH="$PATH:/opt/mssql-tools18/bin"' | ${_sudo} tee /etc/profile.d/mssql-tools.sh >/dev/null
	if [[ ! -x /opt/mssql-tools18/bin/sqlcmd ]]; then
		echo "Installation finished, but /opt/mssql-tools18/bin/sqlcmd was not found." >&2
		apt-cache policy mssql-tools18 >&2 || true
		return 1
	fi
	echo "sqlcmd installed - open a new shell to get it on PATH"
}
