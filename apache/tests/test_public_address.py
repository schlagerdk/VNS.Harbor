import ast
import ipaddress
import io
import logging
import os
import re
import socket
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, jsonify

APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app"


class PublicAddressTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.logger = logging.getLogger(__name__)
        self.cfg = {}
        self.local = ["10.0.0.5"]
        self.ns = dict(app=self.app, jsonify=jsonify, re=re, socket=socket, threading=threading, time=time,
                       cfg=self.cfg, PUBLIC_ADDRESS={}, PUBLIC_ADDRESS_LOCK=threading.Lock(),
                       server_addresses=lambda: list(self.local))
        names = {"valid_ip", "is_private", "detected_public_address", "api_server_address"}
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "public address", "exec"), self.ns)

    def server_address(self, *bodies):
        responses = list(bodies)
        calls = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

        def urlopen(url, timeout):
            calls.append(url)
            body = responses.pop(0)
            if isinstance(body, Exception):
                raise body
            return Response(body.encode())
        with patch("urllib.request.urlopen", urlopen), self.app.app_context():
            self.ns["urllib"] = __import__("urllib.request")
            return self.ns["api_server_address"]().json, calls

    def test_dns_points_to_the_detected_public_address_behind_nat(self):
        data, calls = self.server_address("fl=1\nip=93.161.56.227\nts=1\n")
        self.assertEqual(data["address"], "93.161.56.227")
        self.assertEqual(data["detected"], "93.161.56.227")
        self.assertFalse(data["behind_nat"])
        self.assertEqual(len(calls), 1)
        # Cached: the next page asks no outside service
        self.assertEqual(self.server_address()[1], [])

    def test_fallback_service_and_private_answers_are_not_trusted(self):
        data, calls = self.server_address(urllib.error.URLError("offline"), "93.184.215.14\n")
        self.assertEqual(data["address"], "93.184.215.14")
        self.assertEqual(len(calls), 2)
        self.ns["PUBLIC_ADDRESS"].clear()
        data, _ = self.server_address("ip=192.168.1.2\n", "not an address")
        self.assertEqual(data["detected"], "")
        self.assertEqual(data["address"], "10.0.0.5")
        self.assertTrue(data["behind_nat"])

    def test_address_set_under_preferences_wins_and_is_not_detected(self):
        self.cfg["public_address"] = "198.51.100.7"
        data, calls = self.server_address()
        self.assertEqual(data["address"], "198.51.100.7")
        self.assertEqual(calls, [])



class HeaderAddressTests(unittest.TestCase):
    """The server's internal and external address in the header, for administrators, copied with a click"""
    source = (Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/static/app.js").read_text()
    html = (Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/templates/index.html").read_text()

    def test_header_has_the_addresses_for_administrators_only(self):
        self.assertIn('<div id="server-ips" class="server-ips" role="group" aria-label="Server addresses" hidden></div>', self.html)
        self.assertIn("if (!currentUser.admin) {\n\t\tbox.hidden = true;", self.source)

    def test_external_is_where_dns_should_point_and_a_click_copies(self):
        self.assertIn('const external = data.address && !privateIPv4(data.address) ? data.address : "";', self.source)
        self.assertIn("await copyText(address, document.body);", self.source)

if __name__ == "__main__":
    unittest.main()


class HostNameTests(unittest.TestCase):
    def setUp(self):
        self.ns = dict(re=re, socket=socket)
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("host_name_for", "valid_ip")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "host name", "exec"), self.ns)
        self.dns = {"test-01.vns.dk": "185.150.199.44", "test-01.example.com": "192.0.2.80"}

    def host(self, name, hostname="test-01", fqdn="test-01", visited=""):
        def lookup(candidate, *args, **kwargs):
            if candidate not in self.dns:
                raise socket.gaierror("unknown")
            return [(None, None, None, None, (self.dns[candidate], 0))]
        with patch.object(socket, "gethostname", return_value=hostname), \
                patch.object(socket, "getfqdn", return_value=fqdn), patch.object(socket, "getaddrinfo", lookup):
            return self.ns["host_name_for"](name, {"185.150.199.44"}, visited)

    def test_short_host_name_under_the_bindings_domain(self):
        self.assertEqual(self.host("test.vns.dk"), "test-01.vns.dk")
        self.assertEqual(self.host("a.b.vns.dk"), "test-01.vns.dk")

    def test_full_host_name_is_tried_first(self):
        self.dns["web.vns.dk"] = "185.150.199.44"
        self.assertEqual(self.host("shop.other.dk", fqdn="web.vns.dk"), "web.vns.dk")

    def test_names_of_other_machines_unknown_names_and_the_name_itself_are_not_offered(self):
        self.assertEqual(self.host("shop.example.com"), "")
        self.assertEqual(self.host("vns.dk"), "")
        self.assertEqual(self.host("test-01.vns.dk"), "")
        self.assertEqual(self.host("test.vns.dk", fqdn="1.0.ip6.arpa", hostname="bad_name"), "")

    def test_admin_address_without_its_first_label_while_still_a_subdomain(self):
        self.assertEqual(self.host("min.narhat.dk", hostname="web", visited="admin.test-01.vns.dk"), "test-01.vns.dk")
        self.assertEqual(self.host("min.narhat.dk", hostname="web", visited="admin.test-01.vns.dk:443"), "test-01.vns.dk")

    def test_admin_on_a_domain_of_its_own_is_the_target_itself(self):
        self.dns.update({"admin.vns.dk": "185.150.199.44", "vns.dk": "104.21.75.189"})
        self.assertEqual(self.host("min.narhat.dk", hostname="web", visited="admin.vns.dk"), "admin.vns.dk")

    def test_a_domain_itself_gets_only_an_a_record(self):
        self.dns["admin.vns.dk"] = "185.150.199.44"
        self.assertEqual(self.host("narhat.dk", visited="admin.vns.dk"), "")

    def test_an_admin_visited_by_ip_falls_back_to_the_host_name(self):
        self.assertEqual(self.host("test.vns.dk", visited="185.150.199.44:8443"), "test-01.vns.dk")


class CertificateDnsTests(unittest.TestCase):
    def setUp(self):
        from werkzeug.exceptions import abort
        self.dns = {"shop.dk": "185.150.199.44", "www.shop.dk": "185.150.199.45", "prod.shop.dk": "198.51.100.20"}
        self.prod = {"PROD_BINDING": "prod.shop.dk", "PROD_LOCAL_PATH": "", "PROD_IP": "198.51.100.20"}
        self.ns = dict(os=os, socket=socket, ipaddress=ipaddress, abort=abort, APACHE_SITES="/sites", WEB_ROOT="/www",
                       conf_binding=lambda site: "shop.dk", read_text=lambda path: "",
                       server_aliases=lambda text, binding: ["www.shop.dk"],
                       parse_prod=lambda root, target: self.prod, target_arg=lambda site, data: "prod",
                       display_dns_name=lambda name: name,
                       own_addresses=lambda expect="": {expect} if expect else {"185.150.199.44"})
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("not_pointing", "certificate_names_point", "via_cloudflare")
                 or isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "CLOUDFLARE" for target in node.targets)]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "certificate dns", "exec"), self.ns)

    def check(self, **data):
        def lookup(name, *args, **kwargs):
            if name not in self.dns:
                raise socket.gaierror("unknown")
            return [(None, None, None, None, (self.dns[name], 0))]
        with patch.object(socket, "getaddrinfo", lookup):
            return self.ns["certificate_names_point"]("shop.dk", data)

    def test_every_name_of_the_main_site_must_point_here(self):
        from werkzeug.exceptions import HTTPException
        with self.assertRaises(HTTPException) as raised:
            self.check(scope="test")
        self.assertIn("www.shop.dk does not point to this server", raised.exception.description)
        self.assertNotIn("shop.dk,", raised.exception.description)
        self.dns["www.shop.dk"] = "185.150.199.44"
        self.assertIsNone(self.check(scope="test"))

    def test_a_name_behind_cloudflare_counts_as_pointing_here(self):
        # Proxied: the name resolves to Cloudflare, never to the server behind it
        self.dns["www.shop.dk"] = "104.21.32.1"
        self.assertIsNone(self.check(scope="test"))
        self.dns["www.shop.dk"] = "2606:4700:3030::6815:2001"
        self.assertIsNone(self.check(scope="test"))
        self.assertFalse(self.ns["via_cloudflare"](["104.21.32.1", "198.51.100.7"]))
        self.assertFalse(self.ns["via_cloudflare"]([]))
        self.assertFalse(self.ns["via_cloudflare"](["fe80::1%eth0"]))

    def test_a_remote_environment_must_point_to_its_own_server(self):
        self.assertIsNone(self.check(scope="prod"))
        from werkzeug.exceptions import HTTPException
        self.dns["prod.shop.dk"] = "185.150.199.44"
        with self.assertRaises(HTTPException) as raised:
            self.check(scope="prod")
        self.assertIn("does not point to 198.51.100.20", raised.exception.description)
        # A local environment issues its certificate here
        self.prod["PROD_LOCAL_PATH"] = "/www/shop.dk/_prod"
        self.assertIsNone(self.check(scope="prod"))
