"""Apache's server-wide settings"""
import re

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
