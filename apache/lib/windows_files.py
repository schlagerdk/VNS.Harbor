"""The file manager for an environment on a Windows server, over SMB with rclone.

  windows_files.py REMOTE_ROOT < operation.json

windows_files.sh runs it after it has set up the rclone connection in the environment, with REMOTE_ROOT the
site's folder on the share. It takes the same operations as remote_files.py and answers in the same form,
on stdout as JSON. Windows has no owner or rights to show, and folders are listed before files.
"""
import base64
import datetime
import json
import os
import subprocess
import sys

RCLONE = os.environ.get("VNS_RCLONE", "rclone")
MAX_EDIT = 2 * 1024 * 1024
BINARY = {".dll", ".exe", ".pdb", ".so", ".dylib", ".zip", ".gz", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".ico",
          ".webp", ".pdf", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".avif", ".db", ".sqlite"}


def rclone(*args, data=None):
    result = subprocess.run([RCLONE, *args], input=data, capture_output=True, timeout=110)
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ValueError(message[-1] if message else "The Windows server refused the request")
    return result.stdout


def relative(path):
    """A path inside the site, with / between its parts. Nothing may lead out of it."""
    if not isinstance(path, str):
        raise ValueError("Invalid path")
    parts = [part for part in path.strip("/").split("/") if part]
    for part in parts:
        if part in (".", "..") or any(char in part for char in '\\:*?"<>|\0') or part != part.strip():
            raise ValueError("Invalid path")
    return "/".join(parts)


def valid_name(name):
    if not isinstance(name, str) or not name or "/" in name or relative(name) != name:
        raise ValueError("Invalid name")
    return name


class Share:
    def __init__(self, root):
        self.root = root.rstrip("/")

    def remote(self, path):
        return self.root + ("/" + path if path else "")

    def stat(self, path):
        try:
            return json.loads(rclone("lsjson", "--stat", "--no-mimetype", self.remote(path)))
        except ValueError:
            return None


def modified(value):
    try:
        return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (AttributeError, ValueError):
        return 0


def editable(name, size):
    return size <= MAX_EDIT and os.path.splitext(name)[1].lower() not in BINARY


def handle(share, data):
    action = data["action"]
    path = relative(data.get("path", ""))
    if action in ("delete", "rename") and not path:
        raise ValueError("Use Delete site to remove the site")
    if action == "details":
        # What the site page shows of a Windows environment, read from the files in its folder:
        # the .NET it runs (its runtimeconfig.json) and its database (its appsettings files)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import tempfile
        from database_info import database_details, runtime_version
        names = rclone("lsf", "--files-only", "--max-depth", "1", share.remote("")).decode("utf-8", "replace").splitlines()
        details = {"dotnet": ""}
        for name in sorted(name for name in names if name.lower().endswith(".runtimeconfig.json")):
            try:
                details["dotnet"] = runtime_version(json.loads(rclone("cat", share.remote(name)).decode("utf-8-sig")))
            except ValueError:
                continue
            if details["dotnet"]:
                break
        environment = data.get("environment") if isinstance(data.get("environment"), str) else ""
        wanted = {"appsettings.json"} | ({"appsettings." + environment.lower() + ".json"} if environment else set())
        with tempfile.TemporaryDirectory() as folder:
            for name in names:
                # Windows does not mind the case of a name: the copy gets the name database_details looks for
                if name.lower() in wanted:
                    local = "appsettings.json" if name.lower() == "appsettings.json" else "appsettings." + environment + ".json"
                    with open(os.path.join(folder, local), "wb") as stream:
                        stream.write(rclone("cat", share.remote(name)))
            try:
                details.update(database_details(folder, environment))
            except (OSError, ValueError):
                details["database"] = "Unknown"
        return details
    if action == "list":
        items = []
        for entry in json.loads(rclone("lsjson", "--no-mimetype", share.remote(path))):
            folder = bool(entry.get("IsDir"))
            size = 0 if folder else max(int(entry.get("Size") or 0), 0)
            items.append({"name": entry["Name"], "dir": folder, "size": size, "modified": modified(entry.get("ModTime")),
                          "editable": not folder and editable(entry["Name"], size),
                          # Looking inside every folder over SMB is too slow, so a folder may hold editable files
                          "has_editable_files": folder})
        items.sort(key=lambda item: (not item["dir"], item["name"].lower(), item["name"]))
        return {"path": path, "items": items, "windows": True}
    if action in ("read", "download"):
        info = share.stat(path)
        if not info or info.get("IsDir"):
            raise ValueError("Not found")
        if action == "read" and int(info.get("Size") or 0) > MAX_EDIT:
            raise ValueError("The file is too large to edit here. Download it instead.")
        content = rclone("cat", share.remote(path))
        if action == "download":
            return {"content": base64.b64encode(content).decode("ascii")}
        if b"\0" in content:
            raise ValueError("Binary file. Download it instead.")
        return {"content": content.decode("utf-8-sig", errors="replace")}
    if action in ("mkdir", "newfile", "rename"):
        name = valid_name(data.get("name", ""))
        parent = path.rpartition("/")[0] if action == "rename" else path
        target = "/".join(part for part in (parent, name) if part)
        if share.stat(target) is not None:
            raise ValueError("It already exists")
        if action == "mkdir":
            rclone("mkdir", share.remote(target))
        elif action == "newfile":
            rclone("rcat", share.remote(target), data=b"")
        else:
            if share.stat(path) is None:
                raise ValueError("Not found")
            rclone("moveto", share.remote(path), share.remote(target))
    elif action == "save":
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("Expected the file content")
        rclone("rcat", share.remote(path), data=content.encode("utf-8"))
    elif action == "upload":
        files = data.get("files") or []
        for upload in files:
            name = valid_name(upload.get("name", ""))
            target = "/".join(part for part in (path, name) if part)
            rclone("rcat", share.remote(target), data=base64.b64decode(upload["content"], validate=True))
        return {"ok": True, "count": len(files)}
    elif action == "delete":
        info = share.stat(path)
        if info is None:
            raise ValueError("Not found")
        rclone("purge" if info.get("IsDir") else "deletefile", share.remote(path))
    else:
        raise ValueError("Unknown file action")
    return {"ok": True}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or not sys.argv[1]:
            raise ValueError("Usage: windows_files.py REMOTE_ROOT < operation.json")
        operation = json.load(sys.stdin)
        if not isinstance(operation, dict):
            raise ValueError("Expected a file operation object")
        result = handle(Share(sys.argv[1]), operation)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    print(json.dumps(result))
