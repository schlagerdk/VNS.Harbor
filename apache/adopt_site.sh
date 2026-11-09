#!/bin/bash
# Usage: adopt_site.sh <site> <main-site>                    Make a published site an environment of its main site
#        adopt_site.sh --release <main-site> <environment>   Make it a separate site again
# Only the production scripts are created or removed. The site's files, service and Apache config stay where they
# are and it keeps running. Later publishes to the environment come from the main site's project.
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
cd "$(dirname "$0")"
source lib/common.sh
source lib/prod.sh

restore_test_scripts() {
	[[ -z $(prod_targets) ]] || return 0
	local _name
	for _name in publish clear_logs; do
		if [[ -f ${_siteRoot}/scripts/${_name}_local.sh && ! -e ${_siteRoot}/scripts/${_name}.sh ]]; then
			mv "${_siteRoot}/scripts/${_name}_local.sh" "${_siteRoot}/scripts/${_name}.sh"
		fi
	done
}

if [[ ${1:-} == --release ]]; then
	_site=${2:-}
	validate_site_name "${_site}"
	set_site_vars
	export VNS_TARGET=${3:-}
	[[ -n ${VNS_TARGET} && ${VNS_TARGET} != prod ]] || { echo "Choose the environment to move out." >&2; exit 1; }
	load_prod_settings
	if [[ -z ${PROD_LOCAL_PATH} || ${PROD_LOCAL_PATH%/}/ == "${_siteRoot}"/* ]]; then
		echo "This environment is not a separate site, so it cannot be moved out." >&2
		exit 1
	fi
	rm -rf "${_siteRoot:?}/scripts/targets/${VNS_TARGET:?}"
	rm -f "${_siteRoot}/.vns-publish/target-${VNS_TARGET}.json"
	restore_test_scripts
	echo "${PROD_FOLDER} is a separate site again. Its files and service were not touched."
	exit 0
fi

_adopted=${1:-}
_mainSite=${2:-}
validate_site_name "${_adopted}"
validate_site_name "${_mainSite}"
[[ ${_adopted} != "${_mainSite}" ]] || { echo "A site cannot be moved under itself." >&2; exit 1; }

# ---- the site that is moved
_site=${_adopted}
set_site_vars
_adoptedRoot=${_siteRoot}
_adoptedConfig=${_configPath}
_adoptedService=${_servicePath}
[[ -d ${_adoptedRoot} ]] || { echo "Site not found: ${_adopted}" >&2; exit 1; }
if compgen -G "${_adoptedRoot}/*.csproj" >/dev/null; then
	echo "${_adopted} has its own project, so it is a main site." >&2
	exit 1
fi
if [[ -n $(prod_targets) ]]; then
	echo "${_adopted} has environments of its own." >&2
	exit 1
fi
[[ -f ${_adoptedService} ]] || { echo "${_adopted} has no service." >&2; exit 1; }
[[ -f ${_adoptedConfig} ]] || { echo "${_adopted} has no Apache config." >&2; exit 1; }

_dll=$(sed -n 's/^ExecStart=//p' "${_adoptedService}" | grep -oE '[^ "]+\.dll' | head -n 1 || true)
[[ -n ${_dll} ]] || { echo "Could not find the dll that ${_adopted} runs." >&2; exit 1; }
_dll=$(basename "${_dll}" .dll)
_workingFolder=$(sed -n 's/^WorkingDirectory=//p' "${_adoptedService}" | head -n 1)
_workingFolder=${_workingFolder%/}
_workingFolder=${_workingFolder:-${_adoptedRoot}}
if [[ ${_workingFolder} != "${_adoptedRoot}" && ${_workingFolder} != "${_adoptedRoot}"/* ]]; then
	echo "${_adopted} runs from ${_workingFolder}, outside its own folder." >&2
	exit 1
fi
_adoptedBinding=$(awk '$1 == "ServerName" { print $2; exit }' "${_adoptedConfig}")
[[ -n ${_adoptedBinding} ]] || { echo "${_adopted} has no ServerName." >&2; exit 1; }
_adoptedHttps=n
if grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${_adoptedConfig}"; then _adoptedHttps=y; fi
_adoptedEnvironment=$(sed -n 's/^Environment=ASPNETCORE_ENVIRONMENT=//p' "${_adoptedService}" | head -n 1)
case "${_adoptedEnvironment}" in
	Development | Test | Staging | Production) ;;
	*) _adoptedEnvironment=Production ;;
esac

# ---- the main site. These are also the values create_prod.sh is rendered with.
_site=${_mainSite}
set_site_vars
load_main_site
_assembly=$(sed -n 's:.*<AssemblyName>\(.*\)</AssemblyName>.*:\1:p' "${_projects[0]}" | head -n 1)
_assembly=${_assembly:-${_projectName}}
if [[ ${_assembly} != "${_dll}" ]]; then
	echo "${_adopted} runs ${_dll}.dll, but ${_mainSite} builds ${_assembly}.dll. A publish would break the site." >&2
	exit 1
fi
VNS_PROD_MODAL=1 VNS_PROD_NO_PUBLISH=1 VNS_PROD_TARGET=local VNS_PROD_LOCAL_PATH=${_workingFolder} \
	VNS_PROD_FOLDER=${_adopted} VNS_PROD_BINDING=${_adoptedBinding} VNS_PROD_ENVIRONMENT=${_adoptedEnvironment} \
	VNS_PROD_HTTPS=${_adoptedHttps} run_create_prod
echo "${_adopted} is now an environment of ${_mainSite}. Its files and service were not touched, and nothing was published."
