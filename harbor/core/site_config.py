"""A site's Apache config, service, HTTPS and certificate. From lib/common.sh: install_apache_config, install_service,
enable_https_config, configure_umbraco_https, default_site, make_default_site, require_acme, clean_stale_acme,
issue_certificate and ensure_production_tls_config.

harbor runs as root, so it runs the programs the script ran with sudo without it.
"""
import json
import os
import re
import shutil
from pathlib import Path

from harbor.core.bindings import certificate_domains, replace_server_aliases, validate_binding
from harbor.core.templates import render

ACME = "/etc/acmesh/acme.sh"
ACME_SERVER = "letsencrypt"
DEFAULT_SITE_LINK = "/etc/apache2/sites-enabled/00-vns-default.conf"
SSL_ON = re.compile(r"(?m)^[ \t]*SSLEngine[ \t]+On([ \t]|$)")


def read_template(template):
    """A template of harbor's own, from where harbor is installed: not a file of the server it works on"""
    return Path(template).read_text()


def http_only(text):
    """A rendered config with HTTP only, for a site without a certificate: only its HTTPS site is kept, on port 80"""
    lines = []
    active = False
    for line in text.splitlines():
        if re.search(r"<VirtualHost \*:443>", line):
            active = True
            line = line.replace(":443", ":80", 1)
        if active and not re.match(r"[ \t]*SSL", line):
            if not re.match(r"[ \t]*ProxyPass /.well-known/acme-challenge/ !", line) and "Strict-Transport-Security" not in line:
                line = line.replace('X-Forwarded-Proto "https"', 'X-Forwarded-Proto "http"')
                line = re.sub(r"-443-error.log", "-80-error.log", line)
                lines.append(line)
                if "ProxyRequests Off" in line:
                    lines.append("  ProxyPass /.well-known/acme-challenge/ !")
        if "</VirtualHost>" in line:
            active = False
    return "".join(line + "\n" for line in lines)


def enabled(system, site):
    return os.path.lexists(system.path(f"/etc/apache2/sites-enabled/{site.name}.conf"))


def install_apache_config(system, site, template, certificate=True):
    system.say("Create Apache config")
    text = render(read_template(template), site)
    system.path(site.config_path).write_text(text if certificate else http_only(text))
    system.run("apache2ctl", "configtest")
    if not enabled(system, site):
        system.run("a2ensite", f"{site.name}.conf")
    system.run("systemctl", "reload", "apache2")


def configure_umbraco_https(system, path, binding, secure):
    """Umbraco's HTTPS settings in an appsettings file. Returns whether the file changed."""
    path = system.path(path)
    settings = json.loads(path.read_text())
    cms = settings.get("Umbraco", {}).get("CMS")
    if cms is None:
        return False
    url = ("https://" if secure else "http://") + binding
    if cms.get("Global", {}).get("UseHttps") == secure and cms.get("WebRouting", {}).get("UmbracoApplicationUrl") == url:
        return False
    cms.setdefault("Global", {})["UseHttps"] = secure
    cms.setdefault("WebRouting", {})["UmbracoApplicationUrl"] = url
    with open(path, "w") as stream:
        json.dump(settings, stream, indent=2)
        stream.write("\n")
    return True


def enable_https_config(system, site, binding):
    """Turn an HTTP config into HTTPS with the site's certificate, behind a redirect from port 80. Restored when
    Apache does not accept it."""
    path = system.path(site.config_path)
    text = path.read_text()
    if "<VirtualHost *:443>" in text:
        return
    system.run("a2enmod", "ssl")
    redirect = (f"<VirtualHost *:80>\n  ServerName {binding}\n  RewriteEngine On\n"
                "  RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/\n"
                "  RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]\n</VirtualHost>\n\n")
    lines = []
    for line in text.splitlines():
        line = line.replace("<VirtualHost *:80>", "<VirtualHost *:443>").replace('X-Forwarded-Proto "http"', 'X-Forwarded-Proto "https"')
        if "</VirtualHost>" in line:
            lines += ["  SSLEngine On", f"  SSLCertificateFile {site.certificate_file}", f"  SSLCertificateKeyFile {site.key_file}"]
        lines.append(line)
    path.write_text(redirect + "".join(line + "\n" for line in lines))
    if not system.run("apache2ctl", "configtest", check=False).ok:
        path.write_text(text)
        raise RuntimeError("Apache did not accept the HTTPS config, so the HTTP one is kept.")
    system.run("systemctl", "reload", "apache2")


def install_service(system, site, template):
    system.say("Create service")
    text = render(read_template(template), site)
    system.path(site.service_path).write_text(text)
    system.run("systemctl", "daemon-reload")
    if re.search(r"(?m)^ExecStart=.*/dotnet ", text):
        # Only lets the site wait for a local SQL Server at boot: if it fails, the site is still set up
        if not system.run("python3", f"{site.harbor_dir}/lib/sql_startup.py", site.service_name, check=False).ok:
            system.warn(f"Could not make {site.service_name} wait for SQL Server at boot; it starts without waiting.")
        # No Server header from Kestrel, and Umbraco 18+'s image signing key (harden_service.py)
        system.run("python3", f"{site.harbor_dir}/lib/harden_service.py", site.service_name, check=False)
    system.run("systemctl", "enable", site.service_name)
    system.run("systemctl", "restart", site.service_name)
    system.run("systemctl", "status", site.service_name, "--no-pager", check=False)


def default_site(system):
    """The folder name of the site that answers any name, or nothing"""
    try:
        target = os.readlink(system.path(DEFAULT_SITE_LINK))
    except OSError:
        return ""
    name = target.rsplit("/", 1)[-1]
    return name[:-len(".conf")] if name.endswith(".conf") else name


def make_default_site(system, site):
    current = default_site(system)
    if current and current != site.name:
        raise RuntimeError(f"{current} already answers any name. Only one site can: delete it or give it a binding first.")
    system.say(f"Make {site.name} answer any name")
    link = system.path(DEFAULT_SITE_LINK)
    if os.path.lexists(link):
        link.unlink()
    link.symlink_to(f"../sites-available/{site.name}.conf")
    system.run("apache2ctl", "configtest")
    system.run("systemctl", "reload", "apache2")


def require_acme(system):
    if not os.access(system.path(ACME), os.X_OK):
        raise RuntimeError(f"acme.sh not found or not executable: {ACME}")


def clean_stale_acme(system, site, domain):
    """Remove leftover acme.sh state for a domain (such as a key from a deleted site), so a certificate can be
    issued. Kept when another enabled site still uses the domain."""
    for path in sorted(system.path("/etc/apache2/sites-enabled").glob("*.conf")):
        if path.name == f"{site.name}.conf" or not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            fields = line.split()
            if fields[:1] in (["ServerName"], ["ServerAlias"]) and domain.lower() in (name.lower() for name in fields[1:]):
                return
    if system.path(f"/etc/ssl/certs/{domain}").is_dir() or system.path(f"/etc/ssl/certs/{domain}_ecc").is_dir():
        system.say(f"Remove leftover certificate data for {domain}")
        system.run(ACME, "--remove", "-d", domain, check=False)
        for folder in (f"/etc/ssl/certs/{domain}", f"/etc/ssl/certs/{domain}_ecc"):
            shutil.rmtree(system.path(folder), ignore_errors=True)


def issue_certificate(system, site, binding):
    """A temporary HTTP-only config, then the certificate for the binding and each alias. Needs the doc root."""
    require_acme(system)
    config = system.path(site.config_path)
    domains = certificate_domains(config.read_text() if config.is_file() else "", binding, str(site.config_path))
    system.say("Create temporary HTTP-only config for certificate validation")
    system.path(site.doc_root).mkdir(parents=True, exist_ok=True)
    text = render(read_template(f"{site.harbor_dir}/templates/sites/common/apache-temp.conf.tpl"), site)
    if len(domains) > 1:
        text = replace_server_aliases(text, domains[1:])
    config.write_text(text)
    system.run("a2enmod", "ssl")
    system.run("apache2ctl", "configtest")
    if not enabled(system, site):
        system.run("a2ensite", f"{site.name}.conf")
    system.run("systemctl", "reload", "apache2")
    for domain in domains:
        clean_stale_acme(system, site, domain)
    system.say("Issue certificate")
    system.say(f"Certificate domains: {' '.join(domains)}")
    arguments = [argument for domain in domains for argument in ("-d", domain)]
    # acme.sh returns 2 when a still valid certificate is kept
    issued = system.run(ACME, "--issue", "--apache", "--force", "--server", ACME_SERVER, *arguments, check=False)
    if issued.returncode not in (0, 2):
        raise RuntimeError(f"The certificate could not be issued (acme.sh exit {issued.returncode}).")
    system.say("Install certificate for Apache")
    system.path(site.certificate_directory).mkdir(parents=True, exist_ok=True)
    system.run(ACME, "--install-cert", "-d", binding, "--key-file", site.key_file, "--fullchain-file", site.certificate_file,
               "--reloadcmd", "systemctl reload apache2")


def production_tls_config(config_text, service_text, site, binding, root, certificate_directory, mail, umbraco):
    """The HTTPS config of an environment, made from its HTTP one. None when it has HTTPS already."""
    if SSL_ON.search(config_text):
        return None
    match = next((re.search(r"(?:localhost|127\.0\.0\.1|\*):([0-9]+)", line) for line in service_text.splitlines()
                  if line.startswith("ExecStart=")), None)
    if not match:
        raise RuntimeError("Could not determine the production port from the service.")
    port = match[1]
    binding_lower = binding.lower()
    aliases = []
    for line in config_text.splitlines():
        fields = line.split()
        if fields[:1] != ["ServerAlias"]:
            continue
        for alias in fields[1:]:
            if alias.startswith("#"):
                break
            alias = alias.lower()
            try:
                validate_binding(alias)
            except ValueError:
                raise RuntimeError(f"Invalid ServerAlias '{alias}'; refusing to build the HTTPS config.") from None
            if alias != binding_lower and alias not in aliases:
                aliases.append(alias)
    alias_line = f"  ServerAlias {' '.join(aliases)}" if aliases else ""
    extra = ""
    if umbraco:
        extra = (f"\n  ProxyPass /robots.txt http://localhost:{port}/seo_robots_txt\n"
                 f"  ProxyPassReverse /robots.txt http://localhost:{port}/seo_robots_txt\n\n"
                 f"  ProxyPass /sitemap.xml http://localhost:{port}/seo_sitemap_xml\n"
                 f"  ProxyPassReverse /sitemap.xml http://localhost:{port}/seo_sitemap_xml\n")
    return f"""<VirtualHost *:80>
  ServerAdmin {mail}
  ServerName {binding}
{alias_line}
  DocumentRoot {root}/wwwroot

  RewriteEngine On
  RewriteCond %{{REQUEST_URI}} !^/\\.well-known/acme-challenge/
  RewriteRule ^(.*)$ https://%{{HTTP_HOST}}%{{REQUEST_URI}} [R=301,L]

  ErrorLog /var/log/apache2/{site}-rewrite-error.log
</VirtualHost>

<VirtualHost *:443>
  ServerAdmin {mail}
  ServerName {binding}
{alias_line}
  DocumentRoot {root}/wwwroot

  AllowEncodedSlashes NoDecode

  RewriteEngine On
  RewriteCond %{{HTTP:Upgrade}} =websocket [NC]
  RewriteCond %{{HTTP:Connection}} upgrade [NC]
  RewriteRule /(.*) ws://localhost:{port}/$1 [P,L]

  ProxyPreserveHost On
  ProxyRequests Off
  RequestHeader set X-Forwarded-Proto "https"
{extra}
  ProxyPass / http://localhost:{port}/ nocanon
  ProxyPassReverse / http://localhost:{port}/

  SSLEngine On
  SSLCertificateFile {certificate_directory}/fullchain.cer
  SSLCertificateKeyFile {certificate_directory}/{binding}.key

  ErrorLog /var/log/apache2/{site}-443-error.log
</VirtualHost>
"""
