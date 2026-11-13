"""Moved to harbor.core.names. This file stays for the admin site and the scripts that run it by its path."""
import os
import sys

_install = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if not os.path.isdir(os.path.join(_install, "harbor")):
    sys.exit(f"The harbor package was not found in {_install}: it is installed beside the lib folder.")
sys.path.insert(0, _install)

from harbor.core.names import display_dns_name, normalize_dns_name, normalize_site_name, service_name  # noqa: E402,F401

if __name__ == "__main__":
    try:
        mode = sys.argv[2]
        print(normalize_site_name(sys.argv[1]) if mode == "site"
              else normalize_dns_name(sys.argv[1], fully_qualified=mode == "binding"))
    except ValueError as error:
        sys.exit(str(error))
