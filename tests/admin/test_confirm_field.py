"""A window that asks for a name to be typed keeps its button off until the name is typed as it is, and shows the
name as the other confirmations do: Delete database's and Delete user's were on at once and set the name apart (1.0.71)"""
from pathlib import Path
import subprocess
import unittest

ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/static/app.js"


class ConfirmFieldTests(unittest.TestCase):
    def test_the_button_waits_for_the_name(self):
        source = APP.read_text()
        start = source.index("function openFormDialog(")
        dialog = source[start:source.index("\n}\n", start)]
        self.assertIn('form.querySelector("input[data-match]")', dialog)
        self.assertIn("confirm.value !== confirm.dataset.match", dialog)
        # After a failed try the button follows the field again, not simply on
        self.assertNotIn("button.disabled = false", dialog)
        field = source[source.index("function confirmField("):]
        self.assertIn("field.dataset.match = match", field[:field.index("\n}\n")])
        harness = r"""
const assert = require("node:assert/strict");
const confirm = { value: "", dataset: { match: "testtest" } };
const ok = { disabled: false };
const syncConfirm = () => { ok.disabled = Boolean(confirm) && confirm.value !== confirm.dataset.match; };
for (const [typed, off] of [["", true], ["testtes", true], ["testtest ", true], ["testtest", false]]) {
	confirm.value = typed;
	syncConfirm();
	assert.equal(ok.disabled, off, typed);
}
"""
        self.assertIn("const syncConfirm = () => { $(\"#form-dialog-ok\").disabled = Boolean(confirm) && confirm.value !== confirm.dataset.match; };", dialog)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_name_looks_as_in_the_other_confirmations(self):
        css = (ROOT_DIR / "admin/static/style.css").read_text()
        self.assertNotIn(".confirm-field .confirmation-copy {", css)


if __name__ == "__main__":
    unittest.main()
