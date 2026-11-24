"""A new server made ready for sites: Apache, acme.sh and the keep-alive job (was apache/init.sh). Each step is harmless
to repeat. .NET comes with the first site that needs it (harbor.core.dotnet), Samba from Server tools (harbor.server.samba)."""
from harbor.core.paths import LIB
from harbor.core.site_config import ACME, ACME_FOLDERS
from harbor.core.templates import ADMIN_MAIL

PACKAGES = ("curl", "unzip", "zip", "tar", "git", "rsync", "ca-certificates", "uuid-runtime", "python3", "python3-venv",
            "python3-pip", "cron", "acl", "apache2", "sshpass")
MODULES = ("ssl", "rewrite", "headers", "proxy", "proxy_http", "proxy_wstunnel")
INITIALIZED = "/var/lib/vns/apache-initialized"


def remove_default_page(system):
    """Ubuntu's own Apache page goes (000-default and /var/www/html), when that is all there is: in /var/www it would
    look like a site. A page put there by hand stays."""
    folder = system.path("/var/www/html")
    files = sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []
    if files != ["index.html"] or "Apache2 Ubuntu Default Page" not in (folder / "index.html").read_text(errors="replace"):
        return
    system.say("Remove Ubuntu's default Apache page")
    if system.path("/etc/apache2/sites-enabled/000-default.conf").exists():
        system.run("a2dissite", "-q", "000-default")
    (folder / "index.html").unlink()
    folder.rmdir()
    system.run("systemctl", "reload", "apache2", check=False)


def init(system):
    from harbor.server.apache import install_apache_conf, install_tls_policy, set_servername
    system.plan(["Install the packages Harbor needs", *([] if system.which("rclone") else ["Install rclone"]), "Set up Apache",
                 *([] if system.path(ACME).is_file() else ["Install acme.sh"]),
                 "Install the keep-alive job"])
    system.step("Install the packages Harbor needs")
    system.run("apt-get", "update")
    system.run("apt-get", "install", "-y", *PACKAGES, env={"DEBIAN_FRONTEND": "noninteractive"})
    if not system.which("rclone"):
        system.step("Install rclone")
        system.run("bash", "-c", "curl -fsSL https://rclone.org/install.sh | bash")

    system.step("Set up Apache")
    # The per-site empty roots that isolate the site services
    system.path("/var/lib/vns-roots").mkdir(parents=True, exist_ok=True)
    system.run("a2enmod", "-q", *MODULES)
    set_servername(system)
    install_tls_policy(system, start_if_stopped=True)
    install_apache_conf(system)
    remove_default_page(system)

    if not system.path(ACME).is_file():
        system.step("Install acme.sh")
        system.run("bash", "-c", "set -e; folder=$(mktemp -d); trap 'rm -rf \"$folder\"' EXIT; "
                   "git clone --quiet --depth 1 https://github.com/acmesh-official/acme.sh.git \"$folder\"; cd \"$folder\"; "
                   f"./acme.sh --install --home /etc/acmesh --config-home /etc/ssl/data --cert-home /etc/ssl/certs --accountemail {ADMIN_MAIL}")
    # acme.sh is given its folders, as when it was installed: without them it looks for its config in /root/.acme.sh
    system.run(ACME, *ACME_FOLDERS, "--set-default-ca", "--server", "letsencrypt")
    system.run(ACME, *ACME_FOLDERS, "--install-cronjob")

    # No .NET: a site installs the .NET it needs when it is made, and Server tools adds a version (harbor.core.dotnet)

    system.step("Install the keep-alive job")
    system.run("python3", f"{LIB}/keep_alive.py", "--install")
    system.run("systemctl", "enable", "--now", "cron")
    marker = system.path(INITIALIZED)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    system.say("The server is ready. Add the admin site with: harbor admin install")
