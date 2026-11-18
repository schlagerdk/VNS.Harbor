"""The server a command works on: its files and the programs it runs.

Every command reaches the server through a System, never through open() on an absolute path or subprocess
directly. A test gives one with a temporary folder as the root and a recorder for the programs, so a command is
tested with the files it writes and the programs it runs, and the server it runs on is never touched.
"""
from dataclasses import dataclass, field
import json
import inspect
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

    def ask(self, prompt, secret=False):
        """A question in the terminal, as read -r -p. Nothing when there is nobody to answer. A secret is not shown
        (read -s), and stops the command when nobody answers, as the scripts did."""
        try:
            if secret:
                import getpass
                return getpass.getpass(prompt)
            return input(prompt)
        except EOFError:
            if secret:
                print()
                raise Stop("", 1) from None
            return ""

    def interactive(self):
        """Whether someone is at the terminal to answer a question"""
        return sys.stdin.isatty()

    def event(self, kind, text):
        if progress_wanted():
            sys.stdout.write(PROGRESS.format(json.dumps({"type": kind, "text": text})))
            sys.stdout.flush()

    def plan(self, steps):
        """The steps the command will take, before it takes the first: the admin's window shows those still to come.
        Each step() then is the next of them; a step that is not in the plan is shown where it comes."""
        if progress_wanted():
            sys.stdout.write(PROGRESS.format(json.dumps({"type": "plan", "steps": list(steps)})))
            sys.stdout.flush()

    def step(self, text):
        """A step of the command: a line of output, and a step in the admin's window"""
        self.event("step", text)
        self.say(text)

    def skip(self, text):
        """A planned step that turned out not to be needed (a site that is gone, say): shown as not taken"""
        self.event("skip", text)

    def finished(self):
        """The command has ended well"""

    def say(self, text):
        print(text, flush=True)

    def warn(self, text):
        self.event("warning", text)
        print(text, file=sys.stderr, flush=True)


class PlanMismatch(AssertionError):
    """In a test: a command's steps are not those it planned, so the admin's window would show them wrong"""


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
    pending: list = field(default_factory=list)
    planned: bool = False
    warned: bool = False

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
            # An answer that takes two arguments gets what came on stdin too, one that takes three also the variables
            given = len(inspect.signature(answer).parameters)
            answer = answer(argv, input, dict(env or {})) if given == 3 else answer(argv, input) if given == 2 else answer(argv)
        result = answer if isinstance(answer, Result) else Result(0, answer + ("\n" if answer else ""), "")
        if check and not result.ok:
            raise CommandFailed(argv, result)
        return result

    def sleep(self, seconds):
        self.slept.append(seconds)

    def event(self, kind, text):
        self.events.append((kind, text))

    def plan(self, steps):
        """As the admin's window: a plan made while another runs (a command within a command) comes before the rest of it"""
        self.events.append(("plan", list(steps)))
        self.planned = True
        self.pending[:0] = list(steps)

    def step(self, text):
        """Once a command has a plan, each step is the next of it"""
        if not self.planned:
            raise PlanMismatch(f"Step {text!r} comes without a plan: the admin's window cannot show it from the start")
        if self.planned:
            # After a warning (one of several sites failed, say) the steps before this one were not taken, as the window shows
            if self.warned and text in self.pending:
                del self.pending[:self.pending.index(text)]
            if not self.pending or self.pending[0] != text:
                raise PlanMismatch(f"Step {text!r} is not the next of the plan, which has {self.pending!r} still to come")
            self.pending.pop(0)
            self.warned = False
        super().step(text)

    def skip(self, text):
        if self.planned:
            if not self.pending or self.pending[0] != text:
                raise PlanMismatch(f"Skipped {text!r} is not the next of the plan, which has {self.pending!r} still to come")
            self.pending.pop(0)
        self.events.append(("skip", text))

    def finished(self):
        if self.pending:
            raise PlanMismatch(f"The command ended well without the steps it planned: {self.pending!r}")

    def which(self, program):
        return program in self.programs

    def interactive(self):
        return bool(self.replies)

    def ask(self, prompt, secret=False):
        self.asked.append(prompt)
        if not self.replies and secret:
            raise Stop("", 1)
        return self.replies.pop(0) if self.replies else ""

    def say(self, text):
        self.said.append(text)

    def warn(self, text):
        self.warned = True
        self.said.append(text)
