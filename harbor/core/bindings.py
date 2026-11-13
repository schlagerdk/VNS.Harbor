"""A site's binding and aliases, the names in its certificate, and the names a project and its publish folder may have.
From lib/common.sh: validate_binding, normalize_site_binding, normalize_server_aliases, server_aliases_from_config,
replace_server_aliases, set_certificate_domains, validate_project_name, validate_publish_folder, exclude_from_build."""
import re

from harbor.core.names import normalize_dns_name

LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
BINDING = re.compile(rf"{LABEL}(\.{LABEL})+")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PUBLISH_FOLDER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
RESERVED_FOLDERS = {"bin", "obj", "wwwroot", "views", "usync", "scripts", "properties", "umbraco", "app_plugins"}


def validate_binding(name):
    """A fully qualified name, as a certificate needs"""
    if not BINDING.fullmatch(name):
        raise ValueError("Invalid binding. Enter a fully qualified DNS name, without scheme or path.")
    if len(name) > 253:
        raise ValueError("Binding is too long.")
    if any(len(label) > 63 for label in name.split(".")):
        raise ValueError("DNS labels cannot exceed 63 characters.")


def site_binding(value, http_only=False):
    """A binding or alias of a site, in lowercase. A site on HTTP may use a name without a dot, like a server's own
    name on the local network: a certificate needs a public name with one."""
    if http_only and "." not in value:
        return normalize_dns_name(value).lower()
    if "." not in value:
        raise ValueError(f"'{value}' has no dot, so it cannot be in a certificate. Only a site on HTTP can use a name without a dot.")
    name = normalize_dns_name(value, fully_qualified=True)
    validate_binding(name)
    return name.lower()


def server_aliases(primary, raw, http_only=False):
    """The aliases in raw (separated by spaces, commas or lines), each once, without the primary name"""
    primary = primary.lower()
    aliases = []
    for alias in raw.replace(",", " ").split():
        try:
            alias = site_binding(alias, http_only)
        except ValueError as error:
            raise ValueError(f"{error}\nInvalid alias '{alias}'.") from None
        if alias != primary and alias not in aliases:
            aliases.append(alias)
    return aliases


def aliases_in_config(text):
    """The names after ServerAlias in an Apache config, as written, until a comment"""
    names = []
    for line in text.splitlines():
        fields = line.split()
        if fields[:1] == ["ServerAlias"]:
            for name in fields[1:]:
                if name.startswith("#"):
                    break
                names.append(name)
    return names


def server_aliases_from_config(text, primary, http_only=False):
    return server_aliases(primary, " ".join(aliases_in_config(text)), http_only)


def replace_server_aliases(text, aliases):
    """The config with its ServerAlias lines replaced by one after each ServerName. aliases is a list or a string."""
    if not isinstance(aliases, str):
        aliases = " ".join(aliases)
    lines = []
    for line in text.splitlines():
        if re.match(r"[ \t]*ServerAlias[ \t]", line):
            continue
        lines.append(line)
        if line.split()[:1] == ["ServerName"] and aliases:
            lines.append("  ServerAlias " + aliases)
    return "".join(line + "\n" for line in lines)


def certificate_domains(text, primary, config="the Apache config"):
    """The names a certificate is issued for: the binding, then each alias in the config once"""
    domains = [primary.lower()]
    for alias in aliases_in_config(text):
        domain = alias.lower()
        try:
            validate_binding(domain)
        except ValueError as error:
            raise ValueError(f"{error}\nInvalid ServerAlias '{alias}' in {config}; certificate issuance stopped.") from None
        if domain not in domains:
            domains.append(domain)
    return domains


def validate_project_name(name):
    if not PROJECT.fullmatch(name):
        raise ValueError("Invalid C# project name. Use letters, numbers, dots, hyphens or underscores.")


def validate_publish_folder(name):
    """A single folder in the site's folder, or . for the folder itself"""
    if name == ".":
        return
    if not PUBLISH_FOLDER.fullmatch(name):
        raise ValueError("Invalid publish folder. Use a single folder name with letters, numbers, dots, hyphens or underscores.")
    if name.lower() in RESERVED_FOLDERS:
        raise ValueError(f"Publish folder name '{name}' is reserved.")


def exclude_from_build(text, folder):
    """The project file with the publish folder left out of its build, so a build does not take in the last one.
    Unchanged when it is left out already, or the site publishes into its own folder."""
    if folder == "." or f'Compile Remove="{folder}/**"' in text:
        return text
    block = (f'  <!-- Exclude publish folder from build -->\n  <ItemGroup>\n    <Content Remove="{folder}/**" />\n'
             f'    <Compile Remove="{folder}/**" />\n    <None Remove="{folder}/**" />\n  </ItemGroup>\n</Project>\n')
    return re.sub(r"</Project>\s*\Z", lambda match: block, text, count=1)
