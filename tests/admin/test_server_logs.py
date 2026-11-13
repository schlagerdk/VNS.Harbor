import ast
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request

from lib.domain_names import service_name
from tests.admin.app_targets import add_target_helpers


ROOT_DIR = Path(__file__).resolve().parents[2]
class ServerLogTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.runner = Mock(return_value=Mock(returncode=0, stdout="example log\n"))
        self.ns = dict(app=self.app, request=request, jsonify=jsonify, os=os,
                       ADMIN_SITE="admin.example.test", service_name=service_name,
                       subprocess=Mock(run=self.runner, SubprocessError=subprocess.SubprocessError),
                       error=lambda text, status=400: (jsonify(error=text), status))
        source = ROOT_DIR / "admin/app.py"
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in ("apache_error_logs", "api_server_logs", "api_server_logs_clear")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "server logs", "exec"), self.ns)
        add_target_helpers(self.ns)
        self.client = self.app.test_client()

    def test_admin_journal_is_bounded_and_has_no_pager(self):
        response = self.client.get("/api/server-logs?source=admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["text"], "example log\n")
        self.assertEqual(self.runner.call_args.args[0],
                         ["journalctl", "-u", "admin.example.test", "-n", "300", "--no-pager", "-o", "short-iso"])

    def test_clear_empties_only_a_shown_log_file(self):
        with patch.object(os.path, "isfile", return_value=True), patch.object(os, "truncate") as truncate:
            refused = self.client.post("/api/server-logs/clear", json={"path": "/etc/passwd"})
            cleared = self.client.post("/api/server-logs/clear", json={"path": "/var/log/apache2/error.log"})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(cleared.status_code, 200)
        truncate.assert_called_once_with("/var/log/apache2/error.log", 0)

    def test_apache_reads_only_fixed_existing_error_logs(self):
        with patch.object(os.path, "isfile", return_value=True):
            response = self.client.get("/api/server-logs?source=apache&path=/etc/passwd")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.call_args.args[0],
                         ["tail", "-v", "-n", "300", "--", "/var/log/apache2/error.log",
                          "/var/log/apache2/admin.example.test-rewrite-error.log",
                          "/var/log/apache2/admin.example.test-443-error.log"])

    def test_invalid_missing_and_failed_logs_report_errors(self):
        self.assertEqual(self.client.get("/api/server-logs?source=unknown").status_code, 400)
        self.runner.assert_not_called()
        with patch.object(os.path, "isfile", return_value=False):
            self.assertEqual(self.client.get("/api/server-logs?source=apache").status_code, 404)
        self.runner.return_value = Mock(returncode=1, stderr="Permission denied")
        self.assertEqual(self.client.get("/api/server-logs").status_code, 502)
        self.runner.side_effect = subprocess.TimeoutExpired("journalctl", 20)
        self.assertEqual(self.client.get("/api/server-logs").status_code, 502)
