"""acme.sh's state where its cron job looks (1.0.67).

harbor server init installs acme.sh with --config-home /etc/ssl/data and --cert-home /etc/ssl/certs, and its cron job
runs with those folders. Harbor ran acme.sh without them until 1.0.67, so acme.sh kept each certificate it issued in
/root/.acme.sh, and only the installed key and full chain landed in /etc/ssl/certs/<domain>_ecc: the cron job skipped
them ("is not an issued domain"), and they were never renewed.

adopt moves each certificate in /root/.acme.sh that is installed somewhere into /etc/ssl/certs/<folder>, where the cron
job renews it. The installed files there are the same files as acme.sh's own (it does not copy a file onto itself),
so nothing Apache reads changes. The old folder is kept in /root/.acme.sh/moved. Harmless to repeat.
"""
import re
import shutil

from harbor.core.site_config import ACME, ACME_FOLDERS

LEGACY = "/root/.acme.sh"
MOVED = f"{LEGACY}/moved"
CERT_HOME = "/etc/ssl/certs"
CONFIG_HOME = "/etc/ssl/data"
VALUE = re.compile(r"^(Le_\w+)='(.*)'$", re.M)


def domain_of(folder):
    return folder.name[:-len("_ecc")] if folder.name.endswith("_ecc") else folder.name


def values(conf):
    return dict(VALUE.findall(conf.read_text(errors="replace")))


def ascii_name(name):
    """A domain as acme.sh names it: lower case, an international one in punycode"""
    name = name.strip().lower().rstrip(".")
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


def served(system):
    """The names the enabled Apache sites answer to (ServerName and ServerAlias)"""
    names = set()
    for path in sorted(system.path("/etc/apache2/sites-enabled").glob("*.conf")):
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            fields = line.split("#", 1)[0].split()
            if fields[:1] in (["ServerName"], ["ServerAlias"]):
                names.update(ascii_name(name) for name in fields[1:])
    return names


def certificate_names(domain, settings):
    """The certificate's domain and its other names (Le_Alt, "no" when it has none)"""
    others = settings.get("Le_Alt", "")
    return [domain, *(name for name in others.split(",") if name and others != "no")]


def found(system):
    """(folder, domain, what to do, why) for each certificate folder of acme.sh in /root/.acme.sh"""
    legacy = system.path(LEGACY)
    names = served(system)
    rows = []
    for folder in sorted(legacy.iterdir()) if legacy.is_dir() else []:
        # A certificate's folder is named after its domain (ca, moved and the like have no dot)
        if not folder.is_dir() or folder.is_symlink() or "." not in folder.name:
            continue
        domain = domain_of(folder)
        conf = folder / f"{domain}.conf"
        if not conf.is_file():
            rows.append((folder, domain, "keep", "no certificate issued (removed or failed)"))
            continue
        settings = values(conf)
        installed = settings.get("Le_RealFullChainPath", "")
        foreign = [name for name in certificate_names(domain, settings) if ascii_name(name) not in names]
        if not installed or not system.path(installed).is_file():
            rows.append((folder, domain, "keep", "not installed for a site"))
        elif foreign:
            # Such as another server's admin site, in a server made from a copy of it: renewing it here would fail,
            # or put another server's certificate in place of this one's
            rows.append((folder, domain, "keep", f"no site here answers to {' '.join(foreign)}"))
        elif (system.path(CERT_HOME) / folder.name / f"{domain}.conf").is_file():
            rows.append((folder, domain, "keep", f"{CERT_HOME}/{folder.name} has its own already"))
        else:
            rows.append((folder, domain, "move", f"installed in {installed}"))
    return rows


def unrenewed(system, moving=()):
    """The folders in /etc/ssl/certs with an installed certificate that none of acme.sh's certificates there (or of
    those moving there) is installed to: nothing renews it. Told by where acme.sh installs, as a folder may be named
    otherwise (an international name in its own letters, its certificate in punycode)."""
    home = system.path(CERT_HOME)
    confs = [*(home.glob("*/*.conf") if home.is_dir() else []), *(folder / f"{domain_of(folder)}.conf" for folder in moving)]
    installed = set()
    for conf in confs:
        if conf.name == f"{domain_of(conf.parent)}.conf" and conf.is_file():
            path = values(conf).get("Le_RealFullChainPath", "")
            installed.add(system.path(path) if path else conf.parent / "fullchain.cer")
    names = []
    for folder in sorted(home.iterdir()) if home.is_dir() else []:
        chain = folder / "fullchain.cer"
        # A folder of acme.sh's own is renewed, wherever it installs
        if folder.is_dir() and not folder.is_symlink() and chain.is_file() and chain not in installed \
                and not (folder / f"{domain_of(folder)}.conf").is_file():
            names.append(domain_of(folder))
    return names


def move(system, folder):
    target = system.path(CERT_HOME) / folder.name
    target.mkdir(parents=True, exist_ok=True)
    for item in folder.iterdir():
        if item.is_dir():
            shutil.copytree(item, target / item.name, dirs_exist_ok=True)
        else:
            # The installed key and full chain are copies of these: the same certificate
            shutil.copy2(item, target / item.name)
    kept = system.path(MOVED) / folder.name
    kept.parent.mkdir(parents=True, exist_ok=True)
    if kept.exists():
        shutil.rmtree(kept)
    folder.rename(kept)


def has_cron_job(system):
    crontab = system.run("crontab", "-l", check=False, capture=True)
    return any("--cron" in line and CONFIG_HOME in line for line in crontab.stdout.splitlines() if not line.lstrip().startswith("#"))


def adopt(system, dry_run=False):
    if not system.path(ACME).is_file():
        system.say("acme.sh is not installed here: nothing to do.")
        return
    rows = found(system)
    moving = [row for row in rows if row[2] == "move"]
    cron = has_cron_job(system)
    if dry_run:
        for folder, domain, action, why in rows:
            system.say(f"{'Move' if action == 'move' else 'Keep'} {domain}: {why}")
        if not cron:
            system.say("acme.sh's cron job would be installed.")
        for domain in unrenewed(system, [folder for folder, _, _, _ in moving]):
            system.say(f"Not renewed, to be issued again: {domain}")
        if not moving and cron:
            system.say("Every certificate is where acme.sh's cron job renews it.")
        return
    if moving:
        system.plan([f"Move the certificate of {domain} to {CERT_HOME}" for _, domain, _, _ in moving])
        for folder, domain, _, _ in moving:
            system.step(f"Move the certificate of {domain} to {CERT_HOME}")
            move(system, folder)
    if not cron:
        system.run(ACME, *ACME_FOLDERS, "--install-cronjob")
    for domain in unrenewed(system):
        system.warn(f"The certificate of {domain} is not known to acme.sh and is not renewed: issue it again.")
