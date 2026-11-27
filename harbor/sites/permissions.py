"""Fix permissions: the folder a site runs from owned by www-data, as fix_permissions.sh. The work is done by
lib/site_permissions.py, which also runs over SSH on a server that has no harbor."""
from pathlib import Path
import shlex

from harbor.core.names import normalize_site_name
from harbor.core.system import Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.core.paths import LIB

HELPER = LIB / "site_permissions.py"


def remote_helper():
    """The helper as it runs over SSH (python3 -), with the names it needs"""
    from harbor.core.names import standalone
    return standalone(HELPER.read_text())


def fix_permissions(system, name, scope="test", target="prod"):
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    site = Site(name)
    if scope == "test":
        system.plan([f"Fix permissions of {name}"])
        system.step(f"Fix permissions of {name}")
        system.run("sudo", "env", f"SITE={name}", f"ROOT={site.site_root}", "python3", str(HELPER))
    elif scope == "prod":
        environment = settings.load(system, site.site_root, target)
        try:
            normalize_site_name(environment.FOLDER)
        except ValueError:
            raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
        system.plan([f"Fix permissions of {environment.FOLDER} on {environment.description}"])
        system.step(f"Fix permissions of {environment.FOLDER} on {environment.description}")
        variables = f"SITE={shlex.quote(environment.FOLDER)} ROOT={shlex.quote(environment.root)}"
        settings.remote_exec(system, environment, f"env {variables} python3 -", input=remote_helper())
    else:
        raise Stop("Scope must be test or prod.")


def permissions_needed(system, name, scope="test", target="prod"):
    """Whether Fix permissions has something to fix, without changing anything: {needed, example} (needed None when
    it cannot be told, with error)"""
    import json
    name = normalize_site_name(name)
    site = Site(name)
    if scope == "test":
        result = system.run("sudo", "env", "CHECK=1", f"SITE={name}", f"ROOT={site.site_root}", "python3", str(HELPER), capture=True, check=False)
    elif scope == "prod":
        environment = settings.load(system, site.site_root, target)
        normalize_site_name(environment.FOLDER)
        variables = f"CHECK=1 SITE={shlex.quote(environment.FOLDER)} ROOT={shlex.quote(environment.root)}"
        result = settings.remote_exec(system, environment, f"env {variables} python3 -", input=remote_helper(), capture=True, check=False)
    else:
        raise Stop("Scope must be test or prod.")
    try:
        return json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"needed": None, "error": (result.stderr or result.stdout or "no answer").strip()[-300:]}
