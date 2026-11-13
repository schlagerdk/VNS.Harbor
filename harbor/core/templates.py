"""A site's names and paths, and the templates rendered with them. From lib/common.sh: set_site_vars and render."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from harbor.core.names import normalize_dns_name
from harbor.core.paths import APACHE

ADMIN_MAIL = "mhs@vns.dk"


@dataclass
class Site:
    """What a template is rendered with. Unset values get the defaults render() in common.sh gives them."""
    name: str
    binding: str = ""
    project_name: str = ""
    # Where the sites are: VNS_WEB_ROOT, as common.sh reads it (tests run sites from a folder of their own)
    root_path: str = field(default_factory=lambda: os.environ.get("VNS_WEB_ROOT") or "/var/www")
    publish_folder: str = "_publish"
    doc_root: str = ""
    port_http: str = ""
    dotnet_version: str = ""
    umbraco_version: str = ""
    admin_mail: str = ADMIN_MAIL
    build_config: str = ""
    environment: str = ""
    # The apache folder of this installation (/vns/apache on a server), which the scripts of a site run
    apache_dir: str = str(APACHE)
    csharp_kind: str = ""
    access_rules: str = ""

    @property
    def site_root(self):
        return f"{self.root_path}/{self.name}"

    @property
    def publish_root(self):
        """The folder the site runs from: its publish folder, or its own folder when it publishes there"""
        return self.site_root if self.publish_folder == "." else f"{self.site_root}/{self.publish_folder}"

    @property
    def certificate_directory(self):
        return f"/etc/ssl/certs/{self.name}_ecc"

    @property
    def certificate_file(self):
        return f"{self.certificate_directory}/fullchain.cer"

    @property
    def key_file(self):
        return f"{self.certificate_directory}/{self.name}.key"

    @property
    def config_path(self):
        return f"/etc/apache2/sites-available/{self.name}.conf"

    @property
    def service_name(self):
        return normalize_dns_name(self.name)

    @property
    def service_path(self):
        return f"/etc/systemd/system/{self.service_name}.service"


def render(text, site):
    """A template with its @PLACEHOLDERS@ filled in, in the order render() in common.sh replaces them"""
    replacements = [
        ("@SITE_ROOT@/@PUBLISH_FOLDER@", site.publish_root),
        ("@SITE@", site.name),
        ("@SERVICE_NAME@", site.service_name),
        ("@BINDING@", site.binding or site.name),
        ("@PROJECT_NAME@", site.project_name or site.name),
        ("@ROOT_PATH@", site.root_path),
        ("@SITE_ROOT@", site.site_root),
        ("@PUBLISH_FOLDER@", site.publish_folder),
        ("@DOC_ROOT@", site.doc_root),
        ("@PORT_HTTP@", site.port_http),
        ("@DOTNET_VERSION@", site.dotnet_version),
        ("@UMBRACO_VERSION@", site.umbraco_version),
        ("@CERT_FILE@", site.certificate_file),
        ("@KEY_FILE@", site.key_file),
        ("@ADMIN_MAIL@", site.admin_mail),
        ("@BUILD_CONFIG@", site.build_config or "Debug"),
        ("@ENVIRONMENT@", site.environment or "Test"),
        ("@APACHE_DIR@", site.apache_dir),
        ("@CSHARP_KIND@", site.csharp_kind),
        ("@ACCESS_RULES@", site.access_rules or "Require all granted"),
    ]
    for placeholder, value in replacements:
        text = text.replace(placeholder, value)
    return text
