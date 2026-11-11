#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
clear

export DEBIAN_FRONTEND=noninteractive

echo Set your timezone
sudo timedatectl set-timezone Europe/Copenhagen

echo Enable timesyncd
sudo timedatectl set-ntp on

echo Update APT and upgrade all packages
sudo apt-get update
sudo apt-get full-upgrade -y
sudo apt-get autoremove -y
sudo apt-get autoclean

echo Install base packages
sudo apt-get install -y curl git net-tools nano unzip ca-certificates rsync

echo Deploy to /vns
SRC="$(pwd)"
# The newest release is installed, like an update from the admin. VNS_INSTALL_REF=main installs the clone as it
# is, and VNS_INSTALL_REF=v1.2.0 that release. The release is cloned apart, as this script runs from the clone.
REF="${VNS_INSTALL_REF:-$(git -C "$SRC" tag -l 'v[0-9]*' --sort=-v:refname 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)}"
if [ -n "$REF" ] && [ "$REF" != main ]; then
    [[ $REF =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid release: $REF" >&2; exit 1; }
    echo "Install release $REF"
    RELEASE="$(mktemp -d)"
    git clone --quiet --branch "$REF" "$SRC" "$RELEASE/VNS.Linux"
    bash "$RELEASE/VNS.Linux/update.sh" "$RELEASE/VNS.Linux"
    rm -rf "$RELEASE"
else
    bash ./update.sh "$SRC"
fi
rm -f /vns/.gitignore

# Remove the cloned directory, but only if it is the VNS.Linux clone and not /vns
if [ "$(basename "$SRC")" = "VNS.Linux" ] && [ "$SRC" != "/vns" ]; then
    cd /
    rm -rf "$SRC"
fi

echo "Done. Next steps: /vns/apache/init.sh (apache, acme.sh, .NET 10, python) and /vns/install_nodejs.sh (optional). Update later with /vns/update.sh"
