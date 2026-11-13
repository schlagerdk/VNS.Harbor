"""harbor env move against move_env.sh (frozen in tests/legacy).

Each case builds the same fake server twice, runs the script on one and harbor on the other, and compares what is left
on them and what ran. The site's create_prod.sh and publish_prod.sh are played by a fake bash on both sides: create_prod
writes the new environment's settings, publish only records that it ran. rsync runs for real; the remote server's
folder is a folder here.
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
from harbor.core.paths import BIN  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from tests.test_actions import legacy_script  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402
from tests.test_environments import Fake  # noqa: E402

SERVICE = ("[Unit]\nDescription=Running .NET Core [{name}]\n[Service]\nWorkingDirectory={root}\nExecStart=/usr/lib/dotnet/dotnet {root}/Shop.dll\n"
           "SyslogIdentifier={name}\nBindPaths={root}\nRootDirectory=/var/lib/vns-roots/{name}\n")
CONF = ("<VirtualHost *:80>\n  ServerName {binding}\n  DocumentRoot {root}/wwwroot\n  ErrorLog /var/log/apache2/{name}-80-error.log\n"
        "  ProxyPass / http://127.0.0.1:4100/\n</VirtualHost>\n")
SETTINGS = ("PROD_TARGET={target}\nPROD_LOCAL_PATH='{local}'\nPROD_IP='{ip}'\nPROD_USER='{user}'\nPROD_PASSWORD='{password}'\nPROD_SSH_PORT='22'\n"
            "PROD_FOLDER='{folder}'\nPROD_BINDING='{binding}'\nPROD_HTTPS='{https}'\n")
# create_prod.sh: the new environment's settings, from what it was given
CREATE = r'''d="$WEB/shop.dk/scripts/targets/$VNS_PROD_ID"; mkdir -p "$d"
folder=${VNS_PROD_FOLDER:-$(basename "${VNS_PROD_LOCAL_PATH%/}")}
target=; [[ $VNS_PROD_TARGET == windows ]] && target=windows
printf "PROD_TARGET=%s\nPROD_LOCAL_PATH='%s'\nPROD_IP='%s'\nPROD_USER='%s'\nPROD_PASSWORD='%s'\nPROD_SSH_PORT='22'\nPROD_FOLDER='%s'\nPROD_BINDING='%s'\nPROD_HTTPS='%s'\n" \
	"$target" "${VNS_PROD_LOCAL_PATH%/}" "${VNS_PROD_IP-}" "${VNS_PROD_USER-}" "${VNS_PROD_PASSWORD-}" "$folder" "$VNS_PROD_BINDING" "${VNS_PROD_HTTPS:-n}" >"$d/publish_prod.sh"
exit "${FAIL_CREATE:-0}"
'''


@NEEDS_BASH_4
class MoveEnvironmentTests(unittest.TestCase):
    maxDiff = 4000

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)

    # ---- the fake server
    def make(self, side, source="local"):
        """A main site shop.dk with the environment staging: on this server in /var/www/shop-staging.dk, or on 10.0.0.5"""
        folder = self.base / side
        shutil.rmtree(folder, ignore_errors=True)
        web = folder / "var/www"
        prefix = str(folder) if side == "legacy" else ""
        for path in ("etc/apache2/sites-available", "etc/apache2/sites-enabled", "etc/systemd/system", "var/lib/vns-roots/shop-staging.dk",
                     "var/www/shop.dk/scripts/targets/staging", "remote/var/www/shop-staging.dk", "run/lock"):
            (folder / path).mkdir(parents=True)
        site = web / "shop.dk"
        (site / "Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n')
        (folder / "etc/apache2/sites-available/shop.dk.conf").write_text(CONF.format(binding="shop.dk", root=f"{prefix}/var/www/shop.dk/_publish", name="shop.dk"))
        (folder / "etc/systemd/system/shop.dk.service").write_text(SERVICE.format(name="shop.dk", root=f"{prefix}/var/www/shop.dk/_publish"))
        if source == "local":
            root = web / "shop-staging.dk"
            (folder / "etc/systemd/system/shop-staging.dk.service").write_text(SERVICE.format(name="shop-staging.dk", root=f"{prefix}/var/www/shop-staging.dk"))
            (folder / "etc/apache2/sites-available/shop-staging.dk.conf").write_text(
                CONF.format(binding="staging.shop.dk", root=f"{prefix}/var/www/shop-staging.dk", name="shop-staging.dk"))
            (folder / "etc/apache2/sites-enabled/shop-staging.dk.conf").symlink_to("../sites-available/shop-staging.dk.conf")
            settings = SETTINGS.format(target="", local=f"{prefix}/var/www/shop-staging.dk", ip="", user="", password="", folder="shop-staging.dk",
                                       binding="staging.shop.dk", https="n")
        else:
            root = folder / "remote/var/www/shop-staging.dk"
            settings = SETTINGS.format(target="", local="", ip="10.0.0.5", user="deploy", password="pw", folder="shop-staging.dk",
                                       binding="staging.shop.dk", https="n")
        root.mkdir(parents=True, exist_ok=True)
        for name, content in (("Shop.dll", "build"), ("appsettings.json", '{"ConnectionStrings": {"umbracoDbDSN": "Data Source=|DataDirectory|/Umbraco.sqlite.db"}}\n'),
                              ("umbraco/Data/Umbraco.sqlite.db", "data"), ("wwwroot/media/1/a.jpg", "jpg")):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(content)
        (site / "scripts/targets/staging/publish_prod.sh").write_text(settings)
        (site / "scripts/targets/staging/sync_prod.sh").write_text(f"VNS_TARGET=staging bash sync_env.sh shop.dk\n")
        return folder

    def programs(self, folder):
        fake = Fake(folder, variables=("VNS_MOVING", "VNS_MIGRATE_ONLY", "VNS_PUBLISH_TRACKED"))
        web = folder / "var/www"
        remote = folder / "remote"
        for name in ("systemctl", "a2ensite", "a2dissite", "apache2ctl", "chown"):
            fake.program(name)
        fake.program("uname", "echo x86_64\n")
        fake.program("hostname", 'case "$1" in -I) echo "10.0.0.1 ::1" ;; *) echo web.here.dk ;; esac\n')
        fake.program("sudo", 'exec "$@"\n')
        fake.program("sshpass", 'shift; exec "$@"\n')
        # The remote server: test -d/-e of its folders, uname, and a script on stdin
        fake.program("ssh", f'''command=${{@: -1}}
case "$command" in
	*"test -d "*|*"test -e "*) path=${{command##* }}; path=${{path//\\'/}}; [[ ${{command}} == *"test -d "* ]] && {{ [[ -d "{remote}$path" ]]; exit; }}; [[ -e "{remote}$path" ]]; exit ;;
	*"uname -m"*) echo x86_64 ;;
	*"bash -s"*) cat >>"{folder}/remote.sent" ;;
	*) echo "$command" >>"{folder}/remote.sent" ;;
esac
''')
        fake.program("rsync", f'args=(); for a in "$@"; do args+=("${{a/deploy@10.0.0.5:/{remote}}}"); done\nexec /usr/bin/rsync "${{args[@]}}"\n')
        fake.program("bash", f'''case "$1" in
	*/publish_prod.sh) exit "${{FAIL_PUBLISH:-0}}" ;;
	/tmp/*) WEB="{web}"
{CREATE} ;;
	*) exec /bin/bash "$@" ;;
esac
''')
        return fake

    def files(self, folder):
        """What is on the server, its own folder written as / again"""
        found = {}
        for path in sorted(folder.rglob("*")):
            name = str(path.relative_to(folder))
            if name.split("/")[0] in ("bin",) or name in ("calls.log", "remote.sent", "common.sh") or name.startswith("run/"):
                continue
            if path.is_symlink():
                found[name] = "-> " + os.readlink(path).replace(str(folder), "")
            elif path.is_file():
                found[name] = path.read_text().replace(str(folder), "")
            elif not any(path.iterdir()):
                found[name] = "(empty)"
        return found

    def calls(self, fake, folder):
        return [re.sub(r"/tmp/tmp[.\w]+", "TMP", line.rsplit(" |", 1)[0].replace(str(folder), ""))
                for line in fake.calls() if not line.startswith(("sudo ", "sshpass ", "hostname ", "uname ", "rsync ", "bash /bin", "ssh "))]

    def compare(self, request, source="local", env=None):
        env = env or {}
        # ---- the script
        folder = self.make("legacy", source)
        fake = self.programs(folder)
        text = legacy_script("move_env.sh", folder, folder / "var/www").replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"', "")
        for path in ("_rootsFolder=/var/lib/vns-roots", "_oldService=/etc/", "_oldConf=/etc/", "_newService=/etc/", "_newConf=/etc/"):
            text = text.replace(path, path.replace("=/", f"={folder}/", 1))
        legacy_request = {key: value.replace("/var/www", f"{folder}/var/www") if key == "VNS_PROD_LOCAL_PATH" else value for key, value in request.items()}
        result = subprocess.run(["/bin/bash", "-c", text, "move_env.sh", "shop.dk", "staging"], capture_output=True, text=True, input="",
                                env=fake.env() | legacy_request | env, cwd=ROOT)
        legacy_files, legacy_calls = self.files(folder), self.calls(fake, folder)
        legacy_sent = (folder / "remote.sent").read_text().replace(str(folder), "") if (folder / "remote.sent").exists() else ""
        result.stderr = result.stderr.replace(str(folder), "")

        # ---- harbor, on the same server made again
        folder = self.make("harbor", source)
        fake = self.programs(folder)
        sent = []

        def real(argv, stdin=None, variables=None):
            done = subprocess.run(argv, capture_output=True, text=True, input=stdin, env=fake.env() | (variables or {}) | env)
            return Result(done.returncode, done.stdout, done.stderr)

        def on_server(argv, stdin=None, variables=None):
            """A path harbor names on the server is under the fake server's folder"""
            argv = [re.sub(r"(?<![\w.-])(/var/www|/etc/|/var/lib/vns)", lambda match: f"{folder}{match[1]}", arg) for arg in argv]
            return real(argv, stdin, variables)
        def create(argv, stdin=None, variables=None):
            """harbor env create, played by the same fake as the create_prod.sh the script ran"""
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as script:
                pass
            try:
                return real(["bash", script.name], stdin, variables)
            finally:
                os.unlink(script.name)
        answers = {"rsync": real, "bash": real, "sshpass": real, "ssh": real, "uname": real, "hostname": real, "python3": on_server,
                   f"python3 {BIN} env create": create, "systemctl": real, "a2ensite": real, "a2dissite": real, "apache2ctl": real}
        saved = dict(os.environ)
        os.environ.update(request | env)
        try:
            system = RecordingSystem(root=folder, answers=answers)
            code = main(["env", "move", "shop.dk", "staging"], system)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        self.assertEqual(self.files(folder), legacy_files)
        self.assertEqual(self.calls(fake, folder), legacy_calls)
        harbor_sent = (folder / "remote.sent").read_text().replace(str(folder), "") if (folder / "remote.sent").exists() else ""
        self.assertEqual(harbor_sent, legacy_sent)
        if result.returncode:
            self.assertEqual(system.said[-1], result.stderr.strip().splitlines()[-1])
        return result, system, folder

    # ---- the cases
    def test_to_another_folder_on_this_server(self):
        result, system, folder = self.compare({"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/var/www/shop-test.dk"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((folder / "var/www/shop-test.dk/Shop.dll").is_file())
        self.assertIn("PROD_FOLDER='shop-test.dk'", (folder / "var/www/shop.dk/scripts/targets/staging/publish_prod.sh").read_text())

    def test_a_copy_on_this_server_becomes_a_new_environment(self):
        for stop in ("", "y"):
            with self.subTest(stop=stop):
                result, system, folder = self.compare({"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/var/www/shop.dk/_test",
                                                       "VNS_PROD_BINDING": "test.shop.dk", "VNS_PROD_ENVIRONMENT": "Test", "VNS_ENV_COPY": "1",
                                                       "VNS_COPY_STOP": stop})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((folder / "var/www/shop.dk/_test/umbraco/Data/Umbraco.sqlite.db").is_file())
                self.assertTrue((folder / "var/www/shop.dk/scripts/targets/test/publish_prod.sh").is_file())
                self.assertTrue((folder / "var/www/shop-staging.dk/Shop.dll").is_file(), "the source is kept")

    def test_from_a_remote_server_to_this_one(self):
        for delete in ("", "y"):
            with self.subTest(delete=delete):
                result, system, folder = self.compare({"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/var/www/shop-staging.dk",
                                                       "VNS_PROD_BINDING": "staging.shop.dk", "VNS_PROD_ENVIRONMENT": "Staging"},
                                                      source="remote", env={"VNS_MOVE_DELETE_LOCAL": delete})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((folder / "var/www/shop-staging.dk/Shop.dll").is_file())

    def test_a_failed_publish_leaves_the_source_running(self):
        result, system, folder = self.compare({"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/var/www/shop.dk/_test",
                                               "VNS_PROD_BINDING": "test.shop.dk", "VNS_PROD_ENVIRONMENT": "Test", "VNS_ENV_COPY": "1"},
                                              env={"FAIL_PUBLISH": "3"})
        self.assertEqual(result.returncode, 3)
        self.assertFalse((folder / "var/www/shop.dk/_test").exists())
        self.assertFalse((folder / "var/www/shop.dk/scripts/targets/test").exists())

    def test_what_is_refused(self):
        for request in ({"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/srv/elsewhere"},
                        {"VNS_PROD_TARGET": "local", "VNS_PROD_LOCAL_PATH": "/var/www/shop.dk"},
                        {"VNS_PROD_TARGET": "same", "VNS_PROD_FOLDER": "x.dk"},
                        {"VNS_PROD_TARGET": "ftp"}):
            with self.subTest(request=request):
                result, system, folder = self.compare(request)
                self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
