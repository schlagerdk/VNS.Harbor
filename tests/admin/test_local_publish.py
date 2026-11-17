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
        run = 'const location = { hostname: "admin.web-02.example.com" };\n' + code + f"\nprocess.stdout.write(localPublishScript({json.dumps(options)}));"
        return subprocess.run(["node", "-e", run], capture_output=True, text=True, check=True).stdout

    def test_the_script_is_bash_that_sends_the_build_and_runs_receive_build(self):
        text = self.script(site="shop.example.com", binding="www.shop.example.com", https="y", environment="Production")
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)
        self.assertIn("SERVER='root@web-02.example.com'", text, "this server, by the name its admin is reached by")
        self.assertIn('dotnet publish "$PROJECT" -c Release -r "$RID" --self-contained false -o "$OUT/build"', text)
        self.assertIn("x86_64) RID=linux-x64 ;;", text, "the server's architecture, asked")
        self.assertIn("aarch64|arm64) RID=linux-arm64 ;;", text)
        self.assertIn('tar -C "$OUT" -czf - . | "${SSH[@]}" "$REMOTE"', text, "one tar over one login")
        self.assertIn("ControlMaster=auto", text, "the two ssh calls share one login")
        self.assertNotIn("rsync", text, "nothing that needs a newer rsync than macOS has")

    def remote(self, binding, keep=()):
        """The command the script runs on the server, as the server's shell splits it"""
        text = self.script(site="shop.example.com", binding=binding, https="n", environment="Test")
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

    def test_the_button_works_on_a_site_not_published_from_a_machine_yet(self):
        """A build-only site set up before 1.0.32 has no local_publish: the button failed on null.binding"""
        source = (APP / "static/app.js").read_text()
        start = source.index('addManage("Publish from your machine…"')
        call = source[start:source.index("}));", start)]
        self.assertIn("site.local_publish?.binding", call)
        self.assertNotIn("site.local_publish.binding", call)

if __name__ == "__main__":
    unittest.main()
