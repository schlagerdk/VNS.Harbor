""".NET for the sites: the runtime a build targets, and the SDKs and runtimes in the folder their services use.
From lib/dotnet.sh: linux_runtime_identifier, ensure_dotnet_channel and configure_project_dotnet."""
import re
import tempfile
import xml.etree.ElementTree as ET

DOTNET_DIR = "/usr/lib/dotnet"
INSTALLER = "https://dot.net/v1/dotnet-install.sh"
GLOBAL_JSON_NET8 = '{"sdk":{"version":"8.0.100","rollForward":"latestFeature","allowPrerelease":false}}\n'


def runtime_identifier(architecture):
    if architecture == "x86_64":
        return "linux-x64"
    if architecture in ("aarch64", "arm64"):
        return "linux-arm64"
    raise ValueError(f"Unsupported Linux architecture for .NET publish: {architecture}")


def has_channel(sdks, runtimes, channel):
    escaped = re.escape(channel)
    return (re.search(rf"(?m)^{escaped}\.[0-9]+ \[", sdks) is not None
            and re.search(rf"(?m)^Microsoft\.AspNetCore\.App {escaped}\.[0-9]+ \[", runtimes) is not None)


def ensure_channel(system, channel):
    """The SDK and ASP.NET Core runtime of a channel, such as 8.0, installed when missing. An older channel never
    replaces a newer dotnet host."""
    dotnet = f"{DOTNET_DIR}/dotnet"
    sdks = runtimes = ""
    if system.path(dotnet).is_file():
        sdks = system.output(dotnet, "--list-sdks")
        runtimes = system.output(dotnet, "--list-runtimes")
    if has_channel(sdks, runtimes, channel):
        return
    system.say(f"Install .NET {channel} SDK and ASP.NET Core runtime")
    with tempfile.NamedTemporaryFile(suffix=".sh") as installer:
        system.run("curl", "-fsSL", INSTALLER, "-o", installer.name)
        system.path(DOTNET_DIR).mkdir(parents=True, exist_ok=True)
        majors = [int(match) for match in re.findall(r"(?m)^([0-9]+)\.", sdks)]
        extra = []
        if system.path(dotnet).is_file() and majors and max(majors) > int(channel.split(".")[0]):
            extra = ["--skip-non-versioned-files"]
        system.run("bash", installer.name, "--channel", channel, "--install-dir", DOTNET_DIR, *extra)
        system.run("bash", installer.name, "--channel", channel, "--runtime", "aspnetcore", "--install-dir", DOTNET_DIR, *extra)


def target_framework(project_text):
    """The project's TargetFramework, or nothing when it has none or several"""
    frameworks = [(node.text or "").strip() for node in ET.fromstring(project_text).iter() if node.tag.rsplit("}", 1)[-1] == "TargetFramework"]
    return frameworks[0] if len(frameworks) == 1 else ""


def configure_project(system, project):
    """A project for another .NET than the default gets its SDK and runtime, e.g. net8.0 for Umbraco 13. A net8.0
    project gets a global.json, so it builds with an 8.0 SDK."""
    path = system.path(project)
    framework = target_framework(path.read_text())
    match = re.fullmatch(r"net([0-9]+)\.0", framework)
    if match and int(match[1]) >= 8:
        ensure_channel(system, f"{match[1]}.0")
    if framework == "net8.0":
        (path.parent / "global.json").write_text(GLOBAL_JSON_NET8)
