"""Helpers for move_site.sh: a site that arrives on a new server is set up there with HTTP and a port of its own.

  http-conf <conf>                       the Apache config with HTTP only: the redirect to HTTPS goes, the HTTPS site answers on port 80
  port <port> <file>...                  the site's local port in its service and Apache config
  http-settings <folder> <binding>       UmbracoApplicationUrl and UseHttps in the folder's appsettings*.json, for HTTP
"""
import re
import sys
from pathlib import Path

VHOST = re.compile(r"<VirtualHost\s+([^>]*)>.*?</VirtualHost>[ \t]*\n?", re.S | re.I)
LOCAL_PORT = re.compile(r"(?<![\w.])(127\.0\.0\.1|localhost):\d{1,5}\b")


def http_conf(text):
    """The config as Create site makes it without a certificate. Issue certificate turns HTTPS on again."""
    if not re.search(r"<VirtualHost\s+[^>]*:443\s*>", text, re.I):
        return text

    def block(match):
        body = match[0]
        if not match[1].strip().endswith(":443"):
            # Only the redirect to HTTPS: the HTTPS site takes over port 80
            if re.search(r"^\s*RewriteRule\s.*https://", body, re.M | re.I) and not re.search(r"^\s*ProxyPass\s", body, re.M | re.I):
                return ""
            return body
        lines = []
        for line in body.splitlines(keepends=True):
            stripped = line.strip()
            if re.match(r"SSL", stripped, re.I) or "Strict-Transport-Security" in line:
                continue
            if re.match(r"ProxyPass\s+/\.well-known/acme-challenge/\s+!", stripped, re.I):
                continue
            line = re.sub(r"(<VirtualHost\s+[^>]*):443\s*>", r"\1:80>", line, flags=re.I)
            line = line.replace('X-Forwarded-Proto "https"', 'X-Forwarded-Proto "http"').replace("-443-error.log", "-80-error.log")
            lines.append(line)
            if re.match(r"ProxyRequests\s+Off", stripped, re.I):
                # Certificates are issued over HTTP: the challenge is Apache's own, never the service's
                lines.append(re.match(r"\s*", line)[0] + "ProxyPass /.well-known/acme-challenge/ !\n")
        return "".join(lines)

    text = VHOST.sub(block, text)
    return re.sub(r"\A\s*\n", "", text)


def set_port(text, port):
    return LOCAL_PORT.sub(lambda match: f"{match[1]}:{port}", text)


def http_settings(folder, binding):
    """The site's own address with HTTP. Only the two values change, so comments and layout are kept."""
    changed = 0
    for path in sorted(Path(folder).glob("appsettings*.json")):
        if path.is_symlink() or path.name.startswith("appsettings-schema"):
            continue
        text = path.read_text()
        new = re.sub(r'("UmbracoApplicationUrl"\s*:\s*")[^"]*', lambda match: match[1] + "http://" + binding, text)
        if '"UmbracoApplicationUrl"' in text:
            new = re.sub(r'("UseHttps"\s*:\s*)true\b', r"\1false", new)
        if new != text:
            path.write_text(new)
            changed += 1
    return changed


def main(argv):
    if len(argv) == 2 and argv[0] == "http-conf":
        path = Path(argv[1])
        path.write_text(http_conf(path.read_text()))
    elif len(argv) >= 3 and argv[0] == "port":
        if not argv[1].isdecimal() or not 1 <= int(argv[1]) <= 65535:
            sys.exit(f"Invalid port: {argv[1]}")
        for name in argv[2:]:
            path = Path(name)
            path.write_text(set_port(path.read_text(), argv[1]))
    elif len(argv) == 3 and argv[0] == "http-settings":
        print(http_settings(argv[1], argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
