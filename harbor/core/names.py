"""DNS names: a site's folder, a binding, and the name of its service. One place for the admin site, harbor and the scripts."""
import re
import sys
import unicodedata


def normalize_dns_name(value, fully_qualified=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Enter a DNS name, without scheme or path.")
    try:
        name = value.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("Invalid international DNS name.") from error
    labels = name.split(".")
    if (len(name) > 253 or (fully_qualified and len(labels) < 2)
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in labels)):
        # The name is shown as Python writes it, so a space or a control character in it can be seen
        raise ValueError(f"{value!r} is not a valid DNS name. Use letters, digits and hyphens between the dots "
                         "(at most 63 characters each, 253 total).")
    return name.lower() if fully_qualified else name


def display_dns_name(value):
    return ".".join(label.encode("ascii").decode("idna") if label.lower().startswith("xn--") else label
                    for label in value.split("."))


def normalize_site_name(value):
    normalize_dns_name(value)
    name = unicodedata.normalize("NFC", value)
    if (len(name.encode("utf-8")) > 255
            or not re.fullmatch(r"[^\W_](?:[^\W_]|[.-])*", name)):
        raise ValueError("Invalid site folder name.")
    return name


def service_name(value):
    return normalize_dns_name(value)


def standalone(code):
    """A helper in lib/ as it runs on another server, sent as python3 - or -c: with these names in it, since there is no
    lib/domain_names.py beside a script that has no file, and the server may have no harbor to import them from"""
    from pathlib import Path
    return Path(__file__).read_text() + "\n\n" + code
