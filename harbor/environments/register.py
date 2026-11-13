"""Make a site that already runs on another Linux server an environment of a main site here. From register_env.sh.

Its binding, protocol and environment are read from the server. Nothing is published, and the only thing written on
the server is the note that tells its admin that the site is managed from here. The settings are written by the site's
create_prod.sh template, which runs as it did; what runs on the server is sent word for word.
"""
import os
from pathlib import Path
import re
import shlex
import tempfile

from harbor.core.names import normalize_site_name, service_name
from harbor.core.system import Stop
from harbor.core.templates import Site, render
from harbor.environments import settings
from harbor.environments.settings import Environment

READ_SITE = r'''root=/var/www/${FOLDER}
service=/etc/systemd/system/${UNIT}.service
conf=/etc/apache2/sites-available/${FOLDER}.conf
[[ -d ${root} ]] && echo "folder=yes"
compgen -G "${root}/*.csproj" >/dev/null && echo "project=yes"
[[ -e /var/lib/vns-managed/${FOLDER} ]] && echo "managed=$(tr '\n' ' ' </var/lib/vns-managed/"${FOLDER}")"
if [[ -f ${service} ]]; then
	echo "working=$(sed -n 's/^WorkingDirectory=//p' "${service}" | head -n 1)"
	echo "dll=$(sed -n 's/^ExecStart=//p' "${service}" | grep -oE '[^ "]+\.dll' | head -n 1)"
	echo "environment=$(sed -n 's/^Environment=ASPNETCORE_ENVIRONMENT=//p' "${service}" | head -n 1)"
fi
if [[ -f ${conf} ]]; then
	echo "binding=$(awk '$1 == "ServerName" { print $2; exit }' "${conf}")"
	grep -Eq '^[[:space:]]*SSLEngine[[:space:]]+On([[:space:]]|$)' "${conf}" && echo "https=y"
fi
exit 0
'''
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
SAFE = re.compile(r"[A-Za-z0-9._+:@%/,=-]+")


def bash_quote(value):
    """A value as printf %q writes it, for the plain names it is used with; anything else quoted for bash"""
    return value if SAFE.fullmatch(value) else shlex.quote(value)


def site_name(value):
    if not value:
        raise Stop("No site given.")
    try:
        return normalize_site_name(value)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None


def run_create_prod(system, site, variables):
    """harbor env create for the site, with the VNS_PROD_* settings (what its create_prod.sh ran)"""
    from harbor.core.paths import BIN
    return system.run("python3", str(BIN), "env", "create", site.name, "--site-root", site.site_root, "--binding", site.binding,
                      "--project", site.project_name, "--publish-folder", site.publish_folder, "--admin-mail", site.admin_mail,
                      "--kind", site.csharp_kind, env=variables, check=False)


def register_environment(system, name):
    name = site_name(name)
    site = Site(name)
    main = settings.load_main_site(system, site)
    site.project_name, site.binding, site.admin_mail = main.project_name, main.binding, main.admin_mail
    site.publish_folder, site.csharp_kind = main.publish_folder, main.csharp_kind

    environment = Environment(IP=os.environ.get("VNS_PROD_IP", ""), USER=os.environ.get("VNS_PROD_USER", ""),
                              PASSWORD=os.environ.get("VNS_PROD_PASSWORD", ""), SSH_PORT=os.environ.get("VNS_PROD_SSH_PORT") or "22")
    folder = site_name(os.environ.get("VNS_PROD_FOLDER", ""))
    if not HOST.fullmatch(environment.IP):
        raise Stop("Invalid server host.")
    if not USER.fullmatch(environment.USER):
        raise Stop("Invalid SSH username.")
    unit = service_name(folder)

    # Read what is needed from the server. The answer is parsed, never executed.
    system.step(f"Read {folder} on {environment.IP}")
    script = f"FOLDER={bash_quote(folder)}\nUNIT={bash_quote(unit)}\n" + READ_SITE
    result = settings.remote_exec(system, environment, "bash -s", input=script, capture=True, check=False)
    if not result.ok:
        raise Stop(f"Could not read {folder} on {environment.IP}.")
    found = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        found[key] = value
    working = found.get("working", "")
    working = working[:-1] if working.endswith("/") else working
    dll = os.path.basename(found.get("dll", ""))
    dll = dll[:-len(".dll")] if dll.endswith(".dll") else dll
    binding = found.get("binding", "")
    if not found.get("folder"):
        raise Stop(f"{folder} was not found on {environment.IP}.")
    if found.get("project"):
        raise Stop(f"{folder} has its own project on {environment.IP}, so it is a main site.")
    if found.get("managed"):
        raise Stop(f"{folder} is already an environment of another main site: {found['managed']}")
    if not dll:
        raise Stop(f"{folder} has no service that runs a dll on {environment.IP}.")
    if not binding:
        raise Stop(f"{folder} has no Apache config with a ServerName on {environment.IP}.")
    if working != f"/var/www/{folder}":
        raise Stop(f"{folder} runs from {working}. An environment on a server must run from /var/www/{folder}.")
    project = system.path(f"{site.site_root}/{main.project_name}.csproj").read_text(errors="replace")
    assembly = next((match[1] for match in re.finditer(r"<AssemblyName>(.*)</AssemblyName>", project)), "") or main.project_name
    if assembly != dll:
        raise Stop(f"{folder} runs {dll}.dll, but {name} builds {assembly}.dll. A publish would break the site.")
    environment_name = found.get("environment", "")
    if environment_name not in ("Development", "Test", "Staging", "Production"):
        environment_name = "Production"

    system.step(f"Add {folder} as an environment of {name}")
    variables = {"VNS_PROD_MODAL": "1", "VNS_PROD_NO_PUBLISH": "1", "VNS_PROD_TARGET": "remote", "VNS_PROD_IP": environment.IP,
                 "VNS_PROD_USER": environment.USER, "VNS_PROD_PASSWORD": environment.PASSWORD, "VNS_PROD_SSH_PORT": environment.SSH_PORT,
                 "VNS_PROD_FOLDER": folder, "VNS_PROD_BINDING": binding, "VNS_PROD_ENVIRONMENT": environment_name,
                 "VNS_PROD_HTTPS": found.get("https") or "n"}
    created = run_create_prod(system, site, variables)
    if not created.ok:
        raise Stop("", created.returncode)

    system.step(f"Note on {environment.IP} that {folder} is managed from here")
    here = system.output("hostname", "-f", check=False) or system.output("hostname", check=False)
    note = settings.remote_exec(system, environment, f"bash -c 'mkdir -p /var/lib/vns-managed && cat >\"/var/lib/vns-managed/$0\"' {bash_quote(folder)}",
                                input=f"{name}\n{here}\n", check=False)
    if not note.ok:
        system.warn(f"WARNING: could not write the note on {environment.IP}. It is written when the admin here next reads the environment's status.")
    system.say(f"{folder} on {environment.IP} is now an environment of {name}. Nothing was published.")
