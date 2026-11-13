"""A fake server for parity tests of the bigger scripts: the frozen script and harbor run against servers made the
same, and what they leave (files) and run (programs) is compared.

The frozen script runs from a copy of the apache folder in which every server path points into the fake root, the
Python helpers too. The programs that change a server are recorded instead of run.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APACHE = ROOT / "apache"
LEGACY = ROOT / "tests/legacy"
sys.path.insert(0, str(ROOT))

from harbor.core.system import RecordingSystem, Result  # noqa: E402

FOLDERS = ("var/log/apache2", "root/.acme.sh", "etc/apache2/sites-available", "etc/apache2/sites-enabled", "etc/apache2/conf-available", "etc/systemd/system",
           "etc/ssl/certs", "etc/acmesh", "var/www", "var/lib/vns-roots", "var/lib/vns-managed", "var/lib/vns", "run/lock")
RECORDED = ("systemctl", "apache2ctl", "a2ensite", "a2dissite", "a2enmod", "a2enconf", "a2query", "dotnet", "curl", "ss", "clear",
            "hostname", "runuser", "useradd", "chown", "uname")
# Helpers the scripts run with python3 that change the server: recorded, as harbor's calls to them are
RECORDED_HELPERS = ("sql_startup.py", "publish_status.py", "build_as.py", "site_permissions.py")


class Server:
    """A fake server. name tells two apart: one for the script, one for harbor."""

    def __init__(self, folder, name, hostname="web.example.test", answers=None):
        answers = {"uname": "x86_64", **(answers or {})}
        self.folder = Path(folder)
        self.root = self.folder / name
        for path in FOLDERS:
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.log = self.folder / f"{name}.log"
        self.log.write_text("")
        self.hostname = hostname
        self.answers = answers

    # ---- the script's side
    def legacy_apache(self):
        """A copy of the apache folder with the frozen scripts, every server path inside the root"""
        copy = self.folder / f"{self.root.name}-apache"
        if copy.exists():
            return copy
        shutil.copytree(APACHE, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
        for frozen in LEGACY.glob("*.sh"):
            shutil.copy(frozen, copy / frozen.name)
        for frozen in (LEGACY / "lib").glob("*"):
            if frozen.is_file():
                shutil.copy(frozen, copy / "lib" / frozen.name)
        # domain_names.py runs harbor from beside the apache folder
        (self.folder / "harbor").exists() or (self.folder / "harbor").symlink_to(ROOT / "harbor")
        (self.folder / "bin").exists() or (self.folder / "bin").symlink_to(ROOT / "bin")
        for path in copy.rglob("*"):
            if not path.is_file() or path.suffix not in ("", ".sh", ".py", ".tpl", ".conf", ".targets"):
                continue
            text = path.read_text(errors="surrogateescape")
            new = self.redirect(text)
            if path.parent == copy and path.suffix == ".sh":
                new = self.without_sudo_restart(new)
            if new != text:
                path.write_text(new, errors="surrogateescape")
        return copy

    @staticmethod
    def without_sudo_restart(text):
        """A script that restarts itself with sudo as root would restart forever: the tests run as a user, sudo is fake.
        Only for the scripts the tests run: the templates a site gets keep theirs."""
        text = re.sub(r"(?m)^\[\[ \$EUID -eq 0 \]\] \|\| exec sudo .*$", "", text)
        return re.sub(r"(?ms)^if \[\[ \$EUID -ne 0 \]\]; then\n\s*exec sudo .*?\nfi$", "", text)

    def redirect(self, text):
        root = str(self.root)
        # Every server path a script writes or deletes: one missing here would reach the real server (the tests run
        # as a user who may not change it, which is what caught /var/log and /root)
        for path in ("/etc/", "/var/www", "/var/lib/vns", "/var/log/apache2", "/root/.acme.sh", "/run/lock", "/usr/lib/dotnet"):
            text = text.replace(path, root + path)
        return text

    def programs(self):
        bin_folder = self.folder / f"{self.root.name}-bin"
        if bin_folder.exists():
            return bin_folder
        bin_folder.mkdir()
        for program in RECORDED:
            answer = self.answers.get(program, "")
            (bin_folder / program).write_text(f'#!/bin/bash\necho "{program} $*" >>"{self.log}"\n'
                                              + (f"printf '%s\\n' {answer!r}\n" if answer else "")
                                              + f'exit ${{FAIL_{program.upper()}:-0}}\n')
        (bin_folder / "hostname").write_text(f'#!/bin/bash\necho "hostname $*" >>"{self.log}"\necho {self.hostname}\n')
        (bin_folder / "sudo").write_text('#!/bin/bash\nexec "$@"\n')
        helpers = "|".join(f"*/{name}" for name in RECORDED_HELPERS)
        # A virtual environment is recorded, with a pip that records what it would install
        venv = (f'if [[ $1 == -m && $2 == venv ]]; then echo "python3 $*" >>"{self.log}"; mkdir -p "$3/bin"; '
                f'printf \'#!/bin/bash\\necho "$0 $*" >>"{self.log}"\\n\' >"$3/bin/pip"; chmod +x "$3/bin/pip"; exit 0; fi\n')
        (bin_folder / "python3").write_text(f'#!/bin/bash\ncase "$1" in {helpers}) echo "python3 $*" >>"{self.log}"; exit 0 ;; esac\n'
                                            + venv + f'exec {sys.executable} "$@"\n')
        (self.root / "etc/acmesh/acme.sh").write_text(f'#!/bin/bash\necho "/etc/acmesh/acme.sh $*" >>"{self.log}"\n')
        for path in [*bin_folder.iterdir(), self.root / "etc/acmesh/acme.sh"]:
            path.chmod(0o755)
        return bin_folder

    def run_legacy(self, script, *args, env=None, stdin=""):
        apache = self.legacy_apache()
        bin_folder = self.programs()
        # A script that waits for something never holds up the tests
        return subprocess.run(["bash", str(apache / script), *map(str, args)], capture_output=True, text=True, input=stdin,
                              env=os.environ | {"PATH": f"{bin_folder}:{os.environ['PATH']}"} | (env or {}), timeout=120)

    # ---- harbor's side
    def system(self, **extra):
        answers = {"ss -H -ltn": "", "hostname -f": self.hostname, "hostname": self.hostname, "uname -m": "x86_64", **extra.pop("answers", {})}
        (self.root / "etc/acmesh/acme.sh").exists() or (self.root / "etc/acmesh/acme.sh").write_text("#!/bin/bash\n")
        os.chmod(self.root / "etc/acmesh/acme.sh", 0o755)
        return RecordingSystem(root=self.root, answers=answers, **extra)

    # ---- what they leave
    def files(self, skip=("etc/acmesh/acme.sh",)):
        """Every file under the root, with the root written as / again, and each link as where it points"""
        found = {}
        for path in sorted(self.root.rglob("*")):
            name = str(path.relative_to(self.root))
            if name in skip or name.startswith("run/lock/") or "__pycache__" in name:
                continue
            if path.is_symlink():
                found[name] = "-> " + os.readlink(path).replace(str(self.root), "")
            elif path.is_file():
                # The apache folder each side runs from is APACHE, as in the commands
                found[name] = (path.read_bytes().replace(str(self.folder / f"{self.root.name}-apache").encode(), b"APACHE")
                               .replace(str(APACHE).encode(), b"APACHE").replace(str(self.root).encode(), b""))
            elif path.is_dir() and not any(path.iterdir()):
                found[name] = "(empty folder)"
        return found

    def commands(self):
        apache = str(self.folder / f"{self.root.name}-apache")
        return [line.replace(apache, "APACHE").replace(str(self.root), "") for line in self.log.read_text().splitlines()]


TEMPORARY = re.compile(r"/tmp/tmp[.\w]+")


def harbor_commands(system):
    """harbor's commands as the script's are compared: the apache folder as APACHE"""
    return [line.replace(str(APACHE), "APACHE").replace(str(system.root), "") for line in system.commands]


def without_temporary(commands):
    """Commands with the temporary files they name (mktemp's or Python's) as TEMP"""
    return [TEMPORARY.sub("TEMP", line) for line in commands]
