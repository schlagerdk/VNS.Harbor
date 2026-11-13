"""The server a command works on: its files and the programs it runs.

Every command reaches the server through a System, never through open() on an absolute path or subprocess
directly. A test gives one with a temporary folder as the root and a recorder for the programs, so a command is
tested with the files it writes and the programs it runs, and the server it runs on is never touched.
"""
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self):
        return self.returncode == 0


class CommandFailed(Exception):
    def __init__(self, argv, result):
        self.argv = list(argv)
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"{' '.join(self.argv)} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))


class System:
    """This server. root is where / is, which is / except in tests."""

    def __init__(self, root="/"):
        self.root = Path(root)

    def path(self, absolute):
        """A path on the server, such as /etc/apache2, under the root"""
        absolute = str(absolute)
        if not absolute.startswith("/"):
            raise ValueError(f"Not an absolute path: {absolute}")
        return self.root / absolute.lstrip("/")

    def run(self, *argv, check=True, capture=False, input=None):
        """Run a program. Without capture its output goes to the terminal as it comes, as a script's would."""
        argv = [str(item) for item in argv]
        try:
            completed = subprocess.run(argv, input=input, text=True, capture_output=capture)
        except FileNotFoundError:
            result = Result(127, "", f"{argv[0]}: command not found")
        else:
            result = Result(completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and not result.ok:
            raise CommandFailed(argv, result)
        return result

    def output(self, *argv, check=True):
        """What a program prints, without its trailing line break"""
        return self.run(*argv, check=check, capture=True).stdout.rstrip("\n")

    def say(self, text):
        print(text, flush=True)

    def warn(self, text):
        print(text, file=sys.stderr, flush=True)


@dataclass
class RecordingSystem(System):
    """A System for tests: files under a temporary root, and programs recorded instead of run.
    answers maps the start of a command line to what it prints and returns, such as {"hostname -f": "web.local"}."""
    root: Path = Path(".")
    answers: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)
    said: list = field(default_factory=list)

    def __post_init__(self):
        self.root = Path(self.root)

    def run(self, *argv, check=True, capture=False, input=None):
        argv = [str(item) for item in argv]
        line = " ".join(argv)
        self.commands.append(line)
        answer = next((value for start, value in self.answers.items() if line == start or line.startswith(start + " ")), "")
        result = answer if isinstance(answer, Result) else Result(0, answer + ("\n" if answer else ""), "")
        if check and not result.ok:
            raise CommandFailed(argv, result)
        return result

    def say(self, text):
        self.said.append(text)

    def warn(self, text):
        self.said.append(text)
