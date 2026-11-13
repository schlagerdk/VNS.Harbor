"""harbor env publish against publish_prod.sh as it was (tests/legacy/solutionfiles), for an environment on this server
and one on another server. The scripts that run as root where the environment is are recorded with the variables they
get, the build is played by a fake that leaves a dll and an appsettings.json, and rsync runs for real."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from harbor.cli import main
from harbor.core.system import Result
from tests.harness import APACHE, LEGACY, Server, as_frozen
from tests.test_core_bindings import NEEDS_BASH_4

TEMPLATE = LEGACY / "solutionfiles/csharp/prod-scripts/publish_prod.sh"
APPSETTINGS = {"ConnectionStrings": {"umbracoDbDSN": "Server=db;Password=secret"}, "Umbraco": {"CMS": {"WebRouting": {"UmbracoApplicationUrl": "x"}}}}
LOCAL = {"PROD_LOCAL_PATH": "/var/www/shop.dk/_staging", "PROD_IP": "", "PROD_USER": "", "PROD_PASSWORD": "", "PROD_SSH_PORT": "22",
         "PROD_FOLDER": "staging-shop.dk", "PROD_BINDING": "staging.shop.dk", "PROD_HTTPS": "n", "PROD_ENVIRONMENT": "Staging",
         "ADMIN_MAIL": "it@shop.dk"}
REMOTE = {**LOCAL, "PROD_LOCAL_PATH": "", "PROD_IP": "10.0.0.5", "PROD_USER": "deploy", "PROD_PASSWORD": "pw", "PROD_FOLDER": "shop.dk",
          "PROD_BINDING": "shop.dk", "PROD_HTTPS": "y", "PROD_ENVIRONMENT": "Production"}


def build(publish_dir):
    folder = Path(publish_dir)
    folder.mkdir(parents=True, exist_ok=True)
    # As the script's fake build writes them
    (folder / "Shop.dll").write_text("build\n")
    (folder / "appsettings.json").write_text(json.dumps(APPSETTINGS))
    (folder / "appsettings-schema.json").write_text("{}\n")


@NEEDS_BASH_4
class PublishTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def make(self, side, values):
        server = Server(self.folder, side)
        site = server.root / "var/www/shop.dk"
        (site / "_publish").mkdir(parents=True)
        (site / "_publish/appsettings.json").write_text(json.dumps({**APPSETTINGS, "Local": True}))
        (site / "Shop.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup><PackageReference Include="Umbraco.Cms" /></ItemGroup></Project>\n')
        (site / "appsettings-schema.json").write_text("{}")
        (site / "bin").mkdir()
        (server.root / "remote/var/www").mkdir(parents=True)
        target = site / "scripts/targets/staging"
        target.mkdir(parents=True)
        text = TEMPLATE.read_text()
        # The script's paths go into the fake folder with the rest of it (Server.redirect)
        prefix = ""
        for key, value in {**values, "PROJECT_NAME": "Shop", "PUBLISH_FOLDER": "_publish"}.items():
            value = prefix + value if key == "PROD_LOCAL_PATH" and value else value
            text = text.replace(f"@{key}@", value if key in ("PROD_ENVIRONMENT", "ADMIN_MAIL") else f"'{value}'")
        return server, target, text

    def programs(self, server):
        """Fake programs: the recorded root scripts, the build, and the other server in a folder of its own"""
        bin_folder = server.programs()
        log, remote = server.folder / f"{server.root.name}.scripts", server.root / "remote"
        fakes = {
            "bash": f'if [[ $1 == -c ]]; then {{ echo "== $2"; cat; }} >>"{log}"; exit 0; fi\nexec /bin/bash "$@"\n',
            "python3": f'''case "$1" in
	*/build_as.py) for a in "$@"; do [[ $a == -p:PublishDir=* ]] && dir=${{a#-p:PublishDir=}}; done
		mkdir -p "$dir"; echo build >"$dir/Shop.dll"; printf '%s' '{json.dumps(APPSETTINGS)}' >"$dir/appsettings.json"; echo '{{}}' >"$dir/appsettings-schema.json"
		echo "python3 $*" >>"{server.log}"; exit 0 ;;
esac
exec /usr/bin/python3 "$@"
''',
            "ssh": f'''command=${{@: -1}}
case "$command" in
	*"bash -s") {{ echo "== $command"; cat; }} >>"{log}" ;;
	*"uname -m") echo x86_64 ;;
	*"test -f "*) path=${{command##*test -f }}; path=${{path//\\'/}}; [[ -f "{remote}$path" ]] ;;
	*) echo "$command" >>"{server.log}" ;;
esac
''',
            "sshpass": 'shift; exec "$@"\n',
            "rsync": f'args=(); for a in "$@"; do args+=("${{a/deploy@10.0.0.5:/{remote}}}"); done\nexec /usr/bin/rsync "${{args[@]}}"\n',
        }
        for name, body in fakes.items():
            (bin_folder / name).write_text("#!/bin/bash\n" + body)
            (bin_folder / name).chmod(0o755)
        return bin_folder, log

    def compare(self, values, first=True):
        # ---- the script
        server, target, text = self.make("old", values)
        bin_folder, log = self.programs(server)
        if not first:
            (server.root / "remote/var/www/shop.dk").mkdir(parents=True)
            (server.root / "remote/var/www/shop.dk/appsettings.json").write_text("{}")
        apache = server.legacy_apache()
        script = target / "publish_prod.sh"
        # The other server's folder stays its own path: only this server's paths go into the fake folder
        script.write_text(server.redirect(text.replace("@APACHE_DIR@", str(apache))).replace(f"_root={server.root}/var/www/", "_root=/var/www/"))
        result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=120,
                                env=os.environ | {"PATH": f"{bin_folder}:{os.environ['PATH']}", "VNS_PUBLISH_TRACKED": "target-staging"})
        legacy = self.state(server, log)

        # ---- harbor
        server, target, text = self.make("new", values)
        bin_folder, log = self.programs(server)
        if not first:
            (server.root / "remote/var/www/shop.dk").mkdir(parents=True)
            (server.root / "remote/var/www/shop.dk/appsettings.json").write_text("{}")
        (target / "publish_prod.sh").write_text(text)

        def real(argv, stdin=None, variables=None):
            done = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                                  env=os.environ | {"PATH": f"{bin_folder}:{os.environ['PATH']}"} | (variables or {}))
            return Result(done.returncode, done.stdout, done.stderr)

        def python3(argv, stdin=None, variables=None):
            if any(arg.endswith("build_as.py") for arg in argv):
                build(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("-p:PublishDir=")))
                return Result(0)
            return real(argv, stdin, variables)
        system = server.system(answers={"bash": real, "ssh": real, "sshpass": real, "rsync": real, "python3": python3}, programs=["sshpass"])
        os.environ.update(VNS_PUBLISH_TRACKED="target-staging", VNS_TARGET="staging")
        try:
            code = main(["env", "publish", "shop.dk", "--project", "Shop", "--local-publish", "_publish"], system)
        finally:
            del os.environ["VNS_PUBLISH_TRACKED"], os.environ["VNS_TARGET"]
        self.assertEqual(code, result.returncode, (result.stdout, result.stderr, system.said))
        mine = self.state(server, log)
        self.assertEqual(mine[0], legacy[0], "the files")
        self.assertEqual(as_frozen(mine[1]), legacy[1], "what ran as root where the environment is")
        return result, system, server

    def state(self, server, log):
        files = {name: content for name, content in server.files().items() if "/scripts/" not in name and not name.endswith(".log")}
        scripts = log.read_text().replace(str(server.root), "") if log.exists() else ""
        # The folder the build went to is temporary
        return files, re.sub(r"/tmp/tmp[.\w]+", "TMP", scripts)

    def test_an_environment_on_this_server(self):
        result, system, server = self.compare(LOCAL)
        self.assertEqual(result.returncode, 0, result.stderr)
        staging = server.root / "var/www/shop.dk/_staging"
        self.assertTrue((staging / "Shop.dll").is_file())
        settings = json.loads((staging / "appsettings.json").read_text())
        self.assertNotIn("ConnectionStrings", settings, "a first publish leaves the connections out")
        self.assertTrue(settings["Local"], "from the site's own publish folder")
        self.assertIn('Compile Remove="_staging/**"', (server.root / "var/www/shop.dk/Shop.csproj").read_text())
        self.assertFalse((server.root / "var/www/shop.dk/bin").exists())

    def test_an_environment_on_another_server(self):
        for first in (True, False):
            with self.subTest(first=first):
                self.setUp()
                result, system, server = self.compare(REMOTE, first)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((server.root / "remote/var/www/shop.dk/Shop.dll").is_file())
                self.assertEqual((server.root / "remote/var/www/shop.dk/appsettings.json").read_text() == "{}", not first)


class TemplateTests(unittest.TestCase):
    def test_the_settings_stay_and_the_publish_is_harbors(self):
        text = (APACHE / "solutionfiles/csharp/prod-scripts/publish_prod.sh").read_text()
        for line in ("PROD_LOCAL_PATH=@PROD_LOCAL_PATH@", "PROD_PASSWORD=@PROD_PASSWORD@", "_projectName=@PROJECT_NAME@", "_localPublish=@PUBLISH_FOLDER@"):
            self.assertIn(line, text)
        self.assertIn('/bin/harbor" env publish "${_siteFolder##*/}"', text)


if __name__ == "__main__":
    unittest.main()
