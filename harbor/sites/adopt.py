"""Make a published site an environment of its main site, or a separate site again. From adopt_site.sh.

Only the production scripts are created or removed. The site's files, service and Apache config stay where they are
and it keeps running. Later publishes to the environment come from the main site's project.
"""
import os
import re
import shlex
import shutil

from harbor.core.system import Stop
from harbor.core.templates import Site
from harbor.environments import settings
from harbor.environments.register import run_create_prod, site_name


def restore_test_scripts(system, site):
    """With no environments left, the site is a site without environments again"""
    if settings.target_ids(system, site.site_root):
        return
    for script in ("publish", "clear_logs"):
        local = system.path(f"{site.site_root}/scripts/{script}_local.sh")
        if local.is_file() and not os.path.lexists(system.path(f"{site.site_root}/scripts/{script}.sh")):
            local.rename(local.with_name(f"{script}.sh"))


def release_site(system, name, target=""):
    """The environment becomes a separate site again, in the folder it has"""
    site = Site(site_name(name))
    if not target or target == "prod":
        raise Stop("Choose the environment to move out.")
    environment = settings.load(system, site.site_root, target)
    if environment.windows:
        raise Stop("Delete this environment instead: on a Windows server that only removes its scripts here.")
    if not environment.LOCAL_PATH:
        # Without the note the admin on the server can copy the site again
        if not settings.remote_exec(system, environment, f"rm -f {shlex.quote(f'/var/lib/vns-managed/{environment.FOLDER}')}", check=False).ok:
            raise Stop(f"Could not reach {environment.IP}. Nothing was changed.")
    elif f"{environment.root}/".startswith(f"{site.site_root}/"):
        raise Stop("This environment is not a separate site, so it cannot be moved out.")
    shutil.rmtree(system.path(f"{site.site_root}/scripts/targets/{target}"), ignore_errors=True)
    system.path(f"{site.site_root}/.vns-publish/target-{target}.json").unlink(missing_ok=True)
    restore_test_scripts(system, site)
    system.say(f"{environment.FOLDER} is a separate site again. Its files and service were not touched.")


def adopt_site(system, adopted, main_name):
    adopted, main_name = site_name(adopted), site_name(main_name)
    if adopted == main_name:
        raise Stop("A site cannot be moved under itself.")

    # ---- the site that is moved
    moved = Site(adopted)
    root = system.path(moved.site_root)
    if not root.is_dir():
        raise Stop(f"Site not found: {adopted}")
    if any(root.glob("*.csproj")):
        raise Stop(f"{adopted} has its own project, so it is a main site.")
    if settings.target_ids(system, moved.site_root):
        raise Stop(f"{adopted} has environments of its own.")
    service, config = system.path(moved.service_path), system.path(moved.config_path)
    if not service.is_file():
        raise Stop(f"{adopted} has no service.")
    if not config.is_file():
        raise Stop(f"{adopted} has no Apache config.")
    service_text, config_text = service.read_text(), config.read_text()
    started = [line[len("ExecStart="):] for line in service_text.splitlines() if line.startswith("ExecStart=")]
    dll = next((match for line in started for match in re.findall(r'[^ "]+\.dll', line)), "")
    if not dll:
        raise Stop(f"Could not find the dll that {adopted} runs.")
    dll = os.path.basename(dll)[:-len(".dll")]
    working = next((line[len("WorkingDirectory="):] for line in service_text.splitlines() if line.startswith("WorkingDirectory=")), "")
    working = (working[:-1] if working.endswith("/") else working) or moved.site_root
    if working != moved.site_root and not working.startswith(f"{moved.site_root}/"):
        raise Stop(f"{adopted} runs from {working}, outside its own folder.")
    binding = next((line.split()[1] for line in config_text.splitlines() if line.split()[:1] == ["ServerName"] and len(line.split()) > 1), "")
    if not binding:
        raise Stop(f"{adopted} has no ServerName.")
    https = "y" if re.search(r"(?m)^[ \t]*SSLEngine[ \t]+On([ \t]|$)", config_text) else "n"
    environment = next((line[len("Environment=ASPNETCORE_ENVIRONMENT="):] for line in service_text.splitlines()
                        if line.startswith("Environment=ASPNETCORE_ENVIRONMENT=")), "")
    if environment not in ("Development", "Test", "Staging", "Production"):
        environment = "Production"

    # ---- the main site. These are also the values create_prod.sh is rendered with.
    site = Site(main_name)
    main = settings.load_main_site(system, site)
    site.project_name, site.binding, site.admin_mail = main.project_name, main.binding, main.admin_mail
    site.publish_folder, site.csharp_kind = main.publish_folder, main.csharp_kind
    project = system.path(f"{site.site_root}/{main.project_name}.csproj").read_text(errors="replace")
    assembly = next((match[1] for match in re.finditer(r"<AssemblyName>(.*)</AssemblyName>", project)), "") or main.project_name
    if assembly != dll:
        raise Stop(f"{adopted} runs {dll}.dll, but {main_name} builds {assembly}.dll. A publish would break the site.")
    system.step(f"Make {adopted} an environment of {main_name}")
    created = run_create_prod(system, site, {"VNS_PROD_MODAL": "1", "VNS_PROD_NO_PUBLISH": "1", "VNS_PROD_TARGET": "local",
                                             "VNS_PROD_LOCAL_PATH": working, "VNS_PROD_FOLDER": adopted, "VNS_PROD_BINDING": binding,
                                             "VNS_PROD_ENVIRONMENT": environment, "VNS_PROD_HTTPS": https})
    if not created.ok:
        raise Stop("", created.returncode)
    system.say(f"{adopted} is now an environment of {main_name}. Its files and service were not touched, and nothing was published.")
