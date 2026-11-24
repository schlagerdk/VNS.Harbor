#!/bin/bash
# Shell helpers that harbor sends along when it works on another server (source /opt/harbor/current/lib/common.sh):
# names, bindings, certificates and the production TLS config there. The site templates (templates/sites) use
# @PLACEHOLDERS@ that render() fills in.

# The Harbor installation this file is in (its lib/ folder); named _apacheDir, as the commands sent to a server on VNS
# 1.x find the same in /vns/apache
_apacheDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export R_APACHE_DIR="${_apacheDir}"
_solutionFolder="${_apacheDir}/templates/sites"
_portReservationDir=/run/lock/vns-apache-port-reservations
_portAllocationLock=/run/lock/vns-apache-port-allocation.lock
_portReservationFile=""
_rootPath=${VNS_WEB_ROOT:-/var/www}
_publishFolder=_publish
_acme=/etc/acmesh/acme.sh
# Its folders, as harbor server init installed it: without them acme.sh keeps its state in /root/.acme.sh
_acmeFolders=(--home /etc/acmesh --config-home /etc/ssl/data)
_acmeServer=letsencrypt
_adminMail=mhs@vns.dk

# Ask for a value unless it is already set. Usage: ask VARNAME "Prompt" [default]
# A yes/no question, a prompt ending in (y/n) with y or n as default, is shown as [Y/n] or [y/N] like the other scripts'.
# -e lets backspace and the arrow keys edit the answer, so no control characters end up in it; spaces around it are dropped.
ask() {
	local _name=$1 _prompt=$2 _default=${3:-} _answer
	if [[ -z ${!_name:-} ]]; then
		if [[ ${_prompt} == *" (y/n)" && ${_default} == [yYnN] ]]; then
			_prompt=${_prompt% (y/n)}
			[[ ${_default} == [yY] ]] && _prompt+=" [Y/n]" || _prompt+=" [y/N]"
		elif [[ -n ${_default} ]]; then
			_prompt+=" [${_default}]"
		fi
		read -r -e -p "${_prompt}: " _answer
		_answer=${_answer#"${_answer%%[![:space:]]*}"}
		_answer=${_answer%"${_answer##*[![:space:]]}"}
		printf -v "${_name}" '%s' "${_answer:-${_default}}"
	fi
}

normalize_dns_name() {
	python3 "${_apacheDir}/lib/domain_names.py" "$1" "${2:-site}"
}

validate_site_name() {
	local _name=$1
	if [[ -z ${_name} ]]; then
		echo "No site given." >&2
		return 1
	fi
	if ! normalize_dns_name "${_name}" site >/dev/null; then
		echo "Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens." >&2
		return 1
	fi
}

validate_binding() {
	local _binding=$1 _label
	if [[ ! ${_binding} =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]; then
		echo "Invalid binding. Enter a fully qualified DNS name, without scheme or path." >&2
		return 1
	fi
	if ((${#_binding} > 253)); then
		echo "Binding is too long." >&2
		return 1
	fi
	IFS=. read -r -a _labels <<<"${_binding}"
	for _label in "${_labels[@]}"; do
		if ((${#_label} > 63)); then
			echo "DNS labels cannot exceed 63 characters." >&2
			return 1
		fi
	done
}

# The site that answers any name: Apache sends a request for a name no site has to the first site it loads on
# the port, so this link sorts before every other config, also Ubuntu's 000-default.conf. It points to the
# site's own config, so the site keeps its own name and config as well.
_defaultSiteLink=/etc/apache2/sites-enabled/00-vns-default.conf

# This server's own name, for a site that answers any name
server_own_name() {
	local _name
	_name=$(hostname -f 2>/dev/null || true)
	[[ ${_name} =~ ^[A-Za-z0-9.-]+$ ]] || _name=$(hostname)
	[[ ${_name} =~ ^[A-Za-z0-9.-]+$ ]] || _name=localhost
	printf '%s\n' "${_name}" | tr '[:upper:]' '[:lower:]'
}

# The folder name of the site that answers any name, or nothing
default_site() {
	local _target
	_target=$(readlink "${_defaultSiteLink}" 2>/dev/null || true)
	[[ -n ${_target} ]] || return 0
	_target=${_target##*/}
	printf '%s\n' "${_target%.conf}"
}

# Make _site the site that answers any name
make_default_site() {
	local _current
	_current=$(default_site)
	if [[ -n ${_current} && ${_current} != "${_site}" ]]; then
		echo "${_current} already answers any name. Only one site can: delete it or give it a binding first." >&2
		return 1
	fi
	echo "Make ${_site} answer any name"
	sudo ln -sfn "../sites-available/${_site}.conf" "${_defaultSiteLink}"
	sudo apache2ctl configtest
	sudo systemctl reload apache2
}

# A binding or alias of a site. With _httpOnly=1, a site without a certificate, it may be a name without a dot,
# like a server's own name on the local network: a certificate needs a public name with one.
normalize_site_binding() {
	local _name
	if [[ ${_httpOnly:-0} == 1 && $1 != *.* ]]; then
		_name=$(normalize_dns_name "$1" name) || return 1
	else
		if [[ $1 != *.* ]]; then
			echo "'$1' has no dot, so it cannot be in a certificate. Only a site on HTTP can use a name without a dot." >&2
			return 1
		fi
		_name=$(normalize_dns_name "$1" binding) || return 1
		validate_binding "${_name}" || return 1
	fi
	printf '%s\n' "${_name}" | tr '[:upper:]' '[:lower:]'
}

normalize_server_aliases() {
	local _primary=$1 _raw=${2//,/ } _alias _candidate _found _aliases=""
	_normalizedAliases=""
	_primary=$(printf '%s' "${_primary}" | tr '[:upper:]' '[:lower:]')
	while IFS= read -r _alias; do
		[[ -n ${_alias} ]] || continue
		_candidate=$(normalize_site_binding "${_alias}") || {
			echo "Invalid alias '${_alias}'." >&2
			return 1
		}
		_alias=${_candidate}
		[[ ${_alias} == "${_primary}" ]] && continue
		_found=0
		for _candidate in ${_aliases}; do [[ ${_candidate} == "${_alias}" ]] && _found=1; done
		if ((!_found)); then
			_aliases="${_aliases:+${_aliases} }${_alias}"
		fi
	done < <(printf '%s\n' "${_raw}" | tr '[:space:]' '\n')
	_normalizedAliases=${_aliases}
}

server_aliases_from_config() {
	local _config=$1 _primary=$2 _raw
	_raw=$(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${_config}")
	normalize_server_aliases "${_primary}" "${_raw}" || return 1
	printf '%s' "${_normalizedAliases}"
}

replace_server_aliases() {
	local _config=$1 _aliases=$2 _tmp
	_tmp=$(mktemp)
	if ! awk -v aliases="${_aliases}" '
		/^[[:space:]]*ServerAlias[[:space:]]/ { next }
		{
			print
			if ($1 == "ServerName" && aliases != "") print "  ServerAlias " aliases
		}
	' "${_config}" >"${_tmp}"; then
		rm -f "${_tmp}"
		return 1
	fi
	cat "${_tmp}" >"${_config}"
	rm -f "${_tmp}"
}

ensure_production_tls_config() {
	local _config=$1 _service=$2 _site=$3 _binding=$4 _root=$5 _certdir=$6 _mail=$7 _umbraco=$8
	local _port _alias _candidate _found _normalizedBinding _aliases="" _aliasLine="" _extra=""

	[[ -f ${_config} ]] || { echo "Apache config not found: ${_config}" >&2; return 1; }
	if grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${_config}"; then
		return 0
	fi
	[[ -f ${_service} ]] || { echo "Production service not found: ${_service}" >&2; return 1; }
	_port=$(grep '^ExecStart=' "${_service}" | grep -oE '(localhost|127\.0\.0\.1|\*):[0-9]+' | head -n 1 | cut -d: -f2 || true)
	[[ ${_port} =~ ^[0-9]+$ ]] || { echo "Could not determine the production port from ${_service}." >&2; return 1; }
	_normalizedBinding=$(printf '%s' "${_binding}" | tr '[:upper:]' '[:lower:]')

	while IFS= read -r _alias; do
		[[ -n ${_alias} ]] || continue
		_alias=$(printf '%s' "${_alias}" | tr '[:upper:]' '[:lower:]')
		validate_binding "${_alias}" || {
			echo "Invalid ServerAlias '${_alias}' in ${_config}; refusing to build the HTTPS config." >&2
			return 1
		}
		_found=0
		[[ ${_alias} == "${_normalizedBinding}" ]] && _found=1
		for _candidate in ${_aliases}; do [[ ${_candidate} == "${_alias}" ]] && _found=1; done
		if ((!_found)); then
			_aliases="${_aliases:+${_aliases} }${_alias}"
		fi
	done < <(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${_config}")
	if [[ -n ${_aliases} ]]; then
		_aliasLine="  ServerAlias ${_aliases}"
	fi
	if [[ ${_umbraco} == 1 ]]; then
		_extra="
  ProxyPass /robots.txt http://localhost:${_port}/seo_robots_txt
  ProxyPassReverse /robots.txt http://localhost:${_port}/seo_robots_txt

  ProxyPass /sitemap.xml http://localhost:${_port}/seo_sitemap_xml
  ProxyPassReverse /sitemap.xml http://localhost:${_port}/seo_sitemap_xml
"
	fi

	cat >"${_config}" <<EOF
<VirtualHost *:80>
  ServerAdmin ${_mail}
  ServerName ${_binding}
${_aliasLine}
  DocumentRoot ${_root}/wwwroot

  RewriteEngine On
  RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/
  RewriteRule ^(.*)\$ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]

  ErrorLog /var/log/apache2/${_site}-rewrite-error.log
</VirtualHost>

<VirtualHost *:443>
  ServerAdmin ${_mail}
  ServerName ${_binding}
${_aliasLine}
  DocumentRoot ${_root}/wwwroot

  AllowEncodedSlashes NoDecode

  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteCond %{HTTP:Connection} upgrade [NC]
  RewriteRule /(.*) ws://localhost:${_port}/\$1 [P,L]

  ProxyPreserveHost On
  ProxyRequests Off
  RequestHeader set X-Forwarded-Proto "https"
${_extra}
  ProxyPass / http://localhost:${_port}/ nocanon
  ProxyPassReverse / http://localhost:${_port}/

  SSLEngine On
  SSLCertificateFile ${_certdir}/fullchain.cer
  SSLCertificateKeyFile ${_certdir}/${_binding}.key

  ErrorLog /var/log/apache2/${_site}-443-error.log
</VirtualHost>
EOF
}

set_certificate_domains() {
	local _config=$1 _primary _alias _domain _found _candidate
	local -a _seen
	_primary=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
	_certificateDomainArgs=(-d "${_primary}")
	_certificateDomainAliases=()
	_certificateDomains=("${_primary}")
	_seen=("${_primary}")
	_certificateDomainNames="${_primary}"
	[[ -f ${_config} ]] || return 0
	while IFS= read -r _alias; do
		[[ -n ${_alias} ]] || continue
		_domain=$(printf '%s' "${_alias}" | tr '[:upper:]' '[:lower:]')
		if ! validate_binding "${_domain}"; then
			echo "Invalid ServerAlias '${_alias}' in ${_config}; certificate issuance stopped." >&2
			return 1
		fi
		_found=0
		for _candidate in "${_seen[@]}"; do
			[[ ${_candidate} == "${_domain}" ]] && _found=1
		done
		((_found)) && continue
		_seen+=("${_domain}")
		_certificateDomainArgs+=(-d "${_domain}")
		_certificateDomainAliases+=("${_domain}")
		_certificateDomains+=("${_domain}")
		_certificateDomainNames+=" ${_domain}"
	done < <(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${_config}")
}

validate_project_name() {
	if [[ ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
		echo "Invalid C# project name. Use letters, numbers, dots, hyphens or underscores." >&2
		return 1
	fi
}

validate_publish_folder() {
	[[ $1 != . ]] || return 0
	if [[ ! $1 =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]]; then
		echo "Invalid publish folder. Use a single folder name with letters, numbers, dots, hyphens or underscores." >&2
		return 1
	fi
	case "${1,,}" in
		bin | obj | wwwroot | views | usync | scripts | properties | umbraco | app_plugins)
			echo "Publish folder name '$1' is reserved." >&2
			return 1
			;;
	esac
}

# Exclude a publish folder from the project's build globs. Usage: exclude_from_build <csproj> <folder>. Safe to repeat.
exclude_from_build() {
	local _csproj=$1 _folder=$2
	[[ ${_folder} != . ]] || return 0
	if grep -qF "Compile Remove=\"${_folder}/**\"" "${_csproj}"; then
		return 0
	fi
	sudo perl -0pi -e 's|</Project>\s*$|  <!-- Exclude publish folder from build -->\n  <ItemGroup>\n    <Content Remove="'"${_folder}"'/**" />\n    <Compile Remove="'"${_folder}"'/**" />\n    <None Remove="'"${_folder}"'/**" />\n  </ItemGroup>\n</Project>\n|' "${_csproj}"
}

# Placeholders available in every template. Call set_site_vars after _site, _binding and ports are known.
set_site_vars() {
	_siteRoot="${_rootPath}/${_site}"
	_certificateDirectory="/etc/ssl/certs/${_site}_ecc"
	_certificateFile="${_certificateDirectory}/fullchain.cer"
	_keyFile="${_certificateDirectory}/${_site}.key"
	_configPath="/etc/apache2/sites-available/${_site}.conf"
	_serviceName=$(normalize_dns_name "${_site}" unit)
	_servicePath="/etc/systemd/system/${_serviceName}.service"
}

# Print a template with placeholders replaced
render() {
	local _publishRoot="${_siteRoot}"
	if [[ ${_publishFolder} != . ]]; then
		_publishRoot+="/${_publishFolder}"
	fi
	sed \
		-e "s|@SITE_ROOT@/@PUBLISH_FOLDER@|${_publishRoot}|g" \
		-e "s|@SITE@|${_site}|g" \
		-e "s|@SERVICE_NAME@|$(normalize_dns_name "${_site}" unit)|g" \
		-e "s|@BINDING@|${_binding:-${_site}}|g" \
		-e "s|@PROJECT_NAME@|${_projectName:-${_site}}|g" \
		-e "s|@ROOT_PATH@|${_rootPath}|g" \
		-e "s|@SITE_ROOT@|${_siteRoot}|g" \
		-e "s|@PUBLISH_FOLDER@|${_publishFolder}|g" \
		-e "s|@DOC_ROOT@|${_docRoot:-}|g" \
		-e "s|@PORT_HTTP@|${_portHttp:-}|g" \
		-e "s|@DOTNET_VERSION@|${_dotNetVersion:-}|g" \
		-e "s|@UMBRACO_VERSION@|${_umbracoVersion:-}|g" \
		-e "s|@CERT_FILE@|${_certificateFile}|g" \
		-e "s|@KEY_FILE@|${_keyFile}|g" \
		-e "s|@ADMIN_MAIL@|${_adminMail}|g" \
		-e "s|@BUILD_CONFIG@|${_buildConfig:-Debug}|g" \
		-e "s|@ENVIRONMENT@|${_environment:-Test}|g" \
		-e "s|@APACHE_DIR@|${_apacheDir}|g" \
		-e "s|@CSHARP_KIND@|${_csharpKind:-}|g" \
		-e "s|@ACCESS_RULES@|${_accessRules:-Require all granted}|g" \
		"$1"
}

# render_to <template> <destination>  (destination written with sudo)
render_to() {
	render "$1" | sudo tee "$2" >/dev/null
}

# Copy script templates into "<siteRoot>/scripts" (later calls overwrite earlier ones)
install_scripts() {
	local _src=$1 _dest="${_siteRoot}/scripts" _file
	[[ -d ${_src} ]] || return 0
	sudo mkdir -p "${_dest}"
	for _file in "${_src}"/*; do
		[[ -f ${_file} ]] || continue
		render_to "${_file}" "${_dest}/$(basename "${_file}")"
		sudo chmod +x "${_dest}/$(basename "${_file}")"
	done
}

# Reserve the first available port starting at 4000 and set _portHttp.
# Existing Apache/systemd configurations, listening sockets and concurrent creations are respected.
allocate_ports() {
	local _port _siteName=${_site:-${SITE:-unknown}}
	sudo mkdir -p "${_portReservationDir}"
	_port=$(sudo flock -x "${_portAllocationLock}" bash -c '
		set -euo pipefail
		dir=$1
		owner_pid=$2
		site_name=$3
		shopt -s nullglob
		declare -a used=()

		for reservation in "$dir"/*.reserve; do
			port=${reservation##*/}
			port=${port%.reserve}
			read -r pid _site <"$reservation" || true
			if [[ ! ${pid:-} =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
				rm -f "$reservation"
			else
				used[$port]=1
			fi
		done

		command -v ss >/dev/null || { echo "ss is required to detect listening ports." >&2; exit 1; }
		while read -r _state _recv _send address _peer; do
			port=${address##*:}
			[[ $port =~ ^[0-9]+$ ]] && used[$port]=1
		done < <(ss -H -ltn)

		for config in /etc/apache2/sites-available/*.conf /etc/systemd/system/*.service; do
			[[ -f $config ]] || continue
			while IFS= read -r port; do
				[[ $port =~ ^[0-9]+$ ]] && used[$port]=1
			done < <(grep -hEo "(127\\.0\\.0\\.1|localhost|\\*):[0-9]{1,5}" "$config" 2>/dev/null | cut -d: -f2 || true)
		done

		for ((port = 4000; port <= 65535; port++)); do
			if [[ ! ${used[$port]+yes} && ! -e $dir/$port.reserve ]]; then
				printf "%s %s\\n" "$owner_pid" "$site_name" >"$dir/$port.reserve"
				printf "%s\\n" "$port"
				exit 0
			fi
		done
		echo "No available site ports in the range 4000-65535." >&2
		exit 1
	' _ "${_portReservationDir}" "$$" "${_siteName}") || return 1
	_portHttp=${_port}
	_portReservationFile="${_portReservationDir}/${_port}.reserve"
	echo "Port: ${_portHttp}"
}

release_port_reservation() {
	if [[ -n ${_portReservationFile:-} ]]; then
		sudo rm -f "${_portReservationFile}" || true
		_portReservationFile=""
	fi
}

require_acme() {
	if [[ ! -x ${_acme} ]]; then
		echo "acme.sh not found or not executable: ${_acme}" >&2
		exit 1
	fi
}

# Remove leftover acme.sh state (e.g. a domain key from an earlier site) so a fresh certificate can be issued.
# Skipped when another enabled site still uses the domain.
clean_stale_acme() {
	local _domain=$1 _conf
	for _conf in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${_conf} && ${_conf} != "/etc/apache2/sites-enabled/${_site}.conf" ]] || continue
		if awk -v d="${_domain}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${_conf}"; then
			return 0
		fi
	done
	if [[ -d /etc/ssl/certs/${_domain} || -d /etc/ssl/certs/${_domain}_ecc ]]; then
		echo "Remove leftover certificate data for ${_domain}"
		sudo "${_acme}" "${_acmeFolders[@]}" --remove -d "${_domain}" >/dev/null 2>&1 || true
		sudo rm -rf "/etc/ssl/certs/${_domain}" "/etc/ssl/certs/${_domain}_ecc"
	fi
}

# Temporary HTTP-only vhost, then issue and install the certificate. Requires _docRoot to exist.
issue_certificate() {
	require_acme
	set_certificate_domains "${_configPath}" "${_binding}"
	echo "Create temporary HTTP-only config for certificate validation"
	sudo mkdir -p "${_docRoot}"
	render_to "${_solutionFolder}/common/apache-temp.conf.tpl" "${_configPath}"
	if ((${#_certificateDomainAliases[@]})); then
		sudo env NEW_ALIASES="${_certificateDomainAliases[*]}" bash -c 'source "$1"; replace_server_aliases "$2" "$NEW_ALIASES"' _ "${_apacheDir}/lib/common.sh" "${_configPath}"
	fi

	sudo a2enmod ssl
	sudo apache2ctl configtest
	[[ -e "/etc/apache2/sites-enabled/${_site}.conf" ]] || sudo a2ensite "${_site}.conf"
	sudo systemctl reload apache2

	for _domain in "${_certificateDomains[@]}"; do clean_stale_acme "${_domain}"; done
	echo "Issue certificate"
	echo "Certificate domains: ${_certificateDomainNames}"
	# acme.sh returns 2 when a still-valid certificate is skipped
	sudo "${_acme}" "${_acmeFolders[@]}" --issue --apache --force --server "${_acmeServer}" "${_certificateDomainArgs[@]}" || {
		local _status=$?
		[[ ${_status} -eq 2 ]] || return "${_status}"
	}

	echo "Install certificate for Apache"
	sudo mkdir -p "${_certificateDirectory}"
	sudo "${_acme}" "${_acmeFolders[@]}" --install-cert -d "${_binding}" \
		--key-file "${_keyFile}" \
		--fullchain-file "${_certificateFile}" \
		--reloadcmd "systemctl reload apache2"
}

# A global ServerName, so Apache does not warn that it cannot determine the server's name
ensure_global_servername() {
	sudo bash "${_apacheDir}/set_servername.sh"
}

install_apache_config() {
	echo "Create Apache config"
	if [[ ${_createCertificate:-y} == n ]]; then
		render "$1" | awk '
			/<VirtualHost \*:443>/ { active=1; sub(":443", ":80") }
			active && !/^[[:space:]]*SSL/ {
				if (/^[[:space:]]*ProxyPass \/.well-known\/acme-challenge\/ !/) next
				if (/Strict-Transport-Security/) next
				gsub("X-Forwarded-Proto \"https\"", "X-Forwarded-Proto \"http\"")
				gsub("-443-error.log", "-80-error.log")
				print
				if (/ProxyRequests Off/) print "  ProxyPass /.well-known/acme-challenge/ !"
			}
			/<\/VirtualHost>/ { active=0 }
		' | sudo tee "${_configPath}" >/dev/null
	else
		render_to "$1" "${_configPath}"
	fi
	sudo apache2ctl configtest
	[[ -e "/etc/apache2/sites-enabled/${_site}.conf" ]] || sudo a2ensite "${_site}.conf"
	sudo systemctl reload apache2
}

# Prints "changed" only when application settings were updated.
configure_umbraco_https() {
	sudo python3 - "$1" "$2" "$3" <<'PY'
import json
import sys
path, binding, enabled = sys.argv[1:]
with open(path) as stream:
    settings = json.load(stream)
cms = settings.get("Umbraco", {}).get("CMS")
if cms is not None:
    secure = enabled == "y"
    url = ("https://" if secure else "http://") + binding
    if (cms.get("Global", {}).get("UseHttps") == secure
            and cms.get("WebRouting", {}).get("UmbracoApplicationUrl") == url):
        sys.exit(0)
    cms.setdefault("Global", {})["UseHttps"] = secure
    cms.setdefault("WebRouting", {})["UmbracoApplicationUrl"] = url
    with open(path, "w") as stream:
        json.dump(settings, stream, indent=2)
        stream.write("\n")
    print("changed")
PY
}

enable_https_config() {
	if grep -q '<VirtualHost \*:443>' "${_configPath}"; then
		return
	fi
	sudo a2enmod ssl
	local _updated
	_updated=$(mktemp)
	{
		printf '<VirtualHost *:80>\n  ServerName %s\n  RewriteEngine On\n  RewriteCond %%{REQUEST_URI} !^/\\.well-known/acme-challenge/\n  RewriteRule ^ https://%%{HTTP_HOST}%%{REQUEST_URI} [R=301,L]\n</VirtualHost>\n\n' "${_binding}"
		awk -v cert="${_certificateFile}" -v key="${_keyFile}" '
			{
				gsub("<VirtualHost \\*:80>", "<VirtualHost *:443>")
				gsub("X-Forwarded-Proto \"http\"", "X-Forwarded-Proto \"https\"")
				gsub("-80-error\\.log", "-443-error.log")
				if (/<\/VirtualHost>/) {
					print "  SSLEngine On"
					print "  SSLCertificateFile " cert
					print "  SSLCertificateKeyFile " key
				}
				print
			}
		' "${_configPath}"
	} >"${_updated}"
	sudo cp "${_configPath}" "${_updated}.backup"
	sudo cp "${_updated}" "${_configPath}"
	if ! sudo apache2ctl configtest; then
		sudo cp "${_updated}.backup" "${_configPath}"
		sudo rm -f "${_updated}" "${_updated}.backup"
		return 1
	fi
	sudo rm -f "${_updated}" "${_updated}.backup"
	sudo systemctl reload apache2
}

install_service() {
	echo "Create service"
	render_to "$1" "${_servicePath}"
	sudo systemctl daemon-reload
	if grep -q '^ExecStart=.*/dotnet ' "${_servicePath}"; then
		# Only lets the site wait for a local SQL Server at boot: if it fails, the site is still set up
		sudo python3 "${_apacheDir}/lib/sql_startup.py" "${_serviceName}" \
			|| echo "Could not make ${_serviceName} wait for SQL Server at boot; it starts without waiting." >&2
	fi
	sudo systemctl enable "${_serviceName}"
	sudo systemctl restart "${_serviceName}"
	sudo systemctl status "${_serviceName}" --no-pager || true
}
