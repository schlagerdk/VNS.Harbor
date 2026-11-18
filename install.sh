#!/bin/bash
# VNS Harbor on a new Ubuntu server:
#
#   apt-get update && apt-get install -y curl    (a minimal Ubuntu has no curl)
#   curl -fsSL https://raw.githubusercontent.com/schlagerdk/VNS.Harbor/main/install.sh | sudo bash
#
# The base setup (time zone, updates, the few packages Harbor itself needs), the newest release of Harbor in
# /opt/harbor, then the server made ready for sites (harbor server init: Apache, acme.sh, .NET, keep-alive) and,
# if wanted, the admin site (harbor admin install). HARBOR_REF=v1.2.3 installs that release instead, and
# HARBOR_REF=main the newest code on main (to try it before a release).
# Run again, it only brings what is missing; a server that has Harbor is updated from the admin, or harbor self update.
set -euo pipefail

REPOSITORY=https://github.com/schlagerdk/VNS.Harbor.git
export DEBIAN_FRONTEND=noninteractive

if ((EUID != 0)); then
	echo "Run it as root: curl -fsSL https://raw.githubusercontent.com/schlagerdk/VNS.Harbor/main/install.sh | sudo bash" >&2
	exit 1
fi
if ! grep -qx 'ID=ubuntu' /etc/os-release 2>/dev/null; then
	echo "VNS Harbor runs on Ubuntu." >&2
	exit 1
fi
# The questions come from the terminal, also when this script itself comes on stdin (curl | bash)
if [[ -r /dev/tty ]] && ( : </dev/tty ) 2>/dev/null; then
	tty=/dev/tty
else
	tty=/dev/null
fi

echo "== Base setup"
timedatectl set-timezone Europe/Copenhagen
timedatectl set-ntp on
apt-get update
apt-get full-upgrade -y
apt-get install -y curl git ca-certificates python3 python3-venv
apt-get autoremove -y

echo "== VNS Harbor"
ref=${HARBOR_REF:-$(git ls-remote --tags --refs "${REPOSITORY}" 'v*' | awk '{sub("refs/tags/", "", $2); print $2}' |
	grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)}
if [[ ${ref} != main && ! ${ref} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "No release of VNS Harbor was found${ref:+: ${ref}}." >&2
	exit 1
fi
source=$(mktemp -d)
trap 'rm -rf "${source}"' EXIT
git clone --quiet --depth 1 --branch "${ref}" "${REPOSITORY}" "${source}/harbor"
git -C "${source}/harbor" rev-parse HEAD >"${source}/harbor/.vns-version"
python3 "${source}/harbor/bin/harbor" self install "${source}/harbor"

echo "== The server, ready for sites"
# It asks for root's Samba password
harbor server init <"${tty}"

if [[ -n $(python3 -c 'import sys; sys.path.insert(0, "/opt/harbor/current"); from harbor.install.admin import admin_sites; from harbor.core.system import System; print(" ".join(admin_sites(System())))') ]]; then
	echo "The admin site is there already."
	exit 0
fi
read -r -p "Set up the admin site now (the web page that manages the server)? [Y/n]: " answer <"${tty}" || answer=n
if [[ ${answer:-y} == [yY]* ]]; then
	harbor admin install <"${tty}"
else
	echo "Set it up later with: sudo harbor admin install"
fi
