#!/bin/bash
# Usage: dev/deploy.sh [host]   (default linux-01)
# Puts this working copy, as it is and without a commit or a release, on a test server the way an update does:
# it is copied to the server and installed by its own `harbor self finish-update`, which also moves the admin site.
# The server is reached with `ssh <host>` as root. Nothing on this machine is changed.
set -euo pipefail
cd "$(dirname "$0")/.."

_host=${1:-linux-01}
_source=/tmp/vns-dev-source
# The admin's update check needs the commit the copy is built on; that it is a test build is noted beside it
_revision=$(git rev-parse HEAD)
_label="dev $(git rev-parse --short HEAD)"
[[ -z $(git status --porcelain 2>/dev/null) ]] || _label+="+changes"

echo "Copy the working copy to ${_host}"
# A fresh copy each time, so the modes and owner below hold for every file, not only those that changed
ssh "${_host}" "rm -rf $(printf %q "${_source}")"
# As a git checkout has them: readable by all, the executable bit kept, owned by whoever installs it (root). The files
# here can have other modes (Dropbox keeps some at 600) and this Mac's user, which the server does not have.
rsync -az --delete --no-owner --no-group --chmod=Dgo+rx,Fgo+r --exclude .git --exclude .DS_Store --exclude __pycache__ \
	--exclude '.vns-*' ./ "${_host}:${_source}/"

echo "Install it on ${_host}"
ssh "${_host}" "bash -s -- $(printf %q "${_source}") $(printf %q "${_revision}") $(printf %q "${_label}")" <<'REMOTE'
set -euo pipefail
source=$1
revision=$2
label=$3
# As a git checkout: readable by all, executables kept executable, owned by root (macOS's rsync ignores --chmod)
chown -R root:root "${source}"
chmod -R go+rX,go-w "${source}"
# The commit it is built on, and that it is a test build: the release is then always copied again
printf '%s\n' "${revision}" >"${source}/.vns-version"
printf '%s\n' "${label}" >"${source}/.vns-dev"
python3 "${source}/bin/harbor" self finish-update "${source}"
echo "Installed: ${label}"
REMOTE
