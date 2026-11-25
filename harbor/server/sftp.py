"""SFTP for the users of the admin (phase 8d of dev/HARBOR.md).

A Harbor user only ever gets SFTP over SSH, never a shell, locked in /srv/harbor-sftp/<user>: root's folder, with a
folder per site (or environment of a site here) they have Files on, a bind mount of its folder. Its group and its ACLs
(8b) decide what they may do in it. Only users with SFTP turned on (the group harbor-sftp) may log in at all, with the password or the
public keys set in the admin. Harbor stores neither: the password goes to chpasswd, the keys to ~/.ssh/authorized_keys.

The SSH server's config is a file of Harbor's own in sshd_config.d, there only while a user has SFTP. It is checked
with sshd -t before ssh is reloaded; when it does not pass, the one before is put back.
"""
import re

from harbor.core.paths import BIN
from harbor.core.system import Stop
from harbor.state.store import read_json, write_json

GROUP = "harbor-sftp"
USERS_GROUP = "harbor-users"
ROOT = "/srv/harbor-sftp"
CONFIG = "/etc/ssh/sshd_config.d/harbor-sftp.conf"
MOUNTS = "/var/lib/harbor/sftp.json"
SERVICE = "/etc/systemd/system/harbor-sftp-mounts.service"
KEY = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
                 r" [A-Za-z0-9+/]+={0,3}(?: [^\n\r]{0,200})?")
# The first block that fits decides: a Harbor user without SFTP cannot log in at all, one with it may, and every
# Harbor user gets SFTP in their own folder only. "Match all" ends Harbor's blocks, so nothing after them is caught.
SSHD = f"""# Made by Harbor (harbor users sync). The users of the admin get SFTP only, each in their own folder.
Match Group {USERS_GROUP},!{GROUP}
    PasswordAuthentication no
    PubkeyAuthentication no
    KbdInteractiveAuthentication no
Match Group {GROUP}
    PasswordAuthentication yes
    PubkeyAuthentication yes
    ChrootDirectory {ROOT}/%u
    ForceCommand internal-sftp -d /
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    PermitTunnel no
    PermitTTY no
    X11Forwarding no
Match all
"""
UNIT = """[Unit]
Description=Harbor: the sites in the SFTP folders of the admin's users
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {bin} users mounts

[Install]
WantedBy=multi-user.target
"""


def mounted(system):
    """The folders under ROOT that are mount points now"""
    path = system.path("/proc/self/mounts")
    found = set()
    for line in path.read_text().splitlines() if path.is_file() else []:
        parts = line.split()
        if len(parts) > 1 and parts[1].startswith(ROOT + "/"):
            found.add(parts[1].replace("\\040", " "))
    return found


def configure(system, wanted):
    """Harbor's sshd config there while a user has SFTP, gone when none has; ssh reloaded only when it changed and passes"""
    path = system.path(CONFIG)
    before = path.read_text() if path.is_file() else None
    after = SSHD if wanted else None
    if before == after:
        return
    system.plan(["Let the admin's users use SFTP" if wanted else "Take SFTP for the admin's users away"])
    system.step("Let the admin's users use SFTP" if wanted else "Take SFTP for the admin's users away")
    if after is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after)
        path.chmod(0o644)
    if not system.run("sshd", "-t", check=False, capture=True).ok:
        # Never leave a config the SSH server will not start with
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before)
        raise Stop("The SSH server did not accept the SFTP settings; they were taken back.")
    system.run("systemctl", "reload", "ssh", check=False, capture=True)


def mount_service(system, wanted):
    path = system.path(SERVICE)
    if wanted and not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UNIT.format(bin=BIN))
        system.run("systemctl", "daemon-reload", check=False, capture=True)
        system.run("systemctl", "enable", "harbor-sftp-mounts.service", check=False, capture=True)
    elif not wanted and path.is_file():
        system.run("systemctl", "disable", "harbor-sftp-mounts.service", check=False, capture=True)
        path.unlink()
        system.run("systemctl", "daemon-reload", check=False, capture=True)


def make_mounts(system, plan):
    """{user: {name: folder}}: each folder a bind mount under its name in the user's folder, and nothing else mounted
    there. A plan saved before environments had folders of their own is {user: [sites]}."""
    now = mounted(system)
    plan = {user: mounts if isinstance(mounts, dict) else {site: f"/var/www/{site}" for site in mounts} for user, mounts in plan.items()}
    want = {f"{ROOT}/{user}/{name}": folder for user, mounts in plan.items() for name, folder in mounts.items()}
    for folder in sorted(now - set(want), reverse=True):
        system.run("umount", folder, check=False, capture=True)
        try:
            system.path(folder).rmdir()
        except OSError:
            pass
    if plan:
        system.path(ROOT).mkdir(parents=True, exist_ok=True)
        system.path(ROOT).chmod(0o755)
    for user in plan:
        home = system.path(f"{ROOT}/{user}")
        home.mkdir(exist_ok=True)
        # The chroot is root's and not writable by others, or sshd refuses it; the user writes only in their sites
        system.run("chown", "root:root", f"{ROOT}/{user}", capture=True)
        home.chmod(0o755)
    for folder, source in sorted(want.items()):
        if folder not in now:
            system.path(folder).mkdir(exist_ok=True)
            system.run("mount", "--bind", source, folder)
    # A user who no longer has SFTP: their folder goes (empty once its mounts are gone)
    root = system.path(ROOT)
    for home in root.iterdir() if root.is_dir() else []:
        if home.name not in plan and home.is_dir() and not any(str(item).startswith(f"{ROOT}/{home.name}/") for item in mounted(system)):
            try:
                for child in home.iterdir():
                    if child.is_dir():
                        child.rmdir()
                home.rmdir()
            except OSError as error:
                system.warn(f"{home} was left: {error}")


def remount(system):
    """After a restart: the mounts as they were (harbor-sftp-mounts.service)"""
    plan = read_json(system.path(MOUNTS))
    make_mounts(system, plan if isinstance(plan, dict) else {})


def sync(system, users, names, sites):
    """users: the admin's users; names: {user id: Linux user}; sites: {mount name: (folder, Linux users with Files)}"""
    from harbor.server.users import group_exists, members
    on = {names[user["id"]] for user in users if user.get("sftp") and not user.get("disabled") and user.get("role") != "admin"
          and names.get(user["id"])}
    if on and not group_exists(system, GROUP):
        system.run("groupadd", "--system", GROUP)
    current = members(system, GROUP)
    for name in sorted(current - on):
        system.run("gpasswd", "-d", name, GROUP, check=False, capture=True)
        # Off: no password and no keys any more
        system.run("usermod", "--lock", name, check=False, capture=True)
        system.path(f"/home/{name}/.ssh/authorized_keys").unlink(missing_ok=True)
    for name in sorted(on - current):
        system.run("gpasswd", "-a", name, GROUP, capture=True)
        system.run("usermod", "--unlock", name, check=False, capture=True)
    plan = {name: {mount: folder for mount, (folder, users_with) in sorted(sites.items()) if name in users_with} for name in sorted(on)}
    write_json(system.path(MOUNTS), plan)
    make_mounts(system, plan)
    mount_service(system, bool(on))
    configure(system, bool(on))


# ---- what the admin sets for a user
def harbor_user(system, name):
    from harbor.server.users import members
    if name not in members(system, USERS_GROUP):
        raise Stop(f"{name} is not a user Harbor made.")


def set_password(system, name, password):
    harbor_user(system, name)
    if len(password) < 12 or len(password) > 128 or any(char in password for char in ":\n\r"):
        raise Stop("The SFTP password needs 12 to 128 characters, and no colon or line break.")
    # On stdin, never on a command line
    system.run("chpasswd", input=f"{name}:{password}\n", capture=True)
    system.say(f"The SFTP password of {name} is set.")


def set_keys(system, name, text):
    harbor_user(system, name)
    keys = [line.strip() for line in text.splitlines() if line.strip()]
    bad = [key for key in keys if not KEY.fullmatch(key)]
    if bad or len(keys) > 20:
        raise Stop("Each line must be one public SSH key, such as ssh-ed25519 AAAA… name (at most 20).")
    folder = system.path(f"/home/{name}/.ssh")
    folder.mkdir(exist_ok=True)
    path = folder / "authorized_keys"
    path.write_text("".join(key + "\n" for key in keys))
    folder.chmod(0o700)
    path.chmod(0o600)
    system.run("chown", "-R", f"{name}:{name}", f"/home/{name}/.ssh", capture=True)
    system.say(f"{len(keys)} public key(s) for {name}.")


def keys_of(system, name):
    path = system.path(f"/home/{name}/.ssh/authorized_keys")
    return path.read_text() if path.is_file() else ""
