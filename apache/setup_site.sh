#!/bin/bash
# Usage: setup_site.sh <folder> <binding|*> <y|n certificate> [Development|Test|Staging|Production]
# Set up a folder that was put in /var/www by hand, like Create site does, without overwriting what is in it:
#   a C# project (one .csproj): the site's scripts, and a build in _publish unless one is there already
#   a build (a dll with its .runtimeconfig.json, in the folder or in _publish): run as it is, nothing is built
#   anything else: a static HTML site
# Umbraco is recognised by Umbraco.Cms and gets the same service and Apache config as a site Create site makes.
# Only the service, the Apache config, the certificate and missing scripts are created. The folder's files are
# not changed, except that the folder the site runs from is owned by www-data, so the site can write there, and a
# project gets its publish folder excluded from its build, as Create site does.
set -euo pipefail
cd "$(dirname "$0")"
source lib/common.sh
source lib/dotnet.sh

trap 'echo "FAILED at ${BASH_SOURCE[0]}:${LINENO} (exit $?): ${BASH_COMMAND}" >&2' ERR
trap 'release_port_reservation' EXIT
set -o errtrace

_site=${1:-}
_binding=${2:-}
_createCertificate=${3:-}
_environment=${4:-}
ask _site "Enter folder name"
validate_site_name "${_site}"
set_site_vars
[[ -d ${_siteRoot} && ! -L ${_siteRoot} ]] || { echo "${_siteRoot} is not a folder." >&2; exit 1; }
if [[ -e ${_servicePath} ]]; then
	echo "${_site} is already set up: it has a service." >&2
	exit 1
fi

# ---- what is in the folder
_type=html
_csharpKind=""
_publishFolder=.
_projects=("${_siteRoot}"/*.csproj)
if [[ -f ${_projects[0]} ]]; then
	((${#_projects[@]} == 1)) || { echo "${_site} has more than one .csproj. A site needs exactly one project." >&2; exit 1; }
	_type=project
	_projectName=$(basename "${_projects[0]}" .csproj)
	validate_project_name "${_projectName}"
	_assembly=$(sed -n 's:.*<AssemblyName>\(.*\)</AssemblyName>.*:\1:p' "${_projects[0]}" | head -n 1)
	if [[ -n ${_assembly} && ${_assembly} != "${_projectName}" ]]; then
		echo "${_projectName}.csproj builds ${_assembly}.dll. A site's project must build a dll with its own name." >&2
		exit 1
	fi
	_publishFolder=_publish
	_csharpKind=empty
	grep -qs "Umbraco.Cms" "${_projects[0]}" && _csharpKind=umbraco
else
	for _folder in . _publish; do
		[[ -d ${_siteRoot}/${_folder} ]] || continue
		_runtime=$(find "${_siteRoot}/${_folder}" -maxdepth 1 -name '*.runtimeconfig.json' -print -quit 2>/dev/null || true)
		[[ -n ${_runtime} ]] || continue
		_projectName=$(basename "${_runtime}" .runtimeconfig.json)
		[[ -f ${_siteRoot}/${_folder}/${_projectName}.dll ]] || continue
		_type=build
		_publishFolder=${_folder}
		_csharpKind=empty
		grep -qs '"Umbraco.Cms/' "${_siteRoot}/${_folder}/${_projectName}.deps.json" && _csharpKind=umbraco
		break
	done
	if [[ ${_type} == html && -f ${_siteRoot}/app/app.py ]]; then
		echo "${_site} holds a Python app. Setting up a Python site from a folder is not supported: create it with Create site." >&2
		exit 1
	fi
fi
# A C# site with an Apache config but no service was only half set up, e.g. when its build failed: it is finished.
# Its config is replaced. An HTML site has no service, so its config means it is set up.
if [[ -e ${_configPath} ]]; then
	if [[ ${_type} == html ]]; then
		echo "${_site} is already set up: it has an Apache config." >&2
		exit 1
	fi
	echo "${_site} has an Apache config but no service: the setup is finished and the config is replaced"
fi
case "${_type}" in
	project) echo "Found: the ${_csharpKind/empty/C#} project ${_projectName}.csproj" ;;
	build) echo "Found: the ${_csharpKind/empty/C#} build ${_projectName}.dll in ${_publishFolder/#./the folder}" ;;
	html) echo "Found: static files, set up as an HTML site" ;;
esac

# ---- binding and certificate, like Create site
ask _createCertificate "Create an HTTPS certificate? (y/n)" y
case "${_createCertificate,,}" in
	y | yes) _createCertificate=y ;;
	n | no) _createCertificate=n ;;
	*) echo "Certificate option must be y or n." >&2; exit 1 ;;
esac
if [[ -z ${_binding} && ${_site} == *.* ]]; then
	ask _binding "Enter site URL binding (* = answer any name)" "${_site}"
else
	ask _binding "Enter site URL binding (* = answer any name)"
fi
_defaultSite=n
if [[ ${_binding} == "*" ]]; then
	_current=$(default_site)
	[[ -z ${_current} ]] || { echo "${_current} already answers any name. Only one site can." >&2; exit 1; }
	_defaultSite=y
	_binding=$(server_own_name)
	if [[ ${_createCertificate} == y && ${_binding} != *.* ]]; then
		echo "This server's name, ${_binding}, has no dot, so it cannot get a certificate." >&2
		exit 1
	fi
fi
_httpOnly=0
[[ ${_createCertificate} != n ]] || _httpOnly=1
_binding=$(normalize_site_binding "${_binding}")
for _enabledConfig in /etc/apache2/sites-enabled/*.conf; do
	# The site's own config, from a setup that was not finished, does not hold the binding against it
	[[ -e ${_enabledConfig} && $(readlink -f "${_enabledConfig}") != "$(readlink -f "${_configPath}")" ]] || continue
	if awk -v binding="${_binding}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(binding)) found = 1 } END { exit !found }' "${_enabledConfig}"; then
		echo "${_binding} is already used by ${_enabledConfig}" >&2
		exit 1
	fi
done
if [[ ${_type} != html ]]; then
	_environment=${_environment:-$([[ ${_type} == build ]] && echo Production || echo Test)}
	case "${_environment}" in
		Development | Test | Staging | Production) ;;
		*) echo "Environment must be Development, Test, Staging or Production." >&2; exit 1 ;;
	esac
fi

# Scripts are only added: a script that is in the folder already is kept as it is
install_missing_scripts() {
	local _src=$1 _file
	[[ -d ${_src} ]] || return 0
	sudo mkdir -p "${_siteRoot}/scripts"
	for _file in "${_src}"/*; do
		[[ -f ${_file} && ! -e ${_siteRoot}/scripts/$(basename "${_file}") ]] || continue
		render_to "${_file}" "${_siteRoot}/scripts/$(basename "${_file}")"
		sudo chmod +x "${_siteRoot}/scripts/$(basename "${_file}")"
	done
}

if [[ ${_type} == html ]]; then
	_docRoot=${_siteRoot}
	[[ ${_createCertificate} == n ]] || issue_certificate
	install_apache_config "${_solutionFolder}/html/apache.conf.tpl"
	install_missing_scripts "${_solutionFolder}/common/scripts"
else
	_kindFolder="${_solutionFolder}/csharp"
	[[ ${_csharpKind} != umbraco ]] || _kindFolder="${_solutionFolder}/csharp/umbraco"
	_docRoot="${_siteRoot}/wwwroot"
	[[ ${_publishFolder} == . ]] || _docRoot="${_siteRoot}/${_publishFolder}/wwwroot"
	_publishRoot=${_siteRoot}
	[[ ${_publishFolder} == . ]] || _publishRoot="${_siteRoot}/${_publishFolder}"
	_buildConfig=Debug
	[[ ${_environment} != Production ]] || _buildConfig=Release

	allocate_ports
	sudo mkdir -p "${_docRoot}" "/var/lib/vns-roots/${_site}"
	[[ ${_createCertificate} == n ]] || issue_certificate
	install_missing_scripts "${_solutionFolder}/common/scripts"
	install_missing_scripts "${_solutionFolder}/common/service-scripts"
	# Umbraco's clear_logs.sh works on a build as well as on a project
	install_missing_scripts "${_kindFolder}/scripts"
	if [[ ${_type} == project ]]; then
		install_missing_scripts "${_solutionFolder}/csharp/scripts"
		# The publish folder is inside the project: without this, the next build would include the last one
		if ! grep -qs "${_publishFolder}/\*\*" "${_projects[0]}"; then
			echo "Exclude ${_publishFolder} from the project's build"
			exclude_from_build "${_projects[0]}" "${_publishFolder}"
		fi
		if [[ -f ${_publishRoot}/${_projectName}.dll ]]; then
			echo "${_publishFolder} already holds a build: it is used as it is"
		else
			echo "Build the project into ${_publishFolder}"
			cd "${_siteRoot}"
			sudo python3 "${_apacheDir}/lib/publish_status.py" run "${_siteRoot}" test python3 "${_apacheDir}/lib/build_as.py" -- dotnet publish "${_projects[0]}" -c "${_buildConfig}" -p:PublishDir="${_publishFolder}" -p:CustomAfterMicrosoftCommonTargets="${_apacheDir}/lib/publish.targets" --runtime "$(linux_runtime_identifier)" --self-contained false --nologo
			cd "${_apacheDir}"
		fi
	fi
	# The service runs as www-data and writes inside the folder it runs from
	sudo chown -R www-data:www-data "${_publishRoot}"

	_conf="${_kindFolder}/apache.conf.tpl"
	_service="${_kindFolder}/service.tpl"
	[[ -f ${_conf} ]] || _conf="${_solutionFolder}/csharp/apache.conf.tpl"
	[[ -f ${_service} ]] || _service="${_solutionFolder}/csharp/service.tpl"
	install_apache_config "${_conf}"
	install_service "${_service}"
fi

release_port_reservation
[[ ${_defaultSite} != y ]] || make_default_site
_scheme=http
[[ ${_createCertificate} == n ]] || _scheme=https
echo "Site set up: ${_scheme}://${_binding}/ (folder: ${_site})"
[[ ${_defaultSite} != y ]] || echo "It also answers any name that no other site has."
