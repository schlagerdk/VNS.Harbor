#!/bin/bash
# Add a production target to this site: a remote Linux server (SSH), a local folder, or an IIS site on a Windows server (SMB).
# Asks for the production settings, then:
#   publish.sh -> publish_local.sh, clear_logs.sh -> clear_logs_local.sh
#   creates publish_prod.sh, clear_logs_prod.sh and sync_prod.sh (Umbraco) with the answers filled in
# Publishes production, then syncs content when available. Production is always a Release build.
# This script deletes itself after the production scripts have been created.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
	exec sudo "$0" "$@"
fi

source "@APACHE_DIR@/lib/common.sh"

# Values baked in when the site was created
_site=@SITE@
_binding=@BINDING@
_projectName=@PROJECT_NAME@
_siteRoot=@SITE_ROOT@
_publishFolder=@PUBLISH_FOLDER@
_adminMail=@ADMIN_MAIL@
_kind=@CSHARP_KIND@

_scripts="${_siteRoot}/scripts"
_self=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
_tmp=""
cleanup() {
	[[ -z ${_tmp} ]] || rm -rf "${_tmp}"
	if [[ ${VNS_PROD_MODAL:-} == 1 ]]; then
		rm -f -- "${_self}"
	fi
}
trap cleanup EXIT

if [[ ! -f ${_scripts}/publish.sh || -e ${_scripts}/publish_local.sh || -e ${_scripts}/publish_prod.sh ]]; then
	echo "A production target has already been created for this site (or publish.sh is missing)." >&2
	exit 1
fi

_localPath=""
_ip=""
_user=""
_password=""
_sshPort=""
_smbShare=""
_smbUser=""
_smbPassword=""
_smbDomain=""
_https=""
if [[ ${VNS_PROD_MODAL:-} == 1 ]]; then
	_target=${VNS_PROD_TARGET:-}
	_localPath=${VNS_PROD_LOCAL_PATH:-}
	_ip=${VNS_PROD_IP:-}
	_user=${VNS_PROD_USER:-}
	_password=${VNS_PROD_PASSWORD:-}
	_sshPort=${VNS_PROD_SSH_PORT:-22}
	_prodFolder=${VNS_PROD_FOLDER:-}
	_prodBinding=${VNS_PROD_BINDING:-}
	_prodEnvironment=${VNS_PROD_ENVIRONMENT:-Production}
	_smbShare=${VNS_PROD_SMB_SHARE:-}
	_smbUser=${VNS_PROD_SMB_USER:-}
	_smbPassword=${VNS_PROD_SMB_PASSWORD:-}
	_smbDomain=${VNS_PROD_SMB_DOMAIN:-}
	_https=${VNS_PROD_HTTPS:-y}
else
	_target=${1:-}
	ask _target "Production target (remote/local/windows)" remote
	case "${_target}" in
		remote)
			ask _ip "Production server IP or hostname"
			[[ -n ${_ip} ]] || { echo "IP is required." >&2; exit 1; }
			ask _user "SSH login" root
			read -r -s -p "SSH password (empty = use an SSH key): " _password
			echo
			ask _sshPort "SSH port" 22
			[[ ${_sshPort} =~ ^[0-9]+$ ]] || { echo "SSH port must be a number." >&2; exit 1; }
			_defaultFolder=${_site}
			;;
		local)
			ask _localPath "Local production folder (absolute path)" "${_siteRoot}/_prod"
			[[ ${_localPath} == /* ]] || { echo "The path must be absolute." >&2; exit 1; }
			_defaultFolder=prod-${_site}
			;;
		windows)
			echo "The IIS site and its folder must already exist on the Windows server."
			ask _smbShare 'Folder of the IIS site (e.g. //server/share/site-folder)'
			ask _smbUser "SMB username" Administrator
			read -r -s -p "SMB password: " _smbPassword
			echo
			ask _smbDomain "SMB domain (empty = the server is not on a domain)"
			;;
		*)
			echo "Target must be remote, local or windows." >&2
			exit 1
			;;
	esac
	if [[ ${_target} == windows ]]; then
		ask _prodBinding "Production binding (the domain of the IIS site)" "${_binding}"
		_prodFolder=${_prodBinding}
	else
		ask _prodFolder "Production site name (folder /var/www/<name>, service and Apache config)" "${_defaultFolder}"
		ask _prodBinding "Production binding (domain)" "${_prodFolder}"
	fi
	ask _prodEnvironment "Environment (Development/Test/Staging/Production)" Production
	# Follow this site: an HTTP-only site usually has an HTTP-only production site
	_httpsDefault=n
	if grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "/etc/apache2/sites-available/${_site}.conf" 2>/dev/null; then
		_httpsDefault=y
	fi
	if [[ ${_target} == windows ]]; then
		ask _https "Does the IIS site use HTTPS? (y/n)" "${_httpsDefault}"
	else
		ask _https "Use HTTPS? y issues a certificate, n runs on plain HTTP (y/n)" "${_httpsDefault}"
	fi
fi

case "${_prodEnvironment}" in
	Development | Test | Staging | Production) ;;
	*) echo "Environment must be Development, Test, Staging or Production." >&2; exit 1 ;;
esac
case "${_https,,}" in
	y | yes) _https=y ;;
	n | no) _https=n ;;
	*) echo "HTTPS must be y or n." >&2; exit 1 ;;
esac
[[ ${_target} == remote || ${_target} == local || ${_target} == windows ]] || { echo "Target must be remote, local or windows." >&2; exit 1; }
_prodFolder=$(normalize_dns_name "${_prodFolder}")
_prodBinding=$(normalize_dns_name "${_prodBinding}" binding)
validate_site_name "${_prodFolder}"
validate_binding "${_prodBinding}"
if [[ ${_target} == remote ]]; then
	[[ -n ${_ip} && -n ${_user} && ${_sshPort} =~ ^[0-9]+$ && ${_sshPort} -ge 1 && ${_sshPort} -le 65535 ]] || {
		echo "Remote target requires a host, SSH user and valid SSH port." >&2
		exit 1
	}
elif [[ ${_target} == windows ]]; then
	_smbShare=${_smbShare//\\//}
	_smbShare=${_smbShare%/}
	# A folder below the share is required, so a typo cannot mirror files into the root of a drive.
	if [[ ! ${_smbShare} =~ ^//[A-Za-z0-9][A-Za-z0-9.-]*/[^/]+(/[^/]+)+$ || /${_smbShare}/ == */../* || /${_smbShare}/ == */./* ]]; then
		echo "The folder must look like //server/share/folder." >&2
		exit 1
	fi
	[[ -n ${_smbUser} && -n ${_smbPassword} ]] || { echo "Windows target requires an SMB username and password." >&2; exit 1; }
	[[ ${_smbDomain} =~ ^[A-Za-z0-9._-]*$ ]] || { echo "Invalid SMB domain." >&2; exit 1; }
else
	[[ ${_localPath} == /* ]] || { echo "The local production folder must be an absolute path." >&2; exit 1; }
	if [[ $(normalize_dns_name "${_prodFolder}" unit) == "$(normalize_dns_name "${_site}" unit)" || ${_prodBinding} == "${_binding}" ]]; then
		echo "A local production site needs a different name and binding than this site." >&2
		exit 1
	fi
fi
echo
echo "Target:   ${_target}${_ip:+ (${_user}@${_ip}:${_sshPort})}${_localPath:+ (${_localPath})}${_smbShare:+ (${_smbUser}@${_smbShare})}"
echo "Site:     ${_prodFolder}"
echo "Binding:  ${_prodBinding}"
echo "Environment: ${_prodEnvironment}"
echo "Protocol: $([[ ${_https} == y ]] && echo HTTPS || echo HTTP)"
if [[ ${VNS_PROD_MODAL:-} != 1 ]]; then
	read -r -p "Create the production scripts? [Y/n]: " _confirm
	[[ ${_confirm:-y} =~ ^[Yy] ]] || { echo "Cancelled."; exit 1; }
fi

_templates=("${_solutionFolder}/csharp/prod-scripts")
[[ ${_kind} != umbraco ]] || _templates+=("${_solutionFolder}/csharp/umbraco/prod-scripts")
if [[ ${_target} == windows ]]; then
	_templates=("${_solutionFolder}/csharp/windows-prod-scripts")
	[[ ${_kind} != umbraco ]] || _templates+=("${_solutionFolder}/csharp/umbraco/windows-prod-scripts")
	# Stop before anything is changed if the share or the login is wrong
	(
		source "@APACHE_DIR@/lib/windows_prod.sh"
		PROD_WINDOWS_SHARE=${_smbShare} PROD_SMB_USER=${_smbUser} PROD_SMB_PASSWORD=${_smbPassword} PROD_SMB_DOMAIN=${_smbDomain}
		windows_smb_connect
	)
fi

_q() { printf '%q' "$1"; }
export R_PROD_LOCAL_PATH="$(_q "${_localPath}")" R_PROD_IP="$(_q "${_ip}")" R_PROD_USER="$(_q "${_user}")" \
	R_PROD_PASSWORD="$(_q "${_password}")" R_PROD_SSH_PORT="$(_q "${_sshPort}")" R_PROD_FOLDER="$(_q "${_prodFolder}")" \
	R_PROD_BINDING="$(_q "${_prodBinding}")" R_PROD_ENVIRONMENT="${_prodEnvironment}" R_ADMIN_MAIL="${_adminMail}" \
	R_PROJECT_NAME="${_projectName}" R_PUBLISH_FOLDER="${_publishFolder}" R_SITE="${_site}" \
	R_PROD_HTTPS="${_https}" R_PROD_WINDOWS_SHARE="$(_q "${_smbShare}")" R_PROD_SMB_USER="$(_q "${_smbUser}")" \
	R_PROD_SMB_PASSWORD="$(_q "${_smbPassword}")" R_PROD_SMB_DOMAIN="$(_q "${_smbDomain}")"
unset _password VNS_PROD_PASSWORD _smbPassword VNS_PROD_SMB_PASSWORD

# Render everything first, so a missing template fails before anything is renamed
_tmp=$(mktemp -d)
for _dir in "${_templates[@]}"; do
	for _file in "${_dir}"/*.sh; do
		[[ -f ${_file} ]] || continue
		perl -pe 's/\@([A-Z_]+)\@/exists $ENV{"R_$1"} ? $ENV{"R_$1"} : "\@$1\@"/ge' "${_file}" >"${_tmp}/$(basename "${_file}")"
	done
done
unset R_PROD_PASSWORD R_PROD_SMB_PASSWORD
[[ -f ${_tmp}/publish_prod.sh ]] || { echo "Template publish_prod.sh not found." >&2; exit 1; }

mv "${_scripts}/publish.sh" "${_scripts}/publish_local.sh"
[[ ! -f ${_scripts}/clear_logs.sh ]] || mv "${_scripts}/clear_logs.sh" "${_scripts}/clear_logs_local.sh"
for _file in "${_tmp}"/*.sh; do
	install -m 700 "${_file}" "${_scripts}/$(basename "${_file}")"
done

echo "Created in ${_scripts}:"
ls -1 "${_scripts}" | grep -E '_(local|prod)\.sh$' | sed 's/^/  /'
rm -f -- "${_self}"

echo "Publish to production"
bash "${_scripts}/publish_prod.sh"
if [[ -f ${_scripts}/sync_prod.sh ]]; then
	echo "Sync content to production"
	bash "${_scripts}/sync_prod.sh"
fi
echo "Production setup complete."
