#!/bin/bash
# Usage: dev/deploy.sh [host]   (default linux-01)
# Puts this working copy, as it is and without a commit or a release, on a test server the way an update does:
# it is copied to the server and installed with its own update.sh, then the admin site gets the new code.
# The server is reached with `ssh <host>` as root. Nothing on this machine is changed.
set -euo pipefail
cd "$(dirname "$0")/.."

_host=${1:-linux-01}
_source=/tmp/vns-dev-source
_revision="dev $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
[[ -z $(git status --porcelain 2>/dev/null) ]] || _revision+="+changes"

echo "Copy the working copy to ${_host}"
rsync -az --delete --exclude .git --exclude .DS_Store --exclude __pycache__ --exclude '.vns-*' ./ "${_host}:${_source}/"

echo "Install it on ${_host}"
ssh "${_host}" "bash -s -- $(printf %q "${_source}") $(printf %q "${_revision}")" <<'REMOTE'
set -euo pipefail
source=$1
revision=$2
bash "${source}/update.sh" "${source}" 2> >(grep -v "has no Git metadata" >&2)
# Shown in the admin as the installed version, so it is clear that a test build runs
printf '%s\n' "${revision}" >/vns/.vns-version
admin=$(python3 - <<'PY'
import glob, re
for path in glob.glob("/etc/systemd/system/*.service"):
    text = open(path).read()
    match = re.search(r"^WorkingDirectory=/var/www/([^/\n]+)/app$", text, re.M)
    if match and "gunicorn" in text:
        print(match.group(1))
        break
PY
)
if [[ -n ${admin} ]]; then
	echo "Update the admin site ${admin}"
	bash /vns/apache/update_admin_site.sh "${admin}"
else
	echo "No admin site was found to update." >&2
fi
echo "Installed: ${revision}"
REMOTE
