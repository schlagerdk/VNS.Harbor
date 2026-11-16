import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin"


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

    def test_same_environment_is_sorted_by_binding(self):
        source = (APP / "static/app.js").read_text()
        code = source[source.index("const environmentOrder"):source.index("function renderStats")]
        script = code + """
const input = [["Production", "tømrer.dk"], ["Production", "help.kikayo.dk"], ["Test", "zz.dk"], ["Production", "Liise.dk"], ["Production", "website.dk"]]
    .map(([environment, binding]) => ({ environment, binding }));
console.log(JSON.stringify(sortedEnvironments(input).map((env) => env.binding)));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["zz.dk", "help.kikayo.dk", "Liise.dk", "tømrer.dk", "website.dk"])

    def test_case_does_not_matter_and_unknown_comes_last(self):
        self.assertEqual(self.sort(["Unknown", "production", "", "TEST"]), ["TEST", "production", "Unknown", ""])

    def test_detail_page_keeps_the_main_site_first_and_sorts_the_rest(self):
        source = (APP / "static/app.js").read_text()
        render = source[source.index("function renderSite("):source.index("async function updatePackages")]
        self.assertIn('if (!isTarget(env)) envs.append(envPanel(site, env));', render)
        self.assertIn("for (const env of sortedEnvironments(site.environments)) {", render)
        self.assertIn("const panel = envPanel(site, env);", render)
        self.assertIn("targets.append(panel);", render)

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

    def test_move_out_is_a_button_of_its_own_for_administrators(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('if (currentUser.admin && isTarget(env) && env.separate) addManage("Move out…", () => releaseEnvironment(site, env));', source)
        self.assertNotIn('targetCard("release"', source)
        self.assertIn('kind: "release_site"', source)
        self.assertIn('"Move out…": "admin"', source)

    def test_package_list_keeps_the_scroll_to_itself(self):
        self.assertIn("overflow-y: auto; overscroll-behavior: contain;", (APP / "static/style.css").read_text())

    def test_move_under_main_site_is_in_the_title(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('className: "detail-title-action"', source)
        self.assertNotIn('add("Move under main site"', source)

    def test_site_actions_are_beside_the_environments_and_delete_is_at_the_bottom(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('targetsActions.append(el("button", { textContent: "Publish environments"', source)
        # Sync settings… on the main site has Select all, so there is no separate Sync settings to all
        self.assertNotIn('"Sync settings to all"', source)
        self.assertIn('targetsActions.append(el("button", { textContent: "Sync settings…", className: "desktop-only", title: "Sync settings from the main site", onclick: () => run(() => openSyncDialog(site, mainEnv)) }));', source)
        self.assertIn('danger.append(el("button", { textContent: "Delete everything"', source)
        # An environment on its own card; the main site only when it has none (they are built from its project)
        self.assertIn('else if (hasProd(site) && !site.admin && isTarget(env)) addManage("Delete site", () => deleteSite(site, "prod", env), "danger");', source)
        self.assertIn('if (!hasProd(site) && !site.admin) addManage("Delete site", () => deleteSite(site, "all"), "danger");', source)
        page = (APP / "templates/index.html").read_text()
        self.assertLess(page.index('id="site-targets"'), page.index('id="site-danger"'))

    def test_binding_is_the_heading_and_the_overview_button(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('if (isTarget(env)) heading.prepend(el("h3", { className: "env-title", textContent: bindingLabel(env) || address.hostname }));', source)
        self.assertIn('const meta = el("dl", { className: "meta" }, el("dt", { textContent: "Binding" }), el("dd", {}, binding,', source)
        self.assertIn(".env-head .publish-status { flex-basis: auto; }", (APP / "static/style.css").read_text())
        row = source[source.index("function compactEnvironmentRow"):source.index("function siteCard(")]
        self.assertIn("openLink(bindingLabel(env) || address.hostname, address.url", row)
        # The Backend button logs straight in when the site has the VNS.Harbor package and a chosen user
        self.assertIn('backendButton(site, env, address.url + "umbraco"', row)

    def test_add_is_on_the_left_and_publish_and_sync_on_the_right(self):
        page = (APP / "templates/index.html").read_text()
        self.assertLess(page.index('id="site-targets-add"'), page.index('id="site-targets-actions"'))
        source = (APP / "static/app.js").read_text()
        self.assertIn('targetsAdd.append(el("button", { textContent: "Add environment"', source)
        # More specific than the rule that resets the margin of .site-targets-head .actions
        self.assertIn(".site-targets-head .actions.site-targets-right { margin-left: auto; }", (APP / "static/style.css").read_text())

    def test_copy_dialog_shows_the_destination(self):
        self.assertIn('id="copy-site-path"', (APP / "templates/index.html").read_text())
        self.assertIn('`${field("move_here").checked ? "Moved" : "Copied"} to /var/www/${field("target").value.trim() || "…"}`', (APP / "static/app.js").read_text())

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

    def test_add_environment_adopts_an_existing_site_by_itself(self):
        page = (APP / "templates/index.html").read_text()
        self.assertNotIn('id="prod-existing-toggle"', page)
        self.assertNotIn('value: "existing"', (APP / "static/app.js").read_text())
        script = (ROOT_DIR / "tests/legacy/solutionfiles/csharp/scripts/create_prod.sh").read_text()
        self.assertIn('bash "@APACHE_DIR@/register_env.sh" "${_site}"', script)
        self.assertIn('windows_prod.py" describe', script)

    def test_migrating_is_a_choice_in_copy_except_from_windows(self):
        source = (APP / "static/app.js").read_text()
        self.assertNotIn('addManage("Migrate…"', source)
        self.assertIn('prodForm.dataset.canMigrate = moveEnv && moveEnv.mode !== "windows" ? "1" : "";', source)
        self.assertIn('$("#prod-remove-options").hidden = !moving || !prodForm.dataset.canMigrate;', source)
        self.assertNotIn('add("Move…"', source)

    def test_copy_and_migrate_share_one_dialog(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('if (currentUser.admin && isTarget(env) && (env.mode === "local" && env.exists !== false || env.mode === "windows" || env.mode === "remote" && !missing)) addManage("Copy site", () => openCreateProdDialog(site, env, true));', source)
        self.assertIn("const copying = !prodForm.elements.remove_here.checked;", source)
        self.assertIn('targetCard("same", "Same server", moveEnv.host || "Another folder there")', source)
        self.assertIn("data.copy = true;", source)
        self.assertNotIn("copy_env", source)
        app = (APP / "app.py").read_text()
        self.assertNotIn("copy_env", app)
        self.assertIn('extra_env["VNS_ENV_COPY"] = "1"', app)
        self.assertFalse((ROOT_DIR / "copy_env.sh").exists())

    def test_environments_header_is_a_bar_with_a_count(self):
        page = (APP / "templates/index.html").read_text()
        self.assertIn('id="site-targets-count"', page)
        self.assertIn("border-radius: 12px; }", (APP / "static/style.css").read_text().split(".site-targets-head {", 1)[1].split("\n", 1)[0])

    def test_checkbox_style_applies_to_every_dialog(self):
        css = (APP / "static/style.css").read_text()
        self.assertIn("dialog.newsite .copy-connection-toggle { flex-direction: row;", css)
        self.assertNotIn("#dlg-copy-site .copy-connection-toggle", css)

    def test_more_row_unfolds_the_card(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn("expandedCards.add(site.name); renderSites();", source)
        self.assertIn("expandedCards.delete(site.name); renderSites();", source)
        self.assertIn('const listed = compact && !expanded ? allEnvironments.slice(0, maxCardEnvironments) : allEnvironments;', source)

    def test_filter_shows_with_more_than_two_environments_and_matches_all_words(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn("search.hidden = targets.childElementCount <= 2;", source)
        code = source[source.index("function filterTargets()"):source.index('$("#targets-search").oninput')]
        script = """
const search = {value: ""}, none = {hidden: true};
const panels = [{dataset: {search: "help.kikayo.dk production"}}, {dataset: {search: "tømrer.dk production server.test"}}, {dataset: {search: "dev.test development"}}];
const $ = (selector) => selector === "#targets-search" ? search : selector === "#targets-no-match" ? none : {children: panels};
""" + code + """
search.value = "PRODUCTION kikayo"; filterTargets();
const first = panels.map((panel) => panel.hidden), firstNone = none.hidden;
search.value = "nothing"; filterTargets();
console.log(JSON.stringify([first, firstNone, none.hidden]));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[False, True, True], True, False])

    def test_windows_environments_can_edit_their_settings_files(self):
        source = (APP / "static/app.js").read_text()
        self.assertIn('[["Edit web.config", "web.config"], ["Edit appsettings.json", "appsettings.json"]]', source)
        app = (APP / "app.py").read_text()
        self.assertEqual(app.count('@app.route("/api/remote-fs/<site>/<action>"'), 1)
        self.assertLess(app.index("def windows_files("), app.index('@app.route("/api/remote-fs/<site>/<action>"'))
        self.assertIn('response = windows_files(site, target, data)', app)
        self.assertIn('add("Files", () => openFiles("", remoteRef(site, env)), "desktop-only");', source)
        # data_sync.sh still reads and saves appsettings.json with the narrow helper
        script = (ROOT_DIR / "tests/legacy" / "windows_file.sh").read_text()  # as it was; harbor is compared with it in tests/
        self.assertIn("^(web\\.config|appsettings(\\.[A-Za-z0-9_-]+)?\\.json)$", script)

    def test_start_page_and_sql_query(self):
        source = (APP / "static/app.js").read_text()
        self.assertLess(source.index("let startView"), source.index("async function route()"))
        self.assertIn('} else if (!view && startView === "sql") {', source)
        self.assertIn('actions.prepend(el("button", { textContent: "Query", onclick: () => openSqlQuery(database) }));', source)
        page = (APP / "templates/index.html").read_text()
        self.assertIn('<meta name="start-view" content="{{ start_view }}">', page)
        # Sites | SQL Server is a switch in the header; start page and keep-alive are Preferences
        self.assertIn('id="switch-sites"', page)
        self.assertIn('id="switch-sql"', page)
        self.assertIn('id="dlg-preferences"', page)
        self.assertIn('id="menu-preferences"', page)
        for gone in ("settings-sql", "settings-web", "settings-start-view", "settings-keep-alive", "dlg-start-view", "settings-update-group"):
            self.assertNotIn(gone, page)
        self.assertNotIn("sql-back-bar", page)
        self.assertIn('const active = button.dataset.view === (name === "sql" ? "sql" : "sites");', source)
        self.assertIn('if (currentView === "site") go("#sites");', source)
        self.assertIn('body[data-view="sql"] .page-header .search-box { display: none; }', (APP / "static/style.css").read_text())
        for label in ("SQL Server", "Preferences…", "Files (/)", "Test config", "Error logs"):
            self.assertIn(f'"{label}": [', source)
        self.assertIn('id="dlg-sql-query"', page)

    def test_query_dialog_starts_empty_for_another_database(self):
        source = (APP / "static/app.js").read_text()
        code = source[source.index("function openSqlQuery("):source.index("async function runSqlQuery(")]
        script = """
const nodes = {};
const $ = (selector) => nodes[selector] ||= {value: "", textContent: "", focus() {}, classList: {remove() {}}, replaceChildren() { this.textContent = ""; }};
nodes["#sql-connection"] = {value: "server-1"};
const sqlQueryDialog = {dataset: {}, open: true, showModal() {}};
""" + code + """
openSqlQuery({name: "one"}); nodes["#sql-query-text"].value = "DELETE FROM x"; nodes["#sql-query-output"].textContent = "done";
openSqlQuery({name: "one"}); const same = nodes["#sql-query-text"].value;
openSqlQuery({name: "two"}); const other = [nodes["#sql-query-text"].value, nodes["#sql-query-output"].textContent];
nodes["#sql-query-text"].value = "SELECT 1"; nodes["#sql-connection"].value = "server-2";
openSqlQuery({name: "two"}); const server = nodes["#sql-query-text"].value;
console.log(JSON.stringify([same, other, server]));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ["DELETE FROM x", ["", ""], ""])

    def test_only_the_environments_a_task_works_on_show_it(self):
        import ast as _ast
        nodes = [node for node in _ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, _ast.FunctionDef) and node.name in ("terminal_targets", "terminal_scope")]
        ns = {}
        exec(compile(_ast.Module(body=nodes, type_ignores=[]), "targets", "exec"), ns)
        targets = ns["terminal_targets"]
        self.assertEqual(targets({"kind": "script", "script": "publish_prod.sh", "target": "staging"}), ["staging"])
        self.assertEqual(targets({"kind": "script", "script": "publish_prod.sh"}), ["prod"])
        self.assertEqual(targets({"kind": "sync_env", "source": "main", "destination": ["t2", "prod"]}), ["t2", "prod"])
        self.assertEqual(targets({"kind": "sync_env", "source": "t1", "destination": "all"}), [])
        self.assertEqual(targets({"kind": "publish_both", "environments_only": True}), [])
        self.assertEqual(targets({"kind": "create_prod"}), ["(new)"])
        source = (APP / "static/app.js").read_text()
        self.assertIn("task.targets.includes(badge.dataset.target)", source)

    def test_layout_and_wording(self):
        page = (APP / "templates/index.html").read_text()
        layout = re.search(r'<div id="site-layout".*?</div>\s*<section id="site-targets"', page, re.S).group(0)
        self.assertLess(layout.index('id="envs"'), layout.index('id="site-packages"'))
        for text in (page, (APP / "static/app.js").read_text()):
            self.assertNotIn("Create production site", text)
        self.assertIn("Add environment", page)


if __name__ == "__main__":
    unittest.main()
