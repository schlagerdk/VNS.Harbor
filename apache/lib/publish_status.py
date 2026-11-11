"""Track .NET project configuration for publishes and package update checks."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


def fingerprint(root):
    root = Path(root).resolve()
    projects = sorted(root.glob("*.csproj"))
    if not projects:
        raise ValueError("Publish tracking requires a C# project")
    files = list(projects)
    for folder in (root, *root.parents):
        for name in ("Directory.Packages.props", "Directory.Build.props", "Directory.Build.targets"):
            path = folder / name
            if path.is_file():
                files.append(path)
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root) if path.is_relative_to(root) else path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def status(root, scope, current):
    path = Path(root) / ".vns-publish" / (scope + ".json")
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return "unknown"
    if not isinstance(data, dict) or not isinstance(data.get("fingerprint"), str):
        raise ValueError("Invalid publish status: " + str(path))
    return "current" if data["fingerprint"] == current else "pending"


def record(root, scope, value, **metadata):
    folder = Path(root) / ".vns-publish"
    folder.mkdir(mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=scope + "-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"fingerprint": value, **metadata}, stream)
            stream.write("\n")
        os.replace(temporary, folder / (scope + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def updates_details(root, current):
    try:
        data = json.loads((Path(root) / ".vns-publish/updates.json").read_text())
    except FileNotFoundError:
        return {"status": "", "packages": []}
    if not isinstance(data, dict) or not isinstance(data.get("fingerprint"), str) or data.get("status") not in ("current", "available", "error"):
        raise ValueError("Invalid package update status")
    packages = data.get("packages", [])
    if not isinstance(packages, list) or any(
        not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key]
                                         for key in ("name", "current", "latest"))
        for item in packages
    ):
        raise ValueError("Invalid package update details")
    return {"status": data["status"], "packages": packages} if data["fingerprint"] == current else {"status": "", "packages": []}


def updates_status(root, current):
    return updates_details(root, current)["status"]


def parse_updates(output):
    data = json.loads(output)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), list) or not data["projects"]:
        raise ValueError("Invalid package update response")
    packages = {}
    for project in data["projects"]:
        if not isinstance(project, dict) or not isinstance(project.get("frameworks", []), list):
            raise ValueError("Invalid package update project")
        for framework in project.get("frameworks", []):
            if not isinstance(framework, dict) or not isinstance(framework.get("topLevelPackages", []), list):
                raise ValueError("Invalid package update framework")
            for item in framework.get("topLevelPackages", []):
                if not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key]
                    for key in ("id", "resolvedVersion", "latestVersion")
                ):
                    raise ValueError("Invalid package update entry")
                name = item["id"]
                entry = {"name": name, "current": item["resolvedVersion"], "latest": item["latestVersion"]}
                if name.lower() in packages and packages[name.lower()] != entry:
                    raise ValueError("Conflicting package versions across target frameworks: " + name)
                packages[name.lower()] = entry
    return sorted(packages.values(), key=lambda item: item["name"].lower())


# dotnet list package --outdated names the newest version of a package, also when it is built for a newer .NET than
# the project's, like Microsoft.AspNetCore.* 11.0.0 for a net10.0 project: adding that fails with NU1202.
# The check asks NuGet which frameworks each version supports and offers the newest one the project can use.
NUGET_REGISTRATION = "https://api.nuget.org/v3/registration5-gz-semver2/{}/index.json"


def version_key(version):
    """Sort key for a NuGet version: numbers first, and a release after its pre-releases"""
    base, _, pre = version.split("+")[0].partition("-")
    numbers = [int(part) for part in re.findall(r"\d+", base)][:4]
    return (tuple(numbers + [0] * (4 - len(numbers))), not pre, pre.lower())


def _core_framework(name):
    """(major, minor, platform) of a .NET (Core) framework name such as net10.0, .NETCoreApp3.1 or net8.0-windows"""
    match = re.fullmatch(r"(?:\.netcoreapp|netcoreapp|net)(\d+)\.(\d+)(?:-([a-z]+)[\d.]*)?", (name or "").strip().lower())
    return (int(match.group(1)), int(match.group(2)), match.group(3) or "") if match else None


def framework_supports(group, project):
    """Whether a package built for the framework group can be used by a project for the framework project"""
    name = (group or "").strip().lower()
    if not name or name in ("any", "agnostic") or name.startswith((".netstandard", "netstandard")):
        return True
    built, used = _core_framework(name), _core_framework(project)
    if not built or not used:
        return False
    return built[:2] <= used[:2] and (not built[2] or built[2] == used[2])


def _nuget_json(url):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "vns-package-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
    return json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)


def compatible_latest(name, current, frameworks):
    """The newest listed version of a package above current that every one of the project's frameworks can use,
    or None. Pre-releases only when the current version is one."""
    index = _nuget_json(NUGET_REGISTRATION.format(urllib.parse.quote(name.lower())))
    best = None
    for page in index.get("items", []):
        items = page.get("items")
        if items is None:
            items = _nuget_json(page["@id"]).get("items", [])
        for item in items:
            entry = item.get("catalogEntry", {})
            version = entry.get("version", "")
            if not version or entry.get("listed") is False or version_key(version) <= version_key(current):
                continue
            if "-" in version.split("+")[0] and "-" not in current:
                continue
            groups = [group.get("targetFramework", "") for group in entry.get("dependencyGroups") or []]
            if groups and not all(any(framework_supports(group, framework) for group in groups) for framework in frameworks):
                continue
            if best is None or version_key(version) > version_key(best):
                best = version
    return best


def project_frameworks(output):
    """The frameworks the project targets, as dotnet list package names them"""
    data = json.loads(output)
    return sorted({framework["framework"] for project in data.get("projects", []) for framework in project.get("frameworks", [])
                   if isinstance(framework, dict) and isinstance(framework.get("framework"), str)})


def fit_to_frameworks(found, frameworks, show_output=False):
    """The updates the project can use: each package's newest version for the project's frameworks"""
    if not frameworks:
        return found
    fitted = []
    for item in found:
        try:
            latest = compatible_latest(item["name"], item["current"], frameworks)
        except (OSError, ValueError, KeyError) as exc:
            # NuGet could not be asked: the version dotnet named is kept
            if show_output:
                print(f'{item["name"]}: could not check which versions support {", ".join(frameworks)} ({exc})', file=sys.stderr, flush=True)
            fitted.append(item)
            continue
        if latest is None:
            if show_output:
                print(f'{item["name"]} {item["latest"]} needs a newer .NET than {", ".join(frameworks)}: no update for this project', flush=True)
            continue
        if latest != item["latest"] and show_output:
            print(f'{item["name"]} {item["latest"]} needs a newer .NET than {", ".join(frameworks)}: {latest} is offered instead', flush=True)
        fitted.append({**item, "latest": latest})
    return fitted


def check_updates(root, show_output=False):
    value = fingerprint(root)
    available = False
    packages = {}
    try:
        for project in sorted(Path(root).glob("*.csproj")):
            result = subprocess.run(
                ["dotnet", "list", str(project), "package", "--outdated", "--format", "json", "--output-version", "1"],
                cwd=root, env=os.environ | {"DOTNET_CLI_UI_LANGUAGE": "en-US", "VSLANG": "1033"},
                capture_output=True, text=True, timeout=180, check=True)
            found = fit_to_frameworks(parse_updates(result.stdout), project_frameworks(result.stdout), show_output)
            for item in found:
                key = item["name"].lower()
                if key in packages and packages[key] != item:
                    raise ValueError("Conflicting package versions across projects: " + item["name"])
                packages[key] = item
            available |= bool(found)
            if show_output:
                print(f"{project.name}: " + ("Updates available" if found else "No updates available"), flush=True)
                for item in found:
                    print(f'{item["name"]}: {item["current"]} -> {item["latest"]}', flush=True)
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr, flush=True)
        state = "available" if available else "current"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if show_output and isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, end="", flush=True)
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr, flush=True)
        record(root, "updates", value, status="error")
        raise RuntimeError("Package update check failed for " + Path(root).name + ": " + str(exc)) from exc
    record(root, "updates", value, status=state, packages=list(packages.values()))
    return state


def upgrade_packages(root, name=None):
    projects = sorted(Path(root).glob("*.csproj"))
    if len(projects) != 1:
        raise ValueError("Expected exactly one C# project")
    details = updates_details(root, fingerprint(root))
    packages = details["packages"]
    if details["status"] != "available" or not packages:
        raise ValueError("Run Check before updating packages")
    if name:
        packages = [item for item in packages if item["name"].lower() == name.lower()]
        if not packages:
            raise ValueError("Package has no checked update: " + name)
    for item in packages:
        print(f'Update {item["name"]}: {item["current"]} -> {item["latest"]}', flush=True)
        result = subprocess.run(
            ["dotnet", "add", str(projects[0]), "package", item["name"], "--version", item["latest"]],
            cwd=root)
        if result.returncode:
            return result.returncode if result.returncode > 0 else 128 - result.returncode
    check_updates(root, show_output=True)
    return 0


def script_scope(script):
    """The publish status of a script: test, prod, or target-<id> for scripts/targets/<id>/publish_prod.sh."""
    path = Path(script)
    if path.parent.parent.name == "targets" and path.parent.parent.parent.name == "scripts":
        return "target-" + path.parent.name
    return "prod" if path.name == "publish_prod.sh" else "test"


def run_publish(root, scope, command):
    value = fingerprint(root)
    result = subprocess.run(command, cwd=root, env=os.environ | {"VNS_PUBLISH_TRACKED": scope})
    if result.returncode == 0:
        record(root, scope, value)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main(args):
    if len(args) in (2, 3) and args[0] == "upgrade":
        return upgrade_packages(args[1], args[2] if len(args) == 3 else None)
    if len(args) == 2 and args[0] == "check":
        check_updates(args[1], show_output=True)
        return 0
    if len(args) < (3 if args[:1] == ["both"] else 4) or args[0] not in ("run", "both"):
        raise ValueError("Usage: publish_status.py check ROOT | upgrade ROOT [PACKAGE] | run ROOT test|prod COMMAND... | both ROOT LOCAL_SCRIPT PROD_SCRIPT")
    action, root = args[:2]
    if action == "both":
        # The scripts run one at a time, and the first failure stops the rest
        for script in args[2:]:
            result = run_publish(root, script_scope(script), ["/bin/bash", script])
            if result:
                return result
        return 0
    scope = args[2]
    if not re.fullmatch(r"test|prod|target-[a-z0-9][a-z0-9-]{0,62}", scope):
        raise ValueError("Invalid publish scope")
    return run_publish(root, scope, args[3:])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError) as exc:
        print("ERROR: Publish tracking failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
