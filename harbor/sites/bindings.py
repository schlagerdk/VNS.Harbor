"""Change a site's binding and aliases. From change_binding.sh.

The new binding * makes the site answer any name no other site has, with this server's own name as its binding. A
site without a ServerName gets one. With HTTPS the certificate is issued again for the new names; when a step fails,
the Apache config, the certificate and appsettings.json are put back as they were.
"""
import filecmp
import os
from pathlib import Path
import re
import shutil
import tempfile

from harbor.core.bindings import certificate_domains, replace_server_aliases, server_aliases, server_aliases_from_config, site_binding
from harbor.core.names import normalize_site_name
from harbor.core.site_config import (ACME, ACME_SERVER, DEFAULT_SITE_LINK, SSL_ON, clean_stale_acme, default_site,
                                     make_default_site, read_template, require_acme)
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site, render
from harbor.server.apache import own_name


def enabled_elsewhere(system, site, domain):
    """The enabled config other than the site's own that has domain as a name, or None"""
    mine = os.path.realpath(system.path(site.config_path))
    for config in sorted(system.path("/etc/apache2/sites-enabled").glob("*.conf")):
        if not config.exists() or os.path.realpath(config) == mine:
            continue
        for line in config.read_text(errors="replace").splitlines():
            fields = line.split()
            if fields[:1] in (["ServerName"], ["ServerAlias"]) and domain.lower() in (name.lower() for name in fields[1:]):
                return config
    return None


def server_name(text):
    for line in text.splitlines():
        fields = line.split()
        if fields[:1] == ["ServerName"] and len(fields) > 1:
            return fields[1]
    return ""


def change_binding(system, name="", new_binding="", aliases=None, yes=False):
    """aliases None asks for them, as the script did without a third argument"""
    if not name:
        name = system.ask("Enter folder name: ").strip()
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    site = Site(name)
    config = system.path(site.config_path)
    if not config.is_file():
        raise Stop(f"Apache config not found: {site.config_path}")
    if not os.path.lexists(system.path(f"/etc/apache2/sites-enabled/{name}.conf")):
        raise Stop(f"Apache site is not enabled: {name}")
    text = config.read_text()
    old_binding = server_name(text).lower()
    if not old_binding:
        system.say(f"The site has no binding yet (no ServerName in {site.config_path}).")
    # A site without a certificate may use names without a dot
    http_only = not SSL_ON.search(text)
    if not new_binding:
        new_binding = system.ask(f"Enter new site URL binding (* = answer any name){f' [{old_binding}]' if old_binding else ''}: ").strip() or old_binding
    was_default = default_site(system) == name
    make_default = False
    if new_binding == "*":
        current = default_site(system)
        if current and current != name:
            raise Stop(f"{current} already answers any name. Only one site can: delete it or give it a binding first.")
        make_default = True
        new_binding = own_name(system).lower()
        if not http_only and "." not in new_binding:
            raise Stop(f"This server's name, {new_binding}, has no dot, so it cannot be in the site's certificate. Set a full name with hostnamectl.")
    try:
        new_binding = site_binding(new_binding, http_only)
        old_aliases = server_aliases_from_config(text, old_binding, http_only)
    except ValueError as error:
        raise Stop(str(error)) from None
    system.say(f"Current aliases: {' '.join(old_aliases) or 'none'}")
    if aliases is None:
        given = system.ask("New aliases, comma-separated (Enter keeps current; - removes all): ")
        aliases = " ".join(old_aliases) if not given else "" if given == "-" else given
    try:
        new_aliases = server_aliases(new_binding, aliases, http_only)
        compared = server_aliases(old_binding, " ".join(old_aliases), http_only)
    except ValueError as error:
        raise Stop(str(error)) from None
    if new_binding == old_binding and new_aliases == compared and make_default == was_default:
        system.say("The binding and aliases are unchanged.")
        return
    for domain in [new_binding, *new_aliases]:
        used = enabled_elsewhere(system, site, domain)
        if used:
            raise Stop(f"Binding or alias {domain} is already used by /{used.relative_to(system.root)}")

    https = not http_only
    if https:
        try:
            require_acme(system)
        except RuntimeError as error:
            raise Stop(str(error)) from None
        if not system.path(site.certificate_file).is_file() or not system.path(site.key_file).is_file():
            raise Stop("Current HTTPS certificate files are missing; refusing to replace the bindings.")
        system.say("HTTPS is enabled: the certificate will be updated. All names must resolve to this server and be reachable on HTTP/port 80.")
    else:
        system.say("HTTP-only site: no certificate will be issued.")
    scheme = "https" if https else "http"
    if not yes:
        answer = system.ask(f"Change bindings to {scheme}://{new_binding} (aliases: {' '.join(new_aliases) or 'none'})? [y/N]: ")
        if answer.lower() not in ("y", "yes"):
            system.say("Aborted")
            raise Stop("", 1)

    # The publish folder is chosen per site: the service's working directory says which
    publish = site.publish_folder
    service = system.path(site.service_path)
    if service.is_file():
        working = next((line.split("=", 1)[1] for line in service.read_text().splitlines() if line.split("=", 1)[0] == "WorkingDirectory"), "")
        if working:
            publish = os.path.basename(working)
    settings = system.path(f"{site.site_root}/{publish}/appsettings.json")

    revoke = bool(old_binding and new_binding != old_binding and https
                  and not (old_binding in new_aliases or enabled_elsewhere(system, site, old_binding) is not None))
    system.plan([f"Change the binding to {new_binding}", *([f"Issue a certificate for {new_binding}"] if https else []),
                 *([f"Revoke and remove certificate for {old_binding}"] if revoke else [])])
    with tempfile.TemporaryDirectory() as backup:
        backup = Path(backup)
        shutil.copy2(config, backup / "site.conf")
        if https:
            shutil.copy2(system.path(site.certificate_file), backup / "fullchain.cer")
            shutil.copy2(system.path(site.key_file), backup / "site.key")
        if settings.is_file():
            shutil.copy2(settings, backup / "appsettings.json")
        restarted = False

        def rollback():
            system.warn("Restoring previous Apache config and certificate")
            shutil.copy2(backup / "site.conf", config)
            if https:
                shutil.copy2(backup / "fullchain.cer", system.path(site.certificate_file))
                shutil.copy2(backup / "site.key", system.path(site.key_file))
            if (backup / "appsettings.json").is_file():
                shutil.copy2(backup / "appsettings.json", settings)
            system.run("apache2ctl", "configtest")
            system.run("systemctl", "reload", "apache2")
            if restarted:
                system.run("systemctl", "restart", site.service_name, check=False)

        def fail(message="", code=1):
            if message:
                system.warn(message)
            rollback()
            raise Stop("", code)

        system.step(f"Change the binding to {new_binding}")
        text = config.read_text()
        if not old_binding:
            # No ServerName yet: each virtual host gets one
            text, count = re.subn(r"(?m)^([ \t]*<VirtualHost\b.*\n)", lambda match: f"{match[1]}  ServerName {new_binding}\n", text)
            if not count:
                fail("No virtual host found")
        elif new_binding != old_binding:
            count = 0

            def renamed(match):
                nonlocal count
                line, found = re.subn(re.escape(old_binding), new_binding, match[0], flags=re.I)
                count += found
                return line
            text = re.sub(r"(?m)^[ \t]*(?:ServerName|Redirect)\b.*$", renamed, text)
            if not count:
                fail("No binding directive updated")
        config.write_text(replace_server_aliases(text, new_aliases))
        if settings.is_file() and '"UmbracoApplicationUrl"' in settings.read_text():
            content, count = re.subn(r'("UmbracoApplicationUrl"\s*:\s*")[^"]*(")', lambda match: f"{match[1]}{scheme}://{new_binding}{match[2]}",
                                     settings.read_text(), count=1)
            if not count:
                fail("UmbracoApplicationUrl not found")
            settings.write_text(content)

        try:
            if https:
                try:
                    domains = certificate_domains(config.read_text(), new_binding, site.config_path)
                except ValueError as error:
                    fail(str(error))
                new_config = config.read_text()
                site.binding = new_binding
                site.doc_root = site.site_root
                config.write_text(replace_server_aliases(render(read_template(f"{site.harbor_dir}/templates/sites/common/apache-temp.conf.tpl"), site), new_aliases))
                if not system.run("apache2ctl", "configtest", check=False).ok or not system.run("systemctl", "reload", "apache2", check=False).ok:
                    fail()
                system.step(f"Issue a certificate for {new_binding}")
                system.say(f"Names in the certificate: {' '.join(domains)}")
                for domain in domains:
                    if domain not in (old_binding, name):
                        clean_stale_acme(system, site, domain)
                arguments = [argument for domain in domains for argument in ("-d", domain)]
                issued = system.run(ACME, "--issue", "--apache", "--force", "--server", ACME_SERVER, *arguments, check=False)
                if issued.returncode not in (0, 2):
                    fail("", issued.returncode)
                if not system.run(ACME, "--install-cert", "-d", new_binding, "--key-file", site.key_file, "--fullchain-file",
                                  site.certificate_file, "--reloadcmd", "systemctl reload apache2", check=False).ok:
                    fail()
                config.write_text(new_config)
            if not system.run("apache2ctl", "configtest", check=False).ok or not system.run("systemctl", "reload", "apache2", check=False).ok:
                fail()
            if (backup / "appsettings.json").is_file() and not filecmp.cmp(backup / "appsettings.json", settings, shallow=False):
                restarted = True
                if not system.run("systemctl", "restart", site.service_name, check=False).ok:
                    fail()
        except OSError as error:
            fail(str(error))

    # The site that answers any name has a second link to its config
    if make_default and not was_default:
        try:
            make_default_site(system, site)
        except RuntimeError as error:
            raise Stop(str(error)) from None
    elif not make_default and was_default:
        system.say(f"{name} no longer answers any name")
        link = system.path(DEFAULT_SITE_LINK)
        if os.path.lexists(link):
            link.unlink()
        system.run("apache2ctl", "configtest")
        system.run("systemctl", "reload", "apache2")

    if old_binding and new_binding != old_binding and https:
        if not revoke:
            system.say(f"Previous binding {old_binding} is still used; keeping its certificate.")
        else:
            system.step(f"Revoke and remove certificate for {old_binding}")
            system.run(ACME, "--revoke", "-d", old_binding)
            system.run(ACME, "--remove", "-d", old_binding)
            clean_stale_acme(system, site, old_binding)

    system.say(f"Bindings updated: {scheme}://{new_binding}/ (aliases: {' '.join(new_aliases) or 'none'}, folder: {name})")
    if make_default:
        system.say("It also answers any name that no other site has.")
