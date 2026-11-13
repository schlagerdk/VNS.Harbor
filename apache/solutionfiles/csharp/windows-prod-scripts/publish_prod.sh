#!/bin/bash
# Publish this C# site (always a Release build) to its IIS site on a Windows server, over SMB with rclone.
#
# Usage:
#   publish_prod.sh              Publish
#   publish_prod.sh --dry-run    Build, and show what would change on the server
#
# The settings below are the environment's; the publish itself is harbor's (harbor env publish).
# Requirements on the server: the IIS site and its folder exist, and the ASP.NET Core Hosting Bundle and IIS URL Rewrite
# Module are installed. Without URL Rewrite, IIS answers HTTP 500.19 to a web.config with rewrite rules.
# This file contains a password: keep it readable by root only (chmod 700).
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
set -euo pipefail

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

case "${1:-}" in
	"" | --dry-run) ;;
	*) echo "Usage: $0 [--dry-run]" >&2; exit 1 ;;
esac
_self=$(realpath "$0")
_scriptFolder=$(dirname "${_self}")
_siteFolder=${_scriptFolder%/scripts*}   # scripts/ or scripts/targets/<environment>/
_id=prod
[[ ${_scriptFolder} != */scripts/targets/* ]] || _id=${_scriptFolder##*/}
VNS_TARGET=${_id} exec "@APACHE_DIR@/../bin/harbor" env publish "${_siteFolder##*/}" --project "${_projectName}" --local-publish "${_localPublish}" "$@"
