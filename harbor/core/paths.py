"""Where Harbor is: the folders it runs its helpers, templates and scripts from (phase 3).

From a release in /opt/harbor/releases/<version> they are named through /opt/harbor/current, the release in use, so
what is kept elsewhere (scripts rendered into a site, a service's drop-in, a cron line) never names a release that is
removed later. Anywhere else (/vns, a working copy, a test) they are the package's own folders.
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
APACHE = INSTALL / "apache"
LIB = APACHE / "lib"
BIN = INSTALL / "bin/harbor"
# The .sql files Harbor runs: this installation's own
SQL = INSTALL / "sql"
# The sql folder whose scripts hold the sa password: /vns/sql of VNS 1.x while it is there (its custom scripts, such
# as clean_umbraco7_db.sh, still read the password from themselves), else this installation's
SA_SCRIPTS = Path("/vns/sql") if os.path.isdir("/vns/sql") and RELEASES in PACKAGE_ROOT.parents else SQL
