#!/bin/bash
# Install SQL client tools on the admin server, without installing SQL Server.
set -euo pipefail
_sqlDir="$(cd "$(dirname "$0")" && pwd)"
source "${_sqlDir}/lib/microsoft_repo.sh"
source "${_sqlDir}/lib/install_tools.sh"
if [[ $EUID -eq 0 ]]; then _sudo=""; else _sudo="sudo"; fi
source /etc/os-release
if [[ ${ID} != ubuntu ]]; then
	echo "This script supports Ubuntu only (found: ${ID})." >&2
	exit 1
fi
if ! command -v gpg >/dev/null 2>&1; then
	${_sudo} apt-get install -y gnupg
fi
install_microsoft_keys "${_sudo}"
install_tools
