#!/bin/bash
# Helpers for the SQL Server sa password.
# Scripts in sql/ use a password placeholder that is replaced during installation.

# Sets the sa password in all .sh files under <sql-directory>: the placeholder after a fresh copy, or the
# password set before. $2 is the password in shell-quoted form. Only the value after "-U sa -P" changes.
sa_apply_quoted() {
	local _dir=$1
	SA_QUOTED=$2 find "${_dir}" -name '*.sh' -type f -exec \
		perl -pi -e 's/(-U sa -P )\x27(?:[^\x27]*\x27\\\x27\x27)*[^\x27]*\x27/$1$ENV{SA_QUOTED}/g' {} +
}

# The scripts that hold the sa password, under <sql-directory>
sa_scripts() {
	# Built from parts, so this line is not found itself
	local _option="-U sa -P"
	grep -rlE --include='*.sh' -e "${_option} '.*' " "$1" || true
}

# Prompts for the sa password and sets SA_PASSWORD
sa_prompt() {
	local _confirm
	while true; do
		read -r -s -p "Enter the sa password you set during SQL Server setup so the SQL scripts can be updated: " SA_PASSWORD; echo
		read -r -s -p "Repeat password: " _confirm; echo
		if [[ -z ${SA_PASSWORD} ]]; then
			echo "Password cannot be empty" >&2
		elif [[ ${SA_PASSWORD} != "${_confirm}" ]]; then
			echo "Passwords do not match" >&2
		elif ((${#SA_PASSWORD} < 8)); then
			echo "SQL Server requires at least 8 characters" >&2
		else
			break
		fi
	done
}

# Sets SA_PASSWORD in the scripts under <sql-directory>
sa_apply() {
	# Single-quote and escape ' so the password can contain any character
	local _escaped="'\\''"
	sa_apply_quoted "$1" "'${SA_PASSWORD//\'/${_escaped}}'"
}

# Prompts for the sa password, sets SA_PASSWORD, and updates scripts in <sql-directory>.
sa_prompt_and_apply() {
	sa_prompt
	sa_apply "$1"
}
