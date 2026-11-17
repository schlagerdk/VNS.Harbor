"""Publish from your machine: the script the admin hands out is valid bash and runs receive-build for the site"""
import json
from pathlib import Path
import subprocess
import unittest

APP = Path(__file__).resolve().parents[2] / "admin"


class LocalPublishScriptTests(unittest.TestCase):
    def script(self, **options):
        source = (APP / "static/app.js").read_text()
        start = source.index("function localPublishScript(")
        code = source[start:source.index("\nfunction showLocalPublish(", start)]
        run = 'const location = { hostname: "admin.web-02.example.com", origin: "https://admin.web-02.example.com" };\n' + code + f"\nprocess.stdout.write(localPublishScript({json.dumps(options)}));"
        # As bytes: Windows' line ends as they are
        return subprocess.run(["node", "-e", run], capture_output=True, check=True).stdout.decode()

    def test_the_script_is_bash_that_sends_the_build_and_runs_receive_build(self):
        text = self.script(site="shop.example.com", binding="www.shop.example.com", https="y", environment="Production", access="ssh")
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)
        self.assertIn("SERVER='root@web-02.example.com'", text, "this server, by the name its admin is reached by")
        self.assertIn('dotnet publish "$PROJECT" -c Release -r "$RID" --self-contained false -o "$OUT/tar/build"', text)
        self.assertIn("x86_64) RID=linux-x64 ;;", text, "the server's architecture, asked")
        self.assertIn("aarch64|arm64) RID=linux-arm64 ;;", text)
        self.assertIn('tar -C "$OUT/tar" -czf - . | "${SSH[@]}" "$REMOTE"', text, "one tar over one login")
        self.assertIn("ControlMaster=auto", text, "the two ssh calls share one login")
        self.assertNotIn("rsync", text, "nothing that needs a newer rsync than macOS has")

    def remote(self, binding, keep=()):
        """The command the script runs on the server, as the server's shell splits it"""
        text = self.script(site="shop.example.com", binding=binding, https="n", environment="Test", access="ssh")
        start = text.index('REMOTE="$(printf')
        build = text[start:text.index("\nCOPYFILE_DISABLE", start)]
        keeps = " ".join(f"'{path}'" for path in keep)
        prelude = f"set -u\nSITE=shop.example.com\nAPPSETTINGS=server\nCOMMIT=abc1234\nDIRTY=y\nKEEP=({keeps})\n"
        run = prelude + build + '\neval "set -- $REMOTE"; printf "%s\\n" "$@"'
        return subprocess.run(["/bin/bash", "-c", run], capture_output=True, text=True, check=True).stdout.splitlines()

    def test_quotes_in_a_value_cannot_break_out(self):
        args = self.remote("a'; rm -rf / #")
        self.assertEqual(args[args.index("--binding") + 1], "a'; rm -rf / #")
        self.assertEqual(args[:5], ["harbor", "site", "receive-build", "shop.example.com", "--stdin"])
        self.assertEqual(args[args.index("--commit") + 1], "abc1234")
        self.assertEqual(args[args.index("--dirty") + 1], "y")

    def test_kept_paths_are_sent_and_none_is_fine_in_the_bash_of_macos(self):
        self.assertNotIn("--keep=/wwwroot/", self.remote("shop.example.com"))
        args = self.remote("shop.example.com", keep=("/wwwroot/", "/files/"))
        self.assertIn("--keep=/wwwroot/", args)
        self.assertIn("--keep=/files/", args)

    def test_with_a_token_it_publishes_over_https_and_fails_on_a_failed_publish(self):
        text = self.script(site="shop.example.com", binding="shop.example.com", https="n", environment="Production", access="token")
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)
        self.assertNotIn("ssh ", text, "no SSH at all")
        self.assertIn("HARBOR='https://admin.web-02.example.com'", text)
        self.assertIn('TOKEN_FILE="$HOME/.config/vns-harbor/web-02.example.com.token"', text)
        self.assertIn('"$HARBOR/api/publish/$SITE/architecture"', text)
        self.assertIn('[ "$(tail -n 1 "$OUT/log")" = "Harbor: OK" ]', text, "the last line decides")

    def test_python_and_static_sites_send_their_files(self):
        for kind, source in (("python", "app"), ("html", ".")):
            for access in ("token", "ssh"):
                with self.subTest(kind=kind, access=access):
                    text = self.script(site="shop.example.com", binding="shop.example.com", https="n", environment="Production", kind=kind, access=access)
                    subprocess.run(["bash", "-n"], input=text, text=True, check=True)
                    self.assertIn('SOURCE="${SOURCE:-}"' if kind == "python" else f"SOURCE='{source}'", text)
                    self.assertNotIn("dotnet", text)
                    self.assertNotIn("appsettings", text.lower().replace("appsettings.<environment>", ""))

    def test_a_python_project_with_app_py_in_its_folder_or_in_app(self):
        """A project with app.py beside the script failed: tar: could not chdir to 'app'"""
        import shutil
        import tempfile
        text = self.script(site="py.example.com", binding="py.example.com", https="n", environment="Production", kind="python", access="ssh")
        start = text.index('if [ -z "$SOURCE" ]; then')
        check = text[start:text.index("COPYFILE_DISABLE", start)]
        for layout, expected in ((("app.py",), "."), (("app/app.py",), "app"), ((), None)):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as folder:
                for name in layout:
                    (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
                    (Path(folder) / name).write_text("app")
                ran = subprocess.run(["/bin/bash", "-c", 'set -u\nSOURCE=""\n' + check + '\necho "$SOURCE"'], cwd=folder, capture_output=True, text=True)
                if expected is None:
                    self.assertNotEqual(ran.returncode, 0)
                    self.assertIn("No app.py here or in app/", ran.stderr)
                else:
                    self.assertEqual(ran.stdout.strip(), expected)

    def test_windows_gets_a_cmd_script(self):
        for kind in ("csharp", "python"):
            for access in ("token", "ssh"):
                with self.subTest(kind=kind, access=access):
                    text = self.script(site="shop.example.com", binding="shop.example.com", https="y", environment="Production",
                                       kind=kind, access=access, system="windows")
                    self.assertTrue(text.startswith("@echo off\r\n"), "cmd, with Windows' line ends")
                    self.assertNotIn("\n", text.replace("\r\n", ""))
                    self.assertIn("goto failed", text)
                    self.assertIn(":failed\r\n", text)
                    if access == "token":
                        self.assertIn('if not "!LAST!"=="Harbor: OK" goto failed', text)
                        self.assertIn("%USERPROFILE%\\.config\\vns-harbor\\web-02.example.com.token", text)
                    else:
                        self.assertIn('< "%OUT%\\build.tgz" || goto failed', text, "the tar to ssh by redirection: binary safe")
                    self.assertEqual("dotnet publish" in text, kind == "csharp")

    def test_the_button_works_on_a_site_not_published_from_a_machine_yet(self):
        """A build-only site set up before 1.0.32 has no local_publish: the button failed on null.binding"""
        source = (APP / "static/app.js").read_text()
        start = source.index('addManage("Publish from your machine…"')
        call = source[start:source.index("}));", start)]
        self.assertIn("site.local_publish?.binding", call)
        self.assertNotIn("site.local_publish.binding", call)

    def test_the_token_is_saved_where_the_script_of_that_computer_looks_and_stays_shown(self):
        """Windows was given the macOS command to save its token, and choosing another computer hid a new token"""
        source = (APP / "static/app.js").read_text()
        start = source.index("async function renderPublishToken(")
        body = source[start:source.index("\n}\n", start)]
        self.assertIn('if not exist "${folder}" mkdir "${folder}" & echo ${info.token}> "${folder}\\\\${server}.token"', body)
        self.assertIn("const folder = `%USERPROFILE%\\\\.config\\\\vns-harbor`;", body)
        self.assertIn("if (shownPublishToken && info.exists) info.token = shownPublishToken;", body)
        # Where the cmd script reads it
        text = self.script(site="shop.example.com", binding="shop.example.com", https="n", environment="Production", access="token", system="windows")
        self.assertIn('set "TOKEN_FILE=%USERPROFILE%\\.config\\vns-harbor\\web-02.example.com.token"', text)

if __name__ == "__main__":
    unittest.main()
