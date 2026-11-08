"""Copy local site files and rewrite their deployment identities."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil

from domain_names import normalize_dns_name, normalize_site_name, service_name
from database_info import configuration_json, connection_type


def validate_database_name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(
            char in name for char in "\0\r\n;\"'"):
        raise ValueError("Enter a database name of 1–128 characters without quotes, semicolons or line breaks.")
    return name.strip()


def change_database(connection, name):
    pattern = r'''(?i)(?:^|;)\s*(?:database|initial catalog)\s*=\s*("(?:[^"]|"")*"|'(?:[^']|'')*'|[^;]*)'''
    matches = list(re.finditer(pattern, connection))
    if not matches:
        raise ValueError("A SQL connection string has no Database or Initial Catalog to replace.")
    for match in reversed(matches):
        connection = connection[:match.start(1)] + '"' + name + '"' + connection[match.end(1):]
    return connection


def change_connection(connection, settings):
    aliases = {
        "server": {"server", "data source", "datasource", "address", "addr", "network address"},
        "database": {"database", "initial catalog"},
        "username": {"user id", "uid", "user"},
        "password": {"password", "pwd"},
    }
    pattern = r'''(?:^|;)\s*([^=;]+)\s*=\s*("(?:[^"]|"")*"|'(?:[^']|'')*'|[^;]*)'''
    matches = list(re.finditer(pattern, connection))
    replaced = set()
    for match in reversed(matches):
        key = match[1].strip().lower()
        field = next((name for name, names in aliases.items() if key in names), None)
        if field:
            quoted = '"' + settings[field].replace('"', '""') + '"'
            connection = connection[:match.start(2)] + quoted + connection[match.end(2):]
            replaced.add(field)
        elif key in ("integrated security", "trusted_connection"):
            connection = connection[:match.start(2)] + "False" + connection[match.end(2):]
    labels = {"server": "Server", "database": "Database", "username": "User ID", "password": "Password"}
    for field in aliases:
        if field not in replaced:
            connection = connection.rstrip(";") + ";" + labels[field] + '="' + settings[field].replace('"', '""') + '"'
    return connection


def rewrite(text, source, target, binding, old_port="", new_port=""):
    old_unit = service_name(source.name)
    new_unit = service_name(target.name)
    protected = {}

    def protect(match):
        key = f"__VNS_COPY_PROJECT_{len(protected)}__"
        protected[key] = match.group()
        return key

    text = re.sub(r"(?m)^_projectName=.*$|[^\s/\"']+\.(?:csproj|dll|deps\.json|runtimeconfig\.json)", protect, text)
    text = text.replace(str(source), str(target))
    text = text.replace("/var/lib/vns-roots/" + source.name, "/var/lib/vns-roots/" + target.name)
    text = re.sub(r"(?<![\w.-])" + re.escape(old_unit) + r"(?![\w.-])", new_unit, text)
    if source.name != old_unit:
        text = re.sub(r"(?<![\w.-])" + re.escape(source.name) + r"(?![\w.-])", target.name, text)
    if old_port:
        text = re.sub(r"(?<=:)" + re.escape(old_port) + r"(?!\d)", new_port, text)
    for key, value in protected.items():
        text = text.replace(key, value)
    return text


def prepare(source_name, target_name, binding, port="", web_root=Path("/var/www"),
            configs=Path("/etc/apache2/sites-available"), services=Path("/etc/systemd/system"),
            database="", connection=None):
    source_name = normalize_site_name(source_name)
    target_name = normalize_site_name(target_name)
    binding = normalize_dns_name(binding, fully_qualified=True)
    source = web_root / source_name
    target = web_root / target_name
    conf = configs / (source_name + ".conf")
    service = services / (service_name(source_name) + ".service")
    target_conf = configs / (target_name + ".conf")
    target_service = services / (service_name(target_name) + ".service")
    if source.is_symlink() or not source.is_dir() or not conf.is_file():
        raise ValueError("The source must be a local site with an Apache configuration.")
    if (source / "scripts/publish_prod.sh").exists():
        raise ValueError("Sites with a production target cannot be copied.")
    if target.exists() or target.is_symlink() or target_conf.exists() or target_service.exists():
        raise ValueError("The destination folder, configuration or service already exists.")
    if any(service_name(path.name) == service_name(target_name) for path in web_root.iterdir() if path.is_dir()):
        raise ValueError("A site already uses the destination's canonical name.")
    text = conf.read_text()
    original_binding = re.search(r"(?m)^\s*ServerName\s+(\S+)", text)
    if not original_binding:
        raise ValueError("The source Apache configuration has no ServerName.")
    for path in configs.glob("*.conf"):
        if re.search(r"(?mi)^\s*(?:ServerName|ServerAlias)\s+.*(?<!\S)" + re.escape(binding) + r"(?!\S)", path.read_text()):
            raise ValueError("The new binding is already used by an Apache site.")
    service_text = service.read_text() if service.is_file() else ""
    if database or connection is not None:
        if connection is not None:
            if not isinstance(connection, dict) or set(connection) != {"server", "database", "username", "password"}:
                raise ValueError("Enter SQL server, database, username and password.")
            for key, value in connection.items():
                if not isinstance(value, str) or not value.strip() or len(value) > (128 if key == "database" else 4096) or any(
                        char in value for char in "\0\r\n"):
                    raise ValueError("Invalid SQL connection field: " + key)
        else:
            database = validate_database_name(database)
        if re.search(r"(?i)connectionstrings(?:__|:)", service_text):
            raise ValueError("Service environment connection strings must be edited separately before changing the copied database.")
        changes = 0
        for path in source.rglob("appsettings*.json"):
            if path.is_symlink():
                continue
            data = configuration_json(path.read_text(encoding="utf-8-sig"))
            connections = data.get("ConnectionStrings", {})
            for key, value in connections.items():
                if isinstance(value, str) and not key.lower().endswith("_providername"):
                    provider = str(connections.get(key + "_ProviderName", "")).lower()
                    if connection_type(value, provider) in ("MS SQL", "SQL Express", "Azure SQL"):
                        if connection is None:
                            change_database(value, database)
                        changes += 1
        if not changes:
            raise ValueError("No SQL database connection strings were found to change.")
    ports = set(re.findall(r"(?:localhost|127\.0\.0\.1):(\d+)", text + service_text))
    if service_text and (len(ports) != 1 or not port.isdigit()):
        raise ValueError("A service copy requires one identifiable HTTP port and a new allocated port.")
    old_port = next(iter(ports), "")
    https = bool(re.search(r"(?mi)^\s*SSLEngine\s+on\b", text))
    shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    os.chown(target, source.stat().st_uid, source.stat().st_gid)
    for path in target.rglob("*"):
        info = (source / path.relative_to(target)).lstat()
        os.lchown(path, info.st_uid, info.st_gid)
        if path.is_symlink():
            link = os.readlink(path)
            if link == str(source) or link.startswith(str(source) + "/"):
                path.unlink()
                path.symlink_to(str(target) + link[len(str(source)):])
    for path in (target / "scripts").glob("*.sh"):
        if path.is_symlink():
            continue
        content = rewrite(path.read_text(), source, target, binding, old_port, port)
        if original_binding[1] not in (source_name, service_name(source_name)):
            content = re.sub(r"(?<![\w.-])" + re.escape(original_binding[1]) + r"(?![\w.-])", binding, content)
        content = re.sub(r"(?m)^(_binding=).*$", lambda m: m[1] + binding, content)
        path.write_text(content)
    for folder in ("venv/bin", ".venv/bin"):
        for path in (target / folder).glob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            if str(source).encode() in raw and b"\0" not in raw:
                path.write_bytes(raw.replace(str(source).encode(), str(target).encode()))
    for path in target.rglob("appsettings*.json"):
        if path.is_symlink():
            continue
        content = path.read_text(encoding="utf-8-sig")
        if "UmbracoApplicationUrl" not in content and not database and connection is None:
            continue
        data = configuration_json(content)
        if database or connection is not None:
            connections = data.get("ConnectionStrings", {})
            for key, value in connections.items():
                if isinstance(value, str) and not key.lower().endswith("_providername"):
                    provider = str(connections.get(key + "_ProviderName", "")).lower()
                    if connection_type(value, provider) in ("MS SQL", "SQL Express", "Azure SQL"):
                        connections[key] = (change_connection(value, connection) if connection is not None
                                            else change_database(value, database))

        def update_urls(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "UmbracoApplicationUrl" and isinstance(item, str):
                        value[key] = re.sub(r"(?<=://)[^/]+", binding, item)
                    else:
                        update_urls(item)
            elif isinstance(value, list):
                for item in value:
                    update_urls(item)

        update_urls(data)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    text = rewrite(text, source, target, binding, old_port, port)
    text = re.sub(r"(?m)^(\s*ServerName\s+)\S+", lambda m: m[1] + binding, text)
    text = re.sub(r"(?m)^\s*ServerAlias[^\n]*\n?", "", text)
    if https:
        text = re.sub(r"(?m)^(\s*SSLCertificateFile\s+).*$",
                      lambda m: m[1] + f"/etc/ssl/certs/{target_name}_ecc/fullchain.cer", text)
        text = re.sub(r"(?m)^(\s*SSLCertificateKeyFile\s+).*$",
                      lambda m: m[1] + f"/etc/ssl/certs/{target_name}_ecc/{target_name}.key", text)
    target_conf.write_text(text)
    if service_text:
        target_service.write_text(rewrite(service_text, source, target, binding, old_port, port))
    return https


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("binding")
    parser.add_argument("port", nargs="?", default="")
    parser.add_argument("--database", default="")
    args = parser.parse_args()
    try:
        secret = os.environ.pop("VNS_COPY_SQL", None)
        prepare(args.source, args.target, args.binding, args.port, database=args.database,
                connection=json.loads(secret) if secret is not None else None)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Copy site failed: {exc}\n")
