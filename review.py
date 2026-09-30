"""Chapter J — reviewing and fixing an existing project.

The helpers around the graph's `survey` and `review` nodes:

    import_project()   copy the project into the run (workspace/ to change, original/ to diff against)
    detect_tests()     guess the command that runs the project's own tests
    ReadOnlyTools      the reviewer may read and run, but not write
    parse_review()     the reviewer's final answer → findings
    render_review()    REVIEW.md
    make_patch()       CHANGES.patch: a git-style unified diff from original/ to workspace/

The user's folder is only ever read. Applying the patch is the user's decision.
"""

from __future__ import annotations

import difflib
import fnmatch
import importlib.util
import json
import shutil
from pathlib import Path

from loop import ParseError, _first_json_object
from tools.registry import _signature

DEFAULT_IGNORE = [".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
                  ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode", "*.pyc", "*.pyo",
                  "*.egg-info", ".DS_Store", "runs"]
SEVERITIES = ("high", "medium", "low")
WRITE_TOOLS = ("write_file", "edit_file", "delete_file", "move_file", "create_directory")


class ReviewError(RuntimeError):
    """The project can't be imported (missing, too big, …)."""


# ---------------------------------------------------------------- import

def _ignored(rel: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in patterns)


def import_project(src: str | Path, run_dir: Path, *, ignore: list[str] | None = None,
                   max_files: int = 2000, max_bytes: int = 20_000_000) -> dict:
    """Copy `src` into run_dir/workspace (to work on) and run_dir/original (to diff against)."""
    src = Path(src).expanduser().resolve()
    if not src.is_dir():
        raise ReviewError(f"not a folder: {src}")
    patterns = DEFAULT_IGNORE + list(ignore or [])
    files, total = [], 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if _ignored(rel, patterns) or not p.is_file() or p.is_symlink():
            continue
        files.append(rel)
        total += p.stat().st_size
        if len(files) > max_files:
            raise ReviewError(f"{src} has more than {max_files} files (after ignoring {', '.join(DEFAULT_IGNORE[:6])}, …). "
                              "Point the review at a smaller folder, or raise review.max_files")
        if total > max_bytes:
            raise ReviewError(f"{src} is larger than {max_bytes:,} bytes. Point the review at a smaller folder, "
                              "or raise review.max_bytes")
    if not files:
        raise ReviewError(f"no files to review in {src}")
    for target in ("workspace", "original"):
        root = run_dir / target
        for rel in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, root / rel)
    return {"source": str(src), "files": len(files), "bytes": total}


# ---------------------------------------------------------------- baseline tests

def detect_tests(workspace: Path) -> str | None:
    """The command that most likely runs the project's own tests, or None."""
    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            if (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}).get("test"):
                return "npm test"
        except (ValueError, OSError):
            pass
    tests = [p for p in workspace.rglob("*.py")
             if (p.name.startswith("test_") or p.name.endswith("_test.py"))
             and not _ignored(p.relative_to(workspace), DEFAULT_IGNORE)]
    if not tests:
        return None
    uses_pytest = any("pytest" in p.read_text(encoding="utf-8", errors="replace") for p in tests) \
        or any((workspace / f).is_file() for f in ("pytest.ini", "conftest.py"))
    if uses_pytest or importlib.util.find_spec("pytest") is not None:
        return "python -m pytest -q"
    return "python -m unittest discover -q"


# ---------------------------------------------------------------- the reviewer's tools

class ReadOnlyTools:
    """Wraps a tool box so the reviewer can read files and run commands, but not change files."""

    def __init__(self, tools):
        self._tools = tools

    def __getattr__(self, name):
        return getattr(self._tools, name)

    @staticmethod
    def _writes(name: str) -> bool:
        return name.rsplit(".", 1)[-1] in WRITE_TOOLS

    def list_tools(self):
        return [s for s in self._tools.list_tools() if not self._writes(s.name)]

    def describe(self) -> str:
        specs = self.list_tools()
        return "\n".join(f"- {_signature(s)} — {s.description}" for s in specs) or "(none)"

    def call(self, name: str, args: dict) -> str:
        resolved = getattr(self._tools, "resolve", lambda n: n)(name) or name
        if self._writes(resolved):
            return (f"Error: {name} is not available while reviewing: this step only reads and runs the code. "
                    "Describe the change in your findings instead.")
        return self._tools.call(name, args)


# ---------------------------------------------------------------- findings

def parse_review(answer: str | None) -> dict:
    """{"summary", "findings": [...]} from the reviewer's final answer; plain text becomes the summary."""
    text = answer or ""
    try:
        obj = _first_json_object(text, want=("findings",))
    except ParseError:
        obj = {}
    raw = obj.get("findings") if isinstance(obj.get("findings"), list) else []
    findings = []
    for i, f in enumerate(raw, 1):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "medium")).lower()
        line = f.get("line")
        findings.append({
            "id": str(f.get("id") or f"F{i}"),
            "file": str(f.get("file") or ""),
            "line": line if isinstance(line, int) else None,
            "severity": sev if sev in SEVERITIES else "medium",
            "problem": str(f.get("problem") or f.get("issue") or "").strip(),
            "evidence": str(f.get("evidence") or "").strip(),
            "fix": str(f.get("fix") or f.get("suggestion") or "").strip(),
        })
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    summary = str(obj.get("summary") or "").strip() if obj else text.strip()
    return {"summary": summary, "findings": [f for f in findings if f["problem"]], "structured": bool(obj)}


def _where(f: dict) -> str:
    return f"{f['file']}:{f['line']}" if f["file"] and f["line"] else (f["file"] or "—")


def render_findings(review: dict) -> str:
    """Compact list for prompts (planner, judge)."""
    if not review.get("findings"):
        return "(no findings)"
    return "\n".join(f"- {f['id']} [{f['severity']}] {_where(f)}: {f['problem']}"
                     + (f" Evidence: {f['evidence']}" if f["evidence"] else "")
                     + (f" Suggested fix: {f['fix']}" if f["fix"] else "")
                     for f in review["findings"])


def render_baseline(baseline: dict | None) -> str:
    if not baseline or not baseline.get("command"):
        return "No tests were found in the project."
    code = baseline.get("exit_code")
    state = "PASSED" if baseline.get("ok") else ("could not run" if code is None else f"FAILED (exit {code})")
    out = (baseline.get("output") or "").strip()
    return f"`{baseline['command']}` {state}" + (f"\nOutput (end):\n{out}" if out else "")


def render_review(*, request: str, source: str, baseline: dict | None, review: dict) -> str:
    L = ["# Review", "", f"- **Project:** `{source}`", f"- **Asked:** {request}", "",
         "## Tests before any change", "", render_baseline(baseline), ""]
    if review.get("summary"):
        L += ["## Summary", "", review["summary"], ""]
    L += ["## Findings", ""]
    if review.get("findings"):
        L += ["| ID | Severity | Where | Problem | Evidence | Suggested fix |", "|---|---|---|---|---|---|"]
        for f in review["findings"]:
            cells = [f["id"], f["severity"], f"`{_where(f)}`", f["problem"], f["evidence"] or "—", f["fix"] or "—"]
            L.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    else:
        L.append("(none)" if review.get("structured") else "(the reviewer's answer had no structured findings; see the summary)")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- the patch

def _files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*"))
            if p.is_file() and not _ignored(p.relative_to(root), DEFAULT_IGNORE)}


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def _hunks(old: list[str], new: list[str], a: str, b: str) -> list[str]:
    out = []
    for line in difflib.unified_diff(old, new, a, b, n=3):
        if not line.endswith("\n"):                      # a last line without a newline
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return out


def make_patch(original: Path, workspace: Path) -> tuple[str, dict]:
    """A git-style unified diff (applies with `git apply` or `patch -p1`) and a {changed, added, deleted} summary."""
    before, after = _files(original), _files(workspace)
    parts: list[str] = []
    stats = {"changed": [], "added": [], "deleted": []}
    for rel in sorted(set(before) | set(after)):
        old = before[rel].read_bytes() if rel in before else None
        new = after[rel].read_bytes() if rel in after else None
        if old == new:
            continue
        kind = "added" if old is None else "deleted" if new is None else "changed"
        stats[kind].append(rel)
        head = [f"diff --git a/{rel} b/{rel}\n"]
        if kind == "added":
            head.append("new file mode 100644\n")
        elif kind == "deleted":
            head.append("deleted file mode 100644\n")
        if b"\0" in (old or b"") or b"\0" in (new or b""):
            parts += head + [f"Binary files a/{rel} and b/{rel} differ\n"]
            continue
        a = "/dev/null" if old is None else f"a/{rel}"
        b = "/dev/null" if new is None else f"b/{rel}"
        parts += head + _hunks(_lines(old or b""), _lines(new or b""), a, b)
    return "".join(parts), stats
