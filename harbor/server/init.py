"""A new server made ready for sites: Apache, acme.sh, .NET, the templates and the keep-alive job (was apache/init.sh).
Each step is harmless to repeat."""
from harbor.core.dotnet import DOTNET_DIR, ensure_channel
from harbor.core.paths import LIB
from harbor.core.templates import ADMIN_MAIL

PACKAGES = ("curl", "unzip", "zip", "tar", "git", "rsync", "ca-certificates", "uuid-runtime", "python3", "python3-venv",
            "python3-pip", "cron", "acl", "apache2", "sshpass")
MODULES = ("ssl", "rewrite", "headers", "proxy", "proxy_http", "proxy_wstunnel")
ACME = "/etc/acmesh/acme.sh"
ACME_FOLDERS = ("--home", "/etc/acmesh", "--config-home", "/etc/ssl/data")
# The newest .NET is the default host; older channels that sites use are installed beside it: .NET 10 (LTS, which
# most sites use) and .NET 8 for Umbraco 13
DOTNET_CHANNELS = ("11.0", "10.0", "8.0")
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

    system.step(f".NET {', '.join(DOTNET_CHANNELS)}")
    for channel in DOTNET_CHANNELS:
        ensure_channel(system, channel)
    # The generated services use /usr/bin/dotnet
    link = system.path("/usr/bin/dotnet")
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(f"{DOTNET_DIR}/dotnet")
    system.path("/etc/profile.d/dotnet.sh").write_text(f"export DOTNET_ROOT={DOTNET_DIR}\n")
    dotnet = {"DOTNET_ROOT": DOTNET_DIR}
    if not system.run("dotnet", "tool", "install", "--tool-path", "/usr/local/bin", "dotnet-outdated-tool", env=dotnet, check=False, capture=True).ok:
        system.run("dotnet", "tool", "update", "--tool-path", "/usr/local/bin", "dotnet-outdated-tool", env=dotnet)
    system.step("Install the Umbraco and VNS templates")
    system.run("dotnet", "new", "install", "Umbraco.Templates", "--force", env=dotnet)
    system.run("dotnet", "new", "install", "VNS.Templates", "--force", env=dotnet)

    system.step("Install the keep-alive job")
    system.run("python3", f"{LIB}/keep_alive.py", "--install")
    system.run("systemctl", "enable", "--now", "cron")
    marker = system.path(INITIALIZED)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    system.say("The server is ready. Add the admin site with: harbor admin install")
