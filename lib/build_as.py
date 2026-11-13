"""Build a site's project as the unprivileged user vns-build, never as root.

A project's .csproj, its NuGet packages and Directory.Build.* files can run any command while it is built or
restored. The admin and the publish scripts run as root, so a build run by them would give anyone who can change
a project the whole server. Here the build runs as vns-build instead, which may write only:
  - obj/ and bin/ in the project
  - its own home (/var/lib/vns-build: the NuGet cache) and a staging folder there
A publish goes to the staging folder; root then copies the result to the publish folder the command named, without
following or copying symbolic links, so a build cannot make root write anywhere else.

  build_as.py -- dotnet <arguments...>        Run a dotnet command for the project in the current folder
"""
import grp
import os
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

USER = "vns-build"
HOME = Path("/var/lib/vns-build")
OUTPUT_FOLDERS = ("obj", "bin")
# Files a build writes into the project folder itself, like Umbraco's JSON schemas: the build user's to write
GENERATED = ("appsettings-schema*.json", "umbraco-package-schema.json")
# Never the build user's, whatever is in them: scripts run as root, and the publish folder is the live site
NOT_FOR_BUILD = {"scripts", ".vns-publish", ".git", "_publish"}


def ensure_user():
    """The build user, made on first use: a system user without a login and with its home for the NuGet cache"""
    try:
        pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--home-dir", str(HOME), "--shell", "/usr/sbin/nologin", USER], check=True)
    account = pwd.getpwnam(USER)
    for folder in (HOME, HOME / "stage", HOME / ".nuget"):
        folder.mkdir(mode=0o750, exist_ok=True)
        os.chown(folder, account.pw_uid, account.pw_gid)
    # Private NuGet feeds root has set up are needed by the build too
    own = HOME / ".nuget/NuGet/NuGet.Config"
    shared = Path("/root/.nuget/NuGet/NuGet.Config")
    if shared.is_file() and not own.exists():
        own.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared, own)
        for path in (own.parent, own):
            os.chown(path, account.pw_uid, account.pw_gid)
    return account


def give(path, uid, gid):
    """path and everything in it to the build user; symbolic links themselves, never what they point to"""
    if path.is_symlink():
        path.unlink()
        return
    os.lchown(path, uid, gid)
    if path.is_dir():
        for directory, folders, files in os.walk(path, followlinks=False):
            for name in folders + files:
                os.lchown(os.path.join(directory, name), uid, gid)


def group_may_write(folder):
    """The folder's own group may write in it, also when the folder has an ACL. A folder closed to others (Harbor's
    users) has one, and then chmod changes only its mask: the group's own entry stays what it was (r-x when the folder
    was closed before it was ever built), and the build user cannot make the files a build writes there."""
    if shutil.which("setfacl"):
        subprocess.run(["setfacl", "-m", "g::rwx", str(folder)], check=True)


def prepare(project, account, keep):
    """The build user may write obj/ and bin/, and read the rest of the project. The project folder stays root's,
    with the sticky bit, as /tmp has it: the build user may add files of its own there (a build writes some, like
    Umbraco's JSON schemas), but cannot rename, remove or replace what is root's, such as scripts/ and the project
    file. A build that could change those could change what root runs."""
    if project.is_symlink() or not project.is_dir():
        raise ValueError("The project folder is missing or is a symbolic link")
    group = grp.getgrnam(USER).gr_gid
    os.chown(project, 0, group)
    os.chmod(project, (project.stat().st_mode & 0o7777) | stat.S_ISVTX | stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP)
    group_may_write(project)
    # Root's own records stay root's: made here first, so the build user cannot make them
    # A migrated site (phase 6) keeps them in Harbor's copy, with no .vns-publish in its folder
    if not (Path("/var/lib/harbor/sites") / project.name / "migrated.json").is_file():
        (project / ".vns-publish").mkdir(mode=0o700, exist_ok=True)
    # Generated files root's builds left behind become the build user's, so the next build can write them again
    for pattern in GENERATED:
        for path in project.glob(pattern):
            if path.is_file() and not path.is_symlink():
                os.lchown(path, account.pw_uid, account.pw_gid)
    for name in OUTPUT_FOLDERS:
        folder = project / name
        if not folder.exists():
            folder.mkdir(mode=0o755)
        give(folder, account.pw_uid, account.pw_gid)
    # What others cannot read is made readable for the build user's group, and nothing else is changed
    group = grp.getgrnam(USER).gr_gid
    for directory, folders, files in os.walk(project, followlinks=False):
        here = Path(directory)
        folders[:] = [name for name in folders if (here / name) not in keep and name not in NOT_FOR_BUILD | set(OUTPUT_FOLDERS)
                      and not (here / name).is_symlink()]
        for name in folders + files:
            path = here / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & stat.S_IROTH:
                continue
            os.lchown(path, -1, group)
            os.chmod(path, info.st_mode | stat.S_IRGRP | (stat.S_IXGRP if stat.S_ISDIR(info.st_mode) else 0))


def copy_out(staging, target):
    """The published files to the publish folder, as root: plain files and folders only"""
    target.mkdir(parents=True, exist_ok=True)
    for directory, folders, files in os.walk(staging, followlinks=False):
        here = Path(directory)
        out = target / here.relative_to(staging)
        folders[:] = [name for name in folders if not (here / name).is_symlink()]
        for name in folders:
            destination = out / name
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                destination.unlink()
            destination.mkdir(exist_ok=True)
        for name in files:
            source = here / name
            if not stat.S_ISREG(source.lstat().st_mode):
                continue
            destination = out / name
            if destination.is_symlink() or destination.is_dir():
                shutil.rmtree(destination) if destination.is_dir() and not destination.is_symlink() else destination.unlink()
            shutil.copy2(source, destination, follow_symlinks=False)


def environment():
    dotnet = os.environ.get("DOTNET_ROOT") or "/usr/lib/dotnet"
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(HOME), "DOTNET_CLI_HOME": str(HOME),
            "NUGET_PACKAGES": str(HOME / ".nuget/packages"), "DOTNET_ROOT": dotnet, "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "LANG": os.environ.get("LANG", "C.UTF-8"),
            "DOTNET_CLI_UI_LANGUAGE": os.environ.get("DOTNET_CLI_UI_LANGUAGE", "en-US")}


def command_for(command, cwd="."):
    """The argv and environment that run a dotnet command (not a publish) as the build user, for the caller to run.
    Not root (a developer's machine, the tests): the command as it is, and no environment of its own."""
    if os.geteuid() != 0:
        return list(command), None
    account = ensure_user()
    prepare(Path(cwd).resolve(), account, set())
    return ["runuser", "-u", USER, "--", *command], environment()


def run(command, cwd=".", capture=False, timeout=None):
    """Run a dotnet command as the build user, for the project in cwd. Returns the CompletedProcess."""
    if os.geteuid() != 0:
        # Not root (a developer's machine, the tests): nothing to give away
        return subprocess.run(command, cwd=cwd, capture_output=capture, text=capture, timeout=timeout)
    account = ensure_user()
    project = Path(cwd).resolve()
    command = list(command)
    target = None
    staging = None
    for index, argument in enumerate(command):
        if argument.startswith("-p:PublishDir="):
            value = argument.split("=", 1)[1].strip('"')
            target = (project / value).resolve()
            staging = HOME / "stage" / secrets.token_hex(8)
            staging.mkdir(mode=0o750)
            os.chown(staging, account.pw_uid, account.pw_gid)
            command[index] = "-p:PublishDir=" + str(staging) + "/"
    keep = {target} if target else set()
    prepare(project, account, keep)
    try:
        result = subprocess.run(["runuser", "-u", USER, "--", *command], cwd=project, env=environment(),
                                capture_output=capture, text=capture, timeout=timeout)
        if result.returncode == 0 and staging is not None:
            copy_out(staging, target)
        return result
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def main(args):
    if not args or args[0] != "--" or len(args) < 2:
        raise SystemExit(__doc__)
    result = run(args[1:])
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("The build could not be run as %s: %s" % (USER, exc), file=sys.stderr)
        sys.exit(1)
