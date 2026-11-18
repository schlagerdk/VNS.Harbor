"""Whether root may log in over SSH, and how: with a password or a key (yes), with a key only (prohibit-password), or not
at all (no). Set from Server tools (harbor server ssh-root), in a file of Harbor's own in sshd_config.d, which sshd reads
before the rest of its config: the first value sshd finds is the one it uses."""
from harbor.core.system import Stop

DROPIN = "/etc/ssh/sshd_config.d/01-harbor-root-login.conf"
MODES = {"yes": "with a password or a key", "prohibit-password": "with a key only", "no": "not at all"}


def root_login(system):
    """How root may log in now, as sshd has it (sshd -T): yes, prohibit-password or no, or "" when it cannot be told"""
    found = system.run("sshd", "-T", check=False, capture=True)
    for line in (found.stdout if found.ok else "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "permitrootlogin":
            value = value.strip().lower()
            # Older sshd's word for the same
            return "prohibit-password" if value == "without-password" else value
    return ""


def status(system):
    return {"installed": system.path("/etc/ssh/sshd_config").is_file(), "root_login": root_login(system),
            "running": system.output("systemctl", "is-active", "ssh", check=False) == "active"
            or system.output("systemctl", "is-active", "ssh.socket", check=False) == "active"}


def set_root_login(system, mode):
    """PermitRootLogin set to mode, checked by sshd before it is used: a config sshd refuses is taken back, so SSH is
    never left unable to start. Connections open now stay."""
    if mode not in MODES:
        raise Stop(f"Not a way for root to log in: {mode} (yes, prohibit-password or no)")
    system.plan([f"Let root log in over SSH {MODES[mode]}"])
    system.step(f"Let root log in over SSH {MODES[mode]}")
    path = system.path(DROPIN)
    before = path.read_text() if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Set by Harbor (Server tools): how root may log in over SSH\nPermitRootLogin {mode}\n")
    checked = system.run("sshd", "-t", check=False, capture=True)
    if not checked.ok:
        if before is None:
            path.unlink()
        else:
            path.write_text(before)
        raise Stop(f"sshd refused the change, so it is taken back: {(checked.stderr or checked.stdout).strip()}")
    system.run("systemctl", "try-reload-or-restart", "ssh", check=False)
    now = root_login(system)
    if now and now != mode:
        system.warn(f"sshd still lets root log in {MODES.get(now, now)}: another file in /etc/ssh/sshd_config.d is read first.")
        return
    system.say(f"Root may log in over SSH {MODES[mode]}." + (" Servers that manage this one over SSH as root cannot any more." if mode == "no" else ""))
