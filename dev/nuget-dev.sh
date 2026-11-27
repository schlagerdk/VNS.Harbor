#!/bin/bash
# Usage: dev/nuget-dev.sh [host] [site]   (default linux-01, no site)
# Tries this working copy's VNS.Harbor package on a test server without publishing it to nuget.org: it is packed here as
# a development version (the project's version with -dev.<time>, such as 18.3.4-dev.20261127153012, which sorts before
# 18.3.4 itself, so the real release is offered as an update later), and put in a folder on the server that its build
# user has as a NuGet source of its own (harbor-dev). With a site, that site's project takes the new version, restored
# as the build user; publishing it (Publish all) is left to the admin. Nothing on this machine is changed but a
# temporary folder, and nothing goes to nuget.org.
set -euo pipefail
cd "$(dirname "$0")/.."

_host=${1:-linux-01}
_site=${2:-}
_feed=/var/lib/harbor/nuget-dev
_base=$(sed -n 's|.*<Version>\(.*\)</Version>.*|\1|p' nuget/VNS.Harbor.csproj | head -1)
_version="${_base}-dev.$(date +%Y%m%d%H%M%S)"
_work=$(mktemp -d)
trap 'rm -rf "${_work}"' EXIT

echo "Pack VNS.Harbor ${_version}"
# A copy of the package's folder, its manifest at the dev version too: the backoffice loads its scripts by that version,
# so a browser does not keep the ones before
rsync -a --exclude bin --exclude obj --exclude artifacts nuget/ "${_work}/nuget/"
sed -i.bak -E -e "s/\"version\": \"[0-9.]+\"/\"version\": \"${_version}\"/" -e "s/\?v=[0-9.]+/?v=${_version}/g" \
	"${_work}/nuget/wwwroot/App_Plugins/VNS.Harbor/umbraco-package.json"
rm -f "${_work}/nuget/wwwroot/App_Plugins/VNS.Harbor/umbraco-package.json.bak"
dotnet pack "${_work}/nuget/VNS.Harbor.csproj" -c Release -p:Version="${_version}" -o "${_work}/out" --nologo -v quiet

echo "Put it in ${_feed} on ${_host}"
ssh "${_host}" "mkdir -p ${_feed} && chmod 755 ${_feed}"
scp -q "${_work}/out/VNS.Harbor.${_version}.nupkg" "${_host}:${_feed}/"
ssh "${_host}" "chmod 644 ${_feed}/*.nupkg"

ssh "${_host}" "bash -s -- $(printf %q "${_feed}") $(printf %q "${_version}") $(printf %q "${_site}")" <<'REMOTE'
set -euo pipefail
feed=$1
version=$2
site=$3
# The folder as a NuGet source of the build user's, once
if ! sudo -u vns-build -H dotnet nuget list source | grep -q "${feed}"; then
	sudo -u vns-build -H dotnet nuget add source "${feed}" --name harbor-dev >/dev/null
	echo "${feed} is a NuGet source of the build user (harbor-dev)"
fi
if [[ -n ${site} ]]; then
	root="/var/www/${site}"
	project=$(ls "${root}"/*.csproj 2>/dev/null | head -1)
	[[ -n ${project} ]] || { echo "No project in ${root}" >&2; exit 1; }
	# As Harbor updates a package: the version written as root, the restore (which can run build steps) as the build user
	dotnet add "${project}" package VNS.Harbor --version "${version}" --no-restore >/dev/null
	sudo -u vns-build -H bash -c "cd $(printf %q "${root}") && dotnet restore $(printf %q "${project}") --nologo -v quiet"
	echo "${site} uses VNS.Harbor ${version}: Publish all in the admin puts it on the site and its environments"
fi
REMOTE
echo "Done: VNS.Harbor ${_version} on ${_host}"
