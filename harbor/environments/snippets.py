"""Make a Linux environment's Apache config carry the main site's config snippets (Allowed IPs, rewrites and the like),
as the admin's config editor put them in the main site's config. From carry_snippets.sh; publish_prod.sh runs it at
every publish.

They are written between VNS's BEGIN and END lines and made equal to the main site's: rules written by hand stay. The
helper is sent along, so the server need not have this version of VNS. Apache tests the result before it is used; a
config it does not accept is put back as it was. A Windows environment gets them in its own publish.
"""
import base64
from pathlib import Path

from harbor.core.names import normalize_site_name
from harbor.core.system import CommandFailed, Stop
from harbor.environments import settings
from harbor.sites.actions import site_named
from harbor.core.paths import LIB

HELPER = LIB / "config_snippets.py"

CARRY = r'''set -euo pipefail
conf=/etc/apache2/sites-available/${SITE}.conf
[[ -f ${conf} ]] || exit 0
code=$(mktemp)
backup=$(mktemp)
trap 'rm -f "${code}" "${backup}"' EXIT
printf '%s' "${SNIPPET_CODE}" | base64 -d >"${code}"
cp -p "${conf}" "${backup}"
result=$(python3 "${code}" apache "${conf}" "$(printf '%s' "${SNIPPETS}" | base64 -d)")
if [[ ${result} != changed ]]; then
	echo "The config snippets are already the main site's"
elif apache2ctl configtest 2>/dev/null; then
	systemctl reload apache2
	echo "The config snippets are now the main site's"
else
	cp -p "${backup}" "${conf}"
	echo "Apache did not accept the config snippets, so the config is left as it was:" >&2
	apache2ctl configtest >&2 || true
fi
'''


def carry_snippets(system, name, target="prod"):
    site = site_named(system, name)
    environment = settings.load(system, site.site_root, target)
    if environment.windows:
        return
    try:
        normalize_site_name(environment.FOLDER)
    except ValueError:
        raise Stop("Invalid site name. Use a DNS name containing only letters, numbers, dots and hyphens.") from None
    main_config = system.path(f"/etc/apache2/sites-available/{site.name}.conf")
    if not main_config.is_file():
        return
    system.step("Carry the main site's config snippets")
    try:
        # All the helper prints, its last line break too, as the script piped it to base64
        snippets = system.run("python3", str(HELPER), "read", str(main_config), capture=True).stdout
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None
    variables = (f"SITE={environment.FOLDER} SNIPPETS={base64.b64encode(snippets.encode()).decode()} "
                 f"SNIPPET_CODE={base64.b64encode(HELPER.read_bytes()).decode()}")
    result = settings.remote_run(system, environment, variables, CARRY, check=False)
    if not result.ok:
        raise Stop("", result.returncode)
