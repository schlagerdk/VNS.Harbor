"""Create a site: an HTML site, a Python app, or a C# site (Umbraco or a dotnet template). From create_site.sh.

The arguments are the script's, in its order, and asked for when not given:
  [folder] [html|python|csharp] [umbraco|empty] [template-version] [project-name] [binding] [publish-folder]
  [Debug|Release] [y|n certificate] [template-name] [Development|Test|Staging|Production] [NuGet-template-package]
"""
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import uuid

from harbor.core.bindings import exclude_from_build, site_binding, validate_project_name, validate_publish_folder
from harbor.core.dotnet import GLOBAL_JSON_NET8, configure_project, ensure_channel, runtime_identifier
from harbor.core.names import normalize_dns_name, normalize_site_name, service_name
from harbor.core.ports import allocate_port
from harbor.core.site_config import (configure_umbraco_https, default_site, install_apache_config, install_service,
                                     issue_certificate, make_default_site)
from harbor.core.system import CommandFailed, Stop
from harbor.core.templates import Site, render
from harbor.server.apache import own_name
from harbor.core.paths import APACHE

# The admin site reads these: the Umbraco version a site gets when none is given
DEFAULT_UMBRACO_VERSION = "18.3.1"
DEFAULT_UMBRACO13_VERSION = "13.16.4"
DOTNET_VERSION = "net10.0"
NEW_PUBLISH_FOLDER = "_development"

SOLUTION = APACHE / "solutionfiles"
SCHEMA_FILES = ("appsettings-schema.json", "appsettings-schema.Umbraco.Cms.json", "appsettings-schema.usync.json", "umbraco-package-schema.json")
TEMPLATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def ask(system, value, prompt, default=""):
    """As ask in common.sh: only when there is no value; a yes/no question is shown as [Y/n] or [y/N]"""
    if value:
        return value
    if prompt.endswith(" (y/n)") and default in ("y", "Y", "n", "N"):
        prompt = prompt[:-len(" (y/n)")] + (" [Y/n]" if default in ("y", "Y") else " [y/N]")
    elif default:
        prompt += f" [{default}]"
    return system.ask(f"{prompt}: ").strip() or default


def install_scripts(system, site, *kinds):
    """The tools of a kind of site (migrate.SITE_SCRIPTS): Harbor does their work, and their names are kept for the
    admin's buttons. A site never gets a scripts folder of Harbor's."""
    from harbor.state.migrate import SITE_SCRIPTS, born_migrated
    born_migrated(system, site, [name for kind in kinds for name in SITE_SCRIPTS[kind]])


def render_to(system, site, template, target):
    system.path(target).write_text(render(Path(template).read_text(), site))


def edit(system, path, change):
    """A file changed in place with its line endings kept, as sed and perl -i keep them (the bundled views have CRLF)"""
    path = system.path(path)
    with open(path, newline="") as stream:
        text = stream.read()
    with open(path, "w", newline="") as stream:
        stream.write(change(text))


def remove(system, *paths):
    for path in map(system.path, paths):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif os.path.lexists(path):
            path.unlink()


def copy(system, source, target):
    """cp -rf: a folder goes into the target folder, a file replaces the target"""
    source, target = Path(source), system.path(target)
    if source.is_dir():
        shutil.copytree(source, target / source.name, dirs_exist_ok=True)
    else:
        shutil.copyfile(source, target)


class Creation:
    def __init__(self, system, arguments):
        self.system = system
        given = list(arguments) + [""] * 12
        (self.name, self.type, self.csharp_kind, self.umbraco_version, self.project_name, self.binding, self.publish_arg,
         self.build_config, self.certificate, self.template_name, self.environment_arg, self.template_package) = given[:12]
        if len(arguments) >= 7 and not self.publish_arg and self.type == "csharp":
            self.publish_arg = "."
        self.template_provided = len(arguments) >= 10
        self.dotnet_version = DOTNET_VERSION
        self.reservation = None

    def publish(self, site, project):
        """The build into the publish folder, as the admin's Publish shows it"""
        system = self.system
        system.run("python3", f"{site.apache_dir}/lib/publish_status.py", "run", site.site_root, "test", "python3",
                   f"{site.apache_dir}/lib/build_as.py", "--", "dotnet", "publish", project, "-c", site.build_config,
                   f"-p:PublishDir={site.publish_folder}", f"-p:CustomAfterMicrosoftCommonTargets={site.apache_dir}/lib/publish.targets",
                   "--runtime", runtime_identifier(system.output("uname", "-m")), "--self-contained", "false", "--nologo", cwd=site.site_root)

    def ask_first(self):
        system = self.system
        certificate = ask(system, self.certificate, "Create an HTTPS certificate? (y/n)", "y")
        if certificate.lower() in ("y", "yes"):
            self.certificate = "y"
        elif certificate.lower() in ("n", "no"):
            self.certificate = "n"
        else:
            raise Stop("Certificate option must be y or n.")
        original = ask(system, self.name, "Enter folder name")
        try:
            self.name = normalize_site_name(original)
            if self.project_name == original:
                self.project_name = service_name(self.name)
        except ValueError as error:
            raise Stop(str(error) if original else "No site given.") from None
        if not self.type:
            system.say("Site type:\n  1) html\n  2) python\n  3) csharp")
            choice = system.ask("Choose [1]: ").strip()
            self.type = {"": "html", "1": "html", "html": "html", "2": "python", "python": "python",
                         "3": "csharp", "csharp": "csharp", "c#": "csharp"}.get(choice)
            if not self.type:
                raise Stop(f"Unknown choice: {choice}")
        if self.type == "csharp":
            self.project_name = ask(system, self.project_name, "Enter C# project name", service_name(self.name))
            try:
                validate_project_name(self.project_name)
            except ValueError as error:
                raise Stop(str(error)) from None
        self.binding = ask(system, self.binding, "Enter site URL binding (* = answer any name)", self.name if "." in self.name else "")
        # * is the site that answers any name no other site has, like Apache's own default site. Its binding is this
        # server's own name, which can have a certificate when it is a public name with a dot.
        self.answers_any = self.binding == "*"
        if self.answers_any:
            current = default_site(system)
            if current:
                raise Stop(f"{current} already answers any name. Only one site can: delete it or give it a binding first.")
            self.binding = own_name(system)
            if self.certificate == "y" and "." not in self.binding:
                raise Stop(f"This server's name, {self.binding}, has no dot, so it cannot get a certificate. Answer n to the "
                           "certificate question, or set a full name with hostnamectl.")
        try:
            # A site without a certificate may use a name without a dot
            self.binding = site_binding(self.binding, http_only=self.certificate == "n")
        except ValueError as error:
            raise Stop(str(error)) from None
        self.site = Site(self.name, binding=self.binding, project_name=self.project_name)
        if system.path(self.site.service_path).exists():
            raise Stop(f"A service already uses this site's canonical name: {self.site.service_name}")

    def https(self):
        if self.certificate == "y":
            self.system.step(f"Issue a certificate for {self.binding}")
            issue_certificate(self.system, self.site, self.binding)

    def port(self):
        self.reservation = allocate_port(self.system, self.name)
        self.site.port_http = str(self.reservation.port)

    def create_html(self):
        system, site = self.system, self.site
        site.doc_root = site.site_root
        system.step("Create html")
        system.path(site.doc_root).mkdir(parents=True, exist_ok=True)
        render_to(system, site, SOLUTION / "html/site/index.html", f"{site.doc_root}/index.html")
        self.https()
        install_apache_config(system, site, SOLUTION / "html/apache.conf.tpl", certificate=self.certificate == "y")
        install_scripts(system, site, "common")

    def create_python(self):
        system, site = self.system, self.site
        site.doc_root = f"{site.site_root}/wwwroot"
        self.port()
        for folder in (f"{site.site_root}/app", site.doc_root, f"/var/lib/vns-roots/{self.name}"):
            system.path(folder).mkdir(parents=True, exist_ok=True)
        self.https()
        system.step("Create python app and virtual environment")
        for template in sorted((SOLUTION / "python/app").iterdir()):
            render_to(system, site, template, f"{site.site_root}/app/{template.name}")
        system.run("python3", "-m", "venv", f"{site.site_root}/venv")
        system.run(f"{site.site_root}/venv/bin/pip", "install", "-r", f"{site.site_root}/app/requirements.txt")
        system.run("chown", "-R", "www-data:www-data", f"{site.site_root}/app", f"{site.site_root}/venv")
        install_scripts(system, site, "common", "service", "python")
        install_apache_config(system, site, SOLUTION / "python/apache.conf.tpl", certificate=self.certificate == "y")
        install_service(system, site, SOLUTION / "python/service.tpl")

    def create_csharp(self):
        system, site = self.system, self.site
        kind_folder = SOLUTION / "csharp"
        project = f"{site.site_root}/{self.project_name}.csproj"
        self.csharp_kind = ask(system, self.csharp_kind, "C# site kind (umbraco/empty)", "umbraco")
        if self.csharp_kind not in ("umbraco", "empty"):
            raise Stop("C# site kind must be umbraco or empty.")
        site.csharp_kind = self.csharp_kind
        # A new site is where development happens: Development, published to _development
        publish = ask(system, self.publish_arg, "Enter local publish folder", NEW_PUBLISH_FOLDER)
        try:
            validate_publish_folder(publish)
        except ValueError as error:
            raise Stop(str(error)) from None
        site.publish_folder = publish
        site.build_config = ask(system, self.build_config, "Build configuration (Debug/Release)", "Debug")
        if site.build_config not in ("Debug", "Release"):
            raise Stop("Build configuration must be Debug or Release")
        site.environment = self.environment_arg or ("Development" if site.build_config == "Debug" else "Production")
        if site.environment not in ("Development", "Test", "Staging", "Production"):
            raise Stop("Environment must be Development, Test, Staging or Production.")
        site.doc_root = f"{site.publish_root}/wwwroot"

        if self.csharp_kind == "umbraco":
            version = ask(system, self.umbraco_version, "Set umbraco version", DEFAULT_UMBRACO_VERSION)
            kind_folder = SOLUTION / "csharp/umbraco"
            if version == "13" or version.startswith("13."):
                version = DEFAULT_UMBRACO13_VERSION if version == "13" else version
                match = re.fullmatch(r"13\.([0-9]+)\.([0-9]+)", version)
                if not match or (int(match[1]), int(match[2])) < (3, 2):
                    raise Stop("Umbraco 13 requires version 13.3.2 or newer for VNS.Umbraco.")
                self.dotnet_version = "net8.0"
                ensure_channel(system, "8.0")
            self.umbraco_version = site.umbraco_version = version
        else:
            if not self.template_provided:
                self.template_name = ask(system, self.template_name, "Template name (optional)", "web")
            self.template_name = self.template_name or "web"
            if not TEMPLATE_NAME.fullmatch(self.template_name):
                raise Stop("Invalid template name. Use letters, numbers, dots, hyphens or underscores.")
            if self.umbraco_version:
                if (not re.fullmatch(r"[0-9][A-Za-z0-9.+-]*", self.umbraco_version)
                        or not TEMPLATE_NAME.fullmatch(self.template_package)):
                    raise Stop("A template version requires a valid NuGet template package (argument 12) and version.")
                system.say(f"Install template package: {self.template_package}@{self.umbraco_version}")
                system.run("dotnet", "new", "install", f"{self.template_package}@{self.umbraco_version}", "--force")
        site.dotnet_version = self.dotnet_version

        self.port()
        for folder in (site.site_root, site.doc_root):
            system.path(folder).mkdir(parents=True, exist_ok=True)
        self.https()
        system.path(f"/var/lib/vns-roots/{self.name}").mkdir(parents=True, exist_ok=True)
        install_scripts(system, site, "common", "service", "csharp", *(["umbraco"] if self.csharp_kind == "umbraco" else []))

        if self.csharp_kind == "umbraco":
            if self.dotnet_version == "net8.0":
                system.path(f"{site.site_root}/global.json").write_text(GLOBAL_JSON_NET8)
            self.create_umbraco_project(project)
        else:
            system.step(f"Create .NET project from template: {self.template_name}")
            system.run("dotnet", "new", self.template_name, "--name", self.project_name, "--output", ".", "--force", cwd=site.site_root)
            if not system.path(project).is_file():
                raise Stop(f"Template '{self.template_name}' did not create {self.project_name}.csproj; a runnable C# site project is required.")
            edit(system, project, lambda text: exclude_from_build(text, site.publish_folder))
            configure_project(system, project)
            system.step(f"Build {self.project_name}")
            self.publish(site, project)
            remove(system, f"{site.site_root}/bin", f"{site.site_root}/obj")

        # The service runs as www-data and needs to write inside the publish folder only
        system.run("chown", "-R", "www-data:www-data", f"{site.site_root}/{site.publish_folder}")
        conf, service = kind_folder / "apache.conf.tpl", kind_folder / "service.tpl"
        install_apache_config(system, site, conf if conf.is_file() else SOLUTION / "csharp/apache.conf.tpl", certificate=self.certificate == "y")
        install_service(system, site, service if service.is_file() else SOLUTION / "csharp/service.tpl")

    def create_umbraco_project(self, project):
        system, site = self.system, self.site
        files = SOLUTION / "csharp/umbraco/project"
        publish = f"{site.site_root}/{site.publish_folder}"
        major = int(self.umbraco_version.split(".")[0])
        root = site.site_root

        system.step("Create new umbraco instance in folder")
        package = f"Umbraco.Templates{'::' if self.dotnet_version == 'net8.0' else '@'}{self.umbraco_version}"
        system.run("dotnet", "new", "install", package, "--force", cwd=root)
        system.run("dotnet", "new", "umbraco", "--force", cwd=root)
        for template_project in system.path(root).glob("*.csproj"):
            template_project.unlink()
        system.say("Remove central package management from template (our csproj sets versions itself)")
        remove(system, *(f"{root}/{name}" for name in ("Directory.Packages.props", "Directory.Build.props", "Directory.Build.targets")))
        if major >= 18:
            system.say("Add bundled Program.cs")
            copy(system, files / "Program.cs", f"{root}/Program.cs")
        system.say("Add project file (replace umbraco and .NET version)")
        text = (files / "UmbracoCoreProjectName.csproj").read_text()
        text = text.replace("UmbracoCoreInstallVersion", self.umbraco_version).replace("DotNetVersion", self.dotnet_version)
        system.path(project).write_text(exclude_from_build(text, site.publish_folder))
        if major >= 17:
            system.run("dotnet", "add", "package", "Umbraco.Cms.DevelopmentMode.Backoffice", "-v", self.umbraco_version, cwd=root)
        system.step("Install VNS Umbraco and uSync package")
        for package in ("VNS.Umbraco", "uSync"):
            version = ["-v", "13.0.0"] if self.dotnet_version == "net8.0" else []
            system.run("dotnet", "add", "package", package, *version, cwd=root)
        if major >= 17:
            # VNS Harbor in the backoffice (the Backend login and the sync dashboard), for Umbraco 17 and newer. A site is
            # made also when it cannot be added, for instance before it is on the package source.
            system.step("Install VNS Harbor package")
            if not system.run("dotnet", "add", "package", "VNS.Harbor", cwd=root, check=False).ok:
                system.warn("VNS.Harbor could not be added. Add it to the project later: dotnet add package VNS.Harbor")
        if major >= 18:
            system.say("Delete default wwwroot, views and default settings")
            remove(system, *(f"{root}/{name}" for name in ("wwwroot", "Views", "appsettings.json", "appsettings.Development.json")))
        remove(system, f"{root}/Properties", f"{root}/.gitignore", *(f"{root}/{name}" for name in SCHEMA_FILES if name != "appsettings-schema.usync.json"))

        system.step("Publish site")
        self.publish(site, project)
        system.say("Delete bin and schema files")
        remove(system, f"{root}/bin", *(f"{root}/{name}" for name in SCHEMA_FILES), *(f"{publish}/{name}" for name in SCHEMA_FILES))
        if major >= 18:
            system.say("Add bundled appsettings.json")
            text = (files / "appsettings.json").read_text().replace("FolderName", self.binding).replace("UUID", str(uuid.uuid4()))
            system.path(f"{publish}/appsettings.json").write_text(text)
        if self.certificate == "n":
            configure_umbraco_https(system, f"{publish}/appsettings.json", self.binding, False)
        if major >= 18:
            system.say("Add default wwwroot, views and uSync")
            for folder in ("wwwroot", "Views", "uSync"):
                copy(system, files / folder, publish)
            # The template's cache stamp becomes the time the site was made, so its CSS and scripts load fresh
            stamp = f"{datetime.now():%Y%m%d%H%M}"
            edit(system, f"{publish}/Views/ROOT.cshtml",
                 lambda text: re.sub(r'(var cacheDate = ")[0-9]{12}(")', lambda match: f"{match[1]}{stamp}{match[2]}", text))

    def run(self):
        system = self.system
        self.ask_first()
        create = {"html": self.create_html, "python": self.create_python, "csharp": self.create_csharp}.get(self.type)
        if create is None:
            raise Stop(f"Unknown site type: {self.type}")
        # A command that fails leaves the site half made, as the script's trap said; a refusal stops before that
        try:
            create()
        except CommandFailed as error:
            self.incomplete(str(error), error.result.returncode or 1)
        except (OSError, RuntimeError, ValueError) as error:
            self.incomplete(str(error), 1)
        finally:
            if self.reservation:
                self.reservation.release()
        if self.answers_any:
            try:
                make_default_site(system, self.site)
            except RuntimeError as error:
                raise Stop(str(error)) from None
        scheme = "https" if self.certificate == "y" else "http"
        system.say(f"Site created: {scheme}://{self.binding}/ (folder: {self.name})")
        if self.answers_any:
            warning = " Browsers warn for names that are not in its certificate." if scheme == "https" else ""
            system.say(f"It also answers any name that no other site has, like {scheme}://<server-ip>/.{warning}")
        if self.type == "csharp":
            from harbor.state.migrate import server_migrated
            system.say("Use Add environment in the admin when you want a production target for this site." if server_migrated(system)
                       else f"Run {self.site.site_root}/scripts/create_prod.sh when you want a production target for this site.")

    def incomplete(self, message, code):
        if message:
            self.system.warn(message)
        self.system.warn(f"Site is incomplete - run ./delete_site.sh {self.name} n to clean up")
        raise Stop("", code)


def create_site(system, arguments=()):
    Creation(system, arguments).run()
