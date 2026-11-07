"""Canonical DNS names shared by the admin application and shell scripts."""
import re
import sys


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
        raise ValueError("Use valid DNS labels (at most 63 characters each, 253 total).")
    return name.lower() if fully_qualified else name


if __name__ == "__main__":
    try:
        print(normalize_dns_name(sys.argv[1], fully_qualified=sys.argv[2] == "binding"))
    except ValueError as error:
        sys.exit(str(error))
