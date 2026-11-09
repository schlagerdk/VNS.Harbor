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
import unittest
import xml.etree.ElementTree as ET

from flask import abort
from werkzeug.exceptions import BadRequest

from apache.lib.domain_names import normalize_dns_name, normalize_site_name, service_name
from apache.lib import windows_prod

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
        self.assertIn("wwwroot/media: SKIPPED", result.stdout)

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
        self.assertIn("VNS_PUBLISH_TRACKED:-} != prod", (WINDOWS_TEMPLATES / "publish_prod.sh").read_text())

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

    def setting(self, name, script="publish_prod.sh"):
        lines = [line for line in (self.scripts / script).read_text().splitlines() if line.startswith("PROD_")]
        code = "\n".join(lines) + f'\nprintf %s "${{{name}}}"'
        return subprocess.run([BASH, "-c", code], capture_output=True, text=True, check=True).stdout

    def test_windows_setup_publishes_and_syncs(self):
        result = self.windows()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.scripts / "publish_local.sh").read_text(), "# local publish\n")
        self.assertEqual((self.scripts / "clear_logs_local.sh").read_text(), "# local logs\n")
        for name in ("publish_prod.sh", "sync_prod.sh", "clear_logs_prod.sh"):
            path = self.scripts / name
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
        self.assertFalse((self.scripts / "publish_prod.sh").exists())
        self.assertFalse(any(call.startswith("dotnet") for call in self.calls()))

    def test_empty_project_gets_no_umbraco_scripts(self):
        self.assertEqual(self.windows(kind="empty").returncode, 0)
        self.assertFalse((self.scripts / "sync_prod.sh").exists())
        self.assertFalse((self.scripts / "clear_logs_prod.sh").exists())

    def test_share_without_a_folder_is_refused(self):
        result = self.create(VNS_PROD_TARGET="windows", VNS_PROD_SMB_SHARE="//10.0.0.1/share$", VNS_PROD_SMB_USER="a", VNS_PROD_SMB_PASSWORD="b")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.scripts / "publish_prod.sh").exists())

    def test_linux_targets_save_the_protocol_and_use_the_linux_templates(self):
        executable(self.bin / "dotnet", "#!/bin/bash\nexit 1\n")
        executable(self.bin / "ssh", "#!/bin/bash\necho x86_64\n")
        self.create(VNS_PROD_TARGET="remote", VNS_PROD_IP="server.test", VNS_PROD_USER="root", VNS_PROD_SSH_PORT="22", VNS_PROD_HTTPS="n")
        self.assertEqual(self.setting("PROD_HTTPS"), "n")
        text = (self.scripts / "publish_prod.sh").read_text()
        self.assertNotIn("PROD_TARGET=windows", text)
        self.assertIn("HTTPS='${PROD_HTTPS}'", text)
        self.assertIn("rsync", (self.scripts / "sync_prod.sh").read_text())


@NEEDS_BASH_4
class WindowsActionTests(ScriptTestCase):
    """Run the actions from a copy of the apache folder, with its site folder in a temporary folder."""

    def setUp(self):
        super().setUp()
        self.apache = self.root / "apache"
        shutil.copytree(APACHE / "lib", self.apache / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for script in ("prod_action.sh", "change_binding_prod.sh", "remote_site.sh", "delete_site.sh", "fix_permissions.sh"):
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
            result = subprocess.run([sys.executable, "-c", updater, str(root / "www"), str(root / "apache")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((scripts / "publish_prod.sh").read_text(), text)


if __name__ == "__main__":
    unittest.main()
