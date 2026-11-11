#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source lib/dotnet.sh
clear

_adminMail=mhs@vns.dk
_dotnetDir=/usr/lib/dotnet
# The newest .NET is the default host; older channels that sites use are installed beside it
_dotnetChannel=11.0

echo Install Unzip and Zip
sudo apt install -y unzip zip

echo Install prerequisites
sudo apt-get update
sudo apt-get install -y curl unzip tar git ca-certificates uuid-runtime python3 python3-venv python3-pip cron

echo Install rclone
curl -fsSL https://rclone.org/install.sh | sudo bash

echo Install apache2
sudo apt-get install -y apache2

echo Create directory for the per-site empty roots used to isolate the site services
sudo mkdir -p /var/lib/vns-roots

echo Enable apache modules
sudo a2enmod ssl rewrite headers proxy proxy_http proxy_wstunnel
sudo bash "$(pwd)/set_servername.sh"

echo Install sshpass
sudo apt install -y sshpass

echo "Install shared Apache TLS policy"
sudo bash "$(pwd)/install_tls_policy.sh" --start-if-stopped

echo "Install the server-wide configs: the offline page, and headers that do not tell what the server runs"
sudo bash "$(pwd)/install_apache_conf.sh"

if [[ ! -x /etc/acmesh/acme.sh ]]; then
	echo Install acme.sh
	_acmeTmp=$(mktemp -d)
	git clone https://github.com/acmesh-official/acme.sh.git "${_acmeTmp}"
	(cd "${_acmeTmp}" && sudo ./acme.sh --install --home /etc/acmesh --config-home /etc/ssl/data --cert-home /etc/ssl/certs --accountemail "${_adminMail}")
	rm -rf "${_acmeTmp}"
fi

echo Set letsencrypt to default
# acme.sh is given its folders, as when it was installed: without them it looks for its config in /root/.acme.sh
sudo /etc/acmesh/acme.sh --home /etc/acmesh --config-home /etc/ssl/data --set-default-ca --server letsencrypt

echo Add cron job for acme
sudo /etc/acmesh/acme.sh --home /etc/acmesh --config-home /etc/ssl/data --install-cronjob

# Install the newest SDK and runtime, plus .NET 10 (LTS, which most sites use) and .NET 8 for Umbraco 13.
echo "Install .NET ${_dotnetChannel} SDK and ASP.NET Core runtime"
sudo mkdir -p "${_dotnetDir}"
curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
sudo bash /tmp/dotnet-install.sh --channel "${_dotnetChannel}" --install-dir "${_dotnetDir}"
sudo bash /tmp/dotnet-install.sh --channel "${_dotnetChannel}" --runtime aspnetcore --install-dir "${_dotnetDir}"
rm -f /tmp/dotnet-install.sh
ensure_dotnet_channel 10.0
ensure_dotnet_channel 8.0

# The generated systemd services use /usr/bin/dotnet
sudo ln -sf "${_dotnetDir}/dotnet" /usr/bin/dotnet

echo Set DOTNET_ROOT for all users
echo "export DOTNET_ROOT=${_dotnetDir}" | sudo tee /etc/profile.d/dotnet.sh >/dev/null

echo Install dotnet-outdated
sudo DOTNET_ROOT="${_dotnetDir}" \
	dotnet tool install --tool-path /usr/local/bin dotnet-outdated-tool 2>/dev/null ||
	sudo DOTNET_ROOT="${_dotnetDir}" dotnet tool update --tool-path /usr/local/bin dotnet-outdated-tool

echo "Installed runtimes:"
dotnet --list-runtimes
echo "Installed SDKs:"
dotnet --list-sdks

echo "Install configurable site keep-alive cron job"
sudo python3 "$(pwd)/lib/keep_alive.py" --install
sudo systemctl enable --now cron
echo "Mark Apache initialization as complete"
sudo mkdir -p /var/lib/vns
sudo touch /var/lib/vns/apache-initialized

echo
read -r -p "Set up the admin site now (web UI for managing all sites)? [y/N]: " _setupAdmin
if [[ ${_setupAdmin} == [yY]* ]]; then
	sudo "$(pwd)/install_admin_site.sh"
fi
