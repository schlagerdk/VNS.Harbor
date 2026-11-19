"""Whether a task's output tells of a problem: an error, a failure or a warning. The admin's task window then says it
finished with warnings, and Multi action marks the step of the site it came from (lib/bulk_tasks.py)."""

import re


def output_problem(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(
        r"^(?:\[[^\]\r\n]+\] )?(?:"
        r"Checking if there is an error in the Apache config file before starting\."
        r"|In case an error causes it to not be restored automatically, you can restore it yourself\."
        # dotnet's own notes, not problems: a package added without its restore (Harbor restores next), and a build that
        # runs without the MSBuild server ("the server may have failed to start")
        r"|(?:warn|info) : --no-restore\|-n flag was used\..*"
        r"|MSBuild server unavailable: .*Falling back to an in-process build\."
        r")\r?$", "", text, flags=re.M)
    text = re.sub(r"\b(?:0|no)\s+(?:errors?|warnings?|failures?)(?:\(s\))?\b", "", text, flags=re.I)
    return bool(re.search(r"\b(?:errors?|failed|failure|fatal|\w*exception|traceback|warnings?)\b(?![/_-]|\.[A-Za-z0-9])", text, re.I))
