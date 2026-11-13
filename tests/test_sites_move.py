"""harbor site move / site receive against move_site.sh (frozen in tests/legacy).

Each side has two fake servers, here and new. ssh and rsync to the new server run on its folder: the paths a command
names on the server are put under it. The script receives the sites with its own --receive, harbor with harbor site
receive, so both halves are compared. What both servers hold afterwards is compared, with the programs that ran.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.cli import main  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake  # noqa: E402

LEGACY = ROOT / "tests/legacy"
APACHE = ROOT / "apache"
HOST = "new.example.test"
SERVER_PATHS = ("/etc/", "/var/www", "/var/lib/vns", "/usr/lib/dotnet", "/vns/", "/run/lock")
SERVICE = ("[Unit]\nDescription=Running .NET Core [{name}]\n[Service]\nWorkingDirectory={root}\n"
           "ExecStart=/usr/lib/dotnet/dotnet {root}/Shop.dll --urls \"http://localhost:{port}\"\nSyslogIdentifier={name}\n")
CONF = ("<VirtualHost *:80>\n  ServerName {binding}\n  Redirect permanent / https://{binding}/\n</VirtualHost>\n"
        "<VirtualHost *:443>\n  ServerName {binding}\n  DocumentRoot {root}/wwwroot\n  ProxyPass / http://localhost:{port}/\n"
        "  SSLEngine On\n  SSLCertificateFile /etc/ssl/certs/{name}_ecc/fullchain.cer\n  SSLCertificateKeyFile /etc/ssl/certs/{name}_ecc/{name}.key\n</VirtualHost>\n")
APPSETTINGS = '{\n  "Umbraco": {\n    "CMS": {\n      "Global": {"UseHttps": true},\n      "WebRouting": {"UmbracoApplicationUrl": "https://shop.dk"}\n    }\n  }\n}\n'

REWRITE = r'''import re, sys
here, new, text = sys.argv[1:4]
text = text.replace(here, new)
print(re.sub(r"(?<![\w.])(/etc/|/var/www|/var/lib/vns|/usr/lib/dotnet|/vns/|/run/lock|/usr/local/bin/)", lambda m: new + m[1], text), end="")
'''


def redirect(text, root):
    """A script or helper of the old VNS, with every server path under root"""
    for path in SERVER_PATHS:
        text = re.sub(rf"(?<![\w.]){re.escape(path)}", lambda match: f"{root}{match[0]}", text)
    return text


@NEEDS_BASH_4
class MoveSiteTests(unittest.TestCase):
    maxDiff = 6000

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    # ---- the fake servers
    def make(self, side):
        folder = self.base / side
        shutil.rmtree(folder, ignore_errors=True)
        here, new = folder / "here", folder / "new"
        for server in (here, new):
            for path in ("etc/apache2/sites-available", "etc/apache2/sites-enabled", "etc/systemd/system", "var/www", "var/lib/vns-roots",
                         "var/lib/vns-managed", "run/lock", "vns/apache", "usr/lib/dotnet"):
                (server / path).mkdir(parents=True)
        (new / "usr/lib/dotnet/dotnet").write_text("#!/bin/bash\n")
        (new / "usr/lib/dotnet/dotnet").chmod(0o755)
        prefix = str(here) if side == "legacy" else ""
        web = here / "var/www"
        for name, root, binding, port in (("shop.dk", "shop.dk/_publish", "shop.dk", 4100), ("shop-staging.dk", "shop-staging.dk", "staging.shop.dk", 4101)):
            path = web / root
            path.mkdir(parents=True)
            (path / "Shop.dll").write_text("build")
            (path / "appsettings.json").write_text(APPSETTINGS.replace("shop.dk", binding))
            (path / "umbraco/Data").mkdir(parents=True)
            (path / "umbraco/Data/Umbraco.sqlite.db").write_text("data")
            (here / f"etc/systemd/system/{name}.service").write_text(SERVICE.format(name=name, root=f"{prefix}/var/www/{root}", port=port))
            (here / f"etc/apache2/sites-available/{name}.conf").write_text(CONF.format(name=name, root=f"{prefix}/var/www/{root}", binding=binding, port=port))
            (here / f"etc/apache2/sites-enabled/{name}.conf").symlink_to(f"../sites-available/{name}.conf")
            (here / f"var/lib/vns-roots/{name}").mkdir()
        (web / "shop.dk/Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n')
        targets = web / "shop.dk/scripts/targets"
        (targets / "staging").mkdir(parents=True)
        (targets / "staging/publish_prod.sh").write_text(f"PROD_LOCAL_PATH='{prefix}/var/www/shop-staging.dk'\nPROD_FOLDER='shop-staging.dk'\n"
                                                         "PROD_BINDING='staging.shop.dk'\nPROD_HTTPS=y\n")
        (targets / "production").mkdir()
        (targets / "production/publish_prod.sh").write_text("PROD_IP='10.0.0.9'\nPROD_USER='deploy'\nPROD_FOLDER='shop.dk'\nPROD_BINDING='shop.dk'\n")
        # The new server's VNS: the script's own --receive, or harbor's
        if side == "legacy":
            common = redirect((LEGACY / "lib/common.sh").read_text(), new).replace(
                '_apacheDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', f'_apacheDir="{APACHE}"')
            (new / "common.sh").write_text(common)
            receive = redirect((LEGACY / "move_site.sh").read_text(), new)
            receive = receive.replace('cd "$(dirname "$0")"', f"cd {APACHE}").replace("source lib/common.sh", f"source {new}/common.sh")
            receive = receive.replace("source lib/prod.sh", f"source {LEGACY}/lib/prod.sh").replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
            # The script's fixtures name this side's "here" folder: on the new server they name its own
            receive = receive.replace('[[ -d ${_incoming} ]] || { echo "${_incoming} was not found." >&2; exit 1; }',
                                      '[[ -d ${_incoming} ]] || { echo "${_incoming} was not found." >&2; exit 1; }\n'
                                      f'\tsed -i "s|{folder / "here"}|{new}|g" "${{_incoming}}"/*')
            (new / "vns/apache/move_site.sh").write_text(receive)
        else:
            # The new server's harbor: harbor site receive <folder> <SQL server>
            harbor = new / "usr/local/bin/harbor"
            harbor.parent.mkdir(parents=True)
            harbor.write_text(
                f'#!/bin/bash\nshift 2; incoming=${{1#{new}}}\nexec python3 -c "import sys; sys.path.insert(0, \'{ROOT}\'); from harbor.core.system import System; '
                f'from harbor.cli import main; sys.exit(main([\'site\', \'receive\', sys.argv[1], sys.argv[2]], System(root=\'{new}\')))" "$incoming" "$2"\n')
            harbor.chmod(0o755)
        return folder, here, new

    def programs(self, folder, here, new):
        fake = Fake(folder)
        (folder / "rewrite.py").write_text(REWRITE)
        for name in ("systemctl", "a2ensite", "a2dissite", "apache2ctl", "chown"):
            fake.program(name)
        fake.program("ss")
        fake.program("uname", "echo x86_64\n")
        fake.program("hostname", 'case "$1" in -I) echo "10.0.0.1 ::1" ;; *) echo here.example.test ;; esac\n')
        fake.program("sudo", 'exec "$@"\n')
        fake.program("sshpass", 'shift; exec "$@"\n')
        # On the new server, in its folder
        fake.program("ssh", f'command=$(python3 "{folder}/rewrite.py" "{here}" "{new}" "${{@: -1}}")\nexec /bin/bash -c "$command"\n')
        fake.program("rsync", f'args=(); for a in "$@"; do\n\tcase "$a" in root@{HOST}:*) a=$(python3 "{folder}/rewrite.py" "{here}" "{new}" "${{a#root@{HOST}:}}") ;; esac\n'
                              f'\targs+=("$a")\ndone\nexec /usr/bin/rsync "${{args[@]}}"\n')
        return fake

    @staticmethod
    def files(server, *strip):
        found = {}
        for path in sorted(server.rglob("*")):
            name = str(path.relative_to(server))
            if name.startswith(("vns/", "run/", "usr/local/")) or name in ("common.sh",):
                continue
            if path.is_symlink():
                found[name] = "-> " + os.readlink(path)
            elif path.is_file():
                text = path.read_text()
                for prefix in strip:
                    text = text.replace(prefix, "")
                found[name] = text
            elif not any(path.iterdir()):
                found[name] = "(empty)"
        return found

    @staticmethod
    def calls(fake, *strip):
        lines = []
        for line in fake.calls():
            line = line.rsplit(" |", 1)[0]
            if line.startswith(("sudo ", "sshpass ", "hostname ", "uname ", "rsync ", "ssh ", "ss ")):
                continue
            for prefix in strip:
                line = line.replace(prefix, "")
            lines.append(re.sub(r"vns-incoming/shop\.dk-\d+", "vns-incoming/shop.dk-PID", line))
        return lines

    def compare(self, env=None, password=""):
        request = {"VNS_MOVE_IP": HOST, "VNS_MOVE_USER": "root", "VNS_MOVE_PASSWORD": password, **(env or {})}
        # ---- the script
        folder, here, new = self.make("legacy")
        fake = self.programs(folder, here, new)
        common = redirect((LEGACY / "lib/common.sh").read_text(), here).replace(
            '_apacheDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', f'_apacheDir="{APACHE}"')
        (here / "common.sh").write_text(common)
        text = (LEGACY / "move_site.sh").read_text().replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
        text = text.replace('cd "$(dirname "$0")"', f"cd {APACHE}").replace("source lib/common.sh", f"source {here}/common.sh")
        text = text.replace("source lib/prod.sh", f"source {LEGACY}/lib/prod.sh")
        for line in ("_rootsFolder=/var/lib/vns-roots", "_systemdFolder=/etc/systemd/system", "_apacheSites=/etc/apache2/sites-available",
                     "_incomingFolder=/var/lib/vns-incoming", "/var/lib/vns-managed/"):
            text = text.replace(line, line.replace("/", f"{here}/", 1) if line.startswith("/") else line.replace("=/", f"={here}/", 1))
        result = subprocess.run(["/bin/bash", "-c", text, "move_site.sh", "shop.dk"], capture_output=True, text=True, input="",
                                env=fake.env() | request)
        legacy = (self.files(here, str(here)), self.files(new, str(new), str(here)), self.calls(fake, str(here), str(new)))
        result.stderr = result.stderr.replace(str(here), "").replace(str(new), "")

        # ---- harbor
        folder, here, new = self.make("harbor")
        fake = self.programs(folder, here, new)

        told = []

        def real(argv, stdin=None, variables=None):
            done = subprocess.run(argv, capture_output=True, text=True, input=stdin, env=fake.env() | (variables or {}))
            # What the new server says goes to the terminal as it is
            told.extend(line.replace(str(new), "").replace(str(here), "") for line in done.stderr.splitlines())
            return Result(done.returncode, done.stdout, done.stderr)
        answers = {name: real for name in ("rsync", "ssh", "sshpass", "uname", "hostname", "systemctl", "a2ensite", "a2dissite", "apache2ctl")}
        saved = dict(os.environ)
        os.environ.update(request)
        try:
            system = RecordingSystem(root=here, answers=answers, programs=["sshpass"])
            code = main(["site", "move", "shop.dk"], system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(self.files(here, str(here)), legacy[0], "here")
        self.assertEqual(self.files(new, str(new), str(here)), legacy[1], "the new server")
        self.assertEqual(self.calls(fake, str(here), str(new)), legacy[2])
        if result.returncode:
            self.assertIn(result.stderr.strip().splitlines()[-1], [*system.said, *told])
        return result, system, here, new

    # ---- the cases
    def test_a_migration_takes_the_site_and_its_environment_here(self):
        result, system, here, new = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((new / "var/www/shop-staging.dk/Shop.dll").is_file())
        self.assertFalse((here / "etc/systemd/system/shop.dk.service").exists())
        self.assertNotIn("SSLEngine", (new / "etc/apache2/sites-available/shop.dk.conf").read_text())

    def test_a_copy_with_a_new_folder_and_binding(self):
        result, system, here, new = self.compare({"VNS_MOVE_COPY": "1", "VNS_COPY_STOP": "y", "VNS_MOVE_FOLDER": "shop2.dk", "VNS_MOVE_BINDING": "shop2.dk"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((here / "etc/systemd/system/shop.dk.service").is_file())
        self.assertTrue((new / "var/www/shop2.dk/_publish/Shop.dll").is_file())

    def test_a_migration_that_deletes_the_files_here(self):
        result, system, here, new = self.compare({"VNS_MOVE_DELETE_LOCAL": "y"}, password="pw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((here / "var/www/shop.dk").exists())

    def test_a_site_that_is_on_the_new_server_already_is_refused(self):
        def taken(new):
            (new / "var/www/shop.dk").mkdir(parents=True)
        original = self.make

        def make(side):
            folder, here, new = original(side)
            taken(new)
            return folder, here, new
        self.make = make
        result, system, here, new = self.compare()
        self.assertEqual(result.returncode, 1)
        self.assertTrue((here / "etc/systemd/system/shop.dk.service").is_file())


if __name__ == "__main__":
    unittest.main()
