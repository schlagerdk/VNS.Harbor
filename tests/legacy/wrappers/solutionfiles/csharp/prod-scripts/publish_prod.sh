#!/bin/bash
# Publish this C# site (always a Release build) to this environment: a production server over SSH, or a local folder
# when PROD_LOCAL_PATH is set. The settings below are the environment's; the publish itself is harbor's
# (harbor env publish), which makes the folder, service, Apache config and certificate there when they are missing.
# Requirements on prod: VNS apache/init.sh has been run, and the login is root or has passwordless sudo.
# With HTTPS, DNS for PROD_BINDING must point to the target server before the certificate can be issued.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
set -euo pipefail

# ---- Production settings (filled in by create_prod.sh, edit if needed) -----
PROD_LOCAL_PATH=@PROD_LOCAL_PATH@   # If set: publish to this local folder on this server instead of a remote server
PROD_IP=@PROD_IP@                   # IP or hostname of the production server
PROD_USER=@PROD_USER@               # SSH login
PROD_PASSWORD=@PROD_PASSWORD@       # SSH password (needs sshpass). Empty = use an SSH key
PROD_SSH_PORT=@PROD_SSH_PORT@
PROD_FOLDER=@PROD_FOLDER@           # Site name on prod: folder /var/www/<folder>, service and Apache config
PROD_BINDING=@PROD_BINDING@         # Public domain on prod, e.g. account.vns.dk
PROD_HTTPS=@PROD_HTTPS@             # n = plain HTTP without a certificate. Read when the Apache config is created
PROD_ENVIRONMENT="@PROD_ENVIRONMENT@"
PROD_ADMIN_MAIL="@ADMIN_MAIL@"
# ---------------------------------------------------------------------------

_projectName=@PROJECT_NAME@
_localPublish=@PUBLISH_FOLDER@   # The site's local publish folder: source of appsettings*.json on the first publish

_self=$(realpath "$0")
_scriptFolder=$(dirname "${_self}")
_siteFolder=${_scriptFolder%/scripts*}   # scripts/ or scripts/targets/<environment>/
_id=prod
[[ ${_scriptFolder} != */scripts/targets/* ]] || _id=${_scriptFolder##*/}
VNS_TARGET=${_id} exec "@APACHE_DIR@/../bin/harbor" env publish "${_siteFolder##*/}" --project "${_projectName}" --local-publish "${_localPublish}"
