import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "lib"))
import config_snippets  # noqa: E402

MAIN = """<VirtualHost *:443>
  ServerName test.example.dk
  RewriteEngine On

  # Ignore
  # vns {"key":"ignore","values":{"paths":"umbraco/api, umbraco/surface"}}
  RewriteRule ^/(?:umbraco/api|umbraco/surface)/ - [L]

  # Allowed IPs
  # vns {"key":"ips","values":{"addresses":"192.0.2.10 10.0.0.0/24","paths":"umbraco, config","response":"notfound"}}
  RewriteCond expr "!(-R '127.0.0.0/8' || -R '::1' || -R '192.0.2.10' || -R '10.0.0.0/24')"
  RewriteRule ^/(?:umbraco|config)(?:/|$) - [R=404,NC,L]

  # Rewrite to domain
  # vns {"key":"domain","values":{"domain":"test.example.dk"}}
  RewriteCond %{HTTP_HOST} !^test\\.example\\.dk$ [NC]
  RewriteRule ^ https://test.example.dk%{REQUEST_URI} [R=301,L]

  # Rewrite old/page
  # vns {"key":"rewrite","values":{"from":"old/page","to":"new/page"}}
  RewriteRule ^/old/page$ http://127.0.0.1:4019/new/page [P,L]

  ProxyPass / http://127.0.0.1:4019/ nocanon

  # Add Secure to ALL cookies
  # vns {"key":"cookies","values":{}}
  Header always edit Set-Cookie "(?i)^((?!.*;\\s*secure).*)$" "$1; Secure"
</VirtualHost>
"""


class ConfigSnippetTests(unittest.TestCase):
    def setUp(self):
        config_snippets.WARNINGS.clear()

    def test_the_main_sites_snippets_are_read_without_the_domain_redirect(self):
        snippets = config_snippets.read(MAIN)
        self.assertEqual([item["key"] for item in snippets], ["ignore", "ips", "rewrite", "cookies"])
        self.assertEqual(snippets[1]["values"]["response"], "notfound")

    def test_an_environment_gets_them_once_with_its_own_port_and_can_lose_them_again(self):
        template = (ROOT_DIR / "templates/sites/csharp/umbraco/apache.conf.tpl").read_text().replace("@PORT_HTTP@", "5555")
        snippets = config_snippets.read(MAIN)
        carried = config_snippets.apply_apache(template, snippets)
        self.assertIn("ProxyPass /old/page http://127.0.0.1:5555/new/page\n  ProxyPassReverse /old/page http://127.0.0.1:5555/new/page", carried)
        # Before the proxy of the whole site, or it would never be reached
        self.assertLess(carried.index("ProxyPass /old/page"), carried.index("ProxyPass / "))
        self.assertNotIn("4019", carried)
        self.assertNotIn("test.example.dk%{REQUEST_URI}", carried)
        self.assertEqual(config_snippets.apply_apache(carried, snippets), carried)
        # The Allowed IPs and Ignore rules come before the websocket rule, the rewrite before the proxy
        self.assertLess(carried.index("# Allowed IPs"), carried.index("=websocket"))
        self.assertLess(carried.index("# Rewrite old/page"), carried.index("ProxyPreserveHost"))
        self.assertEqual(config_snippets.apply_apache(carried, []), template)

    def test_rules_written_by_hand_are_kept(self):
        template = (ROOT_DIR / "templates/sites/csharp/apache.conf.tpl").read_text().replace("@PORT_HTTP@", "5555")
        own = template.replace("RewriteEngine On\n", "RewriteEngine On\n  RewriteRule ^/mine$ /yours [R=302,L]\n", 1)
        carried = config_snippets.apply_apache(own, config_snippets.read(MAIN))
        self.assertIn("RewriteRule ^/mine$ /yours [R=302,L]", carried)
        self.assertEqual(config_snippets.apply_apache(carried, []), own)

    def test_allowed_ips_for_the_whole_site_leaves_certificate_renewal_open(self):
        snippet = {"key": "ips", "name": "Allowed IPs", "values": {"addresses": "192.0.2.10", "scope": "site"}}
        rules = config_snippets.apache_rules(snippet, "5555")
        self.assertEqual(rules[0], "RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/")
        self.assertEqual(rules[-1], "RewriteRule ^/ - [F,NC,L]")
        rule = config_snippets.iis_rule({**snippet, "values": {**snippet["values"], "response": "forbidden"}})
        self.assertEqual(rule.find("match").get("url"), "^(?!\\.well-known/acme-challenge/)")

    def test_web_config_gets_vns_rules_after_https_and_keeps_its_own(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "web.config"
            path.write_text("""<?xml version="1.0" encoding="utf-8"?>
<configuration><location path="."><system.webServer><rewrite><rules>
<rule name="Rewrite HTTP to HTTPS" stopProcessing="true"><match url="(.*)" /></rule>
<rule name="Mine" stopProcessing="true"><match url="^mine$" /><action type="None" /></rule>
</rules></rewrite></system.webServer></location></configuration>""")
            snippets = config_snippets.read(MAIN)
            self.assertTrue(config_snippets.apply_iis(path, snippets))
            self.assertFalse(config_snippets.apply_iis(path, snippets))
            text = path.read_text()
            names = [line.split('name="')[1].split('"')[0] for line in text.splitlines() if "<rule name=" in line]
            self.assertEqual(names, ["Rewrite HTTP to HTTPS", "VNS: Ignore", "VNS: Allowed IPs", "VNS: Rewrite old/page", "Mine", "VNS: Add Secure to ALL cookies"])
            # A range cannot be matched by IIS: the single address is carried, and the range is told about
            self.assertIn("192\\.0\\.2\\.10", text)
            self.assertNotIn("10.0.0.0/24", text)
            self.assertTrue(any("10.0.0.0/24" in message for message in config_snippets.WARNINGS))
            self.assertTrue(config_snippets.apply_iis(path, []))
            self.assertNotIn("VNS: ", path.read_text())
            self.assertIn('name="Mine"', path.read_text())


if __name__ == "__main__":
    unittest.main()
