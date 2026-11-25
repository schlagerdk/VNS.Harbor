"""Identify configured .NET database providers without exposing connection strings."""
import json
from pathlib import Path
import re


def local_sql_server(server):
    host = re.sub(r"^tcp:", "", server.strip(), flags=re.I).split(",", 1)[0].lower()
    return host in (".", "localhost", "127.0.0.1", "::1", "[::1]")


def runtime_version(data):
    """The .NET version, like 10.0, that a parsed <name>.runtimeconfig.json asks for, or an empty string"""
    options = data.get("runtimeOptions") if isinstance(data, dict) else None
    if not isinstance(options, dict):
        return ""
    frameworks = options.get("frameworks")
    if not isinstance(frameworks, list):
        frameworks = [options["framework"]] if isinstance(options.get("framework"), dict) else []
    for framework in frameworks:
        if (isinstance(framework, dict) and framework.get("name") in ("Microsoft.AspNetCore.App", "Microsoft.NETCore.App")
                and isinstance(framework.get("version"), str) and re.match(r"\d+\.\d+", framework["version"])):
            return ".".join(framework["version"].split(".")[:2])
    match = re.fullmatch(r"net(\d+\.\d+)", str(options.get("tfm", "")))
    return match.group(1) if match else ""


def dotnet_runtime(folder, dll=""):
    """The .NET a published site runs, from the runtimeconfig.json beside its dll (the given one first)"""
    folder = Path(folder)
    candidates = ([folder / (dll + ".runtimeconfig.json")] if dll else []) + sorted(folder.glob("*.runtimeconfig.json"))
    for path in candidates:
        try:
            version = runtime_version(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
        if version:
            return version
    return ""


def project_dotnet(root):
    """The .NET a site's project targets, like 10.0, from <TargetFramework>net10.0</TargetFramework>"""
    for project in sorted(Path(root).glob("*.csproj")):
        try:
            match = re.search(r"<TargetFrameworks?>\s*net(\d+\.\d+)", read_config_text(project))
        except OSError:
            continue
        if match:
            return match.group(1)
    return ""


def read_config_text(path):
    """An appsettings file as text. Most are UTF-8; one saved by an old Windows editor is Windows-1252,
    where æ, ø and å are single bytes that are not valid UTF-8. Such a file is read, not refused."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def configuration_json(text):
    # ASP.NET configuration permits comments and trailing commas outside strings.
    tokens = r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/'
    text = re.sub(tokens, lambda match: match[0] if match[0].startswith('"') else "", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
                  lambda match: match[0] if match[0].startswith('"') else "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected an appsettings object")
    return data


def connection_fields(connection):
    fields = {}
    pattern = r'''(?:^|;)\s*([^=;]+)\s*=\s*("(?:[^"]|"")*"|'(?:[^']|'')*'|[^;]*)'''
    for match in re.finditer(pattern, connection):
        value = match[2].strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            quote = value[0]
            value = value[1:-1]
            value = value.replace(quote * 2, quote)
        fields[match[1].strip().lower()] = value
    return fields


def connection_type(connection, provider):
    if "sqlite" in provider:
        return "SQLite"
    if "npgsql" in provider or "postgres" in provider:
        return "PostgreSQL"
    if provider and not any(name in provider for name in ("sqlclient", "sqlserver")):
        return "Unknown"
    fields = {key: value.lower() for key, value in connection_fields(connection).items()}
    # Npgsql's words: Host and Username (SQL Server has neither)
    if "host" in fields or "username" in fields:
        return "PostgreSQL"
    server = next((fields[key] for key in ("server", "data source", "datasource", "filename", "address", "addr", "network address")
                   if key in fields), "")
    if not provider and (server.endswith((".db", ".sqlite", ".sqlite3")) or server == ":memory:"):
        return "SQLite"
    sql = bool(provider) or any(key in fields for key in (
        "server", "initial catalog", "database", "integrated security", "trusted_connection"))
    if not sql:
        return "Unknown"
    host = re.sub(r"^(?:tcp|np|lpc):", "", server).split(",", 1)[0].rstrip(".")
    if any(host.endswith(suffix) for suffix in (
            ".database.windows.net", ".database.usgovcloudapi.net",
            ".database.chinacloudapi.cn", ".database.cloudapi.de")):
        return "Azure SQL"
    if re.search(r"\\sqlexpress(?:$|\\)", host):
        return "SQL Express"
    return "MS SQL"


def configuration_values(root, environment="", variables=None):
    values = {}
    environment = environment or "Production"

    def flatten(data, prefix=""):
        for key, value in data.items():
            name = (prefix + "__" + key if prefix else key).lower()
            if isinstance(value, dict):
                flatten(value, name)
            else:
                values[name] = value

    paths = [Path(root) / "appsettings.json"]
    if environment and "/" not in environment and "\\" not in environment:
        paths.append(Path(root) / ("appsettings." + environment + ".json"))
    for path in paths:
        try:
            text = read_config_text(path)
        except FileNotFoundError:
            continue
        flatten(configuration_json(text))
    for key, value in (variables or {}).items():
        values[key.replace(":", "__").lower()] = value
    return values


def database_details(root, environment="", variables=None):
    values = configuration_values(root, environment, variables)
    connections = {key: value for key, value in values.items()
                   if key.startswith("connectionstrings__") and isinstance(value, str)}
    providers = set()
    names = set()
    servers = set()
    for key, value in connections.items():
        if not value.strip():
            continue
        if key.endswith("_providername"):
            continue
        provider = connections.get(key + "_providername", "").lower()
        kind = connection_type(value, provider)
        providers.add(kind)
        fields = connection_fields(value)
        if kind == "SQLite":
            name = fields.get("data source") or fields.get("datasource") or fields.get("filename", "")
            # Keep runtime substitutions and URI sources literal rather than guessing their location.
            if name and name != ":memory:" and "|" not in name and not name.startswith("file:"):
                name = str(Path(root) / name)
        else:
            name = fields.get("initial catalog") or fields.get("database", "")
            if kind in ("MS SQL", "SQL Express", "Azure SQL"):
                server = next((fields[key] for key in ("server", "data source", "datasource", "address", "addr", "network address")
                               if fields.get(key)), "")
                if server:
                    servers.add(server)
        if name:
            names.add(name)
    # No connection at all: a site without a database ("", not shown), not one Harbor cannot tell
    return {"database": next(iter(providers)) if len(providers) == 1 else "Unknown" if providers else "",
            "database_name": " / ".join(sorted(names)),
            "database_server": " / ".join(sorted(servers))}


def database_type(root, environment="", variables=None):
    return database_details(root, environment, variables)["database"]
