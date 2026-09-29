"""Stage 5 — request → first build.

A build is one run of the ReAct loop against a fresh workspace, recorded in
its own folder:

    runs/<id>/request.json      what was asked
    runs/<id>/workspace/        the project
    runs/<id>/transcript.jsonl  every step, written as it happens
    runs/<id>/summary.json      how it ended

No planning and no verification yet: the loop goes straight from request to
code, and "finished" only means the model said so.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from loop import LoopResult, Step, ToolBox, run_loop

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}


@dataclass
class Run:
    id: str
    dir: Path
    workspace: Path
    request_file: Path
    transcript: Path
    summary_file: Path


def open_run(run_dir: Path) -> Run:
    """Re-open an existing run folder (e.g. after --review)."""
    d = Path(run_dir).resolve()
    if not (d / "request.json").exists():
        raise FileNotFoundError(f"not a run folder (no request.json): {d}")
    (d / "workspace").mkdir(exist_ok=True)
    return Run(d.name, d, d / "workspace", d / "request.json", d / "transcript.jsonl", d / "summary.json")


def slugify(text: str, words: int = 6, max_len: int = 40) -> str:
    tokens = re.findall(r"[a-z0-9]+", text.lower())[:words]
    return ("-".join(tokens)[:max_len].strip("-")) or "build"


def start_run(runs_dir: Path, request: str, meta: dict | None = None, now: datetime | None = None) -> Run:
    """Create runs/<timestamp>-<slug>/ with request.json and an empty workspace/."""
    now = now or datetime.now(timezone.utc)
    base = f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify(request)}"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id, n = base, 1
    while (runs_dir / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    d = runs_dir / run_id
    (d / "workspace").mkdir(parents=True)
    run = Run(run_id, d, d / "workspace", d / "request.json", d / "transcript.jsonl", d / "summary.json")
    record = {"id": run_id, "request": request, "created_at": now.isoformat(timespec="seconds"), **(meta or {})}
    run.request_file.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return run


class CountingModel:
    """Wraps a model to count calls and approximate size (chars / 4). The adapter is unchanged."""

    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.chars_in = 0
        self.chars_out = 0

    def complete(self, system: str, messages: list[dict]) -> str:
        self.calls += 1
        self.chars_in += len(system) + sum(len(m.get("content", "")) for m in messages)
        reply = self.model.complete(system, messages)
        self.chars_out += len(reply or "")
        return reply


def list_files(root: Path) -> list[dict]:
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        files.append({"path": rel.as_posix(), "bytes": p.stat().st_size})
    return files


def execute_build(
    run: Run,
    model,
    tools: ToolBox,
    system_template: str,
    first_message: str,
    *,
    max_iterations: int,
    format_reminder: str,
    on_step: Callable[[Step], None] | None = None,
    plan_info: dict | None = None,
    label: dict | None = None,
    write_summary: bool = True,
) -> dict:
    """Run the loop for one build; always writes summary.json, even on error.

    Ctrl-C writes the summary with status "interrupted" and is then re-raised,
    so the graph (Stage 7) keeps the run resumable.
    """
    counter = CountingModel(model)
    steps: list[Step] = []

    def record(step: Step) -> None:
        steps.append(step)
        with open(run.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps({**(label or {}), **asdict(step)}, ensure_ascii=False) + "\n")
        if on_step:
            on_step(step)

    start = time.perf_counter()
    result: LoopResult | None = None
    error: str | None = None
    interrupted = False
    try:
        result = run_loop(counter, system_template, first_message, tools,
                          max_iterations=max_iterations, format_reminder=format_reminder, on_step=record)
    except KeyboardInterrupt:
        error, interrupted = "interrupted", True
    except Exception as e:  # model/transport failure: keep what we have
        error = f"{type(e).__name__}: {e}"

    if interrupted:
        status = "interrupted"
    elif error:
        status = "error"
    else:
        status = "finished" if result.status == "final" else "max_iterations"

    summary = {
        "id": run.id,
        "status": status,
        "plan": plan_info,
        "answer": result.answer if result else None,
        "error": error,
        "steps": len(steps),
        "tool_calls": dict(Counter(s.action for s in steps if s.action)),
        "malformed": sum(1 for s in steps if s.error),
        "files": list_files(run.workspace),
        "duration_s": round(time.perf_counter() - start, 2),
        "model_calls": counter.calls,
        "approx_tokens_in": counter.chars_in // 4,
        "approx_tokens_out": counter.chars_out // 4,
    }
    if write_summary:
        run.summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if interrupted:
        raise KeyboardInterrupt
    return summary
