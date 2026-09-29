"""Stage 7 — the build state that flows through the graph, saved as state.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "state.json"


@dataclass
class BuildState:
    request: str
    options: dict = field(default_factory=dict)      # review, no_plan, max_iterations
    run_id: str | None = None
    run_dir: str | None = None
    plan: dict | None = None
    spec: str | None = None
    status: str = "new"
    current_task: str | None = None
    next: str | None = None
    history: list[str] = field(default_factory=list)
    final_checks: list | None = None
    verdicts: list = field(default_factory=list)     # Stage 10: every judge verdict, in order
    revisions: int = 0
    lessons_shown: list = field(default_factory=list)   # Chapter B: review lessons given to the planner
    summary: dict | None = None
    error: str | None = None
    updated_at: str | None = None


def save_state(state: BuildState) -> None:
    """Write state.json atomically. A no-op until the run folder exists."""
    if not state.run_dir:
        return
    state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = Path(state.run_dir) / STATE_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(run_dir: str | Path) -> BuildState:
    path = Path(run_dir) / STATE_FILE
    if not path.exists():
        raise FileNotFoundError(f"no {STATE_FILE} in {run_dir}")
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(BuildState)}
    return BuildState(**{k: v for k, v in data.items() if k in known})
