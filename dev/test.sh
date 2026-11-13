#!/bin/bash
# Usage: dev/test.sh [host] [test module ...]   (default linux-01, every test)
# Runs the tests on a Linux test server, where bash and the other programs are as on the servers: on a Mac, bash 3
# skips many of them. They run as the user vns-test, who cannot change the server, from a copy of this working copy.
set -euo pipefail
cd "$(dirname "$0")/.."

_host=${1:-linux-01}
shift || true
_source=/tmp/vns-dev-tests

rsync -az --delete --exclude .git --exclude .DS_Store --exclude __pycache__ ./ "${_host}:${_source}/"
_modules=""
(($# == 0)) || _modules=$(printf ' %q' "$@")
ssh "${_host}" "bash -s -- $(printf %q "${_source}")${_modules}" <<'REMOTE'
set -euo pipefail
source=$1
shift
id vns-test >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin vns-test
chown -R vns-test: "${source}"
# The admin's tests need Flask: the admin site's own Python has it
python=/usr/bin/python3
for candidate in /var/www/*/venv/bin/python3; do
	[[ -x ${candidate} ]] && "${candidate}" -c "import flask" 2>/dev/null && { python=${candidate}; break; }
done
modules=("$@")
if ((${#modules[@]} == 0)); then
	cd "${source}"
	mapfile -t modules < <(ls apache/tests/test_*.py tests/test_*.py | sed 's|\.py$||; s|/|.|g')
fi
cd "${source}"
runuser -u vns-test -- env HOME=/home/vns-test PATH=/usr/local/bin:/usr/bin:/bin "${python}" -m unittest "${modules[@]}"
REMOTE
