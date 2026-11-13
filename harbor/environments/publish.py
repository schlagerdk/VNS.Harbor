"""Publish a C# site to one of its environments on Linux: here (PROD_LOCAL_PATH) or on another server over SSH. From the
site template csharp/prod-scripts/publish_prod.sh (phase 2d).

Always a Release build, into a temporary folder, so the local publish folder is left untouched. The folder, service,
Apache config and (unless PROD_HTTPS=n) certificate are made where the environment is when they are missing; the two
scripts that do that there are sent word for word as the template sent them. Once the site exists there, its content,
settings and data are never overwritten or deleted by a publish. VNS_MIGRATE_ONLY=1 (move_env) only makes the service
and Apache config and starts it, as the whole site was copied there already.
"""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from harbor.core.dotnet import runtime_identifier
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.sites.actions import site_named
from harbor.core.paths import APACHE, BIN

SCHEMA_FILES = ("appsettings-schema.json", "appsettings-schema.usync.json", "appsettings-schema.Umbraco.Cms.json", "umbraco-package-schema.json")

PREPARE = r'''set -euo pipefail
source /opt/harbor/current/apache/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
unit=$(normalize_dns_name "${SITE}" unit)
root=${ROOT}
docroot=${root}/wwwroot
conf=/etc/apache2/sites-available/${SITE}.conf
service=/etc/systemd/system/${unit}.service
# The binding can have been changed on the server: its Apache config decides
if [[ -f ${conf} ]]; then
	configured_binding=$(awk '$1 == "ServerName" { print $2; exit }' "${conf}")
	BINDING=${configured_binding:-${BINDING}}
fi
certdir=/etc/ssl/certs/${BINDING}_ecc
acme=/etc/acmesh/acme.sh
source /opt/harbor/current/apache/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh

if [[ -e ${service} ]] && ! grep -qx "WorkingDirectory=${root}" "${service}"; then
	echo "Service ${SITE} already exists and runs from a different folder. Use another PROD_FOLDER (and PROD_BINDING)." >&2
	exit 1
fi

PORT=""
if [[ -e ${service} ]]; then
	PORT=$(grep '^ExecStart=' "${service}" | grep -oE '(localhost|127\.0\.0\.1|\*):[0-9]+' | head -n 1 | cut -d: -f2 || true)
	[[ -n ${PORT} ]] || { echo "Could not determine the existing port from ${service}." >&2; exit 1; }
elif [[ -e ${conf} ]]; then
	PORT=$(grep -hEo '(127\.0\.0\.1|localhost|\*):[0-9]{1,5}' "${conf}" | head -n 1 | cut -d: -f2)
fi
if [[ -z ${PORT} ]]; then
	_site=${SITE}
	allocate_ports
	PORT=${_portHttp}
fi
echo "Using production port ${PORT}"

mkdir -p "${docroot}" "/var/lib/vns-roots/${SITE}"
if [[ -e ${service} ]]; then
	echo "Stop service"
	systemctl stop "${unit}" || true
fi

if [[ ! -e ${service} ]]; then
	echo "Create service"
	cat >"${service}" <<EOF
[Unit]
Description=Running .NET Core [${SITE}]

[Service]
User=www-data
Group=www-data
WorkingDirectory=${root}
ExecStart=/usr/lib/dotnet/dotnet ${root}/${PROJECT}.dll --urls "http://localhost:${PORT}"
RestartSec=10
Restart=always
KillSignal=SIGINT
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SITE}
Environment=ASPNETCORE_ENVIRONMENT=${ENVIRONMENT}
Environment=DOTNET_ENVIRONMENT=${ENVIRONMENT}
Environment=ASPNETCORE_FORWARDEDHEADERS_ENABLED=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectProc=invisible
ProcSubset=pid
RootDirectory=/var/lib/vns-roots/${SITE}
BindReadOnlyPaths=/usr/lib/dotnet /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu /lib64 /usr/lib/ssl -/usr/share/zoneinfo -/usr/share/ca-certificates /etc/ssl /etc/ld.so.cache /etc/resolv.conf /etc/hosts /etc/nsswitch.conf /etc/passwd /etc/group /etc/localtime /etc/os-release
BindPaths=${root}
MountAPIVFS=yes
ReadWritePaths=${root}

[Install]
WantedBy=multi-user.target
EOF
	systemctl daemon-reload
	systemctl enable "${unit}"
fi

server_aliases=()
if [[ -f ${conf} ]]; then
	while IFS= read -r alias; do
		[[ -n ${alias} ]] || continue
		alias=${alias,,}
		[[ ${alias} =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ && ${#alias} -le 253 ]] || {
			echo "Invalid ServerAlias '${alias}' in ${conf}; certificate issuance stopped." >&2
			exit 1
		}
		found=0
		for domain in "${BINDING}" "${server_aliases[@]}"; do [[ ${domain} == "${alias}" ]] && found=1; done
		((found)) || server_aliases+=("${alias}")
	done < <(awk '$1 == "ServerAlias" { for (i = 2; i <= NF && $i !~ /^#/; i++) print $i }' "${conf}")
fi
server_alias_line=""
if ((${#server_aliases[@]})); then
	printf -v server_alias_line '  ServerAlias %s' "${server_aliases[*]}"
fi

# A site made before it was an environment can have its certificate in another folder: the config knows where
configured_certificate=$(awk '$1 == "SSLCertificateFile" { print $2; exit }' "${conf}" 2>/dev/null || true)
if [[ ${HTTPS} != n && ! -e ${certdir}/fullchain.cer && ! ( -n ${configured_certificate} && -e ${configured_certificate} ) ]]; then
	[[ -x ${acme} ]] || { echo "acme.sh not found on prod (${acme}). Run apache/init.sh there first." >&2; exit 1; }
	certificate_domains=("${BINDING}" "${server_aliases[@]}")
	certificate_args=()
	for domain in "${certificate_domains[@]}"; do certificate_args+=(-d "${domain}"); done
	printf 'Issue certificate for: %s\n' "${certificate_domains[*]}"
	cat >"${conf}" <<EOF
<VirtualHost *:80>
  ServerAdmin ${MAIL}
  ServerName ${BINDING}
${server_alias_line}
  DocumentRoot ${docroot}
  ErrorLog /var/log/apache2/${SITE}-80-error.log
  <Directory ${docroot}>
    AllowOverride All
    Require all granted
  </Directory>
</VirtualHost>
EOF
	a2enmod ssl proxy proxy_http proxy_wstunnel rewrite headers >/dev/null
	[[ -e /etc/apache2/sites-enabled/${SITE}.conf ]] || a2ensite "${SITE}.conf" >/dev/null
	apache2ctl configtest
	systemctl reload apache2
	# Leftover acme data for this domain (e.g. from a deleted site) makes --issue fail with "Domain key exists"
	used=0
	for c in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${c} && ${c} != "/etc/apache2/sites-enabled/${SITE}.conf" ]] || continue
		awk -v d="${BINDING}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${c}" && used=1
	done
	if [[ ${used} == 0 ]]; then
		"${acme}" --remove -d "${BINDING}" >/dev/null 2>&1 || true
		rm -rf "/etc/ssl/certs/${BINDING}" "/etc/ssl/certs/${BINDING}_ecc"
	fi
	rc=0
	"${acme}" --issue --apache --force --server letsencrypt "${certificate_args[@]}" || rc=$?
	if [[ ${rc} -ne 0 && ${rc} -ne 2 ]]; then
		echo "Certificate issue failed. Check that DNS for ${BINDING} points to this server." >&2
		exit 1
	fi
	mkdir -p "${certdir}"
	"${acme}" --install-cert -d "${BINDING}" --key-file "${certdir}/${BINDING}.key" \
		--fullchain-file "${certdir}/fullchain.cer" --reloadcmd "systemctl reload apache2"
fi

extra=""
if [[ ${UMBRACO} == 1 ]]; then
	extra="
  ProxyPass /robots.txt http://localhost:${PORT}/seo_robots_txt
  ProxyPassReverse /robots.txt http://localhost:${PORT}/seo_robots_txt

  ProxyPass /sitemap.xml http://localhost:${PORT}/seo_sitemap_xml
  ProxyPassReverse /sitemap.xml http://localhost:${PORT}/seo_sitemap_xml
"
fi
if [[ ${HTTPS} == n ]]; then
	# An existing proxy config is kept, so a site that later got a certificate stays on HTTPS
	if ! grep -q "ProxyPass" "${conf}" 2>/dev/null; then
		echo "Create Apache config (HTTP only)"
		cat >"${conf}" <<EOF
<VirtualHost *:80>
  ServerAdmin ${MAIL}
  ServerName ${BINDING}
${server_alias_line}
  DocumentRoot ${docroot}

  AllowEncodedSlashes NoDecode

  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteCond %{HTTP:Connection} upgrade [NC]
  RewriteRule /(.*) ws://localhost:${PORT}/\$1 [P,L]

  ProxyPreserveHost On
  ProxyRequests Off
  ProxyPass /.well-known/acme-challenge/ !
  RequestHeader set X-Forwarded-Proto "http"
${extra}
  ProxyPass / http://localhost:${PORT}/ nocanon
  ProxyPassReverse / http://localhost:${PORT}/

  ErrorLog /var/log/apache2/${SITE}-80-error.log
</VirtualHost>
EOF
	fi
elif ! grep -q "SSLEngine" "${conf}" 2>/dev/null; then
	echo "Create Apache config"
	cat >"${conf}" <<EOF
<VirtualHost *:80>
  ServerAdmin ${MAIL}
  ServerName ${BINDING}
${server_alias_line}
  DocumentRoot ${docroot}

  RewriteEngine On
  RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/
  RewriteRule ^(.*)\$ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]

  ErrorLog /var/log/apache2/${SITE}-rewrite-error.log
</VirtualHost>

<VirtualHost *:443>
  ServerAdmin ${MAIL}
  ServerName ${BINDING}
${server_alias_line}
  DocumentRoot ${docroot}

  AllowEncodedSlashes NoDecode

  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteCond %{HTTP:Connection} upgrade [NC]
  RewriteRule /(.*) ws://localhost:${PORT}/\$1 [P,L]

  ProxyPreserveHost On
  ProxyRequests Off
  RequestHeader set X-Forwarded-Proto "https"
${extra}
  ProxyPass / http://localhost:${PORT}/ nocanon
  ProxyPassReverse / http://localhost:${PORT}/

  SSLEngine On

  SSLCertificateFile ${certdir}/fullchain.cer
  SSLCertificateKeyFile ${certdir}/${BINDING}.key

  ErrorLog /var/log/apache2/${SITE}-443-error.log
</VirtualHost>
EOF
fi

# The site must be enabled whenever the config exists, also if the certificate or config was created earlier
a2enmod ssl proxy proxy_http proxy_wstunnel rewrite headers >/dev/null
[[ -e /etc/apache2/sites-enabled/${SITE}.conf ]] || a2ensite "${SITE}.conf" >/dev/null
apache2ctl configtest
systemctl reload apache2
release_port_reservation
'''

START = r'''set -euo pipefail
source /opt/harbor/current/apache/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
dir=${ROOT}
# The Apache config decides the scheme and binding: they stay correct if they are changed on the server
configured_binding=$(awk '$1 == "ServerName" { print $2; exit }' "/etc/apache2/sites-available/${SITE}.conf" 2>/dev/null || true)
BINDING=${configured_binding:-${BINDING}}
scheme=http
use_https=false
if grep -q "SSLEngine" "/etc/apache2/sites-available/${SITE}.conf" 2>/dev/null; then
	scheme=https
	use_https=true
fi
for file in "${dir}"/appsettings*.json; do
	if [[ -f ${file} ]] && grep -q "UmbracoApplicationUrl" "${file}"; then
		echo "Set UmbracoApplicationUrl to ${scheme}://${BINDING} in ${file##*/}"
		sed -i -E "s|(\"UmbracoApplicationUrl\"[[:space:]]*:[[:space:]]*\")[^\"]*|\1${scheme}://${BINDING}|" "${file}"
		sed -i -E "s/(\"UseHttps\"[[:space:]]*:[[:space:]]*)(true|false)/\1${use_https}/" "${file}"
	fi
done
chown -R www-data:www-data "${dir}"
python3 "${_apacheDir}/lib/sql_startup.py" "${SITE}"
systemctl start "$(python3 "${_apacheDir}/lib/domain_names.py" "${SITE}" unit)"
sleep 2
systemctl status "$(python3 "${_apacheDir}/lib/domain_names.py" "${SITE}" unit)" --no-pager
'''


class Failed(Exception):
    def __init__(self, code=1):
        super().__init__()
        self.code = code


def publish_environment(system, name, target="prod", project="", local_publish="", tracked=None, dry_run=False):
    site = site_named(system, name)
    scope = "prod" if target == "prod" else f"target-{target}"
    tracked = os.environ.get("VNS_PUBLISH_TRACKED", "") if tracked is None else tracked
    environment = settings.load(system, site.site_root, target)
    if dry_run and not environment.windows:
        raise Stop("A dry run is for an environment on Windows.")
    # A dry run changes nothing, so it is not recorded as a publish
    if tracked != scope and not dry_run:
        # Run again inside publish_status.py, which records the publish when it works
        harbor = BIN
        result = system.run("python3", str(APACHE / "lib/publish_status.py"), "run", site.site_root, scope, "python3", str(harbor), "env",
                            "publish", site.name, *(["--project", project] if project else []),
                            *(["--local-publish", local_publish] if local_publish else []), env={"VNS_TARGET": target}, check=False)
        if not result.ok:
            raise Stop("", result.returncode)
        return
    if environment.windows:
        from harbor.environments.windows_publish import publish_windows
        projects = sorted(system.path(site.site_root).glob("*.csproj"))
        publish_windows(system, site, environment, project or (projects[0].stem if projects else ""), local_publish, dry_run)
        return
    try:
        Publish(system, site, target, project, local_publish).run()
    except Failed as error:
        raise Stop("", error.code) from None
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None


class Publish:
    def __init__(self, system, site, target, project, local_publish):
        self.system = system
        self.site = site
        self.target = target
        self.environment = settings.load(system, site.site_root, target)
        projects = sorted(system.path(site.site_root).glob("*.csproj"))
        self.project = project or (projects[0].stem if projects else "")
        self.local_publish = local_publish or "_publish"
        self.migrate_only = bool(os.environ.get("VNS_MIGRATE_ONLY"))

    def check(self):
        environment = self.environment
        if not environment.FOLDER or not environment.BINDING:
            raise Stop("Set PROD_FOLDER and PROD_BINDING at the top of this script")
        self.local = bool(environment.LOCAL_PATH)
        self.root = environment.root
        if self.local:
            return
        if not environment.IP or not environment.USER:
            raise Stop("Set PROD_IP and PROD_USER (or PROD_LOCAL_PATH) at the top of this script")
        self.ssh = ["ssh", "-p", environment.SSH_PORT, "-o", "StrictHostKeyChecking=accept-new"]
        self.env = {}
        if environment.PASSWORD:
            if not self.system.which("sshpass"):
                raise Stop("sshpass is required for password login: sudo apt install sshpass")
            self.env["SSHPASS"] = environment.PASSWORD
            self.ssh = ["sshpass", "-e", *self.ssh]
        self.host = f"{environment.USER}@{environment.IP}"
        self.sudo = "" if environment.USER == "root" else "sudo -n"

    def remote(self, command, **options):
        return self.system.run(*self.ssh, self.host, command, env=self.env, **options)

    def run_script(self, variables, script):
        """A script run as root where the environment is (here when it is a folder on this server)"""
        if self.local:
            return self.system.run("bash", "-c", f"env {variables} bash -s", input=script, cwd=self.site.site_root)
        return self.remote(f"{self.sudo} env {variables} bash -s", input=script)

    def rsync(self, *argv):
        remote = [] if self.local else ["-e", " ".join(self.ssh), f"--rsync-path={self.sudo + ' ' if self.sudo else ''}rsync"]
        self.system.run("rsync", *argv[:-2], *remote, *argv[-2:], env=None if self.local else self.env)

    def exclude_local_target(self):
        """A local target inside the project folder is left out of the build, or its files are compiled/included"""
        system, site = self.system, self.site
        project_dir = os.path.realpath(system.path(site.site_root))
        target = os.path.realpath(system.path(self.environment.LOCAL_PATH))
        if not target.startswith(project_dir + "/"):
            return
        relative = target[len(project_dir) + 1:]
        csproj = system.path(f"{site.site_root}/{self.project}.csproj")
        if not csproj.is_file():
            found = sorted(system.path(site.site_root).glob("*.csproj"))
            csproj = found[0]
        text = csproj.read_text()
        if f'Compile Remove="{relative}/**"' in text:
            return
        system.say(f"Exclude {relative} from the build in {csproj.name}")
        block = (f'  <!-- Exclude local prod publish folder from build -->\n  <ItemGroup>\n    <Content Remove="{relative}/**" />\n'
                 f'    <Compile Remove="{relative}/**" />\n    <None Remove="{relative}/**" />\n  </ItemGroup>\n</Project>\n')
        csproj.write_text(re.sub(r"</Project>\s*\Z", lambda match: block, text, count=1))

    def run(self):
        system, site, environment = self.system, self.site, self.environment
        self.check()
        output = tempfile.mkdtemp()
        cleanup = [output]
        try:
            identifier = runtime_identifier(system.output("uname", "-m") if self.local else self.remote("uname -m", capture=True).stdout.strip())
            if self.local:
                self.exclude_local_target()
            if not self.migrate_only:
                system.step("Publish (Release)")
                system.run("python3", str(APACHE / "lib/build_as.py"), "--", "dotnet", "publish", f"{self.project}.csproj", "-c", "Release",
                           f"-p:PublishDir={output}", f"-p:CustomAfterMicrosoftCommonTargets={APACHE}/lib/publish.targets", "--runtime", identifier,
                           "--self-contained", "false", "--nologo", cwd=site.site_root)
                if not Path(output, f"{self.project}.dll").is_file():
                    raise Stop(f"Publish failed: {self.project}.dll not found in the output")
            umbraco = "1" if any("Umbraco.Cms" in path.read_text(errors="replace") for path in system.path(site.site_root).glob("*.csproj")) else "0"
            variables = (f"SITE='{environment.FOLDER}' BINDING='{environment.BINDING}' ENVIRONMENT='{environment.other.get('ENVIRONMENT', '')}' "
                         f"MAIL='{environment.ADMIN_MAIL}' PROJECT='{self.project}' UMBRACO='{umbraco}' ROOT='{self.root}' HTTPS='{environment.HTTPS}'")
            system.step(f"Prepare {environment.FOLDER} on {environment.IP or 'this server'}")
            self.run_script(variables, PREPARE)

            if not self.migrate_only:
                if self.local:
                    system.step(f"Publish to local folder {self.root}")
                    system.path(self.root).mkdir(parents=True, exist_ok=True)
                    destination = f"{system.path(self.root)}/"
                    exists = system.path(f"{self.root}/appsettings.json").is_file()
                else:
                    system.step(f"Upload to {self.host}:{self.root}")
                    destination = f"{self.host}:{self.root}/"
                    exists = self.remote(f"{self.sudo} test -f '{self.root}/appsettings.json'", check=False).ok
                # Schema files are never uploaded. Once the site exists there, its content, settings and data are never
                # overwritten or deleted by a publish (rsync keeps excluded paths when using --delete).
                keep = ["--exclude", "appsettings-schema*.json", "--exclude", "umbraco-package-schema.json", "--exclude", "/scripts/",
                        "--exclude", "/.vns-publish/"]
                if exists:
                    keep += [argument for pattern in ("appsettings*.json", "/umbraco/", "/uSync/", "/Views/", "/wwwroot/", "/App_Data/", "/app_data/")
                             for argument in ("--exclude", pattern)]
                self.rsync("-az", "--delete", *keep, f"{output}/", destination)
                if exists:
                    system.say("appsettings.json already exists in the destination - not touched")
                else:
                    system.say("First publish: copy appsettings*.json (without ConnectionStrings)")
                    first = tempfile.mkdtemp()
                    cleanup.append(first)
                    # Prefer the files from the local publish folder (made by Create site); fall back to the fresh build
                    for source in (system.path(f"{site.site_root}/{self.local_publish}"), Path(output)):
                        for file in sorted(Path(source).glob("appsettings*.json")):
                            shutil.copy(file, first)
                        if Path(first, "appsettings.json").is_file():
                            break
                    for file in Path(first).glob("appsettings-schema*.json"):
                        file.unlink()
                    for file in sorted(Path(first).glob("appsettings*.json")):
                        data = json.loads(file.read_text(encoding="utf-8-sig"))
                        data.pop("ConnectionStrings", None)
                        file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    self.rsync("-az", f"{first}/", destination)

            # The main site's config snippets (Allowed IPs, rewrites and the like), so the environment has the same rules
            from harbor.environments.snippets import carry_snippets
            try:
                carry_snippets(system, site.name, self.target)
            except (Stop, CommandFailed) as error:
                if isinstance(error, Stop) and str(error):
                    system.warn(str(error))
                system.warn("The config snippets were not carried. The publish goes on.")

            system.step("Start service")
            # The connection is written before the site starts, as it reads it then (VNS.Harbor package): here, or over SSH
            from harbor.sites.tools import keep_connection
            keep_connection(system, site, self.target)
            self.run_script(variables, START)
            system.say("Delete schema files and bin")
            for name in (*SCHEMA_FILES, "App_Plugins", "bin"):
                path = system.path(f"{site.site_root}/{name}")
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif os.path.lexists(path):
                    path.unlink()
        finally:
            for folder in cleanup:
                shutil.rmtree(folder, ignore_errors=True)
