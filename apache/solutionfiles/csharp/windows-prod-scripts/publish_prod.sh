#!/bin/bash
# Publish this C# site (always a Release build) to its IIS site on a Windows server, over SMB with rclone.
#
# Usage:
#   publish_prod.sh              Publish
#   publish_prod.sh --dry-run    Build, and show what would change on the server
#
# Requirements on the server: the IIS site and its folder exist, and the ASP.NET Core Hosting Bundle and IIS URL Rewrite
# Module are installed. Without URL Rewrite, IIS answers HTTP 500.19 to a web.config with rewrite rules.
# Requirements here: rclone 1.60 or newer, curl.
# web.config is created when the server has none, with the HTTPS redirect (HTTPS sites) and the sitemap.xml and
# robots.txt rewrites (Umbraco sites). After the first publish web.config, appsettings*.json and the site's content
# are never overwritten by a publish. Use sync_prod.sh (Umbraco) for content.
# This file contains a password: keep it readable by root only (chmod 700).
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
# A dry run changes nothing, so it must not be recorded as a publish.
_self=$(realpath "$0")
_scriptFolder=$(dirname "${_self}")
_siteFolder=${_scriptFolder%/scripts*}   # scripts/ or scripts/targets/<environment>/
_scope=prod
[[ ${_scriptFolder} != */scripts/targets/* ]] || _scope=target-${_scriptFolder##*/}
if [[ ${VNS_PUBLISH_TRACKED:-} != "${_scope}" && ${1:-} != --dry-run ]]; then
	exec python3 "@APACHE_DIR@/lib/publish_status.py" run "${_siteFolder}" "${_scope}" /bin/bash "${_self}" "$@"
fi
cd "${_siteFolder}"
set -euo pipefail
source "@APACHE_DIR@/lib/dotnet.sh"
source "@APACHE_DIR@/lib/windows_prod.sh"

# ---- Production settings (filled in by create_prod.sh, edit if needed) -----
PROD_TARGET=windows
PROD_WINDOWS_SHARE=@PROD_WINDOWS_SHARE@   # The IIS site's folder, e.g. //server/share/site-folder
PROD_SMB_USER=@PROD_SMB_USER@
PROD_SMB_PASSWORD=@PROD_SMB_PASSWORD@
PROD_SMB_DOMAIN=@PROD_SMB_DOMAIN@         # Empty if the server is not on a domain
PROD_FOLDER=@PROD_FOLDER@                 # Name of the production site in the admin
PROD_BINDING=@PROD_BINDING@               # The IIS site's domain. The binding itself is managed in IIS
PROD_HTTPS=@PROD_HTTPS@                   # n = the site answers on http://, otherwise https://
PROD_ENVIRONMENT="@PROD_ENVIRONMENT@"     # Written to web.config on the first publish
PROD_OFFLINE_WAIT=5                       # Seconds IIS gets to release its files after app_offline.htm
# ---------------------------------------------------------------------------

_projectName=@PROJECT_NAME@
_localPublish=@PUBLISH_FOLDER@   # The site's local publish folder: source of appsettings*.json on the first publish
_siteUrl=https://${PROD_BINDING}
[[ ${PROD_HTTPS} != n ]] || _siteUrl=http://${PROD_BINDING}

_dryRun=0
case "${1:-}" in
	"") ;;
	--dry-run) _dryRun=1; echo "DRY RUN: nothing will be changed on the server." ;;
	*) echo "Usage: $0 [--dry-run]" >&2; exit 1 ;;
esac

_workDir=$(mktemp -d /tmp/publish-windows.XXXXXX)
_publish=${_workDir}/publish

_copying=0
on_exit() {
	local _status=$?
	# Bring the site back up, also if the publish fails before its files are touched. A copy that stopped halfway
	# leaves old and new files mixed, so the update page stays until a publish finishes.
	if ((_copying)); then
		echo "FAILED: the copy did not finish, so the site keeps showing its update page. Publish again; it is removed when a publish finishes." >&2
		_status=1
	else
		windows_bring_online || _status=1
	fi
	rm -rf "${_workDir}"
	exit "${_status}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

echo "Check ${PROD_WINDOWS_SHARE}"
windows_smb_connect

# Is this the first publish? Decided before anything is copied.
_listing=$(rclone lsf "${_windowsTarget}" --max-depth 1 --files-only)
_firstPublish=1
if grep -qix 'appsettings.json' <<<"${_listing}"; then _firstPublish=0; fi
_hasWebConfig=0
if grep -qix 'web.config' <<<"${_listing}"; then _hasWebConfig=1; fi

# Built locally first, so the site stays up if the build fails.
echo "Publish (Release, win-x64)"
dotnet publish "${_projectName}.csproj" -c Release -p:PublishDir="${_publish}/" -p:EnvironmentName="${PROD_ENVIRONMENT}" -p:CustomAfterMicrosoftCommonTargets="@APACHE_DIR@/lib/publish.targets" --runtime win-x64 --self-contained false --nologo
if [[ ! -f ${_publish}/${_projectName}.dll ]]; then
	echo "Publish failed: ${_projectName}.dll not found in the output" >&2
	exit 1
fi

echo "Delete schema files and bin"
for _file in appsettings-schema.json appsettings-schema.usync.json appsettings-schema.Umbraco.Cms.json umbraco-package-schema.json; do
	rm -f "${_file}"
done
rm -f "${_publish}"/appsettings-schema*.json "${_publish}/umbraco-package-schema.json"
rm -rf bin

# The project's Linux publish folder must not end up on the Windows server.
if [[ ${_localPublish} != . && -e ${_publish}/${_localPublish} ]]; then
	echo "FAILED: the build output contains ${_localPublish}. Exclude it in the .csproj. Nothing was changed on the server." >&2
	exit 1
fi

if ((_firstPublish)); then
	echo "First publish: prepare appsettings*.json (without ConnectionStrings)"
	# Prefer the files from the local publish folder (created by create_site.sh); fall back to the fresh build
	if [[ -f ${_localPublish}/appsettings.json ]]; then
		rm -f "${_publish}"/appsettings*.json
		cp "${_localPublish}"/appsettings*.json "${_publish}/"
		rm -f "${_publish}"/appsettings-schema*.json
	fi
	for _file in "${_publish}"/appsettings*.json; do
		[[ -f ${_file} ]] || continue
		python3 "@APACHE_DIR@/lib/windows_prod.py" prepare-appsettings "${_file}" "${_siteUrl}" "${PROD_HTTPS}"
	done
fi

# copy, never sync: sync would delete logs and uploaded media on the server.
# Once the site exists, its settings and content are never overwritten by a publish.
_copyOpts=(--create-empty-src-dirs --modify-window 2s --stats 0 -v --ignore-case --exclude '/web.config')
if ((!_firstPublish)); then
	_copyOpts+=(--exclude 'appsettings*.json' --exclude '/umbraco/**' --exclude '/uSync/**' --exclude '/Views/**'
		--exclude '/wwwroot/**' --exclude '/App_Data/**')
fi

# An existing web.config is kept, but its HTTPS redirect follows the site: added when it answers on HTTPS
_updateWebConfig=0
if ((_hasWebConfig)); then
	_webConfigName=$(grep -ix 'web.config' <<<"${_listing}" | head -n 1)
	rclone copyto "${_windowsTarget}/${_webConfigName}" "${_workDir}/web.config"
	_webConfigHttps=y
	[[ ${PROD_HTTPS} != n ]] || _webConfigHttps=n
	# A web.config that is not plain XML is left as it is, with the reason
	if [[ $(python3 "@APACHE_DIR@/lib/windows_prod.py" set-https "${_workDir}/web.config" "${_webConfigHttps}" || true) == changed ]]; then
		_updateWebConfig=1
	fi
fi

if ((!_hasWebConfig)); then
	_webConfigHttps=y
	[[ ${PROD_HTTPS} != n ]] || _webConfigHttps=n
	_webConfigUmbraco=n
	if grep -qs "Umbraco.Cms" "${_projectName}.csproj"; then _webConfigUmbraco=y; fi
	python3 "@APACHE_DIR@/lib/windows_prod.py" web-config "${_projectName}" "${PROD_ENVIRONMENT}" "${_webConfigHttps}" "${_webConfigUmbraco}" >"${_workDir}/web.config"
fi

# The main site's config snippets (Allowed IPs, rewrites and the like) as rules named "VNS: ...", made equal to
# the main site's at every publish. Rules written by hand are kept. A snippet that cannot be carried is told about.
_mainConf="/etc/apache2/sites-available/${_siteFolder##*/}.conf"
if [[ -f ${_mainConf} ]] && [[ $(python3 "@APACHE_DIR@/lib/config_snippets.py" iis "${_mainConf}" "${_workDir}/web.config" || true) == changed ]]; then
	((!_hasWebConfig)) || _updateWebConfig=1
fi

if ((_dryRun)); then
	((_hasWebConfig)) || echo "web.config would be created."
	((!_updateWebConfig)) || echo "web.config would be updated: its HTTPS redirect and the main site's config snippets."
	rclone copy "${_publish}" "${_windowsTarget}" "${_copyOpts[@]}" --dry-run
	echo "Dry run completed."
	exit 0
fi

windows_take_offline "${_siteUrl}"

echo "Copy to ${PROD_WINDOWS_SHARE}"
# A file IIS has not released yet is tried again, rather than leaving the site half updated
_copying=1
rclone copy "${_publish}" "${_windowsTarget}" "${_copyOpts[@]}" --retries 5 --retries-sleep 5s

if ((_updateWebConfig)); then
	echo "web.config: the HTTPS redirect and the main site's config snippets are brought up to date"
	rclone copyto "${_workDir}/web.config" "${_windowsTarget}/${_webConfigName}"
elif ((_hasWebConfig)); then
	echo "web.config already exists on the server - not touched"
else
	echo "Create web.config"
	rclone copyto "${_workDir}/web.config" "${_windowsTarget}/web.config"
fi

# Every file is in place: the site may come back
_copying=0
windows_bring_online

# The first request starts the app, which is why this can take a while.
echo "Check ${_siteUrl}"
_httpStatus=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 180 "${_siteUrl}" || true)
if [[ ${_httpStatus} =~ ^[234] ]]; then
	echo "OK: ${_siteUrl} answered with HTTP ${_httpStatus}"
elif ((_firstPublish)) || [[ ${VNS_MOVING:-} == 1 ]]; then
	# The first publish has no ConnectionStrings, and DNS or the IIS binding may not be ready yet.
	echo "WARNING: ${_siteUrl} answered with HTTP ${_httpStatus}. Set the connection string in appsettings.json on the server if the site needs one, and check the IIS binding."
else
	echo "FAILED: ${_siteUrl} answered with HTTP ${_httpStatus}. Check the log on the Windows server." >&2
	exit 1
fi
