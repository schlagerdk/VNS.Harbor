import ast
import io
import logging
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


if __name__ == "__main__":
    unittest.main()


class HostNameTests(unittest.TestCase):
    def setUp(self):
        self.ns = dict(re=re, socket=socket)
        nodes = [node for node in ast.parse((APP / "app.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == "host_name_for"]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "host name", "exec"), self.ns)
        self.dns = {"test-01.vns.dk": "185.150.199.44", "test-01.example.com": "192.0.2.80"}

    def host(self, name, hostname="test-01", fqdn="test-01"):
        def lookup(candidate, *args, **kwargs):
            if candidate not in self.dns:
                raise socket.gaierror("unknown")
            return [(None, None, None, None, (self.dns[candidate], 0))]
        with patch.object(socket, "gethostname", return_value=hostname), \
                patch.object(socket, "getfqdn", return_value=fqdn), patch.object(socket, "getaddrinfo", lookup):
            return self.ns["host_name_for"](name, {"185.150.199.44"})

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
