"""The OK button of a dialog that waits on a DNS check takes the focus once it is enabled (1.0.68)"""
from pathlib import Path
import subprocess
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
APP = ROOT_DIR / "admin/static/app.js"


class PromptFocusTests(unittest.TestCase):
    def test_the_button_takes_the_focus_once_enabled_unless_a_field_has_it(self):
        source = APP.read_text()
        start = source.index("function focusOnceEnabled(dialog, button) {")
        end = source.index("\n}\n", start) + 3
        harness = r"""
const assert = require("node:assert/strict");
const document = { activeElement: null };
const node = (tag, inside = true) => ({ tag, inside, matches: (selector) => selector.split(", ").includes(tag),
	focus() { document.activeElement = this; } });
const dialog = { contains: (element) => Boolean(element && element.inside) };
const button = node("button");

// Disabled while DNS is checked: the dialog's own focus on it was lost, and it cannot take it yet
document.activeElement = null;
button.disabled = true;
focusOnceEnabled(dialog, button);
assert.equal(document.activeElement, null);

// Enabled once the names point here: it takes the focus
button.disabled = false;
focusOnceEnabled(dialog, button);
assert.equal(document.activeElement, button);

// Not from a field of the dialog the user is typing in
const field = node("input");
document.activeElement = field;
focusOnceEnabled(dialog, button);
assert.equal(document.activeElement, field);

// A field outside the dialog does not count
document.activeElement = node("input", false);
focusOnceEnabled(dialog, button);
assert.equal(document.activeElement, button);
"""
        result = subprocess.run(["node", "-e", source[start:end] + "\n" + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
