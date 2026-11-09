import ast
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET

from flask import abort
from werkzeug.exceptions import BadRequest

from apache.lib.domain_names import normalize_dns_name, normalize_site_name, service_name
from apache.lib import windows_prod
from apache.tests.app_targets import add_target_helpers

APACHE = Path(__file__).resolve().parents[1]
WINDOWS_TEMPLATES = APACHE / "solutionfiles/csharp/windows-prod-scripts"
UMBRACO_TEMPLATES = APACHE / "solutionfiles/csharp/umbraco/windows-prod-scripts"
LINUX_TEMPLATE = APACHE / "solutionfiles/csharp/prod-scripts/publish_prod.sh"
BASH = shutil.which("bash")
BASH_MAJOR = int(subprocess.run([BASH, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True).stdout)
NEEDS_BASH_4 = unittest.skipIf(BASH_MAJOR < 4, "Needs bash 4 or newer, as on the servers")

# Logs every call. Files that exist in $FAKE_SERVER are what the server already holds.
FAKE_RCLONE = r"""#!/bin/bash
printf '%s\n' "$*" >>"$FAKE_LOG"
env | grep -E '^RCLONE_SMB_' >"$FAKE_LOG.env"
case "$1" in
	obscure) cat >/dev/null; echo obscured ;;
	lsf)
		[[ ${2%/} == ":smb:share\$/sites/prod.test" ]] || exit "${FAKE_LSF_STATUS:-3}"
		[[ -z ${FAKE_LOGIN_FAILS:-} ]] || exit 1
		ls -1 "$FAKE_SERVER" ;;
	copy) [[ ${FAKE_COPY_FAILS:-} != 1 ]] || exit 1 ;;
	copyto)
		[[ ${3##*/} != web.config ]] || cp "$2" "$FAKE_SERVER/web.config" ;;
esac
exit 0
"""

FAKE_DOTNET = r"""#!/bin/bash
printf 'dotnet %s\n' "$*" >>"$FAKE_LOG"
for arg in "$@"; do
	case "$arg" in
		-p:PublishDir=*)
			dir=${arg#-p:PublishDir=}
			mkdir -p "$dir"
			touch "$dir/App.dll"
			echo '{"ConnectionStrings":{"db":"build"},"From":"build"}' >"$dir/appsettings.json"
			echo '{}' >"$dir/appsettings-schema.json" ;;
	esac
done
"""

FAKE_CURL = r"""#!/bin/bash
printf 'curl %s\n' "$*" >>"$FAKE_LOG"
printf '%s' "${FAKE_HTTP_STATUS:-200}"
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def render_template(template, values):
    """Render a template as create_prod.sh does. The sudo line is removed: the tests do not run as root."""
    text = template.read_text()
    for key, value in values.items():
        text = text.replace("@" + key + "@", value)
    return re.sub(r"(?m)^\[\[ \$EUID -eq 0 \]\] \|\| exec sudo .*$", "", text)


class WebConfigTests(unittest.TestCase):
    def test_new_web_config_runs_the_project_with_the_environment(self):
        root = ET.fromstring(windows_prod.web_config("My.App", "Staging"))
        module = root.find(".//aspNetCore")
        self.assertEqual(module.get("processPath"), "dotnet")
        self.assertEqual(module.get("arguments"), ".\\My.App.dll")
        self.assertEqual(module.get("hostingModel"), "inprocess")
        self.assertEqual(root.find(".//handlers/add").get("modules"), "AspNetCoreModuleV2")
        self.assertEqual({item.get("name"): item.get("value") for item in module.iter("environmentVariable")},
                         {"ASPNETCORE_ENVIRONMENT": "Staging", "DOTNET_ENVIRONMENT": "Staging"})

    def rule_names(self, *args, **kwargs):
        root = ET.fromstring(windows_prod.web_config("App", "Test", *args, **kwargs))
        return [rule.get("name") for rule in root.iter("rule")], root

    def test_rules_follow_https_and_umbraco(self):
        https, sitemap, robots = "Rewrite HTTP to HTTPS", "Rewrite to Sitemap.xml", "Rewrite to Robots.txt"
        self.assertEqual(self.rule_names(https=False, umbraco=False)[0], [])
        self.assertIsNone(self.rule_names(https=False, umbraco=False)[1].find(".//rewrite"))
        self.assertEqual(self.rule_names(https=True, umbraco=False)[0], [https])
        self.assertEqual(self.rule_names(https=False, umbraco=True)[0], [sitemap, robots])
        self.assertEqual(self.rule_names(https=True, umbraco=True)[0], [https, sitemap, robots])

    def test_rules_have_the_expected_content(self):
        _, root = self.rule_names(https=True, umbraco=True)
        self.assertIs(root.find("./location/system.webServer/rewrite/rules") is not None, True)
        https, sitemap, robots = root.findall(".//rule")
        self.assertEqual(https.find("match").get("url"), "(.*)")
        condition = https.find("conditions/add")
        self.assertEqual((condition.get("input"), condition.get("pattern")), ("{HTTPS}", "^OFF$"))
        action = https.find("action")
        self.assertEqual((action.get("type"), action.get("url"), action.get("appendQueryString")),
                         ("Redirect", "https://{HTTP_HOST}{REQUEST_URI}", "false"))
        self.assertEqual((sitemap.find("match").get("url"), sitemap.find("action").get("type"), sitemap.find("action").get("url")),
                         ("^sitemap.xml", "Rewrite", "SEO_SiteMap_xml"))
        self.assertEqual((robots.find("match").get("url"), robots.find("action").get("type"), robots.find("action").get("url")),
                         ("^robots.txt", "Rewrite", "SEO_Robots_txt"))
        for rule in (https, sitemap, robots):
            self.assertEqual((rule.get("stopProcessing"), rule.get("enabled")), ("true", "true"))

    def test_changing_environment_keeps_the_rules(self):
        changed = self.change_environment(windows_prod.web_config("App", "Production", https=True, umbraco=True))
        self.assertEqual(len(ET.fromstring(changed).findall(".//rule")), 3)

    def test_project_name_is_escaped(self):
        module = ET.fromstring(windows_prod.web_config('A&"B', "Test")).find(".//aspNetCore")
        self.assertEqual(module.get("arguments"), '.\\A&"B.dll')

    def change_environment(self, text, environment="Test"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "web.config"
            path.write_text(text)
            windows_prod.set_environment(path, environment)
            return path.read_text()

    def test_changes_environment_and_keeps_the_rest(self):
        text = windows_prod.web_config("App", "Production").replace(
            "<handlers>", "<!-- keep me --><handlers>").replace(
            "</environmentVariables>", '<environmentVariable name="CUSTOM" value="1" /></environmentVariables>')
        changed = self.change_environment(text)
        self.assertIn("keep me", changed)
        variables = {item.get("name"): item.get("value") for item in ET.fromstring(changed).iter("environmentVariable")}
        self.assertEqual(variables, {"ASPNETCORE_ENVIRONMENT": "Test", "DOTNET_ENVIRONMENT": "Test", "CUSTOM": "1"})

    def test_adds_variables_to_a_handmade_web_config(self):
        changed = self.change_environment(
            '<configuration><system.webServer><aspNetCore processPath="dotnet" /></system.webServer></configuration>')
        self.assertEqual(len(list(ET.fromstring(changed).iter("environmentVariable"))), 2)

    def test_web_config_without_app_is_refused(self):
        with self.assertRaises(ValueError):
            self.change_environment("<configuration />")

    def test_first_publish_settings_lose_connection_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "appsettings.json"
            path.write_text('\ufeff' + json.dumps({
                "ConnectionStrings": {"umbracoDbDSN": "secret"}, "Name": "æøå",
                "Umbraco": {"CMS": {"Global": {"UseHttps": True},
                                    "WebRouting": {"UmbracoApplicationUrl": "https://test"}}}}), encoding="utf-8")
            windows_prod.prepare_appsettings(path, "http://prod.test", "n")
            settings = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("ConnectionStrings", settings)
        self.assertEqual(settings["Name"], "æøå")
        self.assertEqual(settings["Umbraco"]["CMS"]["WebRouting"]["UmbracoApplicationUrl"], "http://prod.test")
        self.assertIs(settings["Umbraco"]["CMS"]["Global"]["UseHttps"], False)


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.server = self.root / "server"
        self.server.mkdir()
        self.log = self.root / "calls.log"
        self.log.touch()
        self.site = self.root / "site"
        (self.site / "scripts").mkdir(parents=True)
        (self.site / "App.csproj").write_text("<Project />\n")
        executable(self.bin / "rclone", FAKE_RCLONE)
        executable(self.bin / "dotnet", FAKE_DOTNET)
        executable(self.bin / "curl", FAKE_CURL)
        executable(self.bin / "sleep", "#!/bin/bash\n")
        self.values = {
            "APACHE_DIR": str(APACHE), "PROD_WINDOWS_SHARE": shlex.quote("//10.0.0.1/share$/sites/prod.test"),
            "PROD_SMB_USER": "Administrator", "PROD_SMB_PASSWORD": shlex.quote("pa$$ 'word"), "PROD_SMB_DOMAIN": "VNS",
            "PROD_FOLDER": "prod.test", "PROD_BINDING": "prod.test", "PROD_HTTPS": "y", "PROD_ENVIRONMENT": "Staging",
            "PROJECT_NAME": "App", "PUBLISH_FOLDER": "_publish",
        }

    def script(self, name, template, **values):
        path = self.site / "scripts" / name
        path.write_text(render_template(template / name, self.values | values))
        return path

    def run_script(self, path, *args, **env):
        return subprocess.run(
            [BASH, str(path), *args], capture_output=True, text=True,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                              "FAKE_SERVER": str(self.server), "VNS_PUBLISH_TRACKED": "prod"} | env)

    def calls(self):
        return self.log.read_text().splitlines()

    def index(self, prefix):
        return next(number for number, call in enumerate(self.calls()) if call.startswith(prefix))


class WindowsPublishTests(ScriptTestCase):
    def publish(self, *args, **env):
        return self.run_script(self.script("publish_prod.sh", WINDOWS_TEMPLATES), *args, **env)

    def test_first_publish_creates_web_config_inside_the_offline_window(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertLess(self.index("dotnet publish"), self.index("copyto "))
        self.assertIn("app_offline.htm", calls[self.index("copyto ")])
        self.assertLess(self.index("copyto "), self.index("copy /"))
        self.assertLess(self.index("copy /"), self.index("deletefile"))
        web_config = next(number for number, call in enumerate(calls) if call.endswith("/web.config"))
        self.assertLess(web_config, self.index("deletefile"))
        module = ET.parse(self.server / "web.config").getroot().find(".//aspNetCore")
        self.assertEqual(module.get("arguments"), ".\\App.dll")
        self.assertIn('value="Staging"', (self.server / "web.config").read_text())
        copy = calls[self.index("copy /")]
        self.assertIn("--exclude /web.config", copy)
        self.assertNotIn("appsettings*.json", copy)
        self.assertIn("--runtime win-x64", calls[self.index("dotnet publish")])
        self.assertIn("https://prod.test", calls[self.index("curl ")])

    def test_password_stays_out_of_the_process_list(self):
        self.assertEqual(self.publish().returncode, 0)
        self.assertNotIn("pa$$", self.log.read_text())
        self.assertIn("RCLONE_SMB_PASS=obscured", Path(str(self.log) + ".env").read_text())
        self.assertIn("RCLONE_SMB_HOST=10.0.0.1", Path(str(self.log) + ".env").read_text())
        self.assertIn("RCLONE_SMB_DOMAIN=VNS", Path(str(self.log) + ".env").read_text())

    def test_later_publish_keeps_settings_web_config_and_content(self):
        (self.server / "appsettings.json").touch()
        (self.server / "web.config").write_text("handmade")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.server / "web.config").read_text(), "handmade")
        self.assertFalse(any(call.endswith("/web.config") for call in self.calls()))
        copy = self.calls()[self.index("copy /")]
        for rule in ("/web.config", "appsettings*.json", "/umbraco/**", "/uSync/**", "/Views/**", "/wwwroot/**", "/App_Data/**"):
            self.assertIn("--exclude " + rule, copy)
        self.assertIn("--ignore-case", copy)
        self.assertFalse(any(call.startswith("sync ") for call in self.calls()))

    def test_site_comes_back_online_when_the_copy_fails(self):
        result = self.publish(FAKE_COPY_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(self.index("copy /"), self.index("deletefile"))

    def test_failed_build_never_takes_the_site_offline(self):
        executable(self.bin / "dotnet", "#!/bin/bash\nexit 1\n")
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertFalse(any("app_offline" in call for call in self.calls()))

    def test_wrong_folder_stops_before_the_build(self):
        result = self.run_script(self.script(
            "publish_prod.sh", WINDOWS_TEMPLATES, PROD_WINDOWS_SHARE=shlex.quote("//10.0.0.1/share$/typo")))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not open", result.stderr)
        self.assertFalse(any(call.startswith("dotnet") for call in self.calls()))

    def test_dry_run_changes_nothing_and_is_not_recorded(self):
        path = self.script("publish_prod.sh", WINDOWS_TEMPLATES)
        result = self.run_script(path, "--dry-run", VNS_PUBLISH_TRACKED="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", self.calls()[self.index("copy /")])
        self.assertFalse(any(call.startswith(("copyto", "deletefile", "curl")) for call in self.calls()))
        self.assertFalse((self.server / "web.config").exists())
        self.assertFalse(list(self.site.rglob("*publish*.json")))

    def test_failed_site_check_fails_except_on_the_first_publish(self):
        result = self.publish(FAKE_HTTP_STATUS="500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stdout)
        (self.server / "appsettings.json").touch()
        self.assertNotEqual(self.publish(FAKE_HTTP_STATUS="500").returncode, 0)

    def server_rules(self):
        return [rule.get("name") for rule in ET.parse(self.server / "web.config").getroot().iter("rule")]

    def test_https_umbraco_site_gets_all_rules(self):
        (self.site / "App.csproj").write_text('<Project><PackageReference Include="Umbraco.Cms" /></Project>\n')
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.server_rules(), ["Rewrite HTTP to HTTPS", "Rewrite to Sitemap.xml", "Rewrite to Robots.txt"])

    def test_https_site_without_umbraco_only_gets_the_redirect(self):
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.server_rules(), ["Rewrite HTTP to HTTPS"])

    def test_http_umbraco_site_only_gets_the_rewrites(self):
        (self.site / "App.csproj").write_text('<Project><PackageReference Include="Umbraco.Cms" /></Project>\n')
        result = self.run_script(self.script("publish_prod.sh", WINDOWS_TEMPLATES, PROD_HTTPS="n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server_rules(), ["Rewrite to Sitemap.xml", "Rewrite to Robots.txt"])

    def test_http_site_is_checked_on_http(self):
        result = self.run_script(self.script("publish_prod.sh", WINDOWS_TEMPLATES, PROD_HTTPS="n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://prod.test", self.calls()[self.index("curl ")])


class WindowsContentTests(ScriptTestCase):
    def test_sync_mirrors_existing_folders_and_skips_missing_ones(self):
        for folder in ("uSync", "Views", "wwwroot/css"):
            (self.site / "_publish" / folder).mkdir(parents=True)
        result = self.run_script(self.script("sync_prod.sh", UMBRACO_TEMPLATES))
        self.assertEqual(result.returncode, 0, result.stderr)
        synced = [call.split()[1] for call in self.calls() if call.startswith("sync ")]
        self.assertEqual(synced, ["uSync", "Views", "wwwroot/css"])
        self.assertIn("wwwroot/scripts: SKIPPED", result.stdout)

    def test_media_is_not_synced(self):
        (self.site / "_publish/wwwroot/media").mkdir(parents=True)
        result = self.run_script(self.script("sync_prod.sh", UMBRACO_TEMPLATES))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("media" in call for call in self.calls()))
        self.assertNotIn("media", result.stdout)

    def test_sync_dry_run(self):
        (self.site / "_publish/uSync").mkdir(parents=True)
        result = self.run_script(self.script("sync_prod.sh", UMBRACO_TEMPLATES), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", next(call for call in self.calls() if call.startswith("sync ")))

    def test_clear_logs_takes_the_site_offline_and_back(self):
        result = self.run_script(self.script("clear_logs_prod.sh", UMBRACO_TEMPLATES))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to delete", result.stdout)
        self.assertFalse(any("app_offline" in call for call in self.calls()))
        executable(self.bin / "rclone", FAKE_RCLONE.replace(
            '[[ ${2%/} == ":smb:share\\$/sites/prod.test" ]] || exit "${FAKE_LSF_STATUS:-3}"', ""))
        self.log.write_text("")
        result = self.run_script(self.script("clear_logs_prod.sh", UMBRACO_TEMPLATES))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(self.index("copyto "), self.index("purge "))
        self.assertLess(self.index("purge "), self.index("deletefile"))


class LinuxHttpTests(ScriptTestCase):
    """Run the block that prepares the target, with the system folders moved into a temporary folder."""

    def prepare(self, https):
        text = LINUX_TEMPLATE.read_text()
        block = re.search(r"run_script <<'REMOTE'\n(.*?)\nREMOTE\n", text, re.S).group(1)
        etc = self.root / "etc"
        for folder in ("systemd/system", "apache2/sites-available", "apache2/sites-enabled", "ssl/certs"):
            (etc / folder).mkdir(parents=True, exist_ok=True)
        (self.root / "var").mkdir(exist_ok=True)
        helpers = self.root / "common.sh"
        helpers.write_text(
            'normalize_dns_name() { printf "%s" "$1"; }\n'
            'allocate_ports() { _portHttp=4123; }\n'
            'release_port_reservation() { :; }\n')
        for command in ("systemctl", "a2enmod", "a2ensite", "apache2ctl"):
            executable(self.bin / command, "#!/bin/bash\n")
        script = (block.replace("source /vns/apache/lib/common.sh", f"source '{helpers}'")
                  .replace("/etc/", f"{etc}/").replace("/var/lib/", f"{self.root}/var/"))
        path = self.root / "prepare.sh"
        path.write_text(script)
        result = subprocess.run(
            [BASH, str(path)], capture_output=True, text=True,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "SITE": "prod.test", "BINDING": "prod.test",
                              "ENVIRONMENT": "Production", "MAIL": "admin@example.test", "PROJECT": "App", "UMBRACO": "1",
                              "ROOT": str(self.root / "www"), "HTTPS": https})
        return result, etc / "apache2/sites-available/prod.test.conf"

    def test_http_creates_a_proxy_without_certificate(self):
        result, conf = self.prepare("n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = conf.read_text()
        self.assertNotIn("Issue certificate", result.stdout)
        self.assertNotIn("SSLEngine", text)
        self.assertNotIn(":443", text)
        self.assertNotIn("https://", text)
        self.assertIn("ProxyPass / http://localhost:4123/ nocanon", text)
        self.assertIn('X-Forwarded-Proto "http"', text)
        self.assertIn("ProxyPass /.well-known/acme-challenge/ !", text)
        self.assertIn("/seo_robots_txt", text)

    def test_http_keeps_an_existing_https_config(self):
        _, conf = self.prepare("n")
        conf.write_text("<VirtualHost *:443>\n  SSLEngine On\n  ProxyPass / http://localhost:4123/\n</VirtualHost>\n")
        before = conf.read_text()
        result, _ = self.prepare("n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(conf.read_text(), before)

    @NEEDS_BASH_4
    def test_https_stays_the_default_for_scripts_without_the_setting(self):
        result, conf = self.prepare("@PROD_HTTPS@")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("acme.sh not found", result.stderr)

    def test_scheme_for_umbraco_follows_the_apache_config(self):
        text = LINUX_TEMPLATE.read_text()
        start = re.findall(r"run_script <<'REMOTE'\n(.*?)\nREMOTE\n", text, re.S)[1]
        self.assertIn('scheme=http\n', start)
        self.assertIn('${scheme}://${BINDING}', start)
        self.assertNotIn('https://${BINDING}', start)


class ProductionSetupTests(unittest.TestCase):
    def setUp(self):
        source = (APACHE / "solutionfiles/admin/app/app.py").read_text()
        wanted = ("production_setup_env", "dns_name", "windows_share", "parse_prod", "read_text")
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        nodes += [node for node in ast.parse(source).body
                  if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PROD_KEYS"]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scripts = Path(self.temp.name) / "example.test/scripts"
        self.scripts.mkdir(parents=True)
        (self.scripts / "create_prod.sh").touch()
        self.ns = {"os": os, "re": re, "shlex": shlex, "abort": abort, "WEB_ROOT": self.temp.name,
                   "NAME_RE": re.compile(r"^[A-Za-z0-9.-]+$"), "BINDING_RE": re.compile(r"^[A-Za-z0-9.-]+$"),
                   "conf_binding": lambda site: "example.test", "_normalize_dns_name": normalize_dns_name,
                   "_normalize_site_name": normalize_site_name, "service_name": service_name}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "production env", "exec"), self.ns)
        add_target_helpers(self.ns)
        self.windows = dict(target="windows", binding="prod.test", smb_share="//10.0.0.1/share$/sites/prod.test",
                            smb_user="Administrator", smb_password="secret", smb_domain="VNS", https="n")

    def setup_env(self, data):
        return self.ns["production_setup_env"](data, "example.test")

    def test_windows_settings_reach_the_script(self):
        result = self.setup_env(self.windows)
        self.assertEqual(result["VNS_PROD_TARGET"], "windows")
        self.assertEqual(result["VNS_PROD_SMB_SHARE"], "//10.0.0.1/share$/sites/prod.test")
        self.assertEqual(result["VNS_PROD_SMB_PASSWORD"], "secret")
        self.assertEqual(result["VNS_PROD_HTTPS"], "n")
        self.assertEqual(result["VNS_PROD_FOLDER"], "prod.test")

    def test_windows_path_style_is_accepted(self):
        result = self.setup_env(self.windows | {"smb_share": "\\\\10.0.0.1\\share$\\sites\\My Site\\"})
        self.assertEqual(result["VNS_PROD_SMB_SHARE"], "//10.0.0.1/share$/sites/My Site")

    def test_invalid_windows_input_is_refused(self):
        for change in ({"smb_share": "//10.0.0.1/share$"}, {"smb_share": "//10.0.0.1/share$/../x"}, {"smb_share": "D:/site"},
                       {"smb_share": "//10.0.0.1/share$/a|b"}, {"smb_share": ["x"]}, {"smb_password": ""},
                       {"smb_user": "VNS\\Administrator"}, {"smb_user": ""}, {"smb_domain": "a b"}, {"https": "maybe"}):
            with self.subTest(change), self.assertRaises(BadRequest):
                self.setup_env(self.windows | change)

    def test_https_defaults_to_yes_and_linux_targets_get_the_choice(self):
        local = dict(target="local", folder="prod.test", binding="prod.test", local_path=str(self.scripts.parent / "_prod"))
        self.assertEqual(self.setup_env(local)["VNS_PROD_HTTPS"], "y")
        self.assertEqual(self.setup_env(local | {"https": "n"})["VNS_PROD_HTTPS"], "n")
        remote = dict(target="remote", folder="prod.test", binding="prod.test", host="server.test", user="root", ssh_port="22", https="n")
        self.assertEqual(self.setup_env(remote)["VNS_PROD_HTTPS"], "n")

    def test_saved_settings_are_read_without_the_password(self):
        share = "//10.0.0.1/share$/sites/My Site"
        text = render_template(WINDOWS_TEMPLATES / "publish_prod.sh", {
            "PROD_WINDOWS_SHARE": subprocess.run(["bash", "-c", 'printf %q "$0"', share], capture_output=True, text=True).stdout,
            "PROD_SMB_USER": "Administrator", "PROD_SMB_PASSWORD": "secret", "PROD_SMB_DOMAIN": "VNS",
            "PROD_FOLDER": "prod.test", "PROD_BINDING": "prod.test", "PROD_HTTPS": "n", "PROD_ENVIRONMENT": "Staging"})
        (self.scripts / "publish_prod.sh").write_text(text)
        settings = self.ns["parse_prod"](str(self.scripts.parent))
        self.assertEqual(settings["PROD_TARGET"], "windows")
        self.assertEqual(settings["PROD_WINDOWS_SHARE"], share)
        self.assertEqual(settings["PROD_HTTPS"], "n")
        self.assertEqual(settings["PROD_ENVIRONMENT"], "Staging")
        self.assertNotIn("secret", settings.values())

    def test_unrendered_setting_counts_as_empty(self):
        (self.scripts / "publish_prod.sh").write_text("PROD_HTTPS=@PROD_HTTPS@   # comment\nPROD_IP=''\n")
        settings = self.ns["parse_prod"](str(self.scripts.parent))
        self.assertEqual((settings["PROD_HTTPS"], settings["PROD_IP"]), ("", ""))


class TemplateTests(unittest.TestCase):
    def test_every_placeholder_is_filled_by_create_prod(self):
        create = (APACHE / "solutionfiles/csharp/scripts/create_prod.sh").read_text()
        templates = [LINUX_TEMPLATE, *WINDOWS_TEMPLATES.glob("*.sh"), *UMBRACO_TEMPLATES.glob("*.sh")]
        for template in templates:
            for name in set(re.findall(r"@([A-Z_]+)@", template.read_text())) - {"APACHE_DIR"}:
                with self.subTest(template=template.name, name=name):
                    self.assertIn("R_" + name + "=", create)

    def test_windows_scripts_have_the_names_the_admin_buttons_use(self):
        self.assertEqual({path.name for path in WINDOWS_TEMPLATES.glob("*.sh")}, {"publish_prod.sh"})
        self.assertEqual({path.name for path in UMBRACO_TEMPLATES.glob("*.sh")}, {"sync_prod.sh", "clear_logs_prod.sh"})
        self.assertIn('VNS_PUBLISH_TRACKED:-} != "${_scope}"', (WINDOWS_TEMPLATES / "publish_prod.sh").read_text())

    def test_actions_without_ssh_are_refused_for_windows(self):
        self.assertIn("PROD_TARGET:-} == windows", (APACHE / "lib/prod.sh").read_text())
        for script in ("change_binding_prod.sh", "prod_action.sh"):
            self.assertIn("PROD_TARGET:-} == windows", (APACHE / script).read_text())
        self.assertIn("PROD_TARGET:-} != windows", (APACHE / "remote_site.sh").read_text())
        self.assertIn('_prodTarget} == windows', (APACHE / "delete_site.sh").read_text())


SUDO_REMOVING_ITSELF = r"""#!/bin/bash
# The scripts re-run themselves with sudo when not root, so the re-run gets a copy without that line.
script=$1
shift
sed '/exec sudo/d' "$script" >"$script.nosudo"
exec bash "$script.nosudo" "$@"
"""


@NEEDS_BASH_4
class CreateProdTests(ScriptTestCase):
    """Run create_prod.sh as the admin modal does, with the real templates."""

    def setUp(self):
        super().setUp()
        executable(self.bin / "sudo", SUDO_REMOVING_ITSELF)
        self.scripts = self.site / "scripts"
        (self.scripts / "publish.sh").write_text("# local publish\n")
        (self.scripts / "clear_logs.sh").write_text("# local logs\n")
        for folder in ("uSync", "Views"):
            (self.site / "_publish" / folder).mkdir(parents=True)

    def create(self, kind="umbraco", **env):
        text = (APACHE / "solutionfiles/csharp/scripts/create_prod.sh").read_text()
        values = {"APACHE_DIR": str(APACHE), "SITE": "example.test", "BINDING": "example.test", "PROJECT_NAME": "App",
                  "SITE_ROOT": str(self.site), "PUBLISH_FOLDER": "_publish", "ADMIN_MAIL": "admin@example.test", "CSHARP_KIND": kind}
        for key, value in values.items():
            text = text.replace("@" + key + "@", value)
        text = re.sub(r"(?ms)^if \[\[ \$EUID -ne 0 \]\]; then.*?^fi\n", "", text, count=1)
        path = self.scripts / "create_prod.sh"
        path.write_text(text)
        return subprocess.run(
            [BASH, str(path)], capture_output=True, text=True, cwd=self.site,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                              "FAKE_SERVER": str(self.server), "VNS_PUBLISH_TRACKED": "prod", "VNS_PROD_MODAL": "1",
                              "VNS_PROD_ENVIRONMENT": "Staging", "VNS_PROD_FOLDER": "prod.test", "VNS_PROD_BINDING": "prod.test"} | env)

    def windows(self, **env):
        return self.create(VNS_PROD_TARGET="windows", VNS_PROD_SMB_SHARE="//10.0.0.1/share$/sites/prod.test",
                           VNS_PROD_SMB_USER="Administrator", VNS_PROD_SMB_PASSWORD="pa$$ 'w\"ord", VNS_PROD_SMB_DOMAIN="VNS",
                           VNS_PROD_HTTPS="n", **env)

    def setting(self, name, script="publish_prod.sh", target="staging"):
        lines = [line for line in (self.scripts / "targets" / target / script).read_text().splitlines() if line.startswith("PROD_")]
        code = "\n".join(lines) + f'\nprintf %s "${{{name}}}"'
        return subprocess.run([BASH, "-c", code], capture_output=True, text=True, check=True).stdout

    def test_windows_setup_publishes_and_syncs(self):
        result = self.windows()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.scripts / "publish_local.sh").read_text(), "# local publish\n")
        self.assertEqual((self.scripts / "clear_logs_local.sh").read_text(), "# local logs\n")
        for name in ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"):
            path = self.scripts / "targets/staging" / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertNotRegex(path.read_text(), r"@[A-Z_]+@")
            self.assertEqual(self.setting("PROD_SMB_PASSWORD", name), "pa$$ 'w\"ord")
            self.assertEqual(self.setting("PROD_WINDOWS_SHARE", name), "//10.0.0.1/share$/sites/prod.test")
        self.assertEqual(self.setting("PROD_TARGET"), "windows")
        self.assertEqual(self.setting("PROD_HTTPS"), "n")
        self.assertIn('value="Staging"', (self.server / "web.config").read_text())
        self.assertLess(self.index("dotnet publish"), self.index("sync "))
        self.assertIn("http://prod.test", self.calls()[self.index("curl ")])

    def test_wrong_login_changes_nothing(self):
        result = self.windows(FAKE_LOGIN_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not open", result.stderr)
        self.assertTrue((self.scripts / "publish.sh").exists())
        self.assertFalse((self.scripts / "targets").exists())
        self.assertFalse(any(call.startswith("dotnet") for call in self.calls()))

    def test_empty_project_gets_no_umbraco_scripts(self):
        self.assertEqual(self.windows(kind="empty").returncode, 0)
        self.assertFalse((self.scripts / "targets/staging/sync_prod.sh").exists())
        self.assertFalse((self.scripts / "targets/staging/clear_logs_prod.sh").exists())

    def test_share_without_a_folder_is_refused(self):
        result = self.create(VNS_PROD_TARGET="windows", VNS_PROD_SMB_SHARE="//10.0.0.1/share$", VNS_PROD_SMB_USER="a", VNS_PROD_SMB_PASSWORD="b")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.scripts / "targets").exists())

    def test_several_environments_get_their_own_folders(self):
        self.assertEqual(self.windows().returncode, 0)
        self.assertEqual(self.windows().returncode, 0)
        self.assertEqual(self.windows(VNS_PROD_ENVIRONMENT="Production").returncode, 0)
        self.assertEqual(sorted(path.name for path in (self.scripts / "targets").iterdir()),
                         ["production", "staging", "staging-2"])
        self.assertTrue((self.site / ".vns-publish/target-staging-2.json").exists())
        self.assertEqual((self.scripts / "publish_local.sh").read_text(), "# local publish\n")

    def test_two_local_environments_cannot_share_a_folder(self):
        executable(self.bin / "dotnet", "#!/bin/bash\nexit 1\n")
        local = dict(VNS_PROD_TARGET="local", VNS_PROD_LOCAL_PATH=str(self.site / "_prod"), VNS_PROD_HTTPS="n")
        self.create(**local)
        result = self.create(**local, VNS_PROD_ENVIRONMENT="Production")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already uses that folder", result.stderr)
        self.assertFalse((self.scripts / "targets/production").exists())

    def test_linux_targets_save_the_protocol_and_use_the_linux_templates(self):
        executable(self.bin / "dotnet", "#!/bin/bash\nexit 1\n")
        executable(self.bin / "ssh", "#!/bin/bash\necho x86_64\n")
        self.create(VNS_PROD_TARGET="remote", VNS_PROD_IP="server.test", VNS_PROD_USER="root", VNS_PROD_SSH_PORT="22", VNS_PROD_HTTPS="n")
        self.assertEqual(self.setting("PROD_HTTPS"), "n")
        text = (self.scripts / "targets/staging/publish_prod.sh").read_text()
        self.assertNotIn("PROD_TARGET=windows", text)
        self.assertIn("HTTPS='${PROD_HTTPS}'", text)
        self.assertIn("rsync", (self.scripts / "targets/staging/sync_prod.sh").read_text())


@NEEDS_BASH_4
class WindowsActionTests(ScriptTestCase):
    """Run the actions from a copy of the apache folder, with its site folder in a temporary folder."""

    def setUp(self):
        super().setUp()
        self.apache = self.root / "apache"
        shutil.copytree(APACHE / "lib", self.apache / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for script in ("prod_action.sh", "change_binding_prod.sh", "remote_site.sh", "delete_site.sh", "fix_permissions.sh", "sync_env.sh", "adopt_site.sh"):
            shutil.copy(APACHE / script, self.apache)
        common = self.apache / "lib/common.sh"
        common.write_text(common.read_text().replace("_rootPath=/var/www", f"_rootPath={self.root}/www"))
        self.scripts = self.root / "www/example.test/scripts"
        self.scripts.mkdir(parents=True)
        (self.scripts / "publish_prod.sh").write_text(render_template(WINDOWS_TEMPLATES / "publish_prod.sh", self.values))
        for name in ("sync_prod.sh", "clear_logs_prod.sh"):
            (self.scripts / name).write_text("# prod\n")
        (self.scripts / "publish_local.sh").write_text("# local\n")
        executable(self.bin / "sudo", '#!/bin/bash\nexec "$@"\n')
        for command in ("clear", "systemctl", "apache2ctl"):
            executable(self.bin / command, "#!/bin/bash\n")
        executable(self.bin / "ssh", "#!/bin/bash\necho SSH-WAS-USED >>\"$FAKE_LOG\"\n")
        self.addCleanup(lambda: self.assertNotIn("SSH-WAS-USED", self.log.read_text()))

    def action(self, script, *args):
        return self.run_script(self.apache / script, *args)

    def test_change_environment_edits_web_config_and_the_setting(self):
        (self.server / "web.config").write_text(windows_prod.web_config("App", "Staging"))
        executable(self.bin / "rclone", FAKE_RCLONE.replace(
            'copyto)\n\t\t[[ ${3##*/} != web.config ]] || cp "$2" "$FAKE_SERVER/web.config" ;;',
            'copyto)\n\t\tif [[ ${2##*/} == web.config && $2 == :smb:* ]]; then cp "$FAKE_SERVER/web.config" "$3"; else cp "$2" "$FAKE_SERVER/web.config"; fi ;;'))
        result = self.action("prod_action.sh", "example.test", "change_env", "Test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        variables = [item.get("value") for item in ET.parse(self.server / "web.config").getroot().iter("environmentVariable")]
        self.assertEqual(variables, ["Test", "Test"])
        self.assertIn('PROD_ENVIRONMENT="Test"', (self.scripts / "publish_prod.sh").read_text())

    def test_change_environment_without_web_config_keeps_the_setting(self):
        executable(self.bin / "rclone", FAKE_RCLONE.replace("copyto)\n", "copyto)\n\t\texit 1\n"))
        result = self.action("prod_action.sh", "example.test", "change_env", "Test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Publish to production first", result.stderr)
        self.assertIn('PROD_ENVIRONMENT="Staging"', (self.scripts / "publish_prod.sh").read_text())

    def test_actions_that_need_ssh_are_refused(self):
        for script, args, message in (
            ("prod_action.sh", ("example.test", "issue_certificate", "--yes"), "managed in IIS"),
            ("change_binding_prod.sh", ("example.test", "new.test", "", "--yes"), "managed in IIS"),
            ("remote_site.sh", ("example.test", "status"), "not a remote production target"),
            ("fix_permissions.sh", ("example.test", "prod"), "not available for a Windows"),
        ):
            with self.subTest(script=script):
                result = self.action(script, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_delete_production_keeps_the_server_and_restores_the_test_site(self):
        (self.server / "web.config").write_text("keep")
        result = self.action("delete_site.sh", "example.test", "n", "prod")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not touched", result.stdout)
        self.assertEqual((self.server / "web.config").read_text(), "keep")
        self.assertEqual(self.calls(), [])
        for name in ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"):
            self.assertFalse((self.scripts / name).exists())
        self.assertEqual((self.scripts / "publish.sh").read_text(), "# local\n")


@NEEDS_BASH_4
class SeveralEnvironmentsTests(WindowsActionTests):
    # This one expects a single environment. With several, test_delete_without_choosing... covers the refusal.
    test_delete_production_keeps_the_server_and_restores_the_test_site = None

    def setUp(self):
        super().setUp()
        self.site_root = self.root / "www/example.test"
        for name in ("staging", "production"):
            folder = self.scripts / "targets" / name
            folder.mkdir(parents=True)
            folder.joinpath("publish_prod.sh").write_text(render_template(
                WINDOWS_TEMPLATES / "publish_prod.sh", self.values | {"PROD_BINDING": name + ".test", "PROD_FOLDER": name + ".test"}))
            folder.joinpath("sync_prod.sh").write_text(
                '#!/bin/bash\nprintf "%s from %s\\n" ' + name + ' "${VNS_SYNC_SOURCE:-main}" >>"$FAKE_LOG"\nexit ${FAIL_SYNC:-0}\n')
            executable(folder / "clear_logs_prod.sh", "# clear\n")
        (self.scripts / "sync_prod.sh").write_text('#!/bin/bash\necho "prod from ${VNS_SYNC_SOURCE:-main}" >>"$FAKE_LOG"\n')
        (self.site_root / "_publish/uSync").mkdir(parents=True)
        # The tests are not root, and the script re-runs itself with sudo when it is not
        env_script = self.apache / "sync_env.sh"
        env_script.write_text(env_script.read_text().replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"\n', ""))

    def run_with(self, script, *args, **env):
        return self.run_script(self.apache / script, *args, **env)

    def test_vns_target_selects_the_environment(self):
        result = self.run_with("change_binding_prod.sh", "example.test", "new.test", "", "--yes", VNS_TARGET="staging")
        self.assertIn(str(self.scripts / "targets/staging/publish_prod.sh"), result.stderr)

    def test_unknown_or_unsafe_target_is_refused(self):
        for target in ("../../etc", "missing", "A"):
            with self.subTest(target):
                result = self.run_with("prod_action.sh", "example.test", "change_env", "Test", VNS_TARGET=target)
                self.assertNotEqual(result.returncode, 0)

    def test_sync_from_main_to_all(self):
        result = self.run_with("sync_env.sh", "example.test", "main", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sorted(call for call in self.calls() if " from " in call),
                         ["prod from main", "production from main", "staging from main"])

    def test_sync_between_environments_uses_a_temporary_copy_and_skips_the_source(self):
        result = self.run_with("sync_env.sh", "example.test", "staging", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [call for call in self.calls() if " from " in call]
        self.assertEqual(sorted(call.split()[0] for call in calls), ["prod", "production"])
        source = calls[0].split()[-1]
        self.assertNotEqual(source, "main")
        self.assertFalse(Path(source).exists(), "The temporary copy is removed afterwards")
        self.assertLess(self.index("lsf "), self.index(calls[0].split()[0] + " from"))

    def test_sync_to_one_environment(self):
        self.assertEqual(self.run_with("sync_env.sh", "example.test", "main", "staging").returncode, 0)
        self.assertEqual([call for call in self.calls() if " from " in call], ["staging from main"])

    def test_failed_pull_stops_before_anything_is_mirrored(self):
        result = self.run_with("sync_env.sh", "example.test", "staging", "production", FAKE_LOGIN_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse([call for call in self.calls() if " from " in call])

    def test_failed_destination_is_reported_and_the_rest_continue(self):
        result = self.run_with("sync_env.sh", "example.test", "main", "all", FAIL_SYNC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("errors in 2 environment", result.stderr)
        self.assertIn("prod from main", self.calls())

    def test_sync_refuses_same_source_and_destination(self):
        self.assertNotEqual(self.run_with("sync_env.sh", "example.test", "staging", "staging").returncode, 0)
        self.assertNotEqual(self.run_with("sync_env.sh", "example.test", "unknown", "all").returncode, 0)

    def test_delete_one_environment_keeps_the_others(self):
        result = self.run_with("delete_site.sh", "example.test", "n", "prod", VNS_TARGET="staging")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.scripts / "targets/staging").exists())
        self.assertTrue((self.scripts / "targets/production").exists())
        self.assertTrue((self.scripts / "publish_prod.sh").exists())
        self.assertFalse((self.scripts / "publish.sh").exists(), "Other environments remain")

    def test_delete_without_choosing_an_environment_is_refused(self):
        result = self.run_with("delete_site.sh", "example.test", "n", "prod")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VNS_TARGET", result.stderr)
        self.assertTrue((self.scripts / "targets/staging").exists())

    def test_last_environment_restores_the_test_site_scripts(self):
        for target in ("staging", "production", "prod"):
            self.assertEqual(self.run_with("delete_site.sh", "example.test", "n", "prod", VNS_TARGET=target).returncode, 0)
        self.assertTrue((self.scripts / "publish.sh").exists())

    def test_delete_everything_reports_every_windows_environment(self):
        result = self.run_with("delete_site.sh", "example.test", "n", "all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("not touched"), 3)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.site_root.exists())


@NEEDS_BASH_4
class AdoptSiteTests(unittest.TestCase):
    """Run adopt_site.sh from a copy of the apache folder, with /etc and /var/www moved into a temporary folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.etc = self.root / "etc"
        self.www = self.root / "www"
        self.apache = self.root / "apache"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.etc / "systemd/system").mkdir(parents=True)
        (self.etc / "apache2/sites-available").mkdir(parents=True)
        shutil.copytree(APACHE / "lib", self.apache / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(APACHE / "solutionfiles/csharp", self.apache / "solutionfiles/csharp",
                        ignore=lambda folder, names: ["project"] if "project" in names else [])
        shutil.copy(APACHE / "adopt_site.sh", self.apache)
        script = self.apache / "adopt_site.sh"
        script.write_text(script.read_text().replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"\n', ""))
        common = self.apache / "lib/common.sh"
        common.write_text(common.read_text().replace("_rootPath=/var/www", f"_rootPath={self.www}").replace('"/etc/', f'"{self.etc}/'))
        create = self.apache / "solutionfiles/csharp/scripts/create_prod.sh"
        create.write_text(re.sub(r"(?ms)^if \[\[ \$EUID -ne 0 \]\]; then.*?^fi\n", "", create.read_text(), count=1))
        executable(self.bin / "dotnet", '#!/bin/bash\necho DOTNET >>"$FAKE_LOG"\n')
        self.log = self.root / "calls.log"
        self.log.touch()
        self.scripts = self.main("dev.example.test", "App")
        self.published("example.test", "App")

    def main(self, name, project):
        scripts = self.www / name / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "publish.sh").write_text("# local publish\n")
        (self.www / name / (project + ".csproj")).write_text('<Project><PackageReference Include="Umbraco.Cms" /></Project>\n')
        self.service(name, project, "_publish", "Test")
        self.conf(name, "n")
        return scripts

    def published(self, name, dll, environment="Production", https="y"):
        (self.www / name).mkdir(parents=True)
        (self.www / name / "App.dll").touch()
        self.service(name, dll, "", environment)
        self.conf(name, https)

    def service(self, name, dll, folder, environment):
        working = self.www / name / folder if folder else self.www / name
        (self.etc / f"systemd/system/{service_name(name)}.service").write_text(
            f"[Service]\nWorkingDirectory={working}\nExecStart=/usr/lib/dotnet/dotnet {working}/{dll}.dll --urls http://localhost:4001\n"
            f"Environment=ASPNETCORE_ENVIRONMENT={environment}\n")

    def conf(self, name, https):
        ssl = "  SSLEngine On\n" if https == "y" else ""
        (self.etc / f"apache2/sites-available/{name}.conf").write_text(
            f"<VirtualHost *:80>\n  ServerAdmin admin@example.test\n  ServerName {name}\n{ssl}</VirtualHost>\n")

    def adopt(self, *args):
        return subprocess.run(
            [BASH, str(self.apache / "adopt_site.sh"), *args], capture_output=True, text=True,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)})

    def setting(self, name, target="production"):
        lines = [line for line in (self.scripts / "targets" / target / "publish_prod.sh").read_text().splitlines() if line.startswith("PROD_")]
        return subprocess.run([BASH, "-c", "\n".join(lines) + f'\nprintf %s "${{{name}}}"'], capture_output=True, text=True, check=True).stdout

    def test_site_becomes_an_environment_without_publishing(self):
        before = (self.etc / "systemd/system/example.test.service").read_text()
        result = self.adopt("example.test", "dev.example.test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("PROD_LOCAL_PATH"), str(self.www / "example.test"))
        self.assertEqual(self.setting("PROD_FOLDER"), "example.test")
        self.assertEqual(self.setting("PROD_BINDING"), "example.test")
        self.assertEqual(self.setting("PROD_HTTPS"), "y")
        self.assertEqual(self.setting("PROD_ENVIRONMENT"), "Production")
        self.assertEqual(self.log.read_text(), "", "Nothing may be published")
        self.assertTrue((self.www / "example.test/App.dll").exists())
        self.assertEqual((self.etc / "systemd/system/example.test.service").read_text(), before)
        self.assertEqual((self.scripts / "publish_local.sh").read_text(), "# local publish\n")

    def test_http_and_environment_are_read_from_the_site(self):
        self.published("staging.example.test", "App", "Staging", "n")
        self.assertEqual(self.adopt("staging.example.test", "dev.example.test").returncode, 0)
        self.assertEqual(self.setting("PROD_HTTPS", "staging"), "n")
        self.assertEqual(self.setting("PROD_ENVIRONMENT", "staging"), "Staging")

    def test_international_name_survives_any_locale(self):
        self.published("tømrer.example.test", "App")
        result = subprocess.run(
            [BASH, str(self.apache / "adopt_site.sh"), "tømrer.example.test", "dev.example.test"], capture_output=True, text=True,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "LC_ALL": "C"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PROD_FOLDER='tømrer.example.test'", (self.scripts / "targets/production/publish_prod.sh").read_text())

    def test_other_dll_is_refused(self):
        self.published("other.example.test", "Other")
        result = self.adopt("other.example.test", "dev.example.test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would break the site", result.stderr)
        self.assertFalse((self.scripts / "targets").exists())

    def test_assembly_name_in_the_project_counts(self):
        self.published("renamed.example.test", "Renamed")
        (self.www / "dev.example.test/App.csproj").write_text("<Project><PropertyGroup><AssemblyName>Renamed</AssemblyName></PropertyGroup></Project>\n")
        self.assertEqual(self.adopt("renamed.example.test", "dev.example.test").returncode, 0)

    def test_main_sites_and_invalid_input_are_refused(self):
        self.main("second.example.test", "App")
        for args, message in ((("second.example.test", "dev.example.test"), "has its own project"),
                              (("dev.example.test", "dev.example.test"), "under itself"),
                              (("missing.example.test", "dev.example.test"), "not found")):
            with self.subTest(args):
                result = self.adopt(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_move_out_restores_everything(self):
        self.assertEqual(self.adopt("example.test", "dev.example.test").returncode, 0)
        result = self.adopt("--release", "dev.example.test", "production")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.scripts / "targets/production").exists())
        self.assertEqual((self.scripts / "publish.sh").read_text(), "# local publish\n")
        self.assertTrue((self.www / "example.test/App.dll").exists())

    def test_move_out_refuses_an_environment_inside_the_main_site(self):
        folder = self.scripts / "targets/staging"
        folder.mkdir(parents=True)
        (folder / "publish_prod.sh").write_text(f"PROD_LOCAL_PATH='{self.www}/dev.example.test/_staging'\nPROD_FOLDER=s.test\nPROD_BINDING=s.test\n")
        result = self.adopt("--release", "dev.example.test", "staging")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(folder.exists())


@NEEDS_BASH_4
class MoveEnvironmentTests(AdoptSiteTests):
    """Move a local environment from a copy of the apache folder, with the system folders in a temporary folder."""

    def setUp(self):
        super().setUp()
        move = (APACHE / "move_env.sh").read_text().replace('[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"\n', "")
        move = move.replace("/etc/", f"{self.etc}/").replace("_rootsFolder=/var/lib/vns-roots", f"_rootsFolder={self.root}/roots")
        (self.apache / "move_env.sh").write_text(move)
        (self.root / "server").mkdir()
        for command in ("systemctl", "a2ensite", "a2dissite", "a2enmod"):
            executable(self.bin / command, f'#!/bin/bash\necho "{command} $*" >>"$FAKE_LOG"\n')
        executable(self.bin / "apache2ctl", '#!/bin/bash\necho "apache2ctl $*" >>"$FAKE_LOG"\nexit ${FAIL_APACHE:-0}\n')
        executable(self.bin / "rclone", FAKE_RCLONE)
        executable(self.bin / "curl", FAKE_CURL)
        executable(self.bin / "sleep", "#!/bin/bash\n")
        executable(self.bin / "dotnet", FAKE_DOTNET)
        executable(self.bin / "sudo", SUDO_REMOVING_ITSELF)
        # An environment made with Add environment, in a subfolder of the main site
        self.old = self.www / "dev.example.test/_prod"
        (self.old / "wwwroot/media").mkdir(parents=True)
        (self.old / "umbraco/Data").mkdir(parents=True)
        (self.old / "umbraco/Data/Umbraco.sqlite.db").write_text("data")
        (self.old / "appsettings.json").write_text("{}")
        (self.old / "App.dll").write_text("linux build")
        (self.root / "roots/prod-site").mkdir(parents=True)
        (self.etc / "systemd/system/prod-site.service").write_text(
            f"[Unit]\nDescription=Running .NET Core [prod-site]\n[Service]\nWorkingDirectory={self.old}\n"
            f"ExecStart=/usr/lib/dotnet/dotnet {self.old}/App.dll --urls \"http://localhost:4100\"\nSyslogIdentifier=prod-site\n"
            f"RootDirectory=/var/lib/vns-roots/prod-site\nBindPaths={self.old}\nReadWritePaths={self.old}\n")
        (self.etc / "apache2/sites-available/prod-site.conf").write_text(
            f"<VirtualHost *:80>\n  ServerName prod-site.example.test\n  DocumentRoot {self.old}/wwwroot\n"
            "  ErrorLog /var/log/apache2/prod-site-80-error.log\n</VirtualHost>\n")
        self.target = self.scripts / "targets/production"
        self.target.mkdir(parents=True)
        (self.target / "publish_prod.sh").write_text(
            f"PROD_LOCAL_PATH='{self.old}'   # comment\nPROD_FOLDER='prod-site'   # comment\nPROD_BINDING='prod-site.example.test'\n")
        (self.target / "sync_prod.sh").write_text(f"PROD_LOCAL_PATH='{self.old}'\nPROD_FOLDER='prod-site'\n")
        (self.scripts / "publish_local.sh").write_text("# local\n")
        self.scripts.joinpath("publish.sh").unlink()
        self.log.write_text("")

    # These belong to the parent class and need a site that has not been moved
    for _name in [name for name in dir(AdoptSiteTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def move(self, **env):
        return subprocess.run(
            [BASH, str(self.apache / "move_env.sh"), "dev.example.test", "production"], capture_output=True, text=True,
            env=os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                              "FAKE_SERVER": str(self.root / "server"), "VNS_PUBLISH_TRACKED": "prod"} | env)

    def windows(self, **env):
        return self.move(VNS_PROD_TARGET="windows", VNS_PROD_SMB_SHARE="//10.0.0.1/share$/sites/prod.test", VNS_PROD_SMB_USER="Administrator",
                         VNS_PROD_SMB_PASSWORD="secret", VNS_PROD_SMB_DOMAIN="VNS", VNS_PROD_FOLDER="prod-site.example.test",
                         VNS_PROD_BINDING="prod-site.example.test", VNS_PROD_ENVIRONMENT="Production", VNS_PROD_HTTPS="y", **env)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_move_to_a_folder_of_its_own(self):
        new = self.www / "subsite"
        result = self.move(VNS_PROD_TARGET="local", VNS_PROD_LOCAL_PATH=str(new))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.old.exists())
        self.assertEqual((new / "umbraco/Data/Umbraco.sqlite.db").read_text(), "data")
        service = (self.etc / "systemd/system/subsite.service").read_text()
        self.assertNotIn(str(self.old), service)
        self.assertIn(f"WorkingDirectory={new}\n", service)
        self.assertIn(f"{new}/App.dll", service)
        self.assertIn("localhost:4100", service)
        self.assertIn("SyslogIdentifier=subsite\n", service)
        self.assertIn("RootDirectory=/var/lib/vns-roots/subsite\n", service)
        conf = (self.etc / "apache2/sites-available/subsite.conf").read_text()
        self.assertIn("ServerName prod-site.example.test", conf, "The binding must not change")
        self.assertIn(f"DocumentRoot {new}/wwwroot", conf)
        self.assertIn("/var/log/apache2/subsite-80-error.log", conf)
        self.assertFalse((self.etc / "systemd/system/prod-site.service").exists())
        self.assertFalse((self.etc / "apache2/sites-available/prod-site.conf").exists())
        self.assertTrue((self.root / "roots/subsite").is_dir())
        settings = (self.target / "publish_prod.sh").read_text()
        self.assertIn(f"PROD_LOCAL_PATH='{new}'", settings)
        self.assertIn("PROD_FOLDER='subsite'", settings)
        self.assertIn("PROD_BINDING='prod-site.example.test'", settings)
        self.assertIn(f"PROD_LOCAL_PATH='{new}'", (self.target / "sync_prod.sh").read_text())
        calls = self.calls()
        self.assertLess(calls.index("systemctl stop prod-site"), calls.index("systemctl start subsite"))
        self.assertFalse(any(call.startswith("dotnet") for call in calls))

    def test_failed_local_move_restores_everything(self):
        new = self.www / "subsite"
        service = (self.etc / "systemd/system/prod-site.service").read_text()
        settings = (self.target / "publish_prod.sh").read_text()
        result = self.move(VNS_PROD_TARGET="local", VNS_PROD_LOCAL_PATH=str(new), FAIL_APACHE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restoring", result.stderr)
        self.assertEqual((self.old / "App.dll").read_text(), "linux build")
        self.assertFalse(new.exists())
        self.assertEqual((self.etc / "systemd/system/prod-site.service").read_text(), service)
        self.assertTrue((self.etc / "apache2/sites-available/prod-site.conf").exists())
        self.assertFalse((self.etc / "systemd/system/subsite.service").exists())
        self.assertFalse((self.etc / "apache2/sites-available/subsite.conf").exists())
        self.assertEqual((self.target / "publish_prod.sh").read_text(), settings)
        self.assertEqual(self.calls()[-1], "systemctl start prod-site")

    def test_existing_or_nested_destination_is_refused(self):
        (self.www / "taken").mkdir()
        for path, message in ((self.www / "taken", "already exists"), (self.www / "a/b", "directly|must be")):
            with self.subTest(path):
                result = self.move(VNS_PROD_TARGET="local", VNS_PROD_LOCAL_PATH=str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, message)
                self.assertTrue(self.old.exists())
                self.assertNotIn("systemctl stop prod-site", self.calls())

    def test_move_to_windows_copies_content_and_publishes(self):
        result = self.windows()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        copies = [call for call in calls if call.startswith("copy /") and "/_prod" in call.split()[1]]
        self.assertTrue(any("/_prod/umbraco " in call for call in copies))
        self.assertTrue(any("/_prod/wwwroot " in call for call in copies))
        self.assertTrue(any("appsettings*.json" in call for call in copies))
        self.assertFalse(any(call.split()[1].endswith("/_prod") and "appsettings" not in call for call in copies), "The Linux build must not be copied")
        self.assertLess(calls.index("systemctl stop prod-site"), self.index_of(calls, "copy /"))
        self.assertLess(self.index_of(calls, "dotnet publish"), calls.index("systemctl disable prod-site"))
        self.assertTrue((self.root / "server/web.config").exists())
        self.assertFalse((self.etc / "systemd/system/prod-site.service").exists())
        self.assertTrue(self.old.exists(), "The local files are kept unless asked")
        self.assertFalse((self.scripts / "targets/production-moving").exists())
        final = (self.target / "publish_prod.sh").read_text()
        self.assertIn("PROD_TARGET=windows", final)
        self.assertNotIn("secret", result.stdout + result.stderr)

    @staticmethod
    def index_of(calls, prefix):
        return next(number for number, call in enumerate(calls) if call.startswith(prefix))

    def test_local_files_are_deleted_only_when_asked(self):
        self.assertEqual(self.windows(VNS_MOVE_DELETE_LOCAL="y").returncode, 0)
        self.assertFalse(self.old.exists())

    def test_failed_publish_keeps_the_environment_running_here(self):
        executable(self.bin / "dotnet", "#!/bin/bash\nexit 1\n")
        before = (self.target / "publish_prod.sh").read_text()
        result = self.windows()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("keeps running", result.stderr)
        self.assertEqual((self.target / "publish_prod.sh").read_text(), before)
        self.assertFalse((self.scripts / "targets/production-moving").exists())
        self.assertTrue((self.etc / "systemd/system/prod-site.service").exists())
        self.assertTrue((self.etc / "apache2/sites-available/prod-site.conf").exists())
        self.assertEqual(self.calls()[-1], "systemctl start prod-site")

    def test_wrong_login_never_stops_the_site(self):
        result = self.windows(FAKE_LOGIN_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("systemctl stop prod-site", self.calls())

    def test_failed_site_check_during_a_move_is_only_a_warning(self):
        result = self.windows(FAKE_HTTP_STATUS="503")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING", result.stdout)


# Runs what would run on the server, with its system folders moved into $FAKE_REMOTE
FAKE_SSH = r"""#!/bin/bash
printf 'ssh %s\n' "${@: -1}" >>"$FAKE_LOG"
[[ -z ${FAKE_SSH_FAILS:-} ]] || exit 255
moved() { sed "s#/var/www#$FAKE_REMOTE/www#g; s#/etc/#$FAKE_REMOTE/etc/#g; s#/var/lib/#$FAKE_REMOTE/lib/#g; s#/vns/apache#$FAKE_REMOTE/vns#g"; }
command=$(printf '%s' "${@: -1}" | moved)
if [[ $command == *"bash -s"* ]]; then
	moved | eval "$command"
else
	eval "$command"
fi
"""


@NEEDS_BASH_4
class RemoteEnvironmentTests(AdoptSiteTests):
    for _name in [name for name in dir(AdoptSiteTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        for script in ("register_env.sh", "copy_env.sh"):
            text = (APACHE / script).read_text()
            (self.apache / script).write_text(re.sub(r"(?m)^\[\[ \$EUID -eq 0 \]\] \|\| exec sudo .*$", "", text))
        adopt = self.apache / "adopt_site.sh"
        executable(self.bin / "ssh", FAKE_SSH)
        self.remote = self.root / "remote"
        for folder in ("www", "etc/systemd/system", "etc/apache2/sites-available", "lib", "vns"):
            (self.remote / folder).mkdir(parents=True)
        self.remote_site("copy.example.test", "App")
        self.env = os.environ | {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "FAKE_REMOTE": str(self.remote)}
        self.settings = dict(VNS_PROD_IP="server.test", VNS_PROD_USER="root", VNS_PROD_PASSWORD="", VNS_PROD_SSH_PORT="22", VNS_PROD_FOLDER="copy.example.test")

    def remote_site(self, name, dll, working=None, https=True, environment="Staging"):
        folder = self.remote / "www" / name
        folder.mkdir(parents=True)
        (self.remote / f"etc/systemd/system/{service_name(name)}.service").write_text(
            f"[Service]\nWorkingDirectory={working or '/var/www/' + name}\nExecStart=/usr/lib/dotnet/dotnet /var/www/{name}/{dll}.dll\n"
            f"Environment=ASPNETCORE_ENVIRONMENT={environment}\n")
        ssl = "  SSLEngine On\n" if https else ""
        (self.remote / f"etc/apache2/sites-available/{name}.conf").write_text(f"<VirtualHost *:80>\n  ServerName {name}\n{ssl}</VirtualHost>\n")

    def register(self, **values):
        return subprocess.run([BASH, str(self.apache / "register_env.sh"), "dev.example.test"], capture_output=True, text=True,
                              env=self.env | self.settings | values)

    def test_existing_site_is_added_without_publishing(self):
        result = self.register()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setting("PROD_IP", "staging"), "server.test")
        self.assertEqual(self.setting("PROD_FOLDER", "staging"), "copy.example.test")
        self.assertEqual(self.setting("PROD_BINDING", "staging"), "copy.example.test")
        self.assertEqual(self.setting("PROD_HTTPS", "staging"), "y")
        self.assertEqual(self.setting("PROD_ENVIRONMENT", "staging"), "Staging")
        self.assertEqual((self.scripts / "publish_local.sh").read_text(), "# local publish\n")
        self.assertNotIn("DOTNET", self.log.read_text())
        self.assertEqual((self.remote / "lib/vns-managed/copy.example.test").read_text().splitlines()[0], "dev.example.test")

    def test_sites_that_do_not_fit_are_refused(self):
        self.remote_site("other.example.test", "Other")
        self.remote_site("nested.example.test", "App", working="/var/www/nested.example.test/_publish")
        self.remote_site("main.example.test", "App")
        (self.remote / "www/main.example.test/App.csproj").touch()
        self.remote_site("taken.example.test", "App")
        (self.remote / "lib/vns-managed").mkdir()
        (self.remote / "lib/vns-managed/taken.example.test").write_text("other.main\nother.host\n")
        for folder, message in (("other.example.test", "would break the site"), ("nested.example.test", "must run from"),
                                ("main.example.test", "own project"), ("taken.example.test", "already an environment"),
                                ("missing.example.test", "was not found")):
            with self.subTest(folder):
                result = self.register(VNS_PROD_FOLDER=folder)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.scripts / "targets").exists())

    def test_unreachable_server_changes_nothing(self):
        result = self.register(FAKE_SSH_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.scripts / "publish.sh").exists())
        self.assertFalse((self.scripts / "targets").exists())

    def environment(self):
        self.assertEqual(self.register().returncode, 0)

    def test_move_out_changes_nothing_when_the_server_cannot_be_reached(self):
        self.environment()
        result = subprocess.run([BASH, str(self.apache / "adopt_site.sh"), "--release", "dev.example.test", "staging"], capture_output=True, text=True,
                                env=self.env | {"FAKE_SSH_FAILS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.scripts / "targets/staging").exists())

    def test_move_out_removes_the_note(self):
        self.environment()
        note = self.remote / "lib/vns-managed/copy.example.test"
        self.assertTrue(note.exists())
        result = subprocess.run([BASH, str(self.apache / "adopt_site.sh"), "--release", "dev.example.test", "staging"], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(note.exists())
        self.assertFalse((self.scripts / "targets/staging").exists())
        self.assertTrue((self.remote / "www/copy.example.test").exists())

    def test_copy_runs_on_the_server_and_is_added_under_the_main_site(self):
        self.environment()
        (self.remote / "vns/copy_site.sh").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            echo "copy_site $* managed=$VNS_COPY_MANAGED sql=$VNS_COPY_SQL" >>"{self.log}"
            mkdir -p "{self.remote}/www/$2"
            printf '[Service]\\nWorkingDirectory=/var/www/%s\\nExecStart=/usr/lib/dotnet/dotnet /var/www/%s/App.dll\\nEnvironment=ASPNETCORE_ENVIRONMENT=Production\\n' "$2" "$2" >"{self.remote}/etc/systemd/system/$2.service"
            printf 'ServerName %s\\n' "$3" >"{self.remote}/etc/apache2/sites-available/$2.conf"
        """))
        result = subprocess.run(
            [BASH, str(self.apache / "copy_env.sh"), "dev.example.test", "copy.example.test", "kunde.example.test", "kunde.vns.dk"],
            capture_output=True, text=True, env=self.env | {"VNS_COPY_REMOTE": "1", "VNS_TARGET": "staging", "VNS_COPY_SQL": "pa$$ word"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.log.read_text()
        self.assertIn("copy_site copy.example.test kunde.example.test kunde.vns.dk managed=1 sql=pa$$ word", log)
        self.assertNotIn("pa$$", "".join(line for line in log.splitlines() if line.startswith("ssh ")), "The password stays out of the command line")
        self.assertEqual(self.setting("PROD_FOLDER", "production"), "kunde.example.test")
        self.assertEqual(self.setting("PROD_BINDING", "production"), "kunde.vns.dk")
        self.assertTrue((self.remote / "lib/vns-managed/kunde.example.test").exists())


class ManagedSiteTests(unittest.TestCase):
    def test_note_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = add_target_helpers({"WEB_ROOT": directory})
            ns["MANAGED_DIR"] = directory
            self.assertIsNone(ns["managed_by"]("site.test"))
            Path(directory, "site.test").write_text("dev.test\nlinux01.test\n")
            self.assertEqual(ns["managed_by"]("site.test"), {"site": "dev.test", "host": "linux01.test"})

    def test_candidates_skip_a_managed_site(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = add_target_helpers({"WEB_ROOT": directory})
            Path(directory, "dev.test").mkdir()
            Path(directory, "dev.test/App.csproj").write_text("<Project />")
            ns.update(service_dll=lambda name: "App", read_text=lambda path: "")
            sites = [{"name": "dev.test", "has_project": True, "kind": "umbraco", "environments": [{}]},
                     {"name": "live.test", "has_project": False, "kind": "umbraco", "environments": [{}], "managed_by": {"site": "x", "host": "y"}}]
            ns["adoption_candidates"](sites)
            self.assertNotIn("adopt_into", sites[1])

    def test_copy_site_refuses_a_managed_site_unless_the_main_server_asks(self):
        source = (APACHE / "copy_site.sh").read_text()
        check = 'if [[ -z ${VNS_COPY_MANAGED:-} && -f /var/lib/vns-managed/${_source} ]]; then'
        self.assertIn(check, source)
        self.assertLess(source.index(check), source.index("python3 lib/copy_site.py"))
        app = (APACHE / "solutionfiles/admin/app/app.py").read_text()
        self.assertIn("Make the copy from there", app)
        self.assertIn('if (!site.managed_by) add("Copy site"', (APACHE / "solutionfiles/admin/app/static/app.js").read_text())

    def test_status_writes_the_note_and_delete_removes_it(self):
        self.assertIn('Path("/var/lib/vns-managed", site)', (APACHE / "remote_site.sh").read_text())
        self.assertEqual((APACHE / "delete_site.sh").read_text().count("/var/lib/vns-managed/"), 2)


class LocalFolderRuleTests(ProductionSetupTests):
    for _name in [name for name in dir(ProductionSetupTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def local(self, path, **values):
        return dict(target="local", folder="subsite", binding="subsite.test", local_path=str(path)) | values

    def test_folder_of_its_own_is_allowed_with_the_same_name(self):
        web = Path(self.temp.name).resolve()
        result = self.setup_env(self.local(web / "subsite"))
        self.assertEqual(result["VNS_PROD_LOCAL_PATH"], str(web / "subsite"))

    def test_subfolder_of_the_site_is_still_allowed(self):
        self.assertTrue(self.setup_env(self.local(self.scripts.parent / "_prod"))["VNS_PROD_LOCAL_PATH"].endswith("/_prod"))

    def test_other_folders_are_refused(self):
        web = Path(self.temp.name).resolve()
        (web / "taken").mkdir()
        for path in (web / "other-name", web / "taken", web / "a/b", "/tmp/subsite", self.scripts.parent, "relative"):
            values = {"folder": "taken"} if path == web / "taken" else {}
            with self.subTest(path), self.assertRaises(BadRequest):
                self.setup_env(self.local(path, **values))

    def test_moving_needs_a_new_folder_outside_the_site_and_takes_its_name(self):
        web = Path(self.temp.name).resolve()
        result = self.ns["production_setup_env"](self.local(web / "new.site", folder="old"), "example.test", moving=True)
        self.assertEqual(result["VNS_PROD_FOLDER"], "new.site")
        with self.assertRaises(BadRequest):
            self.ns["production_setup_env"](self.local(self.scripts.parent / "_other"), "example.test", moving=True)


class CopyEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.log"
        shutil.copy(APACHE / "copy_env.sh", self.root)
        text = (self.root / "copy_env.sh").read_text()
        (self.root / "copy_env.sh").write_text(re.sub(r"(?m)^\[\[ \$EUID -eq 0 \]\] \|\| exec sudo .*$", "", text))

    def stub(self, name, status=0):
        (self.root / name).write_text(f'#!/bin/bash\necho "{name} $*" >>"{self.log}"\nexit {status}\n')

    def run_copy(self, *args):
        return subprocess.run([BASH, str(self.root / "copy_env.sh"), *args], capture_output=True, text=True)

    def test_copy_then_register_under_the_main_site(self):
        self.stub("copy_site.sh")
        self.stub("adopt_site.sh")
        result = self.run_copy("dev.test", "website.test", "new.test", "new.example.test", "newdb")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), [
            "copy_site.sh website.test new.test new.example.test newdb", "adopt_site.sh new.test dev.test"])

    def test_failed_copy_registers_nothing(self):
        self.stub("copy_site.sh", 1)
        self.stub("adopt_site.sh")
        self.assertNotEqual(self.run_copy("dev.test", "website.test", "new.test", "new.example.test").returncode, 0)
        self.assertNotIn("adopt_site.sh", self.log.read_text())

    def test_failed_registration_says_the_copy_is_a_separate_site(self):
        self.stub("copy_site.sh")
        self.stub("adopt_site.sh", 1)
        result = self.run_copy("dev.test", "website.test", "new.test", "new.example.test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate site", result.stderr)


class AdoptionListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.web = Path(self.temp.name) / "www"
        self.services = Path(self.temp.name) / "systemd"
        self.services.mkdir()
        (self.web / "dev.test/scripts/targets/production").mkdir(parents=True)
        (self.web / "dev.test/App.csproj").write_text("<Project />")
        (self.web / "live.test").mkdir()
        (self.services / "live.test.service").write_text(f"[Service]\nExecStart=/usr/lib/dotnet/dotnet {self.web}/live.test/App.dll\n")
        source = (APACHE / "solutionfiles/admin/app/app.py").read_text()
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in ("parse_prod", "read_text")]
        nodes += [node for node in ast.parse(source).body if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PROD_KEYS"]
        self.ns = {"os": os, "re": re, "shlex": shlex, "WEB_ROOT": os.path.realpath(self.web), "SERVICE_DIR": str(self.services), "service_name": service_name}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "listing", "exec"), self.ns)
        add_target_helpers(self.ns)

    def site(self, name, has_project=False, environments=1):
        return {"name": name, "has_project": has_project, "kind": "umbraco", "environments": [{}] * environments}

    def test_candidates_must_build_the_same_dll(self):
        sites = [self.site("dev.test", True), self.site("live.test")]
        self.ns["adoption_candidates"](sites)
        self.assertEqual(sites[1]["adopt_into"], ["dev.test"])
        self.assertNotIn("adopt_into", sites[0])
        (self.services / "live.test.service").write_text("[Service]\nExecStart=/usr/lib/dotnet/dotnet /x/Other.dll\n")
        self.ns["adoption_candidates"](sites)
        self.assertEqual(sites[1]["adopt_into"], [])

    def test_moved_site_is_hidden_but_folders_inside_the_main_site_are_not(self):
        script = self.web / "dev.test/scripts/targets/production/publish_prod.sh"
        script.write_text(f"PROD_LOCAL_PATH='{self.ns['WEB_ROOT']}/live.test'\nPROD_FOLDER=live.test\n")
        self.assertEqual(self.ns["adopted_sites"](["dev.test", "live.test"]), {"live.test"})
        script.write_text(f"PROD_LOCAL_PATH='{self.ns['WEB_ROOT']}/dev.test/_production'\nPROD_FOLDER=p.test\n")
        self.assertEqual(self.ns["adopted_sites"](["dev.test", "live.test"]), set())


class TargetListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.web = Path(self.temp.name)
        self.scripts = self.web / "example.test/scripts"
        (self.scripts / "targets").mkdir(parents=True)
        self.ns = add_target_helpers({"WEB_ROOT": str(self.web)})
        self.root = str(self.web / "example.test")

    def add(self, name):
        folder = self.scripts / "targets" / name
        folder.mkdir()
        (folder / "publish_prod.sh").touch()

    def test_old_single_environment_comes_first(self):
        self.add("staging")
        self.add("production")
        (self.scripts / "publish_prod.sh").touch()
        self.assertEqual(self.ns["target_ids"](self.root), ["prod", "production", "staging"])

    def test_invalid_folders_are_ignored(self):
        self.add("Bad Name")
        (self.scripts / "targets/empty").mkdir()
        self.assertEqual(self.ns["target_ids"](self.root), [])

    def test_target_arg_refuses_unknown_and_defaults_to_the_old_one(self):
        self.add("staging")
        self.assertEqual(self.ns["target_arg"]("example.test", {"target": "staging"}), "staging")
        for target in ("missing", "../x", 5):
            with self.subTest(target), self.assertRaises(BadRequest):
                self.ns["target_arg"]("example.test", {"target": target})
        with self.assertRaises(BadRequest):
            self.ns["target_arg"]("example.test", {})

    def test_with_target_only_changes_named_environments(self):
        self.assertEqual(self.ns["with_target"](["a"], "prod"), ["a"])
        self.assertEqual(self.ns["with_target"](["a"], "staging"), ["/usr/bin/env", "VNS_TARGET=staging", "a"])

    def test_scope(self):
        self.assertEqual(self.ns["target_scope"]("prod"), "prod")
        self.assertEqual(self.ns["target_scope"]("staging"), "target-staging")

    def test_publish_status_scope_of_a_script(self):
        from apache.lib import publish_status
        self.assertEqual(publish_status.script_scope("/s/scripts/publish_local.sh"), "test")
        self.assertEqual(publish_status.script_scope("/s/scripts/publish_prod.sh"), "prod")
        self.assertEqual(publish_status.script_scope("/s/scripts/targets/staging-2/publish_prod.sh"), "target-staging-2")


class WindowsUpdaterTests(unittest.TestCase):
    def test_script_updater_leaves_windows_scripts_alone(self):
        source = (APACHE / "update_admin_site.sh").read_text()
        updater = re.search(r"<<'PY'\n(.*?)\nPY\n", source, re.S).group(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apache/lib").mkdir(parents=True)
            (root / "apache/lib/domain_names.py").write_text((APACHE / "lib/domain_names.py").read_text())
            scripts = root / "www/example.test/scripts"
            scripts.mkdir(parents=True)
            (scripts.parent / "App.csproj").write_text("<Project />\n")
            text = render_template(WINDOWS_TEMPLATES / "publish_prod.sh", {"APACHE_DIR": "/vns/apache"})
            (scripts / "publish_prod.sh").write_text(text)
            (scripts / "targets/staging").mkdir(parents=True)
            (scripts / "targets/staging/publish_prod.sh").write_text(text)
            result = subprocess.run([sys.executable, "-c", updater, str(root / "www"), str(root / "apache")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((scripts / "publish_prod.sh").read_text(), text)
            self.assertEqual((scripts / "targets/staging/publish_prod.sh").read_text(), text)


if __name__ == "__main__":
    unittest.main()
