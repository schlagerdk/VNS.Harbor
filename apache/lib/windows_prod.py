"""Files for a Windows (IIS) production target: web.config and the first publish's appsettings.

Usage:
  windows_prod.py web-config PROJECT ENVIRONMENT HTTPS UMBRACO  Print a new web.config (HTTPS, UMBRACO: y/n)
  windows_prod.py set-environment WEB_CONFIG ENVIRONMENT  Change the environment in an existing web.config
  windows_prod.py set-https WEB_CONFIG HTTPS              Add or remove the HTTPS redirect in an existing web.config
  windows_prod.py prepare-appsettings FILE URL HTTPS      Remove ConnectionStrings and set the Umbraco URL (HTTPS: y/n)
  windows_prod.py set-url FILE URL HTTPS                  Set the Umbraco URL only
"""
import json
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

ENVIRONMENT_VARIABLES = ("ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT")

WEB_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <location path="." inheritInChildApplications="false">
    <system.webServer>
      <handlers>
        <add name="aspNetCore" path="*" verb="*" modules="AspNetCoreModuleV2" resourceType="Unspecified" />
      </handlers>
      <aspNetCore processPath="dotnet" arguments={arguments} stdoutLogEnabled="false" stdoutLogFile=".\\logs\\stdout" hostingModel="inprocess">
        <environmentVariables>
{variables}
        </environmentVariables>
      </aspNetCore>
{rewrite}    </system.webServer>
  </location>
</configuration>
"""

HTTPS_RULE = """          <rule name="Rewrite HTTP to HTTPS" stopProcessing="true" enabled="true">
            <match url="(.*)" />
            <conditions>
              <add input="{HTTPS}" pattern="^OFF$" />
            </conditions>
            <action type="Redirect" url="https://{HTTP_HOST}{REQUEST_URI}" appendQueryString="false" />
          </rule>
"""

UMBRACO_RULES = """          <rule name="Rewrite to Sitemap.xml" stopProcessing="true" enabled="true">
            <match url="^sitemap.xml" />
            <action type="Rewrite" url="SEO_SiteMap_xml" />
          </rule>
          <rule name="Rewrite to Robots.txt" stopProcessing="true" enabled="true">
            <match url="^robots.txt" />
            <action type="Rewrite" url="SEO_Robots_txt" />
          </rule>
"""


def web_config(project, environment, https=False, umbraco=False):
    variables = "\n".join(
        '          <environmentVariable name="%s" value=%s />' % (name, quoteattr(environment))
        for name in ENVIRONMENT_VARIABLES)
    # The HTTPS redirect comes first, so sitemap.xml and robots.txt are also redirected from http://
    rules = (HTTPS_RULE if https else "") + (UMBRACO_RULES if umbraco else "")
    rewrite = "      <rewrite>\n        <rules>\n%s        </rules>\n      </rewrite>\n" % rules if rules else ""
    return WEB_CONFIG.format(arguments=quoteattr(".\\%s.dll" % project), variables=variables, rewrite=rewrite)


def set_environment(path, environment):
    tree = ET.parse(path, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    module = tree.getroot().find(".//aspNetCore")
    if module is None:
        raise ValueError("%s has no aspNetCore element" % path)
    variables = module.find("environmentVariables")
    if variables is None:
        variables = ET.SubElement(module, "environmentVariables")
    for name in ENVIRONMENT_VARIABLES:
        variable = next((item for item in variables.findall("environmentVariable") if item.get("name") == name), None)
        if variable is None:
            variable = ET.SubElement(variables, "environmentVariable", name=name)
        variable.set("value", environment)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


HTTPS_RULE_NAME = "Rewrite HTTP to HTTPS"


def set_https(path, https):
    """Add the HTTPS redirect to an existing web.config, first among its rules, or remove it for a site on HTTP.
    Only the rule made here, found by its name, is added or removed: the rest of the file is kept.
    Returns whether the file changed."""
    tree = ET.parse(path, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    server = tree.getroot().find(".//system.webServer")
    if server is None:
        raise ValueError("%s has no system.webServer element" % path)
    rewrite = server.find("rewrite")
    rules = rewrite.find("rules") if rewrite is not None else None
    existing = [rule for rule in (rules.findall("rule") if rules is not None else []) if rule.get("name") == HTTPS_RULE_NAME]
    if https and existing or not https and not existing:
        return False
    if https:
        if rewrite is None:
            rewrite = ET.SubElement(server, "rewrite")
        if rules is None:
            rules = ET.SubElement(rewrite, "rules")
        rules.insert(0, ET.fromstring(HTTPS_RULE))
    else:
        for rule in existing:
            rules.remove(rule)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return True


def prepare_appsettings(path, url, https, keep_connections=False):
    with open(path, encoding="utf-8-sig") as stream:
        settings = json.load(stream)
    if not keep_connections:
        settings.pop("ConnectionStrings", None)
    cms = (settings.get("Umbraco") or {}).get("CMS") or {}
    routing = cms.get("WebRouting") or {}
    if "UmbracoApplicationUrl" in routing:
        routing["UmbracoApplicationUrl"] = url
    global_settings = cms.get("Global") or {}
    if "UseHttps" in global_settings:
        global_settings["UseHttps"] = https != "n"
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(settings, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def main(args):
    if len(args) == 5 and args[0] == "web-config":
        sys.stdout.write(web_config(args[1], args[2], args[3] == "y", args[4] == "y"))
    elif len(args) == 3 and args[0] == "set-environment":
        set_environment(args[1], args[2])
    elif len(args) == 3 and args[0] == "set-https":
        try:
            print("changed" if set_https(args[1], args[2] != "n") else "unchanged")
        except (ET.ParseError, ValueError, OSError) as exc:
            raise SystemExit("web.config could not be read (%s), so its HTTPS redirect is not changed" % exc)
    elif len(args) == 4 and args[0] == "prepare-appsettings":
        prepare_appsettings(args[1], args[2], args[3])
    elif len(args) == 4 and args[0] == "set-url":
        prepare_appsettings(args[1], args[2], args[3], keep_connections=True)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
