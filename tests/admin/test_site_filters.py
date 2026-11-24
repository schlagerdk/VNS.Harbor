"""The filters above the sites (All, With environments, Umbraco, …) work for every user, also one whose header has no
search (a user's never had one, so a click on a filter was undone at once; 1.0.77)"""
import ast
import json
from pathlib import Path
import subprocess
import unittest

ADMIN = Path(__file__).resolve().parents[2] / "admin"


def function_source(text, name):
	"""A top-level JS function's source, up to the closing brace at the start of a line"""
	start = text.index(f"\nfunction {name}(") + 1
	return text[start:text.index("\n}\n", start) + 2]


class SiteFilterTests(unittest.TestCase):
	def test_a_filter_stays_chosen_without_the_search(self):
		render = function_source((ADMIN / "static/app.js").read_text(), "renderSites")
		head = render[:render.index("renderStats();")]
		harness = """
let filter = "umbraco", query = "shop";
const field = { value: "shop", hidden: false };
const $ = () => field;
const run = (sites, header) => {
	const listedSites = () => sites;
	const currentUser = { header };
	eval(%s);
	return { filter, query, hidden: field.hidden };
};
console.log(JSON.stringify([run([1, 2, 3], { search: false }), run([1], { search: true })]));
""" % json.dumps(head[head.index("{") + 1:])
		result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		without_search, one_site = json.loads(result.stdout)
		self.assertEqual(without_search, {"filter": "umbraco", "query": "", "hidden": True}, "the filter is kept, the search emptied")
		self.assertEqual(one_site["filter"], "all", "one site needs no filter")

	def test_a_user_has_the_site_search(self):
		tree = ast.parse((ADMIN / "app.py").read_text())
		me = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "api_me")
		header = next(keyword.value for node in ast.walk(me) if isinstance(node, ast.Call)
					  for keyword in node.keywords if keyword.arg == "header")
		user_header = eval(compile(ast.Expression(header.orelse), "app.py", "eval"), {"HEADER_PARTS": ("search", "addresses", "files", "terminal")})
		self.assertEqual(user_header, {"search": True, "addresses": False, "files": False, "terminal": False})


if __name__ == "__main__":
	unittest.main()
