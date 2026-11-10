#!/bin/bash
# Usage: set_sa_password.sh
# Sets the SQL Server sa password in every script in this folder that uses it, including those in cron/.
# Use it when the sql folder was deleted and copied again, so the scripts hold the placeholder once more,
# or after the sa password was changed on the server. It asks for the password twice and, when sqlcmd is
# installed, checks it against the local SQL Server before any script is changed.
set -euo pipefail
_sqlDir=$(cd "$(dirname "$0")" && pwd)
source "${_sqlDir}/lib/sa_password.sh"

mapfile -t _scripts < <(sa_scripts "${_sqlDir}")
if ((${#_scripts[@]} == 0)); then
	echo "No script in ${_sqlDir} uses the sa password." >&2
	exit 1
fi
echo "Scripts that use the sa password:"
printf '  %s\n' "${_scripts[@]#"${_sqlDir}/"}"
echo

sa_prompt

# The password goes in the environment, so it is not shown in the process list
_sqlcmd=$(command -v /opt/mssql-tools18/bin/sqlcmd sqlcmd 2>/dev/null | head -n 1 || true)
if [[ -n ${_sqlcmd} ]]; then
	if SQLCMDPASSWORD=${SA_PASSWORD} "${_sqlcmd}" -C -S LOCALHOST -U sa -l 10 -b -Q "SET NOCOUNT ON; SELECT 1" >/dev/null 2>&1; then
		echo "The password works on the local SQL Server."
	else
		read -r -p "The local SQL Server did not accept the password. Set it in the scripts anyway? [y/N]: " _answer
		[[ ${_answer} == [yY] || ${_answer} == [yY][eE][sS] ]] || { echo "Nothing was changed."; exit 1; }
	fi
else
	echo "sqlcmd is not installed, so the password is not checked against the server."
fi

sa_apply "${_sqlDir}"
# The scripts now hold the password: only their owner and group may read them
chmod o-rwx "${_scripts[@]}"
echo "The sa password is set in ${#_scripts[@]} script(s). Others can no longer read them."
