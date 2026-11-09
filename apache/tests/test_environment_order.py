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

    def test_environments_have_the_width_of_the_main_site(self):
        css = (APP / "static/style.css").read_text()
        self.assertIn(".site-layout.with-packages { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);", css)
        self.assertIn(".env-targets { grid-template-columns: repeat(2, minmax(0, 1fr)); }", css)

    def test_move_out_is_a_choice_in_the_move_dialog(self):
        source = (APP / "static/app.js").read_text()
        self.assertNotIn('add("Move out"', source)
        self.assertIn('value: "release", textContent: "Separate site (move out)"', source)
        self.assertIn('kind: "release_site"', source)

    def test_package_list_keeps_the_scroll_to_itself(self):
        self.assertIn("overflow-y: auto; overscroll-behavior: contain;", (APP / "static/style.css").read_text())

    def test_move_under_main_site_is_in_the_title(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('className: "detail-title-action"', source)
        self.assertNotIn('add("Move under main site"', source)

    def test_site_actions_are_beside_the_environments_and_delete_is_at_the_bottom(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('targetsActions.append(el("button", { textContent: "Publish all"', source)
        self.assertIn('targetsActions.append(el("button", { textContent: "Sync to all"', source)
        self.assertIn('danger.append(el("button", { textContent: "Delete everything"', source)
        self.assertIn('add("Delete site", () => deleteSite(site, isTarget(env) ? "prod" : "test", env), "danger");', source)
        page = (APP / "templates/index.html").read_text()
        self.assertLess(page.index('id="site-targets"'), page.index('id="site-danger"'))

    def test_binding_is_the_heading_and_the_overview_button(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('if (isTarget(env)) heading.prepend(el("h3", { className: "env-title", textContent: bindingLabel(env) || address.hostname }));', source)
        self.assertIn('const meta = el("dl", { className: "meta" }, el("dt", { textContent: "Binding" }), el("dd", {}, binding));', source)
        self.assertIn(".env-head .publish-status { flex-basis: auto; }", (APP / "static/style.css").read_text())
        row = source[source.index("function compactEnvironmentRow"):source.index("function siteCard(")]
        self.assertIn("openLink(bindingLabel(env) || address.hostname, address.url", row)
        self.assertIn('openLink("Backend", address.url + "umbraco"', row)

    def test_add_is_on_the_left_and_publish_and_sync_on_the_right(self):
        page = (APP / "templates/index.html").read_text()
        self.assertLess(page.index('id="site-targets-add"'), page.index('id="site-targets-actions"'))
        source = (APP / "static/app.js").read_text()
        self.assertIn('targetsAdd.append(el("button", { textContent: "Add environment"', source)
        # More specific than the rule that resets the margin of .site-targets-head .actions
        self.assertIn(".site-targets-head .actions.site-targets-right { margin-left: auto; }", (APP / "static/style.css").read_text())

    def test_copy_dialog_shows_the_destination(self):
        self.assertIn('id="copy-site-path"', (APP / "templates/index.html").read_text())
        self.assertIn('`Copied to ${copyServer ? copyServer + ":" : ""}/var/www/${name}`', (APP / "static/app.js").read_text())

    def test_site_deleted_on_its_server_is_shown_and_only_publish_and_delete_remain(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('const isMissingEnv = (env) => env.mode === "remote" && env.exists === false;', source)
        self.assertIn('if (missing && label !== "Publish") continue;', source)
        start = source.index("const isMissingEnv")
        code = source[start:source.index("\n", start)]
        result = subprocess.run(["node", "-e", code + """
console.log(JSON.stringify([isMissingEnv({mode: "remote", exists: false}), isMissingEnv({mode: "remote"}),
    isMissingEnv({mode: "remote", exists: true}), isMissingEnv({mode: "local", exists: false})]));"""], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False])

    def test_title_is_the_binding_of_the_main_site(self):
        source = (APP / "static/app.js").read_text()
        code = source[source.index("const siteLabel ="):source.index("function environmentAddress")]
        code = code[:code.index("};", code.index("const siteTitle")) + 2]
        script = code + """
console.log(JSON.stringify([
    siteTitle({name: "folder.test", environments: [{key: "main", binding: "new.test"}, {key: "prod", binding: "other.test"}]}),
    siteTitle({name: "folder.test", environments: [{key: "main", binding: ""}]}),
    siteTitle({name: "folder.test", display_name: "Folder", environments: []}),
]));"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["new.test", "folder.test", "Folder"])
        self.assertIn('el("h2", { textContent: siteTitle(site) })', source)
        self.assertIn('className: "site-title", textContent: siteTitle(site)', source)

    def test_confirmation_names_the_environment_it_acts_on(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn("message: `${confirmText} (${bindingLabel(env) || siteLabel(site)})`", source)
        self.assertIn("creates it again as a new installation", source)
        self.assertNotIn("publish this site to production", source)

    def test_layout_and_wording(self):
        page = (APP / "templates/index.html").read_text()
        layout = re.search(r'<div id="site-layout".*?</div>\s*<section id="site-targets"', page, re.S).group(0)
        self.assertLess(layout.index('id="envs"'), layout.index('id="site-packages"'))
        for text in (page, (APP / "static/app.js").read_text()):
            self.assertNotIn("Create production site", text)
        self.assertIn("Add environment", page)


if __name__ == "__main__":
    unittest.main()
