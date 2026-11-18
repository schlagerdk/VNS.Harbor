"""Actions on a site here (its main environment): a new certificate, and another environment. From test_action.sh."""
import re

from harbor.core.bindings import certificate_domains
from harbor.core.names import normalize_site_name
from harbor.core.site_config import ACME, ACME_SERVER, clean_stale_acme, configure_umbraco_https, enable_https_config, require_acme
from harbor.core.system import Stop
from harbor.core.templates import Site

ENVIRONMENTS = {("1", "d", "dev", "development"): "Development", ("2", "t", "test"): "Test",
                ("3", "s", "stage", "staging"): "Staging", ("4", "p", "prod", "production"): "Production"}
ENVIRONMENT_LINE = re.compile(r"(?m)^(Environment=(?:ASPNETCORE|DOTNET)_ENVIRONMENT=).*$")


def choose_environment(system, given):
    """An environment given by number, letter or name, or asked for as the script did"""
    if not given:
        for line in ("1) Development", "2) Test", "3) Staging", "4) Production"):
            system.say(line)
        given = system.ask("Choose environment: ")
    for names, environment in ENVIRONMENTS.items():
        if given.lower() in names:
            return environment
    raise Stop(f"Unknown environment: {given}")


def site_named(system, name):
    if not name:
        name = system.ask("Enter folder name: ").strip()
    if not name:
        raise Stop("No site given.")
    try:
        return Site(normalize_site_name(name))
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None


def issue_certificate(system, name, yes=False):
    """A certificate for the site's binding and aliases, issued over its own config, which then gets HTTPS"""
    site = site_named(system, name)
    config = system.path(site.config_path)
    if not config.is_file():
        raise Stop(f"Apache config not found: {site.config_path}")
    text = config.read_text()
    binding = next((line.split()[1] for line in text.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")
    if not binding:
        raise Stop(f"Could not determine ServerName in {site.config_path}")
    binding = binding.lower()
    try:
        domains = certificate_domains(text, binding, site.config_path)
    except ValueError as error:
        raise Stop(str(error)) from None
    if not yes:
        answer = system.ask(f"Issue a new certificate for {' '.join(domains)}? [y/N]: ")
        if answer.lower() not in ("y", "yes"):
            system.say("Aborted")
            raise Stop("", 1)
    try:
        require_acme(system)
    except RuntimeError as error:
        raise Stop(str(error)) from None
    system.plan([f"Issue certificate for {' '.join(domains)}", "Install the certificate"])
    system.step(f"Issue certificate for {' '.join(domains)}")
    for domain in domains:
        clean_stale_acme(system, site, domain)
    arguments = [argument for domain in domains for argument in ("-d", domain)]
    issued = system.run(ACME, "--issue", "--apache", "--force", "--server", ACME_SERVER, *arguments, check=False)
    if issued.returncode not in (0, 2):
        raise Stop(f"Certificate issue failed. Check that DNS for {binding} points to this server.")
    system.path(site.certificate_directory).mkdir(parents=True, exist_ok=True)
    system.step("Install the certificate")
    system.run(ACME, "--install-cert", "-d", binding, "--key-file", site.key_file, "--fullchain-file", site.certificate_file,
               "--reloadcmd", "systemctl reload apache2")
    try:
        enable_https_config(system, site, binding)
    except RuntimeError as error:
        raise Stop(str(error)) from None
    service = system.path(site.service_path)
    working = ""
    if service.is_file():
        working = next((line[len("WorkingDirectory="):] for line in service.read_text().splitlines() if line.startswith("WorkingDirectory=")), "")
    if working and system.path(f"{working}/appsettings.json").is_file():
        if configure_umbraco_https(system, f"{working}/appsettings.json", binding, True):
            system.run("systemctl", "restart", site.service_name)
    system.say(f"Certificate installed in {site.certificate_directory}")


def change_environment(system, name, given=""):
    """The environment the site's service runs as (Development, Test, Staging or Production)"""
    site = site_named(system, name)
    service = system.path(site.service_path)
    if not service.is_file():
        raise Stop(f"Service file not found: {site.service_path}")
    text = service.read_text()
    system.say("Current:")
    for line in text.splitlines():
        if "_ENVIRONMENT" in line:
            system.say(line)
    environment = choose_environment(system, given)
    text = ENVIRONMENT_LINE.sub(lambda match: match[1] + environment, text)
    service.write_text(text)
    system.say("Now:")
    for line in text.splitlines():
        if "_ENVIRONMENT" in line:
            system.say(line)
    system.plan([f"Restart {site.service_name} as {environment}"])
    system.step(f"Restart {site.service_name} as {environment}")
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "restart", site.service_name)
    # The last command, as in the script: a service that did not come up again fails the action
    system.run("systemctl", "status", site.service_name, "--no-pager")
