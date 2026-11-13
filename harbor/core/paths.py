"""Where Harbor is. A release (and a working copy) holds:

  bin/harbor   the command
  harbor/      the program
  admin/       the admin site
  lib/         helpers that the program, the admin and the sites' services run (some also on other servers)
  templates/   what Harbor puts on a server: sites/ (a site's Apache config, service and starting files),
               admin/ (the admin site's), apache/ (the server-wide Apache configs) and sql/ (the SQL it runs)

From a release in /opt/harbor/releases/<version> they are named through /opt/harbor/current, the release in use, so
what is kept elsewhere (a cron line, a service, a command sent to another server) never names a release that is
removed later. Anywhere else (a working copy, a test) they are the copy's own folders.
"""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
RELEASES = Path("/opt/harbor/releases")
CURRENT = Path("/opt/harbor/current")


def install_root():
    """The folder Harbor runs from, as it is to be named"""
    if RELEASES in PACKAGE_ROOT.parents:
        return CURRENT
    return PACKAGE_ROOT


INSTALL = install_root()
BIN = INSTALL / "bin/harbor"
ADMIN = INSTALL / "admin"
LIB = INSTALL / "lib"
TEMPLATES = INSTALL / "templates"
SITE_TEMPLATES = TEMPLATES / "sites"
# The SQL Harbor runs (backups, log shrinks, databases)
SQL = TEMPLATES / "sql"
# Where VNS 1.x kept scripts that hold the sa password: its custom scripts there, such as clean_umbraco7_db.sh, still
# read it from themselves, so they get it when it changes, while they are there
SA_SCRIPTS = Path("/vns/sql")
