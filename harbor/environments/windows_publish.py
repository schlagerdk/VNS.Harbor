"""Publish a C# site (always a Release build) to its IIS site on a Windows server, over SMB with rclone. From the site
template csharp/windows-prod-scripts/publish_prod.sh (phase 2d).

The server needs the IIS site and its folder, the ASP.NET Core Hosting Bundle and the IIS URL Rewrite Module. web.config
is made when the server has none, with the HTTPS redirect (HTTPS sites) and the sitemap.xml and robots.txt rewrites
(Umbraco sites). After the first publish web.config, appsettings*.json and the site's content are never overwritten by a
publish. A dry run builds and shows what would change on the server.
"""
from pathlib import Path
import os
import shutil
import tempfile

from harbor.core.system import CommandFailed, Stop
from harbor.environments.windows import Share

APACHE = Path(__file__).resolve().parents[2] / "apache"
SCHEMA_FILES = ("appsettings-schema.json", "appsettings-schema.usync.json", "appsettings-schema.Umbraco.Cms.json", "umbraco-package-schema.json")


def helper(system, name, *args):
    """apache/lib/<name> with the arguments: what it prints, its reasons shown as they come"""
    result = system.run("python3", str(APACHE / "lib" / name), *args, check=False, capture=True)
    if result.stderr.strip():
        system.warn(result.stderr.rstrip("\n"))
    return result


def publish_windows(system, site, environment, project, local_publish, dry_run=False):
    https = "n" if environment.HTTPS == "n" else "y"
    site_url = f"{'http' if https == 'n' else 'https'}://{environment.BINDING}"
    local_publish = local_publish or "_publish"
    if dry_run:
        system.say("DRY RUN: nothing will be changed on the server.")
    work = Path(tempfile.mkdtemp(prefix="publish-windows."))
    output = work / "publish"
    share = None
    copying = False
    try:
        system.step(f"Check {environment.WINDOWS_SHARE}")
        try:
            share = Share(system, environment).connect()
        except RuntimeError as error:
            raise Stop(str(error)) from None
        # Is this the first publish? Decided before anything is copied.
        listing = share.rclone("lsf", share.target, "--max-depth", "1", "--files-only", capture=True).stdout.splitlines()
        first_publish = not any(line.lower() == "appsettings.json" for line in listing)
        web_config_name = next((line for line in listing if line.lower() == "web.config"), "")

        # Built here first, so the site stays up if the build fails
        system.step("Publish (Release, win-x64)")
        system.run("python3", str(APACHE / "lib/build_as.py"), "--", "dotnet", "publish", f"{project}.csproj", "-c", "Release", f"-p:PublishDir={output}/",
                   f"-p:EnvironmentName={environment.other.get('ENVIRONMENT', '')}", f"-p:CustomAfterMicrosoftCommonTargets={APACHE}/lib/publish.targets",
                   "--runtime", "win-x64", "--self-contained", "false", "--nologo", cwd=site.site_root)
        if not (output / f"{project}.dll").is_file():
            raise Stop(f"Publish failed: {project}.dll not found in the output")

        system.say("Delete schema files and bin")
        for name in SCHEMA_FILES:
            system.path(f"{site.site_root}/{name}").unlink(missing_ok=True)
        for file in [*output.glob("appsettings-schema*.json"), output / "umbraco-package-schema.json"]:
            file.unlink(missing_ok=True)
        shutil.rmtree(system.path(f"{site.site_root}/bin"), ignore_errors=True)

        # The project's Linux publish folder must not end up on the Windows server
        if local_publish != "." and os.path.lexists(output / local_publish):
            raise Stop(f"FAILED: the build output contains {local_publish}. Exclude it in the .csproj. Nothing was changed on the server.")

        if first_publish:
            system.say("First publish: prepare appsettings*.json (without ConnectionStrings)")
            # Prefer the files from the local publish folder (made by Create site); fall back to the fresh build
            local = system.path(f"{site.site_root}/{local_publish}")
            if (local / "appsettings.json").is_file():
                for file in output.glob("appsettings*.json"):
                    file.unlink()
                for file in sorted(local.glob("appsettings*.json")):
                    shutil.copy(file, output)
                for file in output.glob("appsettings-schema*.json"):
                    file.unlink()
            for file in sorted(output.glob("appsettings*.json")):
                if helper(system, "windows_prod.py", "prepare-appsettings", str(file), site_url, environment.HTTPS).returncode:
                    raise Stop("", 1)

        # copy, never sync: sync would delete logs and uploaded media on the server. Once the site exists, its
        # settings and content are never overwritten by a publish.
        options = ["--create-empty-src-dirs", "--modify-window", "2s", "--stats", "0", "-v", "--ignore-case", "--exclude", "/web.config"]
        if not first_publish:
            options += [argument for pattern in ("appsettings*.json", "/umbraco/**", "/uSync/**", "/Views/**", "/wwwroot/**", "/App_Data/**")
                        for argument in ("--exclude", pattern)]

        # An existing web.config is kept, but its HTTPS redirect follows the site: added when it answers on HTTPS
        update_web_config = False
        web_config = work / "web.config"
        if web_config_name:
            share.rclone("copyto", f"{share.target}/{web_config_name}", str(web_config))
            # A web.config that is not plain XML is left as it is, with the reason
            if helper(system, "windows_prod.py", "set-https", str(web_config), https).stdout.strip() == "changed":
                update_web_config = True
        else:
            umbraco = "y" if "Umbraco.Cms" in (system.path(f"{site.site_root}/{project}.csproj").read_text(errors="replace")
                                                if system.path(f"{site.site_root}/{project}.csproj").is_file() else "") else "n"
            made = helper(system, "windows_prod.py", "web-config", project, environment.other.get("ENVIRONMENT", ""), https, umbraco)
            if made.returncode:
                raise Stop("", made.returncode)
            web_config.write_text(made.stdout)

        # The main site's config snippets (Allowed IPs, rewrites and the like) as rules named "VNS: ...", made equal to
        # the main site's at every publish. Rules written by hand are kept.
        main_config = system.path(f"/etc/apache2/sites-available/{site.name}.conf")
        if main_config.is_file() and helper(system, "config_snippets.py", "iis", str(main_config), str(web_config)).stdout.strip() == "changed":
            update_web_config = update_web_config or bool(web_config_name)

        if dry_run:
            if not web_config_name:
                system.say("web.config would be created.")
            if update_web_config:
                system.say("web.config would be updated: its HTTPS redirect and the main site's config snippets.")
            share.rclone("copy", str(output), share.target, *options, "--dry-run")
            system.say("Dry run completed.")
            return

        share.take_offline(site_url)
        system.step(f"Copy to {environment.WINDOWS_SHARE}")
        # A file IIS has not released yet is tried again, rather than leaving the site half updated
        copying = True
        share.rclone("copy", str(output), share.target, *options, "--retries", "5", "--retries-sleep", "5s")
        if update_web_config:
            system.say("web.config: the HTTPS redirect and the main site's config snippets are brought up to date")
            share.rclone("copyto", str(web_config), f"{share.target}/{web_config_name}")
        elif web_config_name:
            system.say("web.config already exists on the server - not touched")
        else:
            system.say("Create web.config")
            share.rclone("copyto", str(web_config), f"{share.target}/web.config")
        # Every file is in place: the site may come back
        copying = False
        share.bring_online()

        # The first request starts the app, which is why this can take a while
        system.step(f"Check {site_url}")
        status = system.run("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "180", site_url, check=False, capture=True).stdout.strip()
        if status[:1] in ("2", "3", "4"):
            system.say(f"OK: {site_url} answered with HTTP {status}")
        elif first_publish or os.environ.get("VNS_MOVING") == "1":
            # The first publish has no ConnectionStrings, and DNS or the IIS binding may not be ready yet
            system.say(f"WARNING: {site_url} answered with HTTP {status}. Set the connection string in appsettings.json on the server if the site "
                       "needs one, and check the IIS binding.")
        else:
            raise Stop(f"FAILED: {site_url} answered with HTTP {status}. Check the log on the Windows server.")
    except CommandFailed as error:
        raise Stop("", error.result.returncode or 1) from None
    finally:
        # Bring the site back up, also if the publish fails before its files are touched. A copy that stopped halfway
        # leaves old and new files mixed, so the update page stays until a publish finishes.
        try:
            if copying:
                system.warn("FAILED: the copy did not finish, so the site keeps showing its update page. Publish again; it is removed when a publish finishes.")
            elif share:
                share.bring_online()
        finally:
            shutil.rmtree(work, ignore_errors=True)
