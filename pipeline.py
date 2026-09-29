"""Stage 7 — the build as a graph.

intake → plan → build → finish, with state checkpointed to runs/<id>/state.json
before every node, so a stopped run can resume where it stopped.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from build import execute_build, list_files, open_run, start_run
from graph import END, Graph
from loop import Step
from planner import PlanError, load_plan, make_plan, render_tasks, save_plan
from runtime import build_registry
from state import BuildState, load_state, save_state
from replies import NormalizingModel

RESUMABLE_ENDED = {"error", "interrupted"}


@dataclass
class Context:
    """Everything the nodes need that isn't state (and isn't saved)."""
    model: object
    config: dict
    prompts: dict[str, str]
    runs_dir: Path
    ask: Callable | None = None
    on_step: Callable[[Step], None] | None = None
    say: Callable[[str], None] = print
    meta: dict = field(default_factory=dict)


class RunStopped(RuntimeError):
    """A run stopped early (Ctrl-C, model or server failure) and can be resumed."""

    def __init__(self, run_dir: str | None, cause: BaseException):
        super().__init__(str(cause) or type(cause).__name__)
        self.run_dir = run_dir
        self.cause = cause


def _append_jsonl(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plan_info(plan: dict | None) -> dict | None:
    return {"title": plan["title"], "tasks": len(plan["tasks"])} if plan else None


def _files_listing(workspace: Path) -> str:
    files = list_files(workspace)
    return "\n".join(f"- {f['path']} ({f['bytes']} bytes)" for f in files) or "(empty)"


# ---------------------------------------------------------------- nodes

def build_graph(ctx: Context) -> Graph:
    cfg_build = ctx.config.get("build", {})
    cfg_plan = ctx.config.get("plan", {})
    P = ctx.prompts
    max_tasks = cfg_plan.get("max_tasks", 12)

    def loop_model():
        """Normalize other tool-call formats (e.g. Gemma's call:…{…}) into the JSON action."""
        if not ctx.config.get("replies", {}).get("normalize", True):
            return ctx.model
        return NormalizingModel(ctx.model)

    def intake(s: BuildState) -> None:
        if s.run_dir:
            return
        run = start_run(ctx.runs_dir, s.request, meta={**ctx.meta, **s.options,
                                                        "planned": not s.options.get("no_plan")})
        s.run_id, s.run_dir, s.status = run.id, str(run.dir), "new"
        ctx.say(f"[run] {run.dir}")

    def plan(s: BuildState) -> None:
        if s.options.get("no_plan"):
            s.plan, s.spec = None, None
            return
        run_dir = Path(s.run_dir)
        ctx.say("[plan] asking the planner …")
        try:
            p = make_plan(ctx.model, P, s.request, ask=ctx.ask,
                          max_attempts=cfg_plan.get("max_attempts", 3), max_tasks=max_tasks,
                          max_questions=cfg_plan.get("max_questions", 3),
                          log=lambda e: _append_jsonl(run_dir / "planner.jsonl", e))
        except PlanError as e:
            s.status, s.error = "plan_failed", str(e)
            return
        save_plan(run_dir, p, s.request)
        s.plan, s.spec, s.status = p, (run_dir / "SPEC.md").read_text(encoding="utf-8"), "planned"
        ctx.say(f"[plan] {p['title']} — {len(p['tasks'])} tasks")
        ctx.say("\n".join("       " + line for line in render_tasks(p).splitlines() if line[:1] != " "))

    def build(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        if s.plan is None:                      # --no-plan: the Stage 5 request
            first = P["build_request"].replace("{request}", s.request)
        else:
            s.plan, s.spec = load_plan(run.dir, max_tasks)      # picks up a reviewer's edits
            first = (P["build_with_plan"].replace("{request}", s.request)
                     .replace("{spec}", (s.spec or "").strip()).replace("{tasks}", render_tasks(s.plan)))
        existing = list_files(run.workspace)
        if existing:
            first += "\n\n" + P["build_resume_note"].replace("{files}", _files_listing(run.workspace))
            _append_jsonl(run.transcript, {"event": "resume", "at": _now(), "existing_files": len(existing)})
        s.status = "building"
        with contextlib.ExitStack() as stack:
            s.summary = execute_build(
                run, loop_model(), build_registry(ctx.config, run.workspace, stack),
                P["react_system"] + "\n\n" + P["build_rules"], first,
                max_iterations=s.options.get("max_iterations") or cfg_build.get("max_iterations", 40),
                format_reminder=P["format_reminder"], on_step=ctx.on_step, plan_info=_plan_info(s.plan))
        s.status, s.error = s.summary["status"], s.summary.get("error")

    def finish(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        if s.summary is None:       # ended before building (plan_failed)
            s.summary = {"id": s.run_id, "status": s.status, "error": s.error, "plan": _plan_info(s.plan)}
            run.summary_file.write_text(json.dumps(s.summary, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")

    def after_plan(s: BuildState) -> str:
        return "finish" if s.status == "plan_failed" else "build"

    g = Graph(entry="intake")
    for name, fn in [("intake", intake), ("plan", plan), ("build", build), ("finish", finish)]:
        g.node(name, fn)
    g.edge("intake", "plan")
    g.branch("plan", after_plan, {"build", "finish"})
    g.edge("build", "finish")
    g.edge("finish", END)
    return g


# ---------------------------------------------------------------- running

def _result(s: BuildState) -> dict:
    if s.next == "build" and s.status == "planned":     # paused for review
        return {"id": s.run_id, "status": "planned", "run_dir": s.run_dir, "plan": _plan_info(s.plan)}
    out = dict(s.summary or {"id": s.run_id, "status": s.status, "error": s.error})
    out["run_dir"] = s.run_dir
    return out

def run_pipeline(
    ctx: Context,
    request: str | None = None,
    *,
    resume_dir: str | Path | None = None,
    review: bool = False,
    no_plan: bool = False,
    max_iterations: int | None = None,
    on_enter: Callable[[str, BuildState], None] | None = None,
) -> dict:
    """Start a new build, or resume one from its state.json."""
    if resume_dir:
        state = load_state(resume_dir)
        if state.next:
            start = state.next
        elif state.status in RESUMABLE_ENDED:
            start = "build"
        else:
            raise PlanError(f"{state.run_dir} has already been built (status: {state.status}); "
                            "start a new run instead")
        state.options["review"] = review
        if max_iterations:
            state.options["max_iterations"] = max_iterations
        ctx.say(f"[run] {state.run_dir}  (resuming at '{start}')")
    else:
        state = BuildState(request=request, options={"review": review, "no_plan": no_plan,
                                                     "max_iterations": max_iterations})
        start = None

    graph = build_graph(ctx)
    try:
        state = graph.run(state, start=start, checkpoint=save_state, on_enter=on_enter, max_steps=20,
                          interrupt_before={"build"} if review else set())
    except KeyboardInterrupt as e:
        state.status = "interrupted"
        save_state(state)
        raise RunStopped(state.run_dir, e) from None
    except PlanError:
        raise
    except Exception as e:   # model / MCP failures: state.next still points at the failed node
        state.status = "error"
        state.error = f"{type(e).__name__}: {e}"
        save_state(state)
        raise RunStopped(state.run_dir, e) from e
    return _result(state)
