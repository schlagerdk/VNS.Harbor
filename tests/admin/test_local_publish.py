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
        self.assertIn("dotnet publish -c Release -r linux-x64 --self-contained false", text)
        self.assertIn('"$SERVER:/var/lib/harbor/incoming/$SITE/"', text)
        self.assertIn("harbor site receive-build \"$SITE\" --binding 'www.shop.example.com' --https y --environment 'Production'", text)

    def test_quotes_in_a_value_cannot_break_out(self):
        text = self.script(site="shop.example.com", binding="a'; rm -rf / #", https="n", environment="Test")
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)
        echoed = subprocess.run(["bash", "-c", "eval \"$(sed -n 's/^.*--binding \\(.*\\) --https.*$/printf %s \\1/p')\""], input=text,
                                capture_output=True, text=True).stdout
        self.assertEqual(echoed, "a'; rm -rf / #")


    def test_the_button_works_on_a_site_not_published_from_a_machine_yet(self):
        """A build-only site set up before 1.0.32 has no local_publish: the button failed on null.binding"""
        source = (APP / "static/app.js").read_text()
        start = source.index('addManage("Publish from your machine…"')
        call = source[start:source.index("}));", start)]
        self.assertIn("site.local_publish?.binding", call)
        self.assertNotIn("site.local_publish.binding", call)

if __name__ == "__main__":
    unittest.main()
