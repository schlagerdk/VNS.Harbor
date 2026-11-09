"""Add the environment helpers from the admin's app.py to a test namespace built from selected functions."""
import ast
import glob
import os
from pathlib import Path
import re

from flask import abort

APP = Path(__file__).resolve().parents[1] / "solutionfiles/admin/app/app.py"
FUNCTIONS = {"target_folder", "target_ids", "target_scope", "target_arg", "with_target", "remote_key",
             "adopted_sites", "service_dll", "project_assembly", "adoption_candidates"}
CONSTANTS = {"TARGET_RE", "TARGET_SCRIPTS"}


def add_target_helpers(ns):
    for name, value in (("glob", glob), ("os", os), ("re", re), ("abort", abort)):
        ns.setdefault(name, value)
    nodes = [node for node in ast.parse(APP.read_text()).body
             if (isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS)
             or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in CONSTANTS)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "environment helpers", "exec"), ns)
    return ns
