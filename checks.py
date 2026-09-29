"""Stage 9 — run a task's done_when check.

Commands go through the workspace MCP server's run_command, called by the
harness (not the model), so they follow the same allow-list, no-shell and
timeout rules as everything else. A check that can't run counts as failed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from planner import _bad_path

_EXIT_RE = re.compile(r"^exit code: (-?\d+)", re.MULTILINE)
RUN_TOOL = "workspace.run_command"


@dataclass
class CheckResult:
    ok: bool
    kind: str               # "file" | "command"
    target: str
    exit_code: int | None
    output: str

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        if self.kind == "file":
            return f"file `{self.target}` " + ("exists" if self.ok else "is missing")
        code = "no exit code" if self.exit_code is None else f"exit {self.exit_code}"
        return f"`{self.target}` → {code}"


def _clip(text: str, limit: int) -> str:
    """Keep the end of long output: that's where test failures and tracebacks are."""
    return text if len(text) <= limit else f"… [{len(text) - limit} chars cut]\n" + text[-limit:]


def run_check(done_when: dict, workspace: Path, tools, *, timeout_s: int = 120,
              output_chars: int = 3000) -> CheckResult:
    if "file" in done_when:
        rel = done_when["file"]
        problem = _bad_path(rel)
        if problem:
            return CheckResult(False, "file", rel, None, f"invalid path: {problem}")
        ok = (workspace / rel).is_file()
        return CheckResult(ok, "file", rel, None, "" if ok else f"{rel} does not exist in the workspace")

    command = done_when.get("command", "")
    text = tools.call(RUN_TOOL, {"command": command, "timeout_s": timeout_s})
    m = _EXIT_RE.search(text)
    if text.startswith("Error:") or not m:
        return CheckResult(False, "command", command, None, _clip(text, output_chars))
    code = int(m.group(1))
    return CheckResult(code == 0, "command", command, code, _clip(text, output_chars))
