"""An environment on a Windows server (IIS), reached over SMB with rclone 1.60 or newer, without a mount.
From lib/windows_prod.sh: windows_smb_connect, windows_take_offline, windows_bring_online and windows_set_environment."""
from pathlib import Path
import tempfile

from harbor.core.system import CommandFailed
from harbor.core.paths import LIB

OFFLINE_PAGE = LIB / "app_offline.htm"
FALLBACK_PAGE = '<!doctype html><html><head><meta charset="utf-8"><title>Updating</title></head><body><h1>Updating, back shortly.</h1></body></html>\n'


class Share:
    """The IIS site's folder, as rclone reaches it. Its settings are passed in the environment of each rclone, so
    the password is never in a process list."""

    def __init__(self, system, environment):
        self.system = system
        self.environment = environment
        share = environment.WINDOWS_SHARE
        host = share[2:] if share.startswith("//") else share
        host = host.split("/", 1)[0]
        prefix = f"//{host}/"
        self.target = ":smb:" + (share[len(prefix):] if share.startswith(prefix) else share)
        self.env = {"RCLONE_CONFIG": "/dev/null", "RCLONE_SMB_HOST": host, "RCLONE_SMB_USER": environment.SMB_USER}
        if environment.SMB_DOMAIN:
            self.env["RCLONE_SMB_DOMAIN"] = environment.SMB_DOMAIN
        self.offline = False

    def rclone(self, *args, check=True, capture=False, input=None):
        return self.system.run("rclone", *args, check=check, capture=capture, input=input, env=self.env)

    def connect(self):
        """Log in, and make sure the folder is there: rclone would create a missing one, so a typo in the share would
        otherwise become a new folder"""
        # rclone needs the password in its obscured form. This is encoding, not encryption.
        obscured = self.rclone("obscure", "-", capture=True, input=self.environment.SMB_PASSWORD, check=False)
        if obscured.returncode == 127:
            raise RuntimeError("rclone is not installed. Run harbor server init, or see https://rclone.org/install/")
        if not obscured.ok:
            raise RuntimeError("rclone could not encode the SMB password.")
        self.env["RCLONE_SMB_PASS"] = obscured.stdout.rstrip("\n")
        # Its listing is not output: a file read or a JSON answer comes on stdout
        if not self.rclone("lsf", self.target, "--max-depth", "1", check=False, capture=True).ok:
            raise RuntimeError(f"Could not open {self.environment.WINDOWS_SHARE}. Check the share, the login and that the IIS site folder exists.")
        return self

    def take_offline(self, address="", wait=None):
        """Put app_offline.htm in the folder, so IIS shuts the app down and shows the page. With the site's address,
        wait until it answers with the page (503), so the app's files are released before they are replaced."""
        page = OFFLINE_PAGE.read_text() if OFFLINE_PAGE.is_file() else FALLBACK_PAGE
        self.system.say("Take site offline")
        self.offline = True
        with tempfile.NamedTemporaryFile("w", suffix=".htm") as handle:
            handle.write(page)
            handle.flush()
            self.rclone("copyto", handle.name, f"{self.target}/app_offline.htm")
        if address:
            waited, code = 0, ""
            while waited < 60:
                code = self.system.run("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", address,
                                       check=False, capture=True).stdout.strip()
                if code == "503":
                    break
                self.system.sleep(2)
                waited += 2
            if code == "503":
                self.system.say("The site shows the update page")
            else:
                self.system.say(f"The site did not answer with the update page within 60 seconds (last answer: {code or 'none'}). Going on.")
        self.system.sleep(int(self.environment.other.get("OFFLINE_WAIT") or 5) if wait is None else wait)

    def bring_online(self):
        if not self.offline:
            return
        self.system.say("Remove app_offline.htm")
        if not self.rclone("deletefile", f"{self.target}/app_offline.htm", check=False).ok:
            raise RuntimeError(f"FAILED: could not remove app_offline.htm. The site stays offline until it is deleted from {self.environment.WINDOWS_SHARE}.")
        self.offline = False

    def set_environment(self, name, apache_dir):
        """Change the environment in web.config on the server. IIS restarts the app when the file changes."""
        with tempfile.TemporaryDirectory() as folder:
            local = f"{folder}/web.config"
            if not self.rclone("copyto", f"{self.target}/web.config", local, check=False).ok:
                raise RuntimeError(f"Could not read web.config in {self.environment.WINDOWS_SHARE}. Publish to production first.")
            try:
                self.system.run("python3", f"{apache_dir}/lib/windows_prod.py", "set-environment", local, name)
                self.rclone("copyto", local, f"{self.target}/web.config")
            except CommandFailed as error:
                raise RuntimeError(str(error)) from None
