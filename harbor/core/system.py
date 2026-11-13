"""The server a command works on: its files and the programs it runs.

Every command reaches the server through a System, never through open() on an absolute path or subprocess
directly. A test gives one with a temporary folder as the root and a recorder for the programs, so a command is
tested with the files it writes and the programs it runs, and the server it runs on is never touched.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self):
        return self.returncode == 0


class Stop(Exception):
    """A command stops with a message and an exit code, as a script's echo >&2; exit"""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class CommandFailed(Exception):
    def __init__(self, argv, result):
        self.argv = list(argv)
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"{' '.join(self.argv)} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))


# The admin runs a command with HARBOR_PROGRESS=1 and shows its steps in a window of their own. They come as an OSC
# sequence, which a terminal does not show, so the command's output reads the same in a terminal.
PROGRESS = "\x1b]7777;harbor;{}\x07"


def progress_wanted():
    return os.environ.get("HARBOR_PROGRESS") == "1"


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

    def run(self, *argv, check=True, capture=False, input=None, env=None, cwd=None):
        """Run a program. Without capture its output goes to the terminal as it comes, as a script's would.
        env adds variables, such as a password a program reads from its environment rather than its arguments.
        cwd is a folder on the server to run it in."""
        argv = [str(item) for item in argv]
        try:
            completed = subprocess.run(argv, input=input, text=True, capture_output=capture,
                                       env=os.environ | env if env else None, cwd=self.path(cwd) if cwd else None)
        except FileNotFoundError:
            result = Result(127, "", f"{argv[0]}: command not found")
        else:
            result = Result(completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and not result.ok:
            raise CommandFailed(argv, result)
        return result

    def output(self, *argv, check=True, env=None):
        """What a program prints, without its trailing line break"""
        return self.run(*argv, check=check, capture=True, env=env).stdout.rstrip("\n")

    def sleep(self, seconds):
        time.sleep(seconds)

    def which(self, program):
        """Whether a program is installed, as command -v"""
        return shutil.which(program) is not None

    def ask(self, prompt):
        """A question in the terminal, as read -r -p. Nothing when there is nobody to answer."""
        try:
            return input(prompt)
        except EOFError:
            return ""

    def event(self, kind, text):
        if progress_wanted():
            sys.stdout.write(PROGRESS.format(json.dumps({"type": kind, "text": text})))
            sys.stdout.flush()

    def step(self, text):
        """A step of the command: a line of output, and a step in the admin's window"""
        self.event("step", text)
        self.say(text)

    def say(self, text):
        print(text, flush=True)

    def warn(self, text):
        self.event("warning", text)
        print(text, file=sys.stderr, flush=True)


@dataclass
class RecordingSystem(System):
    """A System for tests: files under a temporary root, and programs recorded instead of run.
    answers maps the start of a command line to what it prints and returns, such as {"hostname -f": "web.local"}."""
    root: Path = Path(".")
    answers: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)
    environments: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    said: list = field(default_factory=list)
    slept: list = field(default_factory=list)
    events: list = field(default_factory=list)
    programs: set = field(default_factory=set)
    replies: list = field(default_factory=list)
    asked: list = field(default_factory=list)

    def __post_init__(self):
        self.root = Path(self.root)

    def run(self, *argv, check=True, capture=False, input=None, env=None, cwd=None):
        argv = [str(item) for item in argv]
        line = " ".join(argv)
        self.commands.append(line)
        self.environments.append(dict(env or {}))
        self.inputs.append(input)
        # The longest start that fits answers, so "hostname -f" is told from "hostname"
        starts = sorted((start for start in self.answers if line == start or line.startswith(start + " ")), key=len, reverse=True)
        answer = self.answers[starts[0]] if starts else ""
        if callable(answer):
            answer = answer(argv)
        result = answer if isinstance(answer, Result) else Result(0, answer + ("\n" if answer else ""), "")
        if check and not result.ok:
            raise CommandFailed(argv, result)
        return result

    def sleep(self, seconds):
        self.slept.append(seconds)

    def event(self, kind, text):
        self.events.append((kind, text))

    def which(self, program):
        return program in self.programs

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.replies.pop(0) if self.replies else ""

    def say(self, text):
        self.said.append(text)

    def warn(self, text):
        self.said.append(text)
