"""The main site's config snippets, carried to its environments when they are published.

The admin's config editor puts snippets in the main site's Apache config: each one a block that starts with
"# <Name>" and a data line "# vns {"key": ..., "values": {...}}". At publish this reads them back and writes
them for the environment, so its rules are the main site's:

  config_snippets.py read MAIN_CONF                      Print the main site's snippets as JSON
  config_snippets.py apache CONF SNIPPETS_JSON           Make an environment's Apache config carry them
  config_snippets.py iis MAIN_CONF WEB_CONFIG            Make a Windows environment's web.config carry them

The two that write print "changed" or "unchanged". Only what this file wrote before is replaced: in Apache the
blocks between its BEGIN and END lines, in web.config the rules named "VNS: ...". Rules written by hand are kept.
Rewrite to domain is not carried: it names the main site's own domain.
"""
import copy
import json
import re
import sys
import xml.etree.ElementTree as ET

CARRIED = ("ignore", "ips", "slash", "www", "rewrite", "compression", "cookies")
ORDER = ("ignore", "ips", "slash", "www", "rewrite", "compression", "cookies")
FIRST = ("ignore", "ips")
LAST = ("compression", "cookies")
PREFIX = "VNS: "
BEGIN = "# BEGIN VNS snippets from the main site (%s): published here, changes are overwritten"
END = "# END VNS snippets from the main site (%s)"
PATH_RE = re.compile(r"^[A-Za-z0-9._~\-/]+$")
WARNINGS = []


# Allowed IPs for the whole site leaves this open: Let's Encrypt's challenge, which renews the certificate
ACME_APACHE = "RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/"
ACME_IIS = "^(?!\\.well-known/acme-challenge/)"


def warn(message):
    """A snippet that cannot be carried is told about and left out; the publish goes on"""
    WARNINGS.append(message)


def paths_of(value):
    paths = [item.strip("/") for item in re.split(r"[\s,]+", str(value or "")) if item.strip("/")]
    if not paths or any(not PATH_RE.match(path) for path in paths):
        raise ValueError("invalid paths: %r" % value)
    return paths


def addresses_of(value, cidr):
    pattern = r"^[0-9a-fA-F:.]+(/\d{1,3})?$" if cidr else r"^[0-9a-fA-F:.]+$"
    addresses = [item for item in re.split(r"[\s,]+", str(value or "")) if item]
    if not addresses or any(not re.match(pattern, item) or not re.search(r"[.:]", item) for item in addresses):
        raise ValueError("invalid addresses: %r" % value)
    return addresses


def rewrite_of(values):
    rewrite = {key: str(values.get(key) or "").strip().lstrip("/") for key in ("from", "to")}
    if not all(rewrite.values()) or any(not PATH_RE.match(path) for path in rewrite.values()):
        raise ValueError("invalid rewrite: %r" % values)
    return rewrite


def target_of(value):
    target = str(value or "").strip() or "/"
    if not re.match(r"^(/[^\s\"'<>]*|https?://[^\s\"'<>]+)$", target):
        raise ValueError("invalid redirect: %r" % value)
    return target


def alternation(items):
    return "|".join(re.escape(item).replace("\\-", "-").replace("\\/", "/") for item in items)


def read(conf_text):
    """The snippets in a config, in the order they come, with their name"""
    found = []
    lines = conf_text.splitlines()
    for index, line in enumerate(lines[:-1]):
        name = re.match(r"^\s*# (.+?)\s*$", line)
        data = re.match(r"^\s*# vns (\{.*\})\s*$", lines[index + 1])
        if not name or not data or name.group(1).startswith(("vns ", "BEGIN VNS", "END VNS")):
            continue
        try:
            snippet = json.loads(data.group(1))
        except ValueError:
            continue
        if isinstance(snippet, dict) and snippet.get("key") in CARRIED and isinstance(snippet.get("values", {}), dict):
            found.append({"name": name.group(1), "key": snippet["key"], "values": snippet.get("values", {})})
    return found


# ---- Apache

def apache_rules(snippet, port):
    key, values = snippet["key"], snippet["values"]
    if key == "ignore":
        return ["RewriteRule ^/(?:%s)/ - [L]" % alternation(paths_of(values.get("paths")))]
    if key == "ips":
        addresses = ["127.0.0.0/8", "::1"] + addresses_of(values.get("addresses"), True)
        response = values.get("response")
        answer = ("- [R=404,NC,L]" if response == "notfound"
                  else "%s [R=302,NC,L]" % target_of(values.get("target")) if response == "redirect" else "- [F,NC,L]")
        condition = 'RewriteCond expr "!(%s)"' % " || ".join("-R '%s'" % address for address in addresses)
        if values.get("scope") == "site":
            # The whole site, but Let's Encrypt's challenge, or its certificate could not be renewed
            return [ACME_APACHE, condition, "RewriteRule ^/ %s" % answer]
        return [condition, "RewriteRule ^/(?:%s)(?:/|$) %s" % (alternation(paths_of(values.get("paths"))), answer)]
    if key == "slash":
        return ["RewriteCond %{REQUEST_METHOD} ^(GET|HEAD)$", "RewriteCond %{REQUEST_URI} !\\.[a-zA-Z0-9]{2,10}$",
                "RewriteCond %{REQUEST_URI} !^/umbraco [NC]", "RewriteCond %{REQUEST_URI} !^/media [NC]",
                "RewriteCond %{REQUEST_URI} !/$", "RewriteRule ^(.*)$ $1/ [R=301,L]"]
    if key == "www":
        return ["RewriteCond %{HTTP_HOST} ^www\\.(.+)$ [NC]", "RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]"]
    if key == "rewrite":
        if not port:
            raise ValueError("the config has no ProxyPass / to the site's service")
        rewrite = rewrite_of(values)
        # As the site's own robots.txt and sitemap.xml: a ProxyPass to its service, placed before ProxyPass /
        target = "http://127.0.0.1:%s/%s" % (port, rewrite["to"])
        return ["ProxyPass /%s %s" % (rewrite["from"], target), "ProxyPassReverse /%s %s" % (rewrite["from"], target)]
    if key == "compression":
        return ["<IfModule mod_deflate.c>",
                "  AddOutputFilterByType DEFLATE text/html text/plain text/css text/xml text/javascript application/javascript application/json application/xml image/svg+xml",
                "</IfModule>"]
    if key == "cookies":
        return ['Header always edit Set-Cookie "(?i)^((?!.*;\\s*secure).*)$" "$1; Secure"']
    raise ValueError("unknown snippet %r" % key)


def apache_region(snippets, part, port, indent):
    blocks = []
    for snippet in snippets:
        lines = ["# " + snippet["name"], "# vns " + json.dumps({"key": snippet["key"], "values": snippet["values"]}, separators=(",", ":"))]
        try:
            blocks.append("\n".join(indent + line for line in lines + apache_rules(snippet, port)))
        except ValueError as exc:
            warn("%s is not carried: %s" % (snippet["name"], exc))
    return "\n".join([indent + BEGIN % part, "\n\n".join(blocks), indent + END % part])


def apply_apache(conf_text, snippets):
    snippets = sorted((item for item in snippets if item["key"] in CARRIED), key=lambda item: ORDER.index(item["key"]))
    # What this wrote before goes, with exactly the blank lines it put around it, so the file is as it was
    region = r"[ \t]*# BEGIN VNS snippets from the main site \(%s\)[^\n]*\n.*?# END VNS snippets from the main site \(%s\)[^\n]*\n"
    text = re.sub(r"\n\n" + region % ("first", "first"), "", conf_text, flags=re.S)
    text = re.sub(region % ("rules", "rules") + r"\n", "", text, flags=re.S)
    text = re.sub(r"\n" + region % ("end", "end"), "", text, flags=re.S)
    if not snippets:
        return text
    hosts = list(re.finditer(r"<VirtualHost\b[^>]*>", text))
    if not hosts:
        raise ValueError("the config has no <VirtualHost>")
    host = next((match for match in hosts if ":443>" in match.group(0)), hosts[0])
    start, end = host.end(), text.index("</VirtualHost>", host.end())
    body = text[start:end]
    indent = (re.search(r"\n([ \t]+)\S", body) or re.match("()", "")).group(1) or "  "
    # The site's own config proxies to 127.0.0.1, an environment's (harbor/environments/publish.py) to localhost: the
    # service listens on both
    port = re.search(r"^[ \t]*ProxyPass\s+/\s+http://(?:127\.0\.0\.1|localhost):(\d+)/", body, re.M)
    port = port.group(1) if port else ""
    first = [item for item in snippets if item["key"] in FIRST]
    middle = [item for item in snippets if item["key"] not in FIRST + LAST]
    last = [item for item in snippets if item["key"] in LAST]
    proxy = re.search(r"^[ \t]*(ProxyPass|ProxyPreserveHost)\b", body, re.M)
    engine = re.search(r"^[ \t]*RewriteEngine\s+On[ \t]*$", body, re.M | re.I)
    # From the end of the site's part backwards, so the earlier places stay where they are
    if last:
        before = text[:end].rstrip(" \t")
        text = before + "\n" + apache_region(last, "end", port, indent) + "\n" + text[end:]
    if middle:
        at = start + proxy.start() if proxy else end
        text = text[:at] + apache_region(middle, "rules", port, indent) + "\n\n" + text[at:]
    if first or (middle and not engine):
        if engine:
            at = start + engine.end()
            text = text[:at] + ("\n\n" + apache_region(first, "first", port, indent) + "\n" if first else "") + text[at:]
        else:
            at = start + proxy.start() if proxy else start
            region = (apache_region(first, "first", port, indent) + "\n\n") if first else ""
            text = text[:at] + indent + "RewriteEngine On\n\n" + region + text[at:]
    return text


# ---- IIS

def iis_rule(snippet):
    key, values, name = snippet["key"], snippet["values"], PREFIX + snippet["name"]
    rule = ET.Element("rule", name=name, stopProcessing="true", enabled="true")
    if key == "ignore":
        ET.SubElement(rule, "match", url="^(?:%s)/" % alternation(paths_of(values.get("paths"))))
        ET.SubElement(rule, "action", type="None")
    elif key == "ips":
        # IIS rewrite rules match single addresses: a range from the Apache side cannot be carried
        listed = addresses_of(values.get("addresses"), True)
        addresses = [item for item in listed if "/" not in item]
        if len(addresses) < len(listed):
            warn("%s: IIS cannot match ranges, so %s are not allowed on Windows" % (snippet["name"], ", ".join(item for item in listed if "/" in item)))
        if not addresses:
            raise ValueError("only ranges are allowed, which IIS cannot match")
        ET.SubElement(rule, "match", url=ACME_IIS if values.get("scope") == "site"
                      else "^(?:%s)(?:/|$)" % alternation(paths_of(values.get("paths"))))
        conditions = ET.SubElement(rule, "conditions")
        ET.SubElement(conditions, "add", input="{REMOTE_ADDR}", matchType="Pattern",
                      pattern="^(?:::1|127\\.0\\.0\\.1|%s)$" % alternation(addresses), ignoreCase="true", negate="true")
        ET.SubElement(conditions, "add", input="{HTTP_X_FORWARDED_FOR}", matchType="Pattern",
                      pattern="(?:^|,\\s*)(?:%s)$" % alternation(addresses), ignoreCase="true", negate="true")
        response = values.get("response")
        if response == "forbidden":
            ET.SubElement(rule, "action", type="CustomResponse", statusCode="403", statusReason="Forbidden", statusDescription="Access denied")
        elif response == "notfound":
            ET.SubElement(rule, "action", type="CustomResponse", statusCode="404", statusReason="Not Found", statusDescription="Not found")
        elif response == "redirect":
            ET.SubElement(rule, "action", type="Redirect", url=target_of(values.get("target")), redirectType="Found")
        else:
            ET.SubElement(rule, "action", type="AbortRequest")
    elif key == "slash":
        ET.SubElement(rule, "match", url=".*")
        conditions = ET.SubElement(rule, "conditions", logicalGrouping="MatchAll")
        ET.SubElement(conditions, "add", input="{REQUEST_FILENAME}", matchType="IsFile", negate="true")
        for pattern in ("\\.[a-zA-Z0-9]{2,10}($|\\?)", "^/umbraco", "^/media", "/$"):
            ET.SubElement(conditions, "add", input="{REQUEST_URI}", pattern=pattern, negate="true")
        ET.SubElement(rule, "action", type="Redirect", url="{R:0}/", redirectType="Permanent")
    elif key == "www":
        ET.SubElement(rule, "match", url="(.*)", ignoreCase="true")
        conditions = ET.SubElement(rule, "conditions", logicalGrouping="MatchAll")
        ET.SubElement(conditions, "add", input="{HTTP_HOST}", pattern="^www\\.(.+)$")
        ET.SubElement(rule, "action", type="Redirect", url="https://{C:1}/{R:0}", appendQueryString="true", redirectType="Permanent")
    elif key == "rewrite":
        rewrite = rewrite_of(values)
        ET.SubElement(rule, "match", url="^%s$" % alternation([rewrite["from"]]))
        ET.SubElement(rule, "action", type="Rewrite", url=rewrite["to"], appendQueryString="true")
    else:
        raise ValueError("unknown snippet %r" % key)
    return rule


COMPRESSION = """<httpCompression directory="%SystemDrive%\\inetpub\\temp\\IIS Temporary Compressed Files">
  <scheme name="gzip" dll="%Windir%\\system32\\inetsrv\\gzip.dll" />
  <dynamicTypes>
    <add mimeType="text/*" enabled="true" />
    <add mimeType="application/javascript" enabled="true" />
    <add mimeType="application/json" enabled="true" />
  </dynamicTypes>
  <staticTypes>
    <add mimeType="text/*" enabled="true" />
    <add mimeType="application/javascript" enabled="true" />
    <add mimeType="application/json" enabled="true" />
  </staticTypes>
</httpCompression>"""


def child(parent, tag, index=None):
    found = parent.find(tag)
    if found is None:
        found = ET.Element(tag)
        if index is None:
            parent.append(found)
        else:
            parent.insert(index, found)
    return found


def normalized(root):
    root = copy.deepcopy(root)
    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def apply_iis(path, snippets):
    snippets = sorted((item for item in snippets if item["key"] in CARRIED), key=lambda item: ORDER.index(item["key"]))
    tree = ET.parse(path, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    before = normalized(tree.getroot())
    server = tree.getroot().find(".//system.webServer")
    if server is None:
        raise ValueError("%s has no system.webServer element" % path)
    rewrite = server.find("rewrite")
    rules = rewrite.find("rules") if rewrite is not None else None
    outbound = rewrite.find("outboundRules") if rewrite is not None else None
    # What was carried before goes; it is written again from the main site
    for parent in [item for item in (rules, outbound) if item is not None]:
        for rule in [item for item in parent.findall("rule") if (item.get("name") or "").startswith(PREFIX)]:
            parent.remove(rule)
    conditions = outbound.find("preConditions") if outbound is not None else None
    if conditions is not None:
        for condition in [item for item in conditions.findall("preCondition") if (item.get("name") or "").startswith(PREFIX)]:
            conditions.remove(condition)
    inbound = []
    for item in snippets:
        if item["key"] in ("compression", "cookies"):
            continue
        try:
            inbound.append(iis_rule(item))
        except ValueError as exc:
            warn("%s is not carried: %s" % (item["name"], exc))
    if inbound:
        rewrite = child(server, "rewrite")
        rules = child(rewrite, "rules", 0)
        # After the HTTPS redirect when there is one, so everything is on HTTPS first
        existing = list(rules)
        at = next((index + 1 for index, item in enumerate(existing) if item.get("name") == "Rewrite HTTP to HTTPS"), 0)
        for offset, rule in enumerate(inbound):
            rules.insert(at + offset, rule)
    if any(item["key"] == "cookies" for item in snippets):
        rewrite = child(server, "rewrite")
        outbound = child(rewrite, "outboundRules")
        rule = ET.Element("rule", name=PREFIX + "Add Secure to ALL cookies", preCondition=PREFIX + "NeedsSecureFlag")
        ET.SubElement(rule, "match", serverVariable="RESPONSE_Set-Cookie", pattern="(.*)")
        ET.SubElement(rule, "action", type="Rewrite", value="{R:1}; secure")
        outbound.insert(0, rule)
        conditions = child(outbound, "preConditions")
        condition = ET.SubElement(conditions, "preCondition", name=PREFIX + "NeedsSecureFlag")
        ET.SubElement(condition, "add", input="{RESPONSE_Set-Cookie}", pattern="secure", negate="true")
    # Compression is a section of its own: added when the site has none, never taken away
    if any(item["key"] == "compression" for item in snippets) and server.find("httpCompression") is None:
        server.append(ET.fromstring(COMPRESSION))
    # Compared with the same indentation on both sides: a web.config that only differs in spaces is left as it is
    if normalized(tree.getroot()) == before:
        return False
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return True


def main(args):
    if len(args) == 2 and args[0] == "read":
        with open(args[1], encoding="utf-8") as stream:
            print(json.dumps(read(stream.read())))
    elif len(args) == 3 and args[0] == "apache":
        with open(args[1], encoding="utf-8") as stream:
            text = stream.read()
        updated = apply_apache(text, json.loads(args[2]))
        if updated != text:
            with open(args[1], "w", encoding="utf-8") as stream:
                stream.write(updated)
        print("changed" if updated != text else "unchanged")
    elif len(args) == 3 and args[0] == "iis":
        try:
            with open(args[1], encoding="utf-8") as stream:
                snippets = read(stream.read())
        except FileNotFoundError:
            snippets = []
        print("changed" if apply_iis(args[2], snippets) else "unchanged")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError, ET.ParseError) as exc:
        raise SystemExit("The config snippets could not be carried: %s" % exc)
    finally:
        for message in WARNINGS:
            print("Warning: " + message, file=sys.stderr)
