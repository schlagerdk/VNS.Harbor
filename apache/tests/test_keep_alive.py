import ast
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request


APACHE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("keep_alive", APACHE / "lib/keep_alive.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.web = self.root / "www"
        self.units = self.root / "units"
        self.configs = self.root / "configs"
        for path in (self.web, self.units, self.configs):
            path.mkdir()

    def site(self, name, command=None):
        (self.web / name).mkdir()
        if command:
            (self.units / (helper.service_name(name) + ".service")).write_text("ExecStart=" + command)
            (self.configs / (name + ".conf")).write_text(
                "<VirtualHost *:443>\nServerName " + helper.service_name(name) + "\n")

    def test_active_dynamic_sites_receive_direct_requests_and_static_and_stopped_skip(self):
        self.site("æøå.test", "/usr/lib/dotnet/dotnet app.dll --urls http://127.0.0.1:4001")
        self.site("python.test", "/var/www/python.test/venv/bin/gunicorn --bind 127.0.0.1:4002 app:app")
        self.site("stopped.test", "/usr/lib/dotnet/dotnet app.dll --urls http://127.0.0.1:4003")
        self.site("html.test")
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock())
        opener.open.return_value.__exit__ = Mock(return_value=False)
        def active(command, **kwargs):
            return Mock(returncode=3 if command[-1] == "stopped.test.service" else 0)
        with patch.object(helper.subprocess, "run", side_effect=active), \
                patch.object(helper.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(helper.warm_sites(self.web, self.units, self.configs))
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual({r.full_url for r in requests},
                         {"http://127.0.0.1:4001/", "http://127.0.0.1:4002/"})
        self.assertIn("xn--5cab8c.test", [r.get_header("Host") for r in requests])
        self.assertTrue(all(r.get_header("X-forwarded-proto") == "https" for r in requests))

    def test_environments_on_other_servers_are_requested_on_their_address(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as directory:
            web = Path(directory)
            for site, target, text in (
                    ("dev.test", "production", "PROD_BINDING='live.test'   # c\nPROD_HTTPS=y\n"),
                    ("dev.test", "staging", "PROD_TARGET=windows\nPROD_BINDING='win.test'\nPROD_HTTPS=n\n"),
                    ("dev.test", "local", "PROD_LOCAL_PATH='/var/www/x'\nPROD_BINDING='local.test'\n"),
                    ("dev.test", "bad", "PROD_BINDING='$(rm -rf /)'\n")):
                folder = web / site / "scripts/targets" / target
                folder.mkdir(parents=True)
                (folder / "publish_prod.sh").write_text(text)
            (web / "old.test/scripts").mkdir(parents=True)
            (web / "old.test/scripts/publish_prod.sh").write_text("PROD_BINDING=old.test\n")
            self.assertEqual(sorted(helper.environment_urls(web)), [
                ("dev.test/production", "https://live.test/"), ("dev.test/staging", "http://win.test/"),
                ("old.test/scripts", "https://old.test/")])

            class Opener:
                def __init__(self):
                    self.urls = []

                def open(self, request, timeout):
                    self.urls.append(request.full_url)
                    if "win.test" in request.full_url:
                        raise helper.urllib.error.URLError("unreachable")
                    from unittest.mock import MagicMock
                    response = MagicMock()
                    response.__enter__.return_value = response
                    return response

            opener = Opener()
            self.assertFalse(helper.warm_environments(web, opener))
            self.assertEqual(len(opener.urls), 3, "One failure does not stop the others")

    def test_interval_boundary_and_disable(self):
        config = self.root / "settings.json"
        state = self.root / "state"
        helper.save({"enabled": True, "interval_minutes": 5}, config)
        with patch.object(helper, "warm_environments", return_value=True), \
                patch.object(helper, "warm_sites", return_value=True) as warm, \
                patch.object(helper.time, "time", side_effect=[1000, 1299, 1300]):
            self.assertTrue(helper.run(config, state))
            self.assertTrue(helper.run(config, state))
            self.assertEqual(warm.call_count, 1)
            self.assertTrue(helper.run(config, state))
            self.assertEqual(warm.call_count, 2)
            helper.save({"enabled": False, "interval_minutes": 5}, config)
            self.assertTrue(helper.run(config, state))
            self.assertEqual(warm.call_count, 2)

    def test_validation_and_persistent_settings(self):
        config = self.root / "settings.json"
        for value in (0, 1441, True, 1.5, "5"):
            with self.assertRaises(ValueError):
                helper.save({"enabled": True, "interval_minutes": value}, config)
        helper.save({"enabled": False, "interval_minutes": 1440}, config)
        self.assertEqual(helper.load(config), {"enabled": False, "interval_minutes": 1440})

    def test_install_preserves_settings_and_creates_minutely_cron(self):
        config = self.root / "settings.json"
        cron = self.root / "cron"
        helper.save({"enabled": False, "interval_minutes": 10}, config)
        with patch.object(helper, "__file__", "/vns/apache/lib/keep_alive.py"):
            helper.install(config, cron)
        self.assertEqual(helper.load(config)["interval_minutes"], 10)
        self.assertIn('* * * * * root /usr/bin/python3 "/vns/apache/lib/keep_alive.py"', cron.read_text())
        self.assertEqual(cron.stat().st_mode & 0o777, 0o644)

    def test_ambiguous_port_is_reported_not_silently_skipped(self):
        self.site("broken.test", "/usr/lib/dotnet/dotnet app.dll")
        output = io.StringIO()
        with patch.object(helper.subprocess, "run", return_value=Mock(returncode=0)), redirect_stderr(output):
            self.assertFalse(helper.warm_sites(self.web, self.units, self.configs))
        self.assertIn("no unambiguous loopback HTTP port", output.getvalue())

    def test_redirects_are_not_followed_externally(self):
        self.assertIsNone(helper.NoRedirect().redirect_request(None, None, 301, "", {},
                                                               "https://external.test"))

    def test_real_http_request_reaches_application_with_binding(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers["Host"], self.headers["X-Forwarded-Proto"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.site("real.test", "/usr/lib/dotnet/dotnet app.dll --urls http://127.0.0.1:" +
                      str(server.server_port))
            with patch.object(helper.subprocess, "run", return_value=Mock(returncode=0)):
                self.assertTrue(helper.warm_sites(self.web, self.units, self.configs))
            self.assertEqual(received, [("/", "real.test", "https")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_settings_api_reads_saves_and_rejects_invalid_interval(self):
        app = Flask(__name__)
        config = self.root / "settings.json"
        helper.save(helper.DEFAULTS, config)
        source = (APACHE / "solutionfiles/admin/app/app.py").read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "api_keep_alive")
        api_helper = Mock(load=lambda: helper.load(config),
                          save=lambda data: helper.save(data, config), validate=helper.validate)
        ns = {"app": app, "request": request, "jsonify": jsonify, "os": os,
              "_keep_alive_module": api_helper,
              "error": lambda message, status: (jsonify(error=message), status)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "keep alive api", "exec"), ns)
        client = app.test_client()
        self.assertEqual(client.get("/api/keep-alive").json["interval_minutes"], 5)
        response = client.put("/api/keep-alive", json={"enabled": False, "interval_minutes": 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(helper.load(config), {"enabled": False, "interval_minutes": 7})
        self.assertEqual(client.put("/api/keep-alive", json={"enabled": True, "interval_minutes": 0}).status_code, 400)
