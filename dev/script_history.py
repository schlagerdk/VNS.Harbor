"""Every version the site templates' scripts have had, from git, into harbor/state/script_history.json.

The migration (harbor/state/migrate.py) takes a script in a site's scripts folder for Harbor's own when it is one of
these versions with the placeholders (@SITE@ and the like) filled in. Run it again when a template changes:
    python3 dev/script_history.py
"""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(*argv):
    return subprocess.run(["git", "-C", str(ROOT), *argv], capture_output=True, text=True, check=True).stdout


def main():
    """Every .sh that has ever been in the site templates (apache/solutionfiles, moved and deleted ones too), by name"""
    found = {}
    log = git("log", "--all", "--name-only", "--format=@%H", "--", "apache/solutionfiles", "apache/templates", "solutionfiles").split("\n")
    commit = None
    for line in log:
        if line.startswith("@"):
            commit = line[1:]
            continue
        line = line.strip()
        if not line.endswith(".sh") or "/project/" in line or not commit:
            continue
        try:
            text = git("show", f"{commit}:{line}")
        except subprocess.CalledProcessError:
            # Deleted in that commit
            continue
        found.setdefault(Path(line).name, set()).add(text)
    data = {name: sorted(texts) for name, texts in sorted(found.items())}
    (ROOT / "harbor/state/script_history.json").write_text(json.dumps(data, indent=1) + "\n")
    print(f"{sum(len(texts) for texts in data.values())} versions of {len(data)} scripts: {', '.join(data)}")


if __name__ == "__main__":
    main()
