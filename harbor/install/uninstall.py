"""Harbor taken off this server (harbor self uninstall). The sites go on running: their folders, services, Apache
configs, certificates, SQL Server and .NET stay, and so do Apache's server-wide configs (the TLS policy and the offline
page), which their configs use. What Harbor knew of them (/var/lib/harbor) and its secrets (/etc/harbor) are kept in
one archive, readable by root only, before they go.

Taken off: the admin site, the Linux users Harbor made (with their site groups, ACLs and SFTP), Harbor's jobs in cron
(backups, log shrinks, keep-alive), its state and secrets, its releases in /opt/harbor and the harbor command.
"""
from datetime import datetime
import os
from pathlib import Path
import shutil

from harbor.core.system import Stop

BACKUP_FOLDER = "/root"
GONE = ("/opt/harbor", "/var/lib/harbor", "/etc/harbor", "/etc/cron.d/vns-keep-alive", "/var/lib/vns", "/etc/vns")


def uninstall(system, assume_yes=False):
    # Everything it needs is loaded first: the code it runs from goes at the end
    from harbor.install import admin
    from harbor.install.release import COMMAND
    from harbor.server import tools, users
    from harbor.server.apache import own_name
    from harbor.sites.delete import delete_site

    name = own_name(system)
    system.say(f"This takes VNS Harbor off {name}: the admin site, the users it made, its jobs, its state and secrets, and")
    system.say("the harbor command. The sites go on running as they are. Run it in a terminal on the server, not in the")
    system.say("admin's own terminal: the admin goes first.")
    if not assume_yes and system.ask(f"Type the server's name ({name}) to take Harbor off it: ").strip() != name:
        raise Stop("Nothing was changed.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = f"{BACKUP_FOLDER}/harbor-{stamp}.tar.gz"
    kept = [path.lstrip("/") for path in ("/var/lib/harbor", "/etc/harbor") if system.path(path).exists()]
    system.plan([*(["Keep Harbor's state and secrets"] if kept else []), *(f"Remove the admin site {site}" for site in admin.admin_sites(system)),
                 "Remove the Linux users Harbor made", "Take Harbor's jobs out of cron", "Remove Harbor"])
    if kept:
        system.step("Keep Harbor's state and secrets")
        if not system.run("tar", "-czf", archive, "-C", "/", *kept, check=False).ok:
            raise Stop("Harbor's state and secrets could not be kept in an archive, so nothing was changed.")
        if system.path(archive).is_file():
            system.path(archive).chmod(0o600)
        system.say(f"Kept in {archive} (root only).")

    for site in admin.admin_sites(system):
        system.step(f"Remove the admin site {site}")
        delete_site(system, site, "n")

    system.step("Remove the Linux users Harbor made")
    try:
        users.sync(system, [])
        system.run("groupdel", users.GROUP, check=False, capture=True)
    except Stop as error:
        system.warn(f"The users were not removed: {error}")

    system.step("Take Harbor's jobs out of cron")
    tools.cron(system, False)

    system.step("Remove Harbor")
    command = system.path(COMMAND)
    if command.is_symlink() and os.readlink(command).startswith("/opt/harbor/"):
        command.unlink()
    for path in GONE:
        target = system.path(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif os.path.lexists(target):
            target.unlink()
    system.say("VNS Harbor is taken off this server. The sites go on running.")
    if kept:
        system.say(f"What it knew of them is in {archive}.")
