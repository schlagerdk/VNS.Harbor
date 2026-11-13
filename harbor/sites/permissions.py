"""Fix permissions: the folder a site runs from owned by www-data, as fix_permissions.sh. The work is done by
apache/lib/site_permissions.py, which also runs over SSH on a server that has no harbor."""
from pathlib import Path
import shlex

from harbor.core.names import normalize_site_name
from harbor.core.system import Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.core.paths import LIB

HELPER = LIB / "site_permissions.py"


def fix_permissions(system, name, scope="test", target="prod"):
    try:
        name = normalize_site_name(name)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    site = Site(name)
    if scope == "test":
        system.step(f"Fix permissions of {name}")
        system.run("sudo", "env", f"SITE={name}", f"ROOT={site.site_root}", "python3", str(HELPER))
    elif scope == "prod":
        environment = settings.load(system, site.site_root, target)
        try:
            normalize_site_name(environment.FOLDER)
        except ValueError:
            raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
        system.step(f"Fix permissions of {environment.FOLDER} on {environment.description}")
        variables = f"SITE={shlex.quote(environment.FOLDER)} ROOT={shlex.quote(environment.root)}"
        settings.remote_exec(system, environment, f"env {variables} python3 -", input=HELPER.read_text())
    else:
        raise Stop("Scope must be test or prod.")
