"""Repair application content permissions without following symbolic links."""
import json
import os
from pathlib import Path
import pwd
import grp
import re
import stat


def admin_data(folder):
    """The admin site's app folder: its login (config.json), with the admin's code beside it (VNS 1.x) or not (Harbor)"""
    if (Path(folder) / "manage.py").exists() and (Path(folder) / "config.json").exists():
        return True
    try:
        config = json.loads((Path(folder) / "config.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and bool(config.get("secret_key"))


def content_folder(root, service, web_root=Path("/var/www")):
    """The folder a site's content is in: its service's WorkingDirectory inside the site, else the site's folder"""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Site folder is missing or is a symbolic link")
    root = root.resolve()
    web_root = web_root.resolve()
    if root == web_root or not root.is_relative_to(web_root):
        raise ValueError("Expected a site folder inside /var/www")
    if admin_data(root / "app"):
        raise ValueError("Admin site permissions must remain root-owned")
    target = root
    if service.is_file():
        text = service.read_text()
        match = re.search(r"^WorkingDirectory=(.+)$", text, re.M)
        if not match:
            raise ValueError("Service WorkingDirectory is missing")
        target = Path(match.group(1).strip().strip('"'))
        if not target.is_absolute() or target.is_symlink():
            raise ValueError("Invalid service WorkingDirectory")
        target = target.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError("Service WorkingDirectory is outside the site")
        if admin_data(target):
            raise ValueError("Admin site permissions must remain root-owned")
    elif (root / "app").is_dir():
        raise ValueError("App folder found without a service; cannot safely select content")
    if not target.is_dir():
        raise ValueError("Content folder is not a directory")
    return target


def entries(target):
    """What fixing touches: the folder and everything under it, but scripts, .git, venv, .env, vns-harbor.json and
    symbolic links"""
    yield target

    def walk_error(error):
        raise error

    for directory, folders, files in os.walk(target, followlinks=False, onerror=walk_error):
        folders[:] = [name for name in folders if name not in ("scripts", ".git", "venv")
                      and not (Path(directory) / name).is_symlink()]
        for name in folders + files:
            # vns-harbor.json stays root:www-data, as Harbor writes it: the site reads its key, and cannot change it
            if name in ("scripts", ".git", ".env", "vns-harbor.json") or name.startswith(".env."):
                continue
            yield Path(directory) / name


def needs_fixing(root, service, uid, gid, web_root=Path("/var/www")):
    """The first entry fixing would change in a way that matters: not owned by www-data, or one www-data cannot read
    (a folder it cannot enter). None when there is none. A file's mode alone (644 where fixing sets 640) or its group is
    not counted: every publish leaves those, and they do the site no harm."""
    for path in entries(content_folder(root, service, web_root)):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            continue
        # Its group is left out: a site's folder may be its builds' (vns-build), which does www-data no harm
        if info.st_uid != uid:
            return str(path)
        if not info.st_mode & stat.S_IRUSR or (stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_IXUSR):
            return str(path)
    return None


def repair(root, service, uid, gid, web_root=Path("/var/www")):
    target = content_folder(root, service, web_root)
    changed = 0

    def update(path):
        nonlocal changed
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return
        mode = 0o750 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o640
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode)
        changed += 1

    for path in entries(target):
        update(path)
    print("Permissions fixed on %s (%d entries). No service restart required." % (target, changed))


if __name__ == "__main__":
    # Sent to another server (python3 -), the names come with it (harbor.core.names.standalone)
    if "normalize_site_name" not in globals():
        from domain_names import normalize_site_name, service_name

    site = os.environ["SITE"]
    normalize_site_name(site)
    arguments = (Path(os.environ["ROOT"]), Path("/etc/systemd/system") / (service_name(site) + ".service"),
                 pwd.getpwnam("www-data").pw_uid, grp.getgrnam("www-data").gr_gid)
    if os.environ.get("CHECK") == "1":
        # Only whether fixing is needed, as JSON: nothing is changed
        try:
            found = needs_fixing(*arguments)
            print(json.dumps({"needed": found is not None, "example": found or ""}))
        except (OSError, ValueError) as exc:
            print(json.dumps({"needed": None, "error": str(exc)}))
    else:
        repair(*arguments)
