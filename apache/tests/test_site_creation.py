import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from flask import Flask, abort, jsonify
from werkzeug.exceptions import BadRequest

from apache.lib.domain_names import display_dns_name, normalize_dns_name, normalize_site_name, service_name

APACHE = Path(__file__).resolve().parents[1]
APP = APACHE / "solutionfiles/admin/app"
CREATE = (APACHE / "create_site.sh").read_text()


class DotnetTemplateTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.runner = Mock()
        self.ns = dict(app=self.app, re=re, os=os, jsonify=jsonify,
                       subprocess=Mock(run=self.runner, SubprocessError=subprocess.SubprocessError),
                       dotnet_root_dir=lambda: "/usr/share/dotnet",
                       error=lambda message, status: (jsonify(error=message), status))
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("parse_dotnet_templates", "api_dotnet_templates")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "dotnet templates", "exec"), self.ns)
        columns = (42, 20, 18, 30)
        rows = [
            ("Template Name", "Short Name", "Language", "Tags"),
            tuple("-" * width for width in columns),
            ("ASP.NET Core Empty", "web", "[C#],F#", "Web/Empty"),
            ("ASP.NET Core Web API", "webapi", "[C#],F#", "Web/WebAPI"),
            ("Console App", "console", "[C#],F#,VB", "Common/Console"),
            ("Custom Template", "custom,alias", "C#", "Custom"),
        ]
        self.output = "Installed templates:\n\n" + "\n".join(
            "  ".join(value.ljust(width) for value, width in zip(row, columns)).rstrip() for row in rows) + "\n"

    def test_lists_installed_templates_and_aliases(self):
        self.runner.return_value = Mock(returncode=0, stdout=self.output)
        response = self.app.test_client().get("/api/dotnet/templates")
        self.assertEqual(response.status_code, 200)
        templates = response.json["templates"]
        self.assertEqual([item["short_name"] for item in templates], ["web", "webapi", "console", "custom", "alias"])
        self.assertEqual(templates[0], dict(name="ASP.NET Core Empty", short_name="web",
                                            language="[C#],F#", tags="Web/Empty"))
        args, kwargs = self.runner.call_args
        self.assertEqual(args[0], ["dotnet", "new", "list"])
        self.assertEqual(kwargs["env"]["HOME"], "/root")
        self.assertEqual(kwargs["env"]["DOTNET_ROOT"], "/usr/share/dotnet")
        self.assertEqual(kwargs["env"]["DOTNET_CLI_UI_LANGUAGE"], "en-US")
        self.assertEqual(kwargs["timeout"], 60)

    def test_failures_are_reported_instead_of_empty_success(self):
        for result in (Mock(returncode=1, stdout="", stderr="SDK missing"),
                       Mock(returncode=0, stdout="Unexpected output")):
            self.runner.return_value = result
            response = self.app.test_client().get("/api/dotnet/templates")
            self.assertEqual(response.status_code, 502)
            self.assertIn("error", response.json)
        for exc in (FileNotFoundError("dotnet"), subprocess.TimeoutExpired("dotnet", 60)):
            self.runner.side_effect = exc
            self.assertEqual(self.app.test_client().get("/api/dotnet/templates").status_code, 502)

    def test_invalid_names_and_empty_tables_are_rejected(self):
        parser = self.ns["parse_dotnet_templates"]
        with self.assertRaises(ValueError):
            parser(self.output.replace("custom,alias", "../bad"))
        with self.assertRaises(ValueError):
            parser("\n".join(self.output.splitlines()[:4]))


def shell_function(name):
    return re.search(r"^" + name + r"\(\) \{.*?^\}", CREATE, re.M | re.S).group()


class CreateCommandTests(unittest.TestCase):
    def setUp(self):
        source = ast.parse((APP / "app.py").read_text())
        nodes = [node for node in source.body if
                 isinstance(node, ast.FunctionDef) and node.name in ("build_command", "dns_name") or
                 isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id in
                     ("NAME_RE", "BINDING_RE", "PROJECT_RE", "FOLDER_RE", "VERSION_RE")
                     for target in node.targets)]
        self.ns = {"re": re, "os": os, "abort": abort, "APACHE_DIR": str(APACHE),
                   "site_dirs": lambda: [], "_normalize_dns_name": normalize_dns_name,
                   "_normalize_site_name": normalize_site_name, "service_name": service_name}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "create command", "exec"), self.ns)
        self.data = dict(kind="create_site", site="example.test", binding="example.test",
                         type="csharp", csharp="empty", certificate="n")

    def command(self, **changes):
        return self.ns["build_command"](self.data | changes)[1]

    def test_custom_template_is_a_separate_argument(self):
        args = self.command(template="webapi", version="18.3.1")
        self.assertEqual(args[2:], ["example.test", "csharp", "empty", "",
                                   "example.test", "example.test", "_publish", "Debug", "n", "webapi", "Test"])

    def test_blank_template_preserves_default(self):
        self.assertEqual(self.command(template="  ")[-2], "")

    def test_umbraco_ignores_hidden_template(self):
        self.assertEqual(self.command(csharp="umbraco", version="13.3.2", template="bad name")[-2], "")

    def test_environment_is_independent_of_build(self):
        for build in ("Debug", "Release"):
            for environment in ("Development", "Test", "Staging", "Production"):
                with self.subTest(build=build, environment=environment):
                    self.assertEqual(self.command(build=build, environment=environment)[-1], environment)
        self.assertEqual(self.command(build="Release")[-1], "Production")
        with self.assertRaises(BadRequest):
            self.command(environment="invalid")

    def test_invalid_templates_are_rejected(self):
        for value in ("--help", "../web", "web api", "web;echo", "web\napi"):
            with self.subTest(value=value), self.assertRaises(BadRequest):
                self.command(template=value)

    def test_html_command_is_unchanged(self):
        self.assertEqual(self.command(type="html")[-1], "n")

    def test_danish_names_are_converted_before_creating_every_site_type(self):
        for site_type in ("html", "python", "csharp"):
            with self.subTest(site_type=site_type):
                args = self.command(site="æøå.dk", binding="ÆØÅ.dk", type=site_type)
                self.assertEqual(args[2], "æøå.dk")
                self.assertEqual(args[7], "xn--5cab8c.dk")
                if site_type == "csharp":
                    self.assertEqual(args[6], "xn--5cab8c.dk")
        self.assertEqual(self.command(site="æøå.dk", project="æøå.dk")[6], "xn--5cab8c.dk")
        self.assertEqual(self.command(site="æøå.dk", project="Example")[6], "Example")

    def test_idn_duplicates_and_invalid_inputs_are_rejected(self):
        self.ns["site_dirs"] = lambda: ["xn--5cab8c.dk"]
        with self.assertRaisesRegex(BadRequest, "already exists"):
            self.command(site="æøå.dk")
        for field in ("site", "binding"):
            for value in (None, 42, "../æøå.dk", "https://æøå.dk", "æøå.dk\n",
                          "æ" * 60 + ".dk"):
                with self.subTest(field=field, value=value), self.assertRaises(BadRequest):
                    self.command(**{field: value})


class DomainNameTests(unittest.TestCase):
    def test_unicode_site_discovery_and_service_lookup(self):
        source = ast.parse((APP / "app.py").read_text())
        nodes = [node for node in source.body if
                 isinstance(node, ast.FunctionDef) and node.name in (
                     "site_dirs", "unit_of", "service_states", "service_port") or
                 isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "NAME_RE" for target in node.targets)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = "æøå.linux-01.vns.dk"
            (root / name).mkdir()
            (root / "services").mkdir()
            unit = service_name(name)
            (root / "services" / (unit + ".service")).write_text(
                "WorkingDirectory=/var/www/" + name + "\nExecStart=app --urls http://localhost:4100")
            runner = Mock(return_value=Mock(stdout="active\n"))
            ns = {"os": os, "re": re, "WEB_ROOT": directory, "service_name": service_name,
                  "SERVICE_DIR": str(root / "services"), "subprocess": Mock(run=runner),
                  "read_text": lambda path: Path(path).read_text()}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "unicode sites", "exec"), ns)
            self.assertIn(name, ns["site_dirs"]())
            self.assertTrue(ns["unit_of"](name))
            self.assertEqual(ns["service_port"](name), "4100")
            self.assertEqual(ns["service_states"]([name]), {name: "active"})
            self.assertEqual(runner.call_args.args[0], ["systemctl", "is-active", unit + ".service"])

    def test_unicode_folders_are_not_converted_to_punycode(self):
        self.assertEqual(normalize_site_name("æøå.linux-01.vns.dk"), "æøå.linux-01.vns.dk")
        self.assertEqual(normalize_site_name("bla\u030abaer.dk"), "blåbaer.dk")
        self.assertEqual(service_name("æøå.linux-01.vns.dk"), "xn--5cab8c.linux-01.vns.dk")
        for name in ("../æøå", "a_b.dk", "a/b", "a;echo", "a\n", "a..dk"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_site_name(name)

    def test_display_names_preserve_unicode_and_ascii_labels(self):
        for value, expected in (("xn--5cab8c.linux-01.vns.dk", "æøå.linux-01.vns.dk"),
                                ("www.xn--blbr-roah.dk", "www.blåbær.dk"),
                                ("Example.test", "Example.test"), ("æøå.dk", "æøå.dk"), ("", "")):
            with self.subTest(value=value):
                self.assertEqual(display_dns_name(value), expected)
        with self.assertRaises(UnicodeError):
            display_dns_name("xn--.dk")

    def test_idn_canonicalization(self):
        for value, expected in (("æøå.dk", "xn--5cab8c.dk"),
                                ("ÆØÅ.dk", "xn--5cab8c.dk"),
                                ("blåbær.dk", "xn--blbr-roah.dk"),
                                ("bla\u030abaer.dk", "xn--blbaer-jua.dk"),
                                ("www.æøå.dk", "www.xn--5cab8c.dk"),
                                ("xn--5cab8c.dk", "xn--5cab8c.dk")):
            with self.subTest(value=value):
                self.assertEqual(normalize_dns_name(value, fully_qualified=True), expected)
        self.assertEqual(normalize_dns_name("Example.test"), "Example.test")
        self.assertEqual(normalize_dns_name("Example.test", fully_qualified=True), "example.test")
        self.assertEqual(normalize_dns_name("æøå"), "xn--5cab8c")

    def test_dns_limits_and_invalid_names(self):
        valid = ".".join(["a" * 63] * 3 + ["a" * 61])
        self.assertEqual(len(normalize_dns_name(valid)), 253)
        for value in ("", None, 42, "localhost", "a" * 64 + ".dk",
                      valid + "a", "æ" * 60 + ".dk", "-bad.dk", "bad-.dk",
                      "a..dk", "a.dk.", "*.æøå.dk", "æøå.dk/path", "æøå.dk:443",
                      "æøå.dk\n", "æ øå.dk", "a\x00.dk"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_dns_name(value, fully_qualified=True)

    def test_shell_normalization_matches_admin(self):
        for value, mode in (("æøå.dk", "binding"), ("blåbær.dk", "binding"),
                            ("æøå", "site"), ("Example.test", "site")):
            result = subprocess.run(
                ["bash", "-c", 'source "$1/lib/common.sh"; normalize_dns_name "$2" "$3"',
                 "test", str(APACHE), value, mode], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(),
                             normalize_dns_name(value, fully_qualified=True) if mode == "binding"
                             else normalize_site_name(value))


class CreateScriptTests(unittest.TestCase):
    def test_all_standard_html_pages_and_python_response_use_utf8(self):
        for template in (APACHE / "solutionfiles").rglob("*.html"):
            with self.subTest(template=template):
                html = template.read_text(encoding="utf-8")
                self.assertIn('<meta charset="utf-8">', html[:1024])
                self.assertLess(html.index('<meta charset="utf-8">'), html.index("<title>"))
        source = (APACHE / "solutionfiles/python/app/app.py").read_text()
        source = source.replace("@SITE@", "æøå.dk").replace("@PORT_HTTP@", "4100")
        ns = {"__name__": "test_standard_python_site"}
        exec(compile(source, "standard Python site", "exec"), ns)
        response = ns["app"].test_client().get("/")
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        self.assertEqual(response.data.decode("utf-8"), "Hello world from æøå.dk!")

    def test_html_site_declares_utf8_before_unicode_title(self):
        template = APACHE / "solutionfiles/html/site/index.html"
        result = subprocess.run(
            ["bash", "-c", 'set -eu; source "$1/lib/common.sh"; _site=æøå.linux-01.vns.dk; '
             'set_site_vars; render "$2"', "test", str(APACHE), str(template)],
            capture_output=True, check=True)
        html = result.stdout.decode("utf-8")
        self.assertIn("Welcome to æøå.linux-01.vns.dk", html)
        self.assertLess(html.index('<meta charset="utf-8">'), html.index("<title>"))
        self.assertLess(html.index('<meta charset="utf-8">'), 1024)
        config = (APACHE / "solutionfiles/html/apache.conf.tpl").read_text()
        self.assertIn("AddDefaultCharset UTF-8", config)

    def test_unicode_paths_and_ascii_service_are_rendered_together(self):
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "fixture.tpl"
            template.write_text("@SITE@\n@SITE_ROOT@\n@SERVICE_NAME@\n")
            result = subprocess.run(
                ["bash", "-c", 'set -eu; source "$1/lib/common.sh"; _rootPath="$2"; '
                 '_site=æøå.dk; set_site_vars; mkdir -p "$_siteRoot"; '
                 'render "$3"; printf "service=%s\\n" "$_servicePath"',
                 "test", str(APACHE), directory, str(template)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "æøå.dk").is_dir())
            self.assertIn(directory + "/æøå.dk", result.stdout)
            self.assertIn("service=/etc/systemd/system/xn--5cab8c.dk.service", result.stdout)
            self.assertNotIn("@SERVICE_NAME@", result.stdout)

    def test_cli_converts_names_before_setting_site_paths(self):
        prefix = CREATE.split("\nset_site_vars\n", 1)[0]
        prefix = re.sub(r"\$\{(\w+),,\}",
                        r"""$(printf '%s' "${\1}" | tr '[:upper:]' '[:lower:]')""", prefix)
        for site_type in ("html", "python", "csharp"):
            with self.subTest(site_type=site_type):
                result = subprocess.run(
                    ["bash", "-c", prefix + '\nprintf "site=%s binding=%s project=%s\\n" '
                     '"$_site" "$_binding" "$_projectName"', str(APACHE / "create_site.sh"),
                     "æøå.dk", site_type, "empty", "", "", "ÆØÅ.dk", "", "", "n"],
                    cwd=APACHE, input="\n", capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("site=æøå.dk binding=xn--5cab8c.dk", result.stdout)
                if site_type == "csharp":
                    self.assertIn("project=xn--5cab8c.dk", result.stdout)

    def run_creation(self, kind="empty", version="18.3.1", template="", extra=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "_publish").mkdir()
            script = """
set -euo pipefail
_site=example.test
_siteRoot=ROOT
_apacheDir=ROOT
_solutionFolder=/templates
_projectName=Example
_publishFolder=_publish
_publishArg=_publish
_buildConfig=Debug
_createCertificate=n
_templateProvided=y
_dotNetVersion=net10.0
_defaultUmbracoVersion=18.3.1
_defaultUmbraco13Version=13.16.4
ask() { :; }
validate_publish_folder() { :; }
allocate_ports() { :; }
install_scripts() { :; }
exclude_from_build() { :; }
configure_project_dotnet() { echo "configure:$1"; }
install_apache_config() { :; }
install_service() { :; }
ensure_dotnet_channel() { echo "ensure:$1"; }
create_umbraco_project() { echo "umbraco:${_dotNetVersion}:${_umbracoVersion}"; }
sudo() {
    printf 'sudo:'
    printf ' <%s>' "$@"
    printf '\\n'
    if [[ $1 == tee ]]; then
        cat > "$2"
    elif [[ $1 == dotnet && $2 == new ]]; then
        touch "${_siteRoot}/${_projectName}.csproj"
    fi
}
""".replace("ROOT", shlex.quote(directory))
            script += "\n_csharpKind=" + shlex.quote(kind)
            script += "\n_umbracoVersion=" + shlex.quote(version)
            script += "\n_templateName=" + shlex.quote(template)
            script += "\n" + shell_function("create_csharp") + "\n" + extra + "\ncreate_csharp\n"
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            global_file = root / "global.json"
            sdk = json.loads(global_file.read_text()) if global_file.exists() else None
            return result, sdk

    def test_empty_defaults_to_web(self):
        result, sdk = self.run_creation()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<dotnet> <new> <web> <--name> <Example> <--output> <.>", result.stdout)
        self.assertIsNone(sdk)

    def test_custom_template_runs_in_site_folder(self):
        result, _ = self.run_creation(template="webapi",
                                      extra='exclude_from_build() { [[ $PWD == "$_siteRoot" ]]; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<dotnet> <new> <webapi>", result.stdout)
        self.assertIn("<dotnet> <publish> <", result.stdout)
        self.assertIn("/Example.csproj> <-c>", result.stdout)
        self.assertIn("configure:", result.stdout)

    def test_invalid_template_fails_before_creating_site(self):
        result, _ = self.run_creation(template="--help")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid template name", result.stderr)
        self.assertNotIn("sudo:", result.stdout)

    def test_umbraco13_uses_sdk_and_target_net8(self):
        for version in ("13", "13.3.2", "13.12.0"):
            with self.subTest(version=version):
                result, sdk = self.run_creation(kind="umbraco", version=version)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ensure:8.0", result.stdout)
                self.assertIn("umbraco:net8.0:", result.stdout)
                self.assertEqual(sdk["sdk"]["version"], "8.0.100")
                self.assertEqual(sdk["sdk"]["rollForward"], "latestFeature")

    def test_unsupported_umbraco13_fails_explicitly(self):
        for version in ("13.0.0", "13.3.1", "13.invalid"):
            with self.subTest(version=version):
                result, _ = self.run_creation(kind="umbraco", version=version)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("13.3.2 or newer", result.stderr)

    def test_modern_umbraco_keeps_net10(self):
        result, sdk = self.run_creation(kind="umbraco")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("umbraco:net10.0:18.3.1", result.stdout)
        self.assertNotIn("ensure:8.0", result.stdout)
        self.assertIsNone(sdk)

    def test_environment_reaches_the_service_template(self):
        for environment in ("Development", "Test", "Staging", "Production"):
            with self.subTest(environment=environment):
                result, _ = self.run_creation(extra=(
                    "_environmentArg=" + environment +
                    '\ninstall_service() { echo "service-environment:${_environment}"; }'))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("service-environment:" + environment, result.stdout)
        result, _ = self.run_creation(extra="_environmentArg=invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Environment must be", result.stderr)

    def test_umbraco_packages_match_the_runtime(self):
        for version, target in (("13.3.2", "net8.0"), ("17.0.0", "net10.0"),
                                ("18.0.0", "net10.0"), ("18.3.1", "net10.0"),
                                ("19.0.0", "net10.0")):
            with self.subTest(version=version):
                script = """
set -euo pipefail
_solutionFolder=/templates
_apacheDir=/apache
_siteRoot=/site
_project=/site/Example.csproj
_projectName=Example
_publishFolder=_publish
_buildConfig=Debug
_binding=example.test
_createCertificate=y
sudo() { printf '<%s> ' "$@"; printf '\\n'; }
uuidgen() { echo test; }
exclude_from_build() { :; }
"""
                script += "\n_umbracoVersion=" + version + "\n_dotNetVersion=" + target
                script += "\n" + shell_function("create_umbraco_project") + "\ncreate_umbraco_project\n"
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("s|DotNetVersion|" + target + "|g", result.stdout)
                for folder in ("wwwroot", "Views", "uSync", "Program.cs", "appsettings.json"):
                    copy = "</templates/csharp/umbraco/project/" + folder + ">"
                    self.assertEqual(copy in result.stdout, int(version.split(".")[0]) >= 18)
                self.assertEqual("<rm> <-rf> <wwwroot> <Views> <appsettings.json> <appsettings.Development.json>"
                                 in result.stdout, int(version.split(".")[0]) >= 18)
                if target == "net8.0":
                    self.assertIn("<Umbraco.Templates::13.3.2>", result.stdout)
                    self.assertNotIn("Umbraco.Cms.DevelopmentMode.Backoffice", result.stdout)
                    for package in ("VNS.Umbraco", "uSync"):
                        self.assertIn("<package> <" + package + "> <-v> <13.0.0>", result.stdout)
                else:
                    self.assertIn("<Umbraco.Templates@" + version + ">", result.stdout)
                    self.assertIn("<Umbraco.Cms.DevelopmentMode.Backoffice> <-v> <" + version + ">", result.stdout)


class DotnetInstallationTests(unittest.TestCase):
    def run_installer(self, runtime):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = root / "dotnet"
            host.write_text('#!/bin/bash\ncase "$1" in\n'
                            '--list-sdks) echo "8.0.100 [/sdk]" ;;\n'
                            '--list-runtimes) echo ' + shlex.quote(runtime) + ' ;;\nesac\n')
            host.chmod(0o700)
            helper = (APACHE / "lib/dotnet.sh").read_text().replace(
                "_dir=/usr/lib/dotnet", "_dir=" + shlex.quote(directory))
            script = helper + """
sudo() { printf 'sudo:'; printf ' <%s>' "$@"; printf '\\n'; }
curl() { echo download; }
ensure_dotnet_channel 8.0
"""
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_existing_sdk_and_runtime_are_kept(self):
        result = self.run_installer("Microsoft.AspNetCore.App 8.0.0 [/shared]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("download", result.stdout)

    def test_missing_aspnet_runtime_installs_both_components(self):
        result = self.run_installer("Microsoft.NETCore.App 8.0.0 [/shared]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("download", result.stdout)
        self.assertIn("<--channel> <8.0> <--install-dir>", result.stdout)
        self.assertIn("<--channel> <8.0> <--runtime> <aspnetcore>", result.stdout)


class ProjectRuntimeTests(unittest.TestCase):
    def test_net8_templates_install_and_select_net8(self):
        helper = (APACHE / "lib/dotnet.sh").read_text()
        for framework in ("net8.0", "net10.0"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = root / "example-site.csproj"
                project.write_text("<Project><PropertyGroup><TargetFramework>" + framework +
                                   "</TargetFramework></PropertyGroup></Project>")
                script = helper + """
set -euo pipefail
ensure_dotnet_channel() { echo "ensure:$1"; }
sudo() { "$@"; }
configure_project_dotnet "$1"
"""
                result = subprocess.run(["bash", "-c", script, "test", str(project)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if framework == "net8.0":
                    self.assertIn("ensure:8.0", result.stdout)
                    self.assertEqual(json.loads((root / "global.json").read_text())["sdk"]["version"],
                                     "8.0.100")
                else:
                    self.assertNotIn("ensure:", result.stdout)
                    self.assertFalse((root / "global.json").exists())

    def test_all_publish_paths_select_the_project(self):
        for path in ("scripts/publish.sh", "prod-scripts/publish_prod.sh"):
            text = (APACHE / "solutionfiles/csharp" / path).read_text()
            self.assertIn('dotnet publish "${_projectName}.csproj"', text)
            runtime_argument = '--runtime "${_runtimeIdentifier}"' if path.startswith("prod-") else '--runtime "$(linux_runtime_identifier)"'
            self.assertIn(runtime_argument, text)
            if path.startswith("prod-"):
                self.assertIn('linux_runtime_identifier "$(remote uname -m)"', text)
                self.assertIn('linux_runtime_identifier)', text)

    def test_linux_runtime_identifier_maps_supported_architectures(self):
        helper = APACHE / "lib/dotnet.sh"
        for architecture, expected in (("x86_64", "linux-x64"), ("aarch64", "linux-arm64"), ("arm64", "linux-arm64")):
            result = subprocess.run(
                ["bash", "-c", f'source "{helper}"; linux_runtime_identifier "$1"', "test", architecture],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        result = subprocess.run(
            ["bash", "-c", f'source "{helper}"; linux_runtime_identifier "$1"', "test", "i686"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported Linux architecture", result.stderr)


if __name__ == "__main__":
    unittest.main()
