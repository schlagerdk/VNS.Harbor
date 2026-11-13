"""The local port a site's service listens on. From lib/common.sh: allocate_ports and release_port_reservation.

Uses the same lock and reservation files as the script, so a site made by a script and one made by harbor at the
same time never get the same port.
"""
import fcntl
import os
import re

RESERVATIONS = "/run/lock/vns-apache-port-reservations"
LOCK = "/run/lock/vns-apache-port-allocation.lock"
FIRST, LAST = 4000, 65535
CONFIGS = re.compile(r"(?:127\.0\.0\.1|localhost|\*):([0-9]{1,5})")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def used_ports(system, reservations):
    """Ports that are taken: reserved by a running creation, listened on, or in an Apache config or a service"""
    used = set()
    for path in reservations.glob("*.reserve"):
        port = path.name[:-len(".reserve")]
        try:
            pid = int(path.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
        if pid and _alive(pid):
            if port.isdigit():
                used.add(int(port))
        else:
            path.unlink(missing_ok=True)
    listening = system.run("ss", "-H", "-ltn", capture=True, check=False)
    if listening.returncode == 127:
        raise RuntimeError("ss is required to detect listening ports.")
    for line in listening.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            port = fields[3].rsplit(":", 1)[-1]
            if port.isdigit():
                used.add(int(port))
    for folder, pattern in (("/etc/apache2/sites-available", "*.conf"), ("/etc/systemd/system", "*.service")):
        for path in system.path(folder).glob(pattern):
            if path.is_file():
                used.update(int(port) for port in CONFIGS.findall(path.read_text(errors="replace")))
    return used


class Reservation:
    """A free port, held for this process until released, so no other creation takes it meanwhile"""

    def __init__(self, port, path):
        self.port = port
        self.path = path

    def release(self):
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def allocate_port(system, site="unknown"):
    """The first port from 4000 that nothing uses, reserved for this process"""
    reservations = system.path(RESERVATIONS)
    reservations.mkdir(parents=True, exist_ok=True)
    lock = system.path(LOCK)
    with open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            used = used_ports(system, reservations)
            for port in range(FIRST, LAST + 1):
                path = reservations / f"{port}.reserve"
                if port not in used and not path.exists():
                    path.write_text(f"{os.getpid()} {site}\n")
                    system.say(f"Port: {port}")
                    return Reservation(port, path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    raise RuntimeError("No available site ports in the range 4000-65535.")
