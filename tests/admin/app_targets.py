"""Add the environment helpers from the admin's app.py to a test namespace built from selected functions."""
import ast
import glob
import os
from pathlib import Path
import re

import flask
from flask import abort

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/app.py"
FUNCTIONS = {"fs_full_path", "target_folder", "target_ids", "target_scope", "target_arg", "with_target", "remote_key",
             "adopted_sites", "service_dll", "project_assembly", "adoption_candidates", "managed_by", "publishable_targets", "sync_prod_binding",
             # A migrated site (phase 6)
             "site_migrated", "migrated_site_json", "custom_scripts_dir", "migrated_scripts", "target_scripts",
             # An empty binding: an administrator's, and only one (1.0.77); custom scripts of an environment
             "any_name_site", "env_scripts_dir", "listed_scripts",
             # A file downloaded, or a picture or PDF shown (1.0.79)
             "file_response",
             "harbor_script_command", "publish_entry", "save_migrated_setting", "harbor_argv",
             # Publish all goes the way the work goes
             "environment_order"}
CONSTANTS = {"TARGET_RE", "TARGET_SCRIPTS", "MANAGED_DIR", "HARBOR_SCRIPTS", "HARBOR_TARGET_SCRIPTS", "HARBOR_WRAPPERS", "ENVIRONMENT_ORDER",
             "PREVIEW_TYPES"}


import importlib.util

_spec = importlib.util.spec_from_file_location("vns_access", APP.parents[1] / "lib/access.py")
access = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(access)
ROOT_USER = {"id": access.ROOT, "username": "admin", "role": "admin", "root": True}


def add_target_helpers(ns):
    """Also the access checks, as the root administrator: the tests of the rest do not test who may do it"""
    import json
    for name, value in (("glob", glob), ("os", os), ("re", re), ("abort", abort), ("json", json),
                        ("HARBOR_COMMAND", "/vns/bin/harbor"), ("SCRIPT_RE", re.compile(r"^[A-Za-z0-9_.-]+\.sh$")),
                        ("CUSTOM_SCRIPT_RE", re.compile(r"[\w-][\w.-]{0,99}\.sh")), ("default_site_name", lambda: ""),
                        # Never the test server's own state: no site in a test is migrated unless the test says so
                        ("HARBOR_SITES", "/nonexistent/harbor/sites"),
                        ("parse_prod", lambda root, target="prod": None), ("remote_status", lambda site, target="prod": {}),
                        ("access", access), ("g", flask.g),
                        # Logged in (a session with a user) is the root administrator, as before there were more users
                        ("me", lambda: ROOT_USER),
                        ("session_user", lambda: ROOT_USER if flask.session.get("user") else None),
                        ("is_admin", lambda: True), ("require_admin", lambda: None), ("require_site", lambda *args: None),
                        ("require_sql", lambda *args: None), ("allowed_targets", lambda site, targets, action: list(targets)),
                        ("audit", lambda message: None), ("authorize_task", lambda data: None), ("sees_session", lambda s: True),
                        ("file_access", lambda *args: None), ("unit_env", lambda site, unit: "main"),
                        ("owns_connection", lambda identifier: True),
                        # This server takes new sites: a test of one that does not says so
                        ("takes_no_sites", lambda why: None),
                        # A script the admin runs is root's: here the tests' own user makes them, so it is not checked
                        ("own_scripts_dir", lambda site: "/nonexistent/harbor/sites/" + site + "/scripts"),
                        ("runnable", lambda path: path)):
        ns.setdefault(name, value)
    nodes = [node for node in ast.parse(APP.read_text()).body
             if (isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS)
             or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in CONSTANTS)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "environment helpers", "exec"), ns)
    return ns
