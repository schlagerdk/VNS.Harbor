"""harbor.environments and harbor.core.dotnet against lib/prod.sh, lib/windows_prod.sh and lib/dotnet.sh (frozen in tests/legacy/lib)"""
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core import dotnet  # noqa: E402
from harbor.core.system import RecordingSystem, Result  # noqa: E402
from harbor.core.templates import Site  # noqa: E402
from harbor.environments import settings  # noqa: E402
from harbor.environments.windows import Share  # noqa: E402
from tests.test_core_bindings import NEEDS_BASH_4  # noqa: E402

LEGACY = ROOT / "tests/legacy/lib"
PASSWORDS = ["plain", "with space", "it's", "a'b'c", 'dou"ble', "$HOME", "#hash", "back\\slash", "æøå", "x # y", ""]


def quote(value):
    """As create_prod.sh writes a value (its _q)"""
    return "'" + value.replace("'", "'\\''") + "'"


class Fake:
    """Programs that record how they were called, with the variables a test asks for, and what came on stdin"""

    def __init__(self, folder, variables=()):
        self.bin = Path(folder) / "bin"
        self.bin.mkdir()
        self.log = Path(folder) / "calls.log"
        self.log.write_text("")
        self.variables = variables

    def program(self, name, body=""):
        shown = "".join(f'; printf " %s=%q" {variable} "${{{variable}-unset}}"' for variable in self.variables)
        path = self.bin / name
        path.write_text(f'#!/bin/bash\n{{ printf "%s" "{name}"; printf " %q" "$@"; printf " |"{shown}; echo; }} >>"{self.log}"\n{body}')
        path.chmod(0o755)

    def calls(self):
        return self.log.read_text().splitlines()

    def env(self):
        return os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}"}


def split_call(line):
    """A call Fake recorded: the program, its arguments and the variables it had"""
    head, shown = line.rsplit(" |", 1)
    name, *args = shlex.split(head)
    variables = dict(item.split("=", 1) for item in shlex.split(shown))
    return name, args, {key: value for key, value in variables.items() if value != "unset"}


TEMPORARY = re.compile(r"^/(?:tmp|var/folders)/\S*$")


def same_temporary(args):
    """Temporary files have other names each time"""
    return ["TMP" if TEMPORARY.match(arg) else arg for arg in args]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.site = self.folder / "www/shop.dk"
        (self.site / "scripts/targets/production").mkdir(parents=True)
        self.system = RecordingSystem(root="/")

    def legacy_values(self, target):
        names = " ".join(f"PROD_{name}" for name in sorted(settings.SETTINGS)) + " _target _root _prodScript"
        script = (f"_siteRoot={shlex.quote(str(self.site))}; source {LEGACY}/prod.sh; VNS_TARGET={target} load_prod_settings || exit 1; "
                  f"for name in {names}; do printf '%s=%s\\0' \"$name\" \"${{!name}}\"; done")
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        if result.returncode:
            return None, result.stderr.strip()
        return dict(item.split("=", 1) for item in result.stdout.split("\0") if item), ""

    def ours(self, target):
        try:
            environment = settings.load(self.system, str(self.site), target)
        except settings.SettingsError as error:
            return None, str(error)
        values = {f"PROD_{name}": getattr(environment, name) for name in settings.SETTINGS}
        values.update(_target=environment.description, _root=environment.root, _prodScript=environment.script)
        return values, ""

    @NEEDS_BASH_4
    def test_values_are_read_as_bash_reads_them(self):
        for password in PASSWORDS:
            script = self.site / "scripts/targets/production/publish_prod.sh"
            script.write_text("#!/bin/bash\nset -euo pipefail\n"
                              f"PROD_LOCAL_PATH=''   # If set: publish here\nPROD_IP={quote('10.0.0.5')}   # IP\n"
                              f"PROD_USER=root\nPROD_PASSWORD={quote(password)}       # SSH password\nPROD_SSH_PORT='2222'\n"
                              f"PROD_FOLDER={quote('shop.dk')}\nPROD_BINDING={quote('Shop.DK')}\nPROD_HTTPS=n\n"
                              'PROD_ENVIRONMENT="Production"\nPROD_ADMIN_MAIL="it@shop.dk"\nPROD_OFFLINE_WAIT=5\n'
                              "  PROD_IP='indented is not read'\necho PROD_USER=not-an-assignment\n")
            self.assertEqual(self.ours("production"), self.legacy_values("production"), password)
            self.assertEqual(settings.load(self.system, str(self.site), "production").PASSWORD, password)

    @NEEDS_BASH_4
    def test_local_windows_and_old_environments(self):
        (self.site / "scripts/publish_prod.sh").write_text("PROD_LOCAL_PATH='/var/www/shop.dk/_production/'\nPROD_FOLDER='prod-shop'\nPROD_BINDING='prod.shop.dk'\n")
        (self.site / "scripts/targets/iis").mkdir()
        (self.site / "scripts/targets/iis/publish_prod.sh").write_text(
            "PROD_TARGET=windows\nPROD_WINDOWS_SHARE='//10.0.0.9/D$/WEB/shop'\nPROD_SMB_USER='Admin'\nPROD_SMB_PASSWORD='p a$$'\nPROD_FOLDER='shop'\nPROD_BINDING='shop.dk'\n")
        (self.site / "scripts/targets/production/publish_prod.sh").write_text("PROD_IP='x'\n")
        for target in ("prod", "iis", "production", "missing", "Bad_Id"):
            ours, legacy = self.ours(target), self.legacy_values(target)
            self.assertEqual(ours[0], legacy[0], target)
            if legacy[0] is None:
                self.assertEqual(ours[1], legacy[1].splitlines()[-1], target)

    def test_sizes_tell_whether_an_environment_is_in_the_sites_folder(self):
        """Usage: an environment in the site's folder is in the site's size (and limit); one in a folder of its own or
        on another server is not (1.0.95)"""
        root = str(self.site)
        (self.site / "scripts/targets/production/publish_prod.sh").write_text(f"PROD_LOCAL_PATH='{root}/_production'\nPROD_FOLDER='p'\nPROD_BINDING='p.dk'\n")
        for name, line in (("staging", f"PROD_LOCAL_PATH='{self.folder}/www/staging-shop.dk'"), ("test", "PROD_IP='10.0.0.5'")):
            (self.site / f"scripts/targets/{name}").mkdir()
            (self.site / f"scripts/targets/{name}/publish_prod.sh").write_text(f"{line}\nPROD_FOLDER='{name}'\nPROD_BINDING='{name}.dk'\n")
        with patch.object(settings, "environment_size", return_value=1.0):
            found = {item["target"]: item["in_site"] for item in settings.environment_sizes(self.system, str(self.folder / "www"))}
        self.assertEqual(found, {"production": True, "staging": False, "test": False})

    def test_environments_in_the_order_the_script_lists_them(self):
        (self.site / "scripts/publish_prod.sh").write_text("PROD_FOLDER=a\n")
        for name in ("staging", "production-2", "test"):
            (self.site / f"scripts/targets/{name}").mkdir()
            (self.site / f"scripts/targets/{name}/publish_prod.sh").write_text("")
        (self.site / "scripts/targets/empty").mkdir()
        result = subprocess.run(["bash", "-c", f"_siteRoot={shlex.quote(str(self.site))}; source {LEGACY}/prod.sh; prod_targets"],
                                capture_output=True, text=True, env=os.environ | {"LC_ALL": "C"})
        self.assertEqual(settings.target_ids(self.system, str(self.site)), result.stdout.split())

    @NEEDS_BASH_4
    def test_commands_reach_the_server_as_the_script_does(self):
        fake = Fake(self.folder, variables=("SSHPASS",))
        for program in ("ssh", "sshpass", "sudo"):
            fake.program(program)
        for user, password in (("root", ""), ("deploy", "it's secret"), ("root", "x")):
            environment = settings.Environment(id="production", IP="10.0.0.5", USER=user, PASSWORD=password, SSH_PORT="2222", FOLDER="shop", BINDING="shop.dk")
            for tty in ("", "tty"):
                fake.log.write_text("")
                script = (f"source {LEGACY}/prod.sh; PROD_TARGET=''; PROD_LOCAL_PATH=''; PROD_IP=10.0.0.5; PROD_USER={user}; "
                          f"PROD_PASSWORD={shlex.quote(password)}; PROD_SSH_PORT=2222; prod_exec 'systemctl status shop' {tty}")
                subprocess.run(["bash", "-c", script], env=fake.env(), check=True)
                system = RecordingSystem(root="/")
                settings.remote_exec(system, environment, "systemctl status shop", tty=bool(tty))
                name, args, variables = split_call(fake.calls()[0])
                self.assertEqual([name, *args], [*settings.ssh_command(environment, bool(tty))[0], f"{user}@10.0.0.5",
                                                 ("" if user == "root" else "sudo -n") + " systemctl status shop"], (user, tty))
                self.assertEqual(system.environments[0], variables)

    def test_main_site(self):
        (self.site / "Shop.csproj").write_text('<Project><ItemGroup><PackageReference Include="Umbraco.Cms" /></ItemGroup></Project>')
        (self.folder / "etc/apache2/sites-available").mkdir(parents=True)
        (self.folder / "etc/systemd/system").mkdir(parents=True)
        (self.folder / "etc/apache2/sites-available/shop.dk.conf").write_text("<VirtualHost *:80>\n  ServerAdmin it@shop.dk\n  ServerName www.shop.dk\n</VirtualHost>\n")
        service = self.folder / "etc/systemd/system/shop.dk.service"
        site = Site("shop.dk", root_path="/www")
        system = RecordingSystem(root=self.folder)
        for working, publish in ((f"{site.site_root}/_publish", "_publish"), (site.site_root, "."), ("", ".")):
            service.write_text(f"WorkingDirectory={working}\n" if working else "")
            main = settings.load_main_site(system, site)
            self.assertEqual((main.project_name, main.binding, main.admin_mail, main.publish_folder, main.csharp_kind),
                             ("Shop", "www.shop.dk", "it@shop.dk", publish, "umbraco"))


class WindowsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.fake = Fake(self.folder, variables=("RCLONE_CONFIG", "RCLONE_SMB_HOST", "RCLONE_SMB_USER", "RCLONE_SMB_DOMAIN", "RCLONE_SMB_PASS"))
        self.fake.program("rclone", 'if [[ $1 == obscure ]]; then cat | sed "s/^/obscured:/"; echo; fi\n')
        self.fake.program("curl", 'echo -n 503\n')
        self.fake.program("sleep")
        self.environment = settings.Environment(id="iis", TARGET="windows", WINDOWS_SHARE="//10.0.0.9/D$/WEB/shop", SMB_USER="Admin",
                                                SMB_PASSWORD="p 'a$", SMB_DOMAIN="VNS", FOLDER="shop", BINDING="shop.dk")

    def legacy(self, snippet):
        values = "".join(f"PROD_{name}={shlex.quote(getattr(self.environment, name))}; " for name in ("WINDOWS_SHARE", "SMB_USER", "SMB_PASSWORD", "SMB_DOMAIN"))
        result = subprocess.run(["bash", "-c", f"source {LEGACY}/windows_prod.sh; {values} windows_smb_connect && {snippet}"],
                                capture_output=True, text=True, env=self.fake.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def answers(self, argv):
        if argv[1] == "obscure":
            return "obscured:" + self.environment.SMB_PASSWORD
        return ""

    def test_login_and_offline_page_as_the_script(self):
        output = self.legacy("windows_take_offline https://shop.dk && windows_bring_online")
        system = RecordingSystem(root="/", answers={"rclone obscure": self.answers, "curl": "503"})
        share = Share(system, self.environment).connect()
        share.take_offline("https://shop.dk")
        share.bring_online()
        legacy = [split_call(line) for line in self.fake.calls() if not line.startswith("sleep")]
        ours = [(command.split(" ")[0], command.split(" ")[1:], env) for command, env in zip(system.commands, system.environments)]
        self.assertEqual(len(ours), len(legacy))
        for (name, args, env), (their_name, their_args, their_env) in zip(ours, legacy):
            self.assertEqual((name, same_temporary(args)), (their_name, same_temporary(their_args)))
            if name == "rclone":
                self.assertEqual(env, their_env, args)
        self.assertEqual(system.said, [line for line in output.splitlines()])
        self.assertEqual(share.env["RCLONE_SMB_PASS"], "obscured:p 'a$")
        self.assertEqual(share.target, ":smb:D$/WEB/shop")


class DotnetTests(unittest.TestCase):
    def test_runtime_identifier(self):
        for architecture in ("x86_64", "aarch64", "arm64", "riscv64"):
            result = subprocess.run(["bash", "-c", f"source {LEGACY}/dotnet.sh; linux_runtime_identifier {architecture}"], capture_output=True, text=True)
            try:
                ours = dotnet.runtime_identifier(architecture)
            except ValueError as error:
                self.assertEqual(result.stderr.strip(), str(error))
            else:
                self.assertEqual(ours, result.stdout.strip())

    @NEEDS_BASH_4
    def test_channels_are_installed_when_missing_and_never_replace_a_newer_host(self):
        listings = [("10.0.100 [/usr/lib/dotnet/sdk]\n", "Microsoft.AspNetCore.App 10.0.1 [/x]\n", "10.0"),
                    ("10.0.100 [/usr/lib/dotnet/sdk]\n", "Microsoft.AspNetCore.App 10.0.1 [/x]\n", "8.0"),
                    ("8.0.400 [/x]\n", "Microsoft.AspNetCore.App 8.0.1 [/x]\n", "10.0"),
                    ("", "", "8.0")]
        for sdks, runtimes, channel in listings:
            with tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                fake = Fake(folder)
                (folder / "usr/lib/dotnet").mkdir(parents=True)
                if sdks:
                    (folder / "sdks").write_text(sdks)
                    (folder / "runtimes").write_text(runtimes)
                    fake.program("dotnet", f'[[ $1 == --list-sdks ]] && cat {folder}/sdks || cat {folder}/runtimes\n')
                    (folder / "usr/lib/dotnet/dotnet").symlink_to(fake.bin / "dotnet")
                fake.program("curl")
                fake.program("sudo", 'shift 0; "$@"\n')
                fake.program("bash-installer")
                text = (LEGACY / "dotnet.sh").read_text().replace("/usr/lib/dotnet", f"{folder}/usr/lib/dotnet")
                text = text.replace('sudo bash "${_installer}"', 'bash-installer "${_installer}"')
                (folder / "dotnet.sh").write_text(text)
                subprocess.run(["bash", "-c", f"source {folder}/dotnet.sh; ensure_dotnet_channel {channel}"], env=fake.env(), check=True, capture_output=True)
                system = RecordingSystem(root=folder, answers={f"{dotnet.DOTNET_DIR}/dotnet --list-sdks": sdks.rstrip("\n"),
                                                               f"{dotnet.DOTNET_DIR}/dotnet --list-runtimes": runtimes.rstrip("\n")})
                dotnet.ensure_channel(system, channel)
                theirs = [line.split(" |")[0] for line in fake.calls() if line.startswith("bash-installer")]
                ours = [line for line in system.commands if line.startswith("bash ")]
                strip = lambda line: re.sub(r"\s\S*tmp\S*", " TMP", line.replace(f"{folder}", "")).replace("bash-installer", "bash")
                self.assertEqual([strip(line) for line in ours], [strip(line) for line in theirs], (sdks, channel))

    def test_project_framework_and_global_json(self):
        for framework, global_json in (("net8.0", True), ("net10.0", False), ("netstandard2.0", False)):
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder) / "srv/Shop.csproj"
                project.parent.mkdir()
                project.write_text(f'<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>{framework}</TargetFramework></PropertyGroup></Project>')
                system = RecordingSystem(root=folder, answers={"/usr/lib/dotnet/dotnet --list-sdks": "8.0.1 [x]\n10.0.1 [x]",
                                                               "/usr/lib/dotnet/dotnet --list-runtimes": "Microsoft.AspNetCore.App 8.0.1 [x]\nMicrosoft.AspNetCore.App 10.0.1 [x]"})
                (Path(folder) / "usr/lib/dotnet").mkdir(parents=True)
                (Path(folder) / "usr/lib/dotnet/dotnet").write_text("")
                dotnet.configure_project(system, "/srv/Shop.csproj")
                self.assertEqual((project.parent / "global.json").exists(), global_json, framework)
                if global_json:
                    self.assertEqual((project.parent / "global.json").read_text(), dotnet.GLOBAL_JSON_NET8)


if __name__ == "__main__":
    unittest.main()
