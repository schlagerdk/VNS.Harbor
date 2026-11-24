"""Delete a site, its environments, or one environment. From delete_site.sh.

  all  = the site and all its environments (the default)
  test = only the site itself (environment folders inside the site folder are kept)
  prod = only the environment given (VNS_TARGET), also one on another server
An environment on a Windows server is only removed here: its IIS site and files are removed on the server.
Like the script, a step that fails does not stop the rest unless it says so: what can be removed is removed.
"""
import os
from pathlib import Path
import re
import shlex
import shutil

from harbor.core.bindings import validate_binding
from harbor.core.names import normalize_site_name, service_name
from harbor.core.site_config import ACME, ACME_FOLDERS, clean_stale_acme, default_site, DEFAULT_SITE_LINK
from harbor.core.system import Stop
from harbor.core.templates import Site
from harbor.environments import settings

REMOVE_REMOTE = r'''set -euo pipefail
source /opt/harbor/current/lib/common.sh 2>/dev/null || source /vns/apache/lib/common.sh
validate_site_name "${SITE}"
unit=$(normalize_dns_name "${SITE}" unit)
[[ ${BINDING} =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] || { echo "Invalid remote binding." >&2; exit 1; }
root="/var/www/${SITE}"
[[ ${root} == "/var/www/${SITE}" && ${root} != /var/www ]] || { echo "Unsafe remote site path." >&2; exit 1; }
if [[ -f /etc/systemd/system/${unit}.service ]]; then
	systemctl stop "${unit}.service"
	systemctl disable "${unit}.service"
fi
rm -f -- "/etc/systemd/system/${unit}.service"
rm -f -- "/etc/apache2/sites-available/${SITE}.conf" "/etc/apache2/sites-enabled/${SITE}.conf"
rm -f -- "/var/log/apache2/${SITE}-80-error.log" "/var/log/apache2/${SITE}-443-error.log" "/var/log/apache2/${SITE}-rewrite-error.log"
rm -rf -- "/var/lib/vns-roots/${SITE}"
rm -f -- "/var/lib/vns-managed/${SITE}"
rm -rf -- "${root:?}"
if [[ ${DELETE_CERTIFICATE} =~ ^([yY]|[yY][eE][sS])$ ]]; then
	domain_in_use=0
	for conf in /etc/apache2/sites-enabled/*.conf; do
		[[ -e ${conf} ]] || continue
		if awk -v d="${BINDING}" '($1 == "ServerName" || $1 == "ServerAlias") { for (i = 2; i <= NF; i++) if (tolower($i) == tolower(d)) f = 1 } END { exit !f }' "${conf}"; then
			domain_in_use=1
			break
		fi
	done
	if ((domain_in_use)); then
		echo "Keep certificate for ${BINDING}; another enabled site uses it."
	else
		if [[ -x /etc/acmesh/acme.sh ]]; then
			/etc/acmesh/acme.sh --revoke -d "${BINDING}" || true
			/etc/acmesh/acme.sh --remove -d "${BINDING}" || true
		fi
		rm -rf -- "/etc/ssl/certs/${BINDING}" "/etc/ssl/certs/${BINDING}_ecc"
		rm -rf -- "/root/.acme.sh/${BINDING}" "/root/.acme.sh/${BINDING}_ecc"
	fi
fi
systemctl daemon-reload
apache2ctl configtest
systemctl reload apache2
echo "Remote production site ${SITE} removed."
'''
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")


def remove(system, path):
    path = system.path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        path.unlink()


class Deletion:
    def __init__(self, system, site, delete_certificates):
        self.system = system
        self.site = site
        self.certificates = bool(re.fullmatch(r"[yY]|[yY][eE][sS]", delete_certificates or ""))

    def service_steps(self, name):
        return [f"Stop, disable and delete service {name}"] if self.system.path(f"/etc/systemd/system/{service_name(name)}.service").is_file() else []

    def certificate_steps(self, domain, removed):
        """The certificate's step, as it will be once the sites in removed are gone"""
        if not self.certificates or not domain:
            return []
        try:
            validate_binding(domain)
        except ValueError:
            return []
        return [] if self.shared(domain, removed) else [f"Revoke and remove certificate for {domain}"]

    def shared(self, domain, removed=()):
        """Whether another enabled site uses the name; the configs of the sites in removed are taken as gone already"""
        for conf in sorted(self.system.path("/etc/apache2/sites-enabled").glob("*.conf")):
            if not conf.exists() or conf.stem in removed or conf.resolve().stem in removed:
                continue
            for line in conf.read_text(errors="replace").splitlines():
                fields = line.split()
                if fields[:1] in (["ServerName"], ["ServerAlias"]) and domain.lower() in (name.lower() for name in fields[1:]):
                    return True
        return False

    def service_and_config(self, name):
        system = self.system
        unit = service_name(name)
        if self.service_steps(name):
            system.step(f"Stop, disable and delete service {name}")
            system.run("systemctl", "stop", unit, check=False)
            system.run("systemctl", "disable", unit, check=False)
            remove(system, f"/etc/systemd/system/{unit}.service")
        system.say(f"Delete apache config and logs for {name}")
        remove(system, f"/etc/apache2/sites-available/{name}.conf")
        remove(system, f"/etc/apache2/sites-enabled/{name}.conf")
        # The site that answers any name has one more link to its config
        if default_site(system) == name:
            system.say(f"{name} no longer answers any name")
            remove(system, DEFAULT_SITE_LINK)
        for log in ("80-error", "443-error", "rewrite-error"):
            remove(system, f"/var/log/apache2/{name}-{log}.log")
        remove(system, f"/var/lib/vns-roots/{name}")
        remove(system, f"/var/lib/vns-managed/{name}")

    def certificate(self, domain, directory):
        system = self.system
        if not self.certificates:
            return
        try:
            validate_binding(domain)
        except ValueError as error:
            raise Stop(str(error)) from None
        if self.shared(domain):
            system.say(f"Keep certificate for {domain}; another enabled site uses it.")
            return
        system.step(f"Revoke and remove certificate for {domain}")
        system.run(ACME, *ACME_FOLDERS, "--revoke", "-d", domain, check=False)
        system.run(ACME, *ACME_FOLDERS, "--remove", "-d", domain, check=False)
        remove(system, directory)
        # acme.sh keeps its files in a folder named after the binding, and --remove does not delete them
        clean_stale_acme(system, self.site, domain)
        remove(system, f"/root/.acme.sh/{domain}")
        remove(system, f"/root/.acme.sh/{domain}_ecc")


def scripts_of(site, target):
    return f"{site.site_root}/scripts" if target == "prod" else f"{site.site_root}/scripts/targets/{target}"


def delete_site(system, name="", delete_certificates="", scope="all", target=""):
    if not name:
        name = system.ask("Enter site name: ").strip()
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    if scope not in ("all", "test", "prod"):
        raise Stop("Scope must be all, test or prod.")
    site = Site(name)
    config = system.path(site.config_path)
    binding = name
    if config.is_file():
        binding = next((line.split()[1] for line in config.read_text().splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "") or name

    all_targets = settings.target_ids(system, site.site_root)
    targets = []
    if scope == "all":
        targets = all_targets
    elif scope == "prod":
        if target:
            targets = [target]
        elif len(all_targets) == 1:
            targets = all_targets
        else:
            raise Stop(f"Choose an environment with VNS_TARGET: {' '.join(all_targets) or 'this site has none'}")
        if targets[0] not in all_targets:
            raise Stop("This site has no such environment.")

    if not delete_certificates:
        delete_certificates = system.ask("Delete certificates? [y/N]: ")
    deletion = Deletion(system, site, delete_certificates)

    def values(target_id):
        # From its script, or Harbor's copy once the site is migrated
        try:
            return settings.values(system, site.site_root, target_id) or {}
        except settings.SettingsError:
            return {}

    # The main site goes last: its environments are built from its project, so without it they could not be published
    if scope == "test" and all_targets:
        from harbor.environments.sync import display_name
        named = ", ".join(display_name(system, site, target_id) for target_id in all_targets)
        raise Stop(f"{name} has environments: {named}. Delete each of them first (Delete site on its card), then the site.")
    # Folders of this site's local environments, kept when only the site itself is deleted
    keep = []
    if scope == "test":
        for target_id in all_targets:
            local_path = values(target_id).get("LOCAL_PATH", "")
            if not local_path.startswith(site.site_root + "/"):
                continue
            if "/" in local_path.rstrip("/")[len(site.site_root) + 1:]:
                raise Stop(f"The folder {local_path} is nested too deep inside the site folder to keep it. Delete all instead.")
            keep.append(local_path[len(site.site_root) + 1:].rstrip("/"))

    # ---- all the steps, before the first
    ubuntu_default = False
    default_conf = system.path("/etc/apache2/sites-available/000-default.conf")
    if name == "html" and default_conf.is_file():
        ubuntu_default = re.search(rf"(?m)^[ \t]*DocumentRoot[ \t]+{re.escape(site.root_path)}/html/?[ \t]*$", default_conf.read_text()) is not None
    steps, removed = [], set()
    for target_id in targets:
        found = values(target_id)
        if found.get("TARGET") == "windows":
            continue
        if found.get("LOCAL_PATH", ""):
            removed.add(found.get("FOLDER", ""))
            steps += deletion.service_steps(found.get("FOLDER", ""))
            steps += deletion.certificate_steps(found.get("BINDING", ""), removed)
        else:
            steps.append(f"Delete the remote production site ({found.get('FOLDER', '')})")
    if scope != "prod":
        removed.add(name)
        steps += deletion.service_steps(name)
        steps.append(f"Delete the site files (environment folders {' '.join(keep)} and scripts are kept)" if scope == "test" and keep
                     else "Delete site and files")
        if not ubuntu_default:
            steps += deletion.certificate_steps(binding, removed)
    system.plan(steps)

    # ---- environments
    for target_id in targets:
        found = values(target_id)
        local_path, folder, environment_binding = found.get("LOCAL_PATH", ""), found.get("FOLDER", ""), found.get("BINDING", "")
        if found.get("TARGET") == "windows":
            # The IIS site is managed on the Windows server, and it holds the only copy of its settings and media
            system.say(f"The site on the Windows server is not touched: remove the IIS site and its folder at {found.get('WINDOWS_SHARE', '')} yourself.")
        elif local_path:
            local_path = local_path.rstrip("/") if local_path != "/" else local_path
            try:
                normalize_site_name(folder)
                valid = folder != name
            except ValueError:
                valid = False
            if not valid:
                raise Stop(f"Invalid production site name: {folder}")
            if not local_path.startswith(site.root_path + "/") or len(local_path) <= len(site.root_path) + 1:
                raise Stop(f"The production folder must be inside {site.root_path}: {local_path}")
            if ".." in local_path:
                raise Stop(f"Invalid production folder: {local_path}")
            deletion.service_and_config(folder)
            system.say(f"Delete production files {local_path}")
            remove(system, local_path)
            if environment_binding:
                deletion.certificate(environment_binding, f"/etc/ssl/certs/{environment_binding}_ecc")
        else:
            system.step(f"Delete the remote production site ({folder})")
            environment = settings.load(system, site.site_root, target_id)
            if environment.LOCAL_PATH:
                raise Stop("Production target settings are inconsistent.")
            try:
                normalize_site_name(environment.FOLDER)
                validate_binding(environment.BINDING)
            except ValueError as error:
                raise Stop(str(error)) from None
            if not HOST.fullmatch(environment.IP):
                raise Stop("Invalid production host.")
            if not USER.fullmatch(environment.USER):
                raise Stop("Invalid production SSH user.")
            if not (environment.SSH_PORT.isdigit() and 1 <= int(environment.SSH_PORT) <= 65535):
                raise Stop("Invalid production SSH port.")
            variables = (f"SITE={shlex.quote(environment.FOLDER)} BINDING={shlex.quote(environment.BINDING)} "
                         f"DELETE_CERTIFICATE={shlex.quote(delete_certificates)}")
            result = settings.remote_run(system, environment, variables, REMOVE_REMOTE, check=False)
            if not result.ok:
                system.warn("Remote production removal failed; local files and production settings have been kept.")
                raise Stop("", result.returncode)
        if scope == "prod":
            if target_id == "prod":
                for script in ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"):
                    remove(system, f"{site.site_root}/scripts/{script}")
            else:
                remove(system, f"{site.site_root}/scripts/targets/{target_id}")
                remove(system, f"{site.site_root}/.vns-publish/target-{target_id}.json")
            if settings.migrated(system, site.site_root):
                # Harbor's copy is its truth: the environment goes from it, with its publish status
                settings.remove(system, site.site_root, target_id)
                remove(system, f"/var/lib/harbor/sites/{site.name}/publish/target-{target_id}.json")
            # With no environments left, the site is a site without environments again
            if not settings.target_ids(system, site.site_root):
                for script in ("publish", "clear_logs"):
                    local = system.path(f"{site.site_root}/scripts/{script}_local.sh")
                    if local.is_file() and not system.path(f"{site.site_root}/scripts/{script}.sh").exists():
                        local.rename(local.with_name(f"{script}.sh"))

    # Ubuntu's own default site serves /var/www/html from 000-default.conf and default-ssl.conf, not from a config
    # named after the folder. Deleting the html folder removes both, so they no longer answer any name (ubuntu_default).

    # ---- the site itself
    if scope != "prod":
        deletion.service_and_config(name)
        if ubuntu_default:
            system.say("Delete Apache's default site: 000-default.conf and default-ssl.conf")
            system.run("a2dissite", "-q", "000-default", "default-ssl", check=False, capture=True)
            for path in ("/etc/apache2/sites-available/000-default.conf", "/etc/apache2/sites-available/default-ssl.conf",
                         "/etc/apache2/sites-enabled/000-default.conf", "/etc/apache2/sites-enabled/default-ssl.conf"):
                remove(system, path)
        if scope == "test" and keep:
            system.step(f"Delete the site files (environment folders {' '.join(keep)} and scripts are kept)")
            root = system.path(site.site_root)
            for entry in sorted(root.iterdir()) if root.is_dir() else []:
                if entry.name not in keep and entry.name != "scripts":
                    remove(system, f"{site.site_root}/{entry.name}")
        else:
            system.step("Delete site and files")
            remove(system, site.site_root)
        # Apache's default site has no binding and no certificate of its own
        if not ubuntu_default:
            deletion.certificate(binding, site.certificate_directory)

    system.say("Reload daemon")
    system.run("systemctl", "daemon-reload", check=False)
    system.say("Test and reload apache2")
    system.run("apache2ctl", "configtest", check=False)
    system.run("systemctl", "reload", "apache2", check=False)
    system.say("All is removed!")
