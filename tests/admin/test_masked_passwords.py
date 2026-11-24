"""A password typed in a dialog reaches the server: the field holds only dots, and the forms were read with FormData,
which sent the dots, so a SQL connection's login was refused with the right password typed (1.0.70)"""
from pathlib import Path
import re
import subprocess
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/static/app.js"


class MaskedPasswordTests(unittest.TestCase):
    def test_form_values_give_the_password_not_its_dots(self):
        source = APP.read_text()
        start = source.index("function formValues(form)")
        end = source.index("\n}\n", start) + 3
        harness = r"""
const assert = require("node:assert/strict");
// A masked field: the field holds dots, its value property gives the password back
const password = { name: "password", value: "Secret-13char" };
const form = { fields: [["name", "sql-01"], ["password", "•".repeat(13)]], querySelectorAll: () => [password] };
globalThis.FormData = class { constructor(form) { return form.fields; } };
assert.deepEqual(formValues(form), { name: "sql-01", password: "Secret-13char" });
"""
        result = subprocess.run(["node", "-e", source[start:end] + "\n" + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_form_is_read_with_form_data_alone(self):
        """Only formValues reads FormData: every form goes through it"""
        self.assertEqual(re.findall(r"Object\.fromEntries\(new FormData\((\w+)\)\)", APP.read_text()), ["form"])
