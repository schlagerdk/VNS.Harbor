import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("move_site", ROOT_DIR / "lib/move_site.py")
move_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(move_site)

HTTPS_CONF = """<VirtualHost *:80>
  ServerAdmin mhs@vns.dk
  ServerName site.example.test
  DocumentRoot /var/www/site/_publish/wwwroot

  RewriteEngine On
  RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/
  RewriteRule ^(.*)$ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]

  ErrorLog /var/log/apache2/site-rewrite-error.log
</VirtualHost>

<VirtualHost *:443>
  ServerAdmin mhs@vns.dk
  ServerName site.example.test
  ServerAlias www.site.example.test
  DocumentRoot /var/www/site/_publish/wwwroot

  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteRule /(.*) ws://127.0.0.1:4003/$1 [P,L]

  ProxyPreserveHost On
  ProxyRequests Off
  RequestHeader set X-Forwarded-Proto "https"
  Header always set Strict-Transport-Security "max-age=31536000"

  # VNS snippet: allowed IPs
  <Location />
    Require ip 10.0.0.0/8
  </Location>

  ProxyPass /vns-offline/ !
  ProxyPass / http://127.0.0.1:4003/ nocanon
  ProxyPassReverse / http://127.0.0.1:4003/

  SSLEngine On

  SSLCertificateFile /etc/ssl/certs/site_ecc/fullchain.cer
  SSLCertificateKeyFile /etc/ssl/certs/site_ecc/site.key

  ErrorLog /var/log/apache2/site-443-error.log
</VirtualHost>
"""


class HttpConfTests(unittest.TestCase):
    def test_https_site_answers_on_port_80_without_a_certificate(self):
        text = move_site.http_conf(HTTPS_CONF)
        self.assertEqual(text.count("<VirtualHost"), 1)
        self.assertIn("<VirtualHost *:80>", text)
        self.assertNotIn("443", text)
        self.assertNotRegex(text, r"(?m)^\s*SSL")
        self.assertNotIn("Strict-Transport-Security", text)
        self.assertNotIn("https://", text)
        self.assertIn('X-Forwarded-Proto "http"', text)
        self.assertIn("ErrorLog /var/log/apache2/site-80-error.log", text)
        self.assertEqual(text.count("ProxyPass /.well-known/acme-challenge/ !"), 1)
        self.assertLess(text.index("ProxyRequests Off"), text.index("acme-challenge"))

    def test_aliases_and_snippets_are_kept(self):
        text = move_site.http_conf(HTTPS_CONF)
        self.assertIn("ServerAlias www.site.example.test", text)
        self.assertIn("Require ip 10.0.0.0/8", text)
        self.assertIn("ProxyPass /vns-offline/ !", text)

    def test_twice_changes_nothing_more(self):
        once = move_site.http_conf(HTTPS_CONF)
        self.assertEqual(move_site.http_conf(once), once)

    def test_http_site_is_left_alone(self):
        conf = "<VirtualHost *:80>\n  ServerName site.test\n  ProxyPass / http://127.0.0.1:4000/\n</VirtualHost>\n"
        self.assertEqual(move_site.http_conf(conf), conf)

    def test_redirect_made_by_issue_certificate_goes_too(self):
        conf = ('<VirtualHost *:80>\n  ServerName site.test\n  RewriteEngine On\n  RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]\n</VirtualHost>\n\n'
                '<VirtualHost *:443>\n  ServerName site.test\n  ProxyRequests Off\n  ProxyPass / http://127.0.0.1:4000/\n  SSLEngine On\n</VirtualHost>\n')
        text = move_site.http_conf(conf)
        self.assertTrue(text.startswith("<VirtualHost *:80>\n  ServerName site.test\n  ProxyRequests Off"), text)


class PortTests(unittest.TestCase):
    def test_local_ports_change_and_apache_ports_do_not(self):
        text = move_site.set_port(HTTPS_CONF, "4100")
        self.assertNotIn("4003", text)
        self.assertEqual(text.count("127.0.0.1:4100"), 3)
        self.assertIn("<VirtualHost *:443>", text)
        service = 'ExecStart=/usr/lib/dotnet/dotnet /var/www/site/_publish/App.dll --urls "http://localhost:4003"\n'
        self.assertIn("localhost:4100", move_site.set_port(service, "4100"))


class HttpSettingsTests(unittest.TestCase):
    def test_umbraco_url_and_use_https_change_and_the_rest_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "appsettings.json"
            settings.write_text('{\n  // a comment\n  "Umbraco": { "CMS": {\n    "Global": { "UseHttps": true },\n'
                                '    "WebRouting": { "UmbracoApplicationUrl": "https://site.example.test" } } }\n}\n')
            other = Path(folder) / "appsettings.Production.json"
            other.write_text('{ "Logging": { "UseHttps": true } }\n')
            self.assertEqual(move_site.http_settings(folder, "site.example.test"), 1)
            text = settings.read_text()
            self.assertIn('"UmbracoApplicationUrl": "http://site.example.test"', text)
            self.assertIn('"UseHttps": false', text)
            self.assertIn("// a comment", text)
            self.assertIn('"UseHttps": true', other.read_text())


class ScriptTests(unittest.TestCase):
    script = (ROOT_DIR / "tests/legacy/move_site.sh").read_text()

    def test_new_server_gets_http_and_its_own_port(self):
        receive = self.script[self.script.index("receive() {"):self.script.index("if [[ ${1:-} == --receive ]]")]
        self.assertIn("lib/move_site.py http-conf", receive)
        self.assertIn("allocate_ports", receive)
        self.assertIn("PROD_HTTPS='n'", receive)
        # Certificates come only after the sites run, and only when asked for
        self.assertLess(receive.index("apache2ctl configtest"), receive.index("issue_certificate"))

    def test_nothing_is_stopped_before_the_new_server_is_checked(self):
        self.assertLess(self.script.index('on_new_server "bash -c $(printf %q "${_check}")"'), self.script.index('systemctl stop "${_unit}"'))

    def test_failure_starts_the_sites_again_and_cleans_up_the_new_server(self):
        undo = self.script[self.script.index("undo() {"):self.script.index("trap undo EXIT")]
        self.assertIn('systemctl start "${_unit}"', undo)
        self.assertIn('rm -rf $(printf %q "${_folder}")', undo)


class HttpsChoiceTests(unittest.TestCase):
    source = (ROOT_DIR / "admin/static/app.js").read_text()
    html = (ROOT_DIR / "admin/templates/index.html").read_text()

    def test_new_site_and_set_up_site_have_the_https_option_card(self):
        self.assertNotIn('<select name="certificate">', self.html)
        # Off at first: HTTPS is chosen, not taken for granted
        self.assertEqual(self.html.count('<input name="https_choice" type="checkbox">'), 2)
        self.assertEqual(self.html.count('<input type="hidden" name="certificate" value="n">'), 2)
        # New site's DNS card also tells the temporary address when the binding does not point here (1.0.77)
        self.assertIn("dnsCard(siteForm.binding, { onState: (state) => {\n\tsiteHttps.state(state);", self.source)
        self.assertIn('dnsCard($("#form-setup-site").binding, { onState: setupHttps.state })', self.source)
        # Shown only when the binding points here: hidden, not greyed out, also for an empty binding (1.0.77)
        self.assertIn('const allowed = value === "here";', self.source)
        self.assertIn('card.closest(".https-choice").hidden = !allowed;', self.source)


class AdminTests(unittest.TestCase):
    source = (ROOT_DIR / "admin/static/app.js").read_text()
    app = (ROOT_DIR / "admin/app.py").read_text()

    def test_main_site_has_one_copy_dialog_for_this_server_and_another(self):
        self.assertIn('if (!isTarget(env) && !site.admin && !site.managed_by) addManage("Copy site", () => copySite(site, env));', self.source)
        self.assertNotIn("openMoveSiteDialog", self.source)
        html = (ROOT_DIR / "admin/templates/index.html").read_text()
        self.assertIn('name="destination" value="server"', html)
        self.assertIn('name="remove_here"', html)

    def test_migrating_is_copying_and_removing_it_here(self):
        self.assertIn('copy: !removing,', self.source)
        self.assertIn('delete_local: removing && field("delete_local").checked ? "y" : "n"', self.source)

    def test_move_site_is_for_administrators_and_runs_the_script(self):
        self.assertRegex(self.app, r'ADMIN_TASKS = \{[^}]*"move_site"')
        self.assertIn('[*harbor_argv("move_site.sh"), site]', self.app)

    def test_environment_to_another_server_gets_https_once_it_runs_there(self):
        self.assertIn('extra_env["VNS_MOVE_HTTPS"] = extra_env["VNS_PROD_HTTPS"]', self.app)
        self.assertIn('extra_env["VNS_PROD_HTTPS"] = "n"', self.app)
        self.assertIn("const otherServer = moving && !same", self.source)
        move_env = (ROOT_DIR / "tests/legacy/move_env.sh").read_text()
        self.assertIn('issue_wanted_certificate "${_moving}"', move_env)
        self.assertIn('issue_wanted_certificate "${_finalId}"', move_env)

    def test_https_is_a_choice_checked_against_the_new_server(self):
        self.assertIn('"VNS_MOVE_HTTPS": "y" if payload.get("https") == "y" else "n",', self.app)
        # A server in Servers behind a router is checked against its public address
        self.assertIn('dnsCard(field("server_binding"), { expect: () => publicAddressOf(field("host").value.trim()) || field("host").value.trim(), aliases: $("#copy-server-env-bindings") })', self.source)
        self.assertIn('const host = target === "remote" ? prodForm.host.value.trim() : target === "same" ? prodForm.dataset.moveHost || "" : "";', self.source)
        self.assertIn('return publicAddressOf(host) || host;', self.source)
        self.assertIn("const expectNow = () => (typeof expected === \"function\" ? expected() : expected) || \"\";", self.source)

    def test_a_certificate_that_fails_leaves_the_site_on_http(self):
        script = (ROOT_DIR / "tests/legacy/move_site.sh").read_text()
        receive = script[script.index("receive() {"):script.index("if [[ ${1:-} == --receive ]]")]
        self.assertIn('bash ./test_action.sh "${_main}" issue_certificate --yes || _failed+=("${_main}")', receive)
        self.assertIn("They answer on HTTP", receive)

    def test_new_server_can_get_a_new_folder_binding_and_project(self):
        self.assertIn('folder: field("server_folder").value.trim(), binding: field("server_binding").value.trim(),', self.source)
        self.assertIn("const serverProject = Boolean(site.project) && !hasProd(site);", self.source)
        self.assertIn('"VNS_MOVE_FOLDER": folder, "VNS_MOVE_BINDING": binding, "VNS_MOVE_PROJECT": project,', self.app)
        self.assertIn("A site with environments keeps its project name", self.app)

    def test_renamed_site_is_rewritten_on_the_new_server_and_built_with_a_new_project(self):
        script = (ROOT_DIR / "tests/legacy/move_site.sh").read_text()
        self.assertIn('python3 lib/copy_site.py rename "${_rootPath}/${_from}" "${_rootPath}/${_to}"', script)
        self.assertIn('[[ -z ${_toProject} ]] || _build=${_to}', script)
        self.assertIn('copy_to_new_server "${_folders[_index]}/" "${_folder}/"', script)
        self.assertIn('for _name in "${_newNames[@]}"; do', script)

    def test_copy_here_of_a_site_with_environments_says_they_stay(self):
        self.assertIn('$("#copy-no-environments").hidden = !hasProd(site);', self.source)
        self.assertNotIn("sites with production targets cannot be copied", self.app)

    def test_only_what_the_user_may_do_is_offered(self):
        self.assertIn("const server = currentUser.admin && Boolean(main && (main.unit || main.apache));", self.source)
        self.assertIn('$("#copy-server-card").hidden = !server;', self.source)
        self.assertIn('$("#copy-destination").hidden = !(here && server);', self.source)


if __name__ == "__main__":
    unittest.main()
