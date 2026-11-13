"""Apache's server-wide settings"""
import os
from pathlib import Path
import re
import shutil
import tempfile

from harbor.core.system import CommandFailed, Stop
from harbor.core.paths import INSTALL

NAME = re.compile(r"[A-Za-z0-9.-]+")
SERVERNAME_CONF = "/etc/apache2/conf-available/vns-servername.conf"


def own_name(system):
    """This server's own name: its full name, else its short one, else localhost. As server_own_name in common.sh,
    without the lowercase, which that adds for a site's binding."""
    name = system.output("hostname", "-f", check=False)
    if not NAME.fullmatch(name):
        name = system.output("hostname", check=False)
    if not NAME.fullmatch(name):
        name = "localhost"
    return name


def has_servername(system):
    """Whether Apache has a ServerName of its own, in apache2.conf or an enabled config"""
    files = [system.path("/etc/apache2/apache2.conf")]
    enabled = system.path("/etc/apache2/conf-enabled")
    if enabled.is_dir():
        files += sorted(path for path in enabled.rglob("*") if path.is_file())
    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"(?m)^[ \t]*ServerName[ \t]", text):
            return True
    return False


def set_servername(system):
    """Apache warns "Could not reliably determine the server's fully qualified domain name" unless a ServerName is
    set globally. This sets it to this server's own name. Nothing changes when one is already set."""
    if has_servername(system):
        return
    name = own_name(system)
    system.say(f"Set Apache's global ServerName to {name}")
    conf = system.path(SERVERNAME_CONF)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"# This server's own name, so Apache does not warn that it cannot determine it\nServerName {name}\n")
    system.run("a2enconf", "-q", "vns-servername")


# ---- install_apache_conf.sh and install_tls_policy.sh
# The Harbor installation the configs come from (templates/apache, and lib/app_offline.htm)
APACHE_SOURCE = INSTALL
SERVER_WIDE = ("vns-offline", "vns-headers")


def install_file(source, target, mode=0o644):
    """As install -m: the file in place with its mode"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chmod(target, mode)


def install_apache_conf(system, source=APACHE_SOURCE):
    """The server-wide configs every site has: the page shown while a site's service does not answer, and headers
    that do not tell others what the server runs. If Apache does not accept them, the ones before are put back."""
    available = system.path("/etc/apache2/conf-available")
    if not available.is_dir():
        system.say("Apache not installed; skipping the server-wide configs.")
        return
    with tempfile.TemporaryDirectory() as backup:
        for name in SERVER_WIDE:
            if (available / f"{name}.conf").is_file():
                shutil.copy2(available / f"{name}.conf", backup)
        install_file(Path(source) / "lib/app_offline.htm", system.path("/var/lib/vns/offline/app_offline.htm"))
        system.run("a2enmod", "-q", "headers", capture=True)
        for name in SERVER_WIDE:
            install_file(Path(source) / f"templates/apache/{name}.conf", available / f"{name}.conf")
            system.run("a2enconf", "-q", name, capture=True)
        if not system.run("apache2ctl", "configtest", check=False).ok:
            system.warn("Apache did not accept the server-wide configs; the previous ones are kept.")
            for name in SERVER_WIDE:
                if (Path(backup) / f"{name}.conf").is_file():
                    shutil.copy2(Path(backup) / f"{name}.conf", available / f"{name}.conf")
                else:
                    system.run("a2disconf", "-q", name, check=False, capture=True)
                    (available / f"{name}.conf").unlink(missing_ok=True)
            raise Stop("", 1)
    if system.run("systemctl", "is-active", "--quiet", "apache2", check=False).ok:
        system.run("systemctl", "reload", "apache2")
    system.say("Server-wide configs installed: the offline page, and headers that do not tell what the server runs.")


def install_tls_policy(system, start_if_stopped=False, source=APACHE_SOURCE):
    """The shared server-side TLS policy: TLS 1.2 and 1.3 only. Restores what was there when a step fails."""
    has_ctl = system.which("apache2ctl")
    has_dir = system.path("/etc/apache2").is_dir()
    if not has_ctl and not has_dir:
        if start_if_stopped:
            raise Stop("Apache is not installed; cannot install TLS policy.")
        system.say("Apache not installed; skipping shared TLS policy.")
        return
    if not has_ctl or not has_dir:
        raise Stop("Incomplete Apache installation; cannot install TLS policy.")
    source_conf = Path(source) / "templates/apache/vns-tls.conf"
    available = system.path("/etc/apache2/conf-available/vns-tls.conf")
    enabled = system.path("/etc/apache2/conf-enabled/vns-tls.conf")
    backup = available.with_name(f"vns-tls.conf.vns-backup-{os.getpid()}")
    if not (source_conf.is_file() and available.parent.is_dir() and enabled.parent.is_dir()):
        raise Stop("Apache TLS policy source or configuration directories are missing.")
    if available.is_symlink() or (available.exists() and not available.is_file()):
        raise Stop(f"Refusing to replace non-regular TLS policy: {server_path(available, system)}")
    if os.path.lexists(enabled) and (not enabled.is_symlink() or os.readlink(enabled) != "../conf-available/vns-tls.conf"):
        raise Stop(f"Refusing to replace unexpected enabled TLS policy: {server_path(enabled, system)}")
    if os.path.lexists(backup):
        raise Stop(f"TLS policy backup already exists: {server_path(backup, system)}")
    had_config = available.exists()
    had_enabled = enabled.is_symlink()
    enabled_ssl = reload_attempted = False
    if had_config:
        shutil.copy2(available, backup)
    try:
        if not system.run("a2query", "-m", "ssl", check=False, capture=True).ok:
            system.run("a2enmod", "ssl")
            enabled_ssl = True
        install_file(source_conf, available)
        if not had_enabled:
            enabled.symlink_to("../conf-available/vns-tls.conf")
        system.say("Testing Apache TLS policy (TLS 1.2 and TLS 1.3 required)")
        system.run("apache2ctl", "configtest")
        if system.run("systemctl", "is-active", "--quiet", "apache2", check=False).ok:
            reload_attempted = True
            system.run("systemctl", "reload", "apache2")
        elif start_if_stopped:
            system.run("systemctl", "start", "apache2")
        else:
            system.say("Apache is stopped; TLS policy will be used when it starts.")
    except (CommandFailed, OSError):
        system.warn("TLS policy installation failed; restoring previous Apache configuration.")
        if had_config:
            os.replace(backup, available)
        else:
            available.unlink(missing_ok=True)
        if not had_enabled and os.path.lexists(enabled):
            enabled.unlink()
        if enabled_ssl and not system.run("a2dismod", "ssl", check=False, capture=True).ok:
            system.warn("Could not disable ssl module after failure.")
        if reload_attempted:
            if system.run("apache2ctl", "configtest", check=False).ok and system.run("systemctl", "reload", "apache2", check=False).ok:
                system.warn("Restored Apache configuration reloaded.")
            else:
                system.warn("Could not reload restored Apache configuration; check Apache manually.")
        raise Stop("", 1) from None
    backup.unlink(missing_ok=True)
    system.say("Shared Apache TLS policy installed.")


def server_path(path, system):
    """A path under the root as the server knows it"""
    return "/" + str(Path(path).relative_to(system.root)).lstrip("/")
