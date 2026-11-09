import json
from pathlib import Path
import re
import subprocess
import unittest

APACHE = Path(__file__).resolve().parents[1]
APP = APACHE / "solutionfiles/admin/app"


class EnvironmentOrderTests(unittest.TestCase):
    def sort(self, names):
        source = (APP / "static/app.js").read_text()
        code = source[source.index("const environmentOrder"):source.index("function renderStats")]
        script = code + f"""
const input = {json.dumps(names)}.map((environment) => ({{ environment }}));
console.log(JSON.stringify(sortedEnvironments(input).map((env) => env.environment)));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_order_is_fixed(self):
        self.assertEqual(self.sort(["Production", "Staging", "Test", "Development"]),
                         ["Development", "Test", "Staging", "Production"])

    def test_case_does_not_matter_and_unknown_comes_last(self):
        self.assertEqual(self.sort(["Unknown", "production", "", "TEST"]), ["TEST", "production", "Unknown", ""])

    def test_detail_page_keeps_the_main_site_first_and_sorts_the_rest(self):
        source = (APP / "static/app.js").read_text()
        render = source[source.index("function renderSite("):source.index("async function updatePackages")]
        self.assertIn('if (!isTarget(env)) envs.append(envPanel(site, env));', render)
        self.assertIn("for (const env of sortedEnvironments(site.environments)) {", render)
        self.assertIn("targets.append(envPanel(site, env));", render)

    def test_empty_state_and_equal_heights(self):
        page = (APP / "templates/index.html").read_text()
        self.assertIn('id="targets-empty"', page)
        self.assertIn('id="btn-empty-add-environment"', page)
        css = (APP / "static/style.css").read_text()
        self.assertIn(".site-layout.with-packages .package-panel { margin-top: 0; display: flex; flex-direction: column; contain: size; border-top: 3px solid var(--blue); }", css)
        self.assertNotIn("gap: 16px; align-items: start; }\n.site-layout", css)

    def test_layout_and_wording(self):
        page = (APP / "templates/index.html").read_text()
        layout = re.search(r'<div id="site-layout".*?</div>\s*<section id="site-targets"', page, re.S).group(0)
        self.assertLess(layout.index('id="envs"'), layout.index('id="site-packages"'))
        for text in (page, (APP / "static/app.js").read_text()):
            self.assertNotIn("Create production site", text)
        self.assertIn("Add environment", page)


if __name__ == "__main__":
    unittest.main()
