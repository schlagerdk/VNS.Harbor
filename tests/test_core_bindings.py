"""harbor.core.bindings against the functions it replaces in lib/common.sh (frozen in tests/legacy/lib/common.sh)"""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harbor.core import bindings  # noqa: E402

COMMON = ROOT / "tests/legacy/lib/common.sh"
BASH_MAJOR = int(subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True).stdout or 0)
NEEDS_BASH_4 = unittest.skipIf(BASH_MAJOR < 4, "Needs bash 4 or newer, as on the servers")


def bash(snippet, *args, folder=None):
    """Run a snippet with the frozen common.sh sourced. Returns (exit code, output, errors)."""
    script = f"source {shlex.quote(str(COMMON))}\n{snippet}"
    env = os.environ.copy()
    if folder:
        # sudo runs what it is given, as the tests are not root
        sudo = Path(folder) / "sudo"
        sudo.write_text('#!/bin/bash\nexec "$@"\n')
        sudo.chmod(0o755)
        env["PATH"] = f"{folder}:{env['PATH']}"
    result = subprocess.run(["bash", "-c", script, "bash", *args], capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def python(function, *args, **kwargs):
    """The same shape for the Python function: (0, value) or (1, message)"""
    try:
        return 0, function(*args, **kwargs)
    except ValueError as error:
        return 1, str(error)


class BindingTests(unittest.TestCase):
    NAMES = ["example.dk", "Example.DK", "www.æblé.dk", "a.b", "-bad.dk", "bad-.dk", "under_score.dk", "with space.dk",
             "https://example.dk", "example.dk/path", "nodot", "x" * 64 + ".dk", ".".join(["a" * 60] * 5), "a..dk", ""]

    def test_validate_binding(self):
        for name in self.NAMES:
            code, _, errors = bash('validate_binding "$1"', name)
            self.assertEqual(python(bindings.validate_binding, name), (code, errors.strip() if code else None), name)

    def test_site_binding(self):
        for http_only in (0, 1):
            for name in self.NAMES:
                code, output, errors = bash(f'_httpOnly={http_only}; normalize_site_binding "$1"', name)
                ours = python(bindings.site_binding, name, http_only=bool(http_only))
                self.assertEqual(ours[0], code, (name, http_only, errors))
                if code == 0:
                    self.assertEqual(ours[1], output.strip(), (name, http_only))
                else:
                    self.assertEqual(ours[1].splitlines()[-1], errors.strip().splitlines()[-1], (name, http_only))

    def test_server_aliases(self):
        cases = [("example.dk", "www.example.dk, shop.example.dk"), ("example.dk", "Example.dk WWW.example.dk www.example.dk"),
                 ("example.dk", "  a.dk\n b.dk,,c.dk  "), ("example.dk", ""), ("example.dk", "nodot"), ("example.dk", "ok.dk bad_.dk")]
        for primary, raw in cases:
            code, output, errors = bash('normalize_server_aliases "$1" "$2" && printf "%s" "${_normalizedAliases}"', primary, raw)
            ours = python(bindings.server_aliases, primary, raw)
            self.assertEqual(ours[0], code, (raw, errors))
            if code == 0:
                self.assertEqual(" ".join(ours[1]), output, raw)
            else:
                self.assertEqual(ours[1].splitlines()[-1], errors.strip().splitlines()[-1], raw)

    def test_aliases_from_config_and_replacing_them(self):
        conf = ("<VirtualHost *:80>\n  ServerName example.dk\n  ServerAlias www.example.dk shop.example.dk # old\n"
                "  ServerAlias Extra.example.dk\n  DocumentRoot /var/www/x\n</VirtualHost>\n\n<VirtualHost *:443>\n"
                "  ServerName example.dk\n\tServerAlias www.example.dk\n</VirtualHost>")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "site.conf"
            path.write_text(conf)
            code, output, _ = bash('server_aliases_from_config "$1" "$2"', path, "example.dk")
            self.assertEqual(code, 0)
            self.assertEqual(" ".join(bindings.server_aliases_from_config(conf, "example.dk")), output)
            for aliases in ("new.example.dk other.example.dk", ""):
                path.write_text(conf)
                code, _, _ = bash('replace_server_aliases "$1" "$2"', path, aliases)
                self.assertEqual(code, 0)
                self.assertEqual(bindings.replace_server_aliases(conf, aliases), path.read_text(), aliases)

    def test_certificate_domains(self):
        for conf in ("ServerName example.dk\nServerAlias www.example.dk WWW.example.dk example.dk shop.example.dk\n",
                     "ServerName example.dk\n", "ServerAlias bad_name.dk\n", "ServerAlias Upper.Example.dk # x y\n"):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "site.conf"
                path.write_text(conf)
                code, output, errors = bash('set_certificate_domains "$1" "$2" && printf "%s" "${_certificateDomainNames}"', path, "Example.dk")
                ours = python(bindings.certificate_domains, conf, "Example.dk", config=str(path))
                self.assertEqual(ours[0], code, (conf, errors))
                if code == 0:
                    self.assertEqual(" ".join(ours[1]), output, conf)
                else:
                    self.assertEqual(ours[1].splitlines()[-1], errors.strip().splitlines()[-1], conf)

    def test_validate_project_name(self):
        for name in ("Site", "my.site_1-a", "-bad", ".bad", "with space", "æble", ""):
            code, _, errors = bash('validate_project_name "$1"', name)
            self.assertEqual(python(bindings.validate_project_name, name), (code, errors.strip() if code else None), name)

    @NEEDS_BASH_4
    def test_validate_publish_folder(self):
        for name in (".", "_publish", "out.1", "Bin", "WWWROOT", "app_plugins", "../x", "a/b", "-x", ""):
            code, _, errors = bash('validate_publish_folder "$1"', name)
            self.assertEqual(python(bindings.validate_publish_folder, name), (code, errors.strip() if code else None), name)

    def test_exclude_from_build(self):
        projects = ['<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup />\n</Project>\n',
                    '<Project>\n</Project>\n\n\n', '<Project>\n</Project>', '<Project></Project>\n<!-- after -->\n']
        for text in projects:
            for folder in ("_publish", "."):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "site.csproj"
                    path.write_text(text)
                    for _ in range(2):
                        code, _, errors = bash('exclude_from_build "$1" "$2"', path, folder, folder=tmp)
                        self.assertEqual(code, 0, errors)
                    once = bindings.exclude_from_build(text, folder)
                    self.assertEqual(bindings.exclude_from_build(once, folder), once)
                    self.assertEqual(once, path.read_text(), (text, folder))


if __name__ == "__main__":
    unittest.main()
