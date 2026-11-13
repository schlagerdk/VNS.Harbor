"""harbor.core.templates and harbor.core.ports against render, set_site_vars and allocate_ports in lib/common.sh"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core.ports import allocate_port  # noqa: E402
from harbor.core.system import RecordingSystem  # noqa: E402
from harbor.core.templates import Site, render  # noqa: E402
from tests.test_core_bindings import COMMON, bash  # noqa: E402

TEMPLATES = sorted(path for path in (ROOT / "apache/solutionfiles").rglob("*")
                   if path.is_file() and (path.suffix == ".tpl" or path.parent.name.endswith("scripts")))


class RenderTests(unittest.TestCase):
    FULL = dict(name="shop.example.dk", binding="www.shop.example.dk", project_name="Shop", publish_folder="_publish",
                doc_root="/var/www/shop.example.dk/_publish/wwwroot", port_http="4012", dotnet_version="10.0",
                umbraco_version="17.1.0", admin_mail="it@example.dk", build_config="Release", environment="Production",
                apache_dir="/vns/apache", csharp_kind="umbraco", access_rules="Require ip 10.0.0.0/8")

    def legacy(self, template, values):
        assignments = "\n".join(f"{key}={value!r}" for key, value in values.items())
        return bash(f"{assignments}\nset_site_vars\nrender \"$1\"", template)

    def variables(self, site):
        names = {"name": "_site", "binding": "_binding", "project_name": "_projectName", "publish_folder": "_publishFolder",
                 "doc_root": "_docRoot", "port_http": "_portHttp", "dotnet_version": "_dotNetVersion",
                 "umbraco_version": "_umbracoVersion", "admin_mail": "_adminMail", "build_config": "_buildConfig",
                 "environment": "_environment", "csharp_kind": "_csharpKind", "access_rules": "_accessRules"}
        values = {names[key]: value for key, value in site.items() if key in names}
        # The apache folder is where common.sh is in the script: here the frozen copy, given to Python as well
        return values

    def test_every_template_renders_as_the_script_does(self):
        self.assertGreater(len(TEMPLATES), 8)
        apache_dir = str(COMMON.parent.parent)
        for values in (self.FULL, dict(name="blog.dk"), dict(name="æblé.dk", publish_folder=".", port_http="4100")):
            site = Site(**values, apache_dir=apache_dir) if "apache_dir" not in values else Site(**{**values, "apache_dir": apache_dir})
            for template in TEMPLATES:
                code, output, errors = self.legacy(template, self.variables(values))
                self.assertEqual(code, 0, errors)
                self.assertEqual(render(template.read_text(), site), output, (template.relative_to(ROOT), values["name"]))

    def test_paths_are_those_of_set_site_vars(self):
        code, output, _ = bash('_site=$1; set_site_vars; printf "%s\\n" "$_siteRoot" "$_certificateFile" "$_keyFile" "$_configPath" "$_serviceName" "$_servicePath"', "æblé.dk")
        self.assertEqual(code, 0)
        site = Site("æblé.dk")
        self.assertEqual([site.site_root, site.certificate_file, site.key_file, site.config_path, site.service_name, site.service_path],
                         output.splitlines())


@unittest.skipUnless(shutil.which("flock") and shutil.which("bash"), "Needs flock, as on the servers")
class PortTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for path in ("etc/apache2/sites-available", "etc/systemd/system", "run/lock", "bin"):
            (self.root / path).mkdir(parents=True)
        (self.root / "etc/apache2/sites-available/a.conf").write_text("<VirtualHost *:80>\nProxyPass / http://127.0.0.1:4000/\n</VirtualHost>\n")
        (self.root / "etc/systemd/system/b.service").write_text('ExecStart=/x --urls "http://localhost:4001"\n')
        self.listening = "LISTEN 0 4096 127.0.0.1:4002 0.0.0.0:*\nLISTEN 0 511 *:80 *:*\nLISTEN 0 50 [::1]:4004 [::]:*\n"
        (self.root / "ss.out").write_text(self.listening)
        (self.root / "bin/ss").write_text(f"#!/bin/bash\ncat {self.root / 'ss.out'}\n")
        (self.root / "bin/sudo").write_text('#!/bin/bash\nexec "$@"\n')
        for name in ("ss", "sudo"):
            (self.root / "bin" / name).chmod(0o755)
        # A reservation by a running process holds 4003; one by a process that has ended is cleaned up
        (self.root / "run/lock/vns-apache-port-reservations").mkdir()
        (self.root / "run/lock/vns-apache-port-reservations/4003.reserve").write_text(f"{os.getpid()} other\n")
        (self.root / "run/lock/vns-apache-port-reservations/4005.reserve").write_text("999999 gone\n")

    def legacy_port(self):
        text = COMMON.read_text().replace("/etc/apache2/sites-available", f"{self.root}/etc/apache2/sites-available") \
            .replace("/etc/systemd/system", f"{self.root}/etc/systemd/system").replace("/run/lock", f"{self.root}/run/lock")
        common = self.root / "common.sh"
        common.write_text(text.replace('_apacheDir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', f'_apacheDir="{COMMON.parent.parent}"'))
        result = subprocess.run(["bash", "-c", f"source {common}; _site=x; allocate_ports; echo \"$_portHttp\"; release_port_reservation"],
                                capture_output=True, text=True, env=os.environ | {"PATH": f"{self.root}/bin:{os.environ['PATH']}"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.splitlines()[-1])

    def test_same_port_as_the_script_and_the_same_reservations(self):
        expected = self.legacy_port()
        (self.root / "run/lock/vns-apache-port-reservations/4005.reserve").write_text("999999 gone\n")
        system = RecordingSystem(root=self.root, answers={"ss -H -ltn": self.listening.rstrip("\n")})
        with allocate_port(system, "x") as reservation:
            self.assertEqual(reservation.port, expected)
            self.assertEqual(reservation.port, 4005)
            held = reservation.path
            self.assertTrue(held.exists())
            # The ended process's reservation was cleaned up, and the port is now held by this one
            self.assertEqual(reservation.path.read_text(), f"{os.getpid()} x\n")
        self.assertFalse(held.exists())
        self.assertTrue((self.root / "run/lock/vns-apache-port-reservations/4003.reserve").exists())


if __name__ == "__main__":
    unittest.main()
