"""The build as a graph.

Stage 7:  intake → plan → build → finish, with state checkpointed to
          runs/<id>/state.json before every node, so a stopped run can resume.
Stage 8:  with a plan, `build` is replaced by a task loop:
          plan → next_task ⇄ run_task → finish. Each task runs in its own
          ReAct loop; task status and hand-off notes live in plan.json.
Stage 9:  run_task → verify: the harness runs each task's done_when itself;
          a failed check sends the task back to run_task in fix mode.
          final_check re-runs every check before finish.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from build import execute_build, list_files, open_run, start_run
from checks import run_check
from graph import END, Graph
from loop import Step
from planner import PlanError, load_plan, make_plan, render_tasks, save_plan
from runtime import build_registry
from state import BuildState, load_state, save_state
from replies import NormalizingModel

RESUMABLE_ENDED = {"error", "interrupted"}
FINISHED_TASK = {"done", "failed", "blocked"}
STATUS_MARK = {"done": "[x]", "in_progress": "[>]", "pending": "[ ]", "failed": "[!]", "blocked": "[-]"}
SUM_FIELDS = ("steps", "malformed", "model_calls", "approx_tokens_in", "approx_tokens_out")


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
    stack: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)
    _registry: object = None

    def registry(self, workspace: Path):
        """One set of MCP servers per run, shared by every task."""
        if self._registry is None:
            self._registry = build_registry(self.config, workspace, self.stack)
        return self._registry

    def close(self) -> None:
        self.stack.close()
        self._registry = None


class RunStopped(RuntimeError):
    """A run stopped early (Ctrl-C, model or server failure) and can be resumed."""

    def __init__(self, run_dir: str | None, cause: BaseException):
        super().__init__(str(cause) or type(cause).__name__)
        self.run_dir = run_dir
        self.cause = cause


class TaskError(RuntimeError):
    """The model or a server failed while a task was running (not the task's fault)."""


def _append_jsonl(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plan_info(plan: dict | None) -> dict | None:
    return {"title": plan["title"], "tasks": len(plan["tasks"])} if plan else None


def _save_plan_json(run_dir: Path, plan: dict) -> None:
    """Task status changes go to plan.json only; SPEC.md may have human edits."""
    tmp = run_dir / "plan.json.tmp"
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(run_dir / "plan.json")


def _done_when(dw: dict) -> str:
    return f"`{dw['command']}` succeeds" if "command" in dw else f"`{dw['file']}` exists"


def _describe_check(c: dict) -> str:
    if c["kind"] == "file":
        return f"file `{c['target']}` " + ("exists" if c["ok"] else "is missing")
    return f"`{c['target']}` → " + ("no exit code" if c["exit_code"] is None else f"exit {c['exit_code']}")


def _files_listing(workspace: Path) -> str:
    files = list_files(workspace)
    return "\n".join(f"- {f['path']} ({f['bytes']} bytes)" for f in files) or "(empty)"


def plan_status(plan: dict, current: str | None = None) -> str:
    lines = []
    for t in plan["tasks"]:
        mark = "[>]" if t["id"] == current else STATUS_MARK.get(t["status"], "[ ]")
        extra = f" — {t['error']}" if t["status"] in ("failed", "blocked") and t.get("error") else ""
        lines.append(f"{mark} {t['id']} {t['title']}{extra}")
    return "\n".join(lines)


def handoffs(plan: dict) -> str:
    notes = [f"{t['id']} ({t['title']}): {t['handoff']}" for t in plan["tasks"]
             if t["status"] == "done" and t.get("handoff")]
    return "\n\n".join(notes) or "(none yet — this is the first task)"


def aggregate(run, plan: dict, status: str) -> dict:
    tools: Counter = Counter()
    totals = {k: 0 for k in SUM_FIELDS}
    duration = 0.0
    for t in plan["tasks"]:
        tools.update(t.get("tool_calls") or {})
        for k in SUM_FIELDS:
            totals[k] += t.get(k, 0) or 0
        duration += t.get("duration_s", 0) or 0
    answer = "\n".join(f"{t['id']} {t['title']}: {t['handoff']}" for t in plan["tasks"] if t.get("handoff"))
    failed = [t for t in plan["tasks"] if t["status"] in ("failed", "blocked")]
    return {
        "id": run.id,
        "status": status,
        "plan": _plan_info(plan),
        "answer": answer or None,
        "error": "; ".join(f"{t['id']} {t['status']}: {t.get('error', '')}" for t in failed) or None,
        "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"],
                   "steps": t.get("steps", 0), "error": t.get("error")} for t in plan["tasks"]],
        **{k: totals[k] for k in ("steps", "malformed")},
        "tool_calls": dict(tools),
        "files": list_files(run.workspace),
        "duration_s": round(duration, 2),
        **{k: totals[k] for k in ("model_calls", "approx_tokens_in", "approx_tokens_out")},
    }


# ---------------------------------------------------------------- nodes

def build_graph(ctx: Context) -> Graph:
    cfg_build = ctx.config.get("build", {})
    cfg_plan = ctx.config.get("plan", {})
    cfg_verify = ctx.config.get("verify", {})
    P = ctx.prompts
    max_tasks = cfg_plan.get("max_tasks", 12)
    load_limit = max_tasks
    max_fix = cfg_verify.get("max_fix_attempts", 2)

    def loop_model():
        """Per loop: normalize other tool-call formats (e.g. Gemma's call:…{…}) into the JSON action."""
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

    # --- Stage 5–7 path, still used with --no-plan
    def build(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        first = P["build_request"].replace("{request}", s.request)
        existing = list_files(run.workspace)
        if existing:
            first += "\n\n" + P["build_resume_note"].replace("{files}", _files_listing(run.workspace))
            _append_jsonl(run.transcript, {"event": "resume", "at": _now(), "existing_files": len(existing)})
        s.status = "building"
        s.summary = execute_build(
            run, loop_model(), ctx.registry(run.workspace), P["react_system"] + "\n\n" + P["build_rules"], first,
            max_iterations=s.options.get("max_iterations") or cfg_build.get("max_iterations", 40),
            format_reminder=P["format_reminder"], on_step=ctx.on_step, plan_info=None)
        s.status, s.error = s.summary["status"], s.summary.get("error")

    # --- Stage 8: the task loop
    def next_task(s: BuildState) -> None:
        run_dir = Path(s.run_dir)
        s.plan, s.spec = load_plan(run_dir, load_limit)       # picks up edits made between runs
        status = {t["id"]: t["status"] for t in s.plan["tasks"]}
        s.current_task = None
        for t in s.plan["tasks"]:
            if t["status"] in FINISHED_TASK:
                continue
            bad = [d for d in t["depends_on"] if status.get(d) in ("failed", "blocked")]
            if bad:
                t["status"], t["error"] = "blocked", f"blocked by {', '.join(bad)}"
                status[t["id"]] = "blocked"
                continue
            if t["status"] == "in_progress" or all(status.get(d) == "done" for d in t["depends_on"]):
                s.current_task = t["id"]
                break
        _save_plan_json(run_dir, s.plan)
        if s.current_task:
            n = [t["id"] for t in s.plan["tasks"]].index(s.current_task) + 1
            t = next(t for t in s.plan["tasks"] if t["id"] == s.current_task)
            ctx.say(f"[task] {t['id']} {t['title']}  ({n}/{len(s.plan['tasks'])})")

    def run_task(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        task = next(t for t in s.plan["tasks"] if t["id"] == s.current_task)
        resuming = bool(task.get("attempt_open"))          # a previous attempt never returned
        fixing = bool(task.get("fix_pending"))
        task["status"], task["attempt_open"] = "in_progress", True
        _save_plan_json(run.dir, s.plan)
        s.status = "building"

        # ---- the first message: the task, plus fix details or a resume note when needed
        files_text = _files_listing(run.workspace)
        fix_text = resume_text = ""
        last = task["checks"][-1] if fixing else None
        if fixing:
            fix_text = (P["fix_request"]
                        .replace("{handoff}", task.get("handoff") or "(none)")
                        .replace("{check}", _describe_check(last))
                        .replace("{output}", last["output"] or "(no output)")
                        .replace("{n}", str(task.get("fix_attempts", 1)))
                        .replace("{max}", str(max_fix)))
        if resuming:
            resume_text = P["build_resume_note"].replace("{files}", _files_listing(run.workspace))
            _append_jsonl(run.transcript, {"event": "resume", "task": task["id"], "at": _now()})

        first = (P["task_request"]
                 .replace("{request}", s.request).replace("{spec}", (s.spec or "").strip())
                 .replace("{plan_status}", plan_status(s.plan, task["id"])).replace("{handoffs}", handoffs(s.plan))
                 .replace("{files}", files_text)
                 .replace("{task_id}", task["id"]).replace("{task_title}", task["title"])
                 .replace("{task_description}", task["description"] or "(no description)")
                 .replace("{task_files}", ", ".join(task["files"]) or "(not specified)")
                 .replace("{task_done_when}", _done_when(task["done_when"])))
        for extra in (fix_text, resume_text):
            if extra:
                first += "\n\n" + extra

        result = execute_build(
            run, loop_model(), ctx.registry(run.workspace),
            P["react_system"] + "\n\n" + P["task_rules"], first,
            max_iterations=cfg_build.get("task_max_iterations", 20),
            format_reminder=P["format_reminder"], on_step=ctx.on_step,
            label={"task": task["id"], "fix": task.get("fix_attempts", 0)} if fixing else {"task": task["id"]},
            write_summary=False)

        for k in SUM_FIELDS:
            task[k] = (task.get(k) or 0) + result[k]
        task["duration_s"] = round((task.get("duration_s") or 0) + result["duration_s"], 2)
        task["tool_calls"] = dict(Counter(task.get("tool_calls") or {}) + Counter(result["tool_calls"]))

        if result["status"] == "error":            # model/server trouble: stop, keep the task in progress
            _save_plan_json(run.dir, s.plan)
            raise TaskError(f"task {task['id']}: {result['error']}")
        task["attempt_open"] = False
        if result["status"] == "finished":
            task["handoff"] = result["answer"] or ""      # still in_progress: verify decides
            ctx.say(f"[task] {task['id']} claims done → checking")
        else:
            task["status"] = "failed"
            task["error"] = f"ran out of steps ({cfg_build.get('task_max_iterations', 20)})"
            task.pop("fix_pending", None)
            ctx.say(f"[task] {task['id']} → failed ({task['error']})")
        _save_plan_json(run.dir, s.plan)

    def verify(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        task = next(t for t in s.plan["tasks"] if t["id"] == s.current_task)
        if task["status"] != "in_progress":          # ran out of steps: nothing to check
            return
        res = run_check(task["done_when"], run.workspace, ctx.registry(run.workspace),
                        timeout_s=cfg_verify.get("timeout_s", 120),
                        output_chars=cfg_verify.get("output_chars", 3000))
        checks = task.setdefault("checks", [])
        checks.append({"attempt": len(checks) + 1, **res.to_dict()})
        _append_jsonl(run.transcript, {"event": "check", "task": task["id"], "ok": res.ok,
                                       "target": res.target, "exit_code": res.exit_code})
        if res.ok:
            task["status"], task["verified"] = "done", True
            task.pop("error", None)
            task.pop("fix_pending", None)
            ctx.say(f"[check] {task['id']} ✓ {res.describe()}")
        elif task.get("fix_attempts", 0) < max_fix:
            task["fix_attempts"] = task.get("fix_attempts", 0) + 1
            task["fix_pending"] = True
            ctx.say(f"[check] {task['id']} ✗ {res.describe()} → fix attempt {task['fix_attempts']}/{max_fix}")
        else:
            task["status"], task["verified"] = "failed", False
            task["error"] = f"check failed after {task.get('fix_attempts', 0)} fix attempts: {res.describe()}"
            task.pop("fix_pending", None)
            ctx.say(f"[check] {task['id']} ✗ {res.describe()} → failed")
        _save_plan_json(run.dir, s.plan)

    def final_check(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        s.plan, _ = load_plan(run.dir, load_limit)
        seen: dict[str, dict] = {}
        results = []
        for task in s.plan["tasks"]:
            if task["status"] != "done":
                continue
            key = json.dumps(task["done_when"], sort_keys=True)
            if key not in seen:
                res = run_check(task["done_when"], run.workspace, ctx.registry(run.workspace),
                                timeout_s=cfg_verify.get("timeout_s", 120),
                                output_chars=cfg_verify.get("output_chars", 3000))
                seen[key] = res.to_dict()
                ctx.say(f"[final] {'✓' if res.ok else '✗'} {res.describe()}")
            results.append({"task": task["id"], **seen[key]})
        s.final_checks = results
        _append_jsonl(run.transcript, {"event": "final_checks", "ok": all(r["ok"] for r in results),
                                       "count": len(seen)})

    def finish(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        if s.plan is not None and s.status != "plan_failed":
            s.plan, _ = load_plan(run.dir, load_limit)
            all_done = all(t["status"] == "done" for t in s.plan["tasks"])
            regressions = [c for c in (s.final_checks or []) if not c["ok"]]
            s.status = "finished" if all_done and not regressions else "partial"
            s.summary = aggregate(run, s.plan, s.status)
            s.summary["verified"] = True
            s.summary["final_checks"] = s.final_checks or []
            if regressions:
                note = "; ".join(f"regression in {c['task']}: {_describe_check(c)}" for c in regressions)
                s.summary["error"] = "; ".join(x for x in [s.summary["error"], note] if x)
            s.error = s.summary["error"]
        elif s.summary is None:     # ended before building (plan_failed)
            s.summary = {"id": s.run_id, "status": s.status, "error": s.error, "plan": _plan_info(s.plan)}
        run.summary_file.write_text(json.dumps(s.summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def after_plan(s: BuildState) -> str:
        if s.status == "plan_failed":
            return "finish"
        return "build" if s.plan is None else "next_task"

    def after_verify(s: BuildState) -> str:
        task = next(t for t in s.plan["tasks"] if t["id"] == s.current_task)
        return "run_task" if task.get("fix_pending") else "next_task"

    g = Graph(entry="intake")
    for name, fn in [("intake", intake), ("plan", plan), ("build", build),
                     ("next_task", next_task), ("run_task", run_task), ("verify", verify),
                     ("final_check", final_check), ("finish", finish)]:
        g.node(name, fn)
    g.edge("intake", "plan")
    g.branch("plan", after_plan, {"build", "next_task", "finish"})
    g.edge("build", "finish")
    g.branch("next_task", lambda s: "run_task" if s.current_task else "final_check",
             {"run_task", "final_check"})
    g.edge("run_task", "verify")
    g.branch("verify", after_verify, {"run_task", "next_task"})
    g.edge("final_check", "finish")
    g.edge("finish", END)
    return g


# ---------------------------------------------------------------- running

def _result(s: BuildState) -> dict:
    if s.next in ("build", "next_task") and s.status == "planned":     # paused for review
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
            start = "build" if state.plan is None else "next_task"
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
    # per task: next_task + run_task + verify, plus (run_task + verify) per fix attempt
    max_tasks = ctx.config.get("plan", {}).get("max_tasks", 12)
    max_fix = ctx.config.get("verify", {}).get("max_fix_attempts", 2)
    max_steps = 10 + max_tasks * (3 + 2 * max_fix)
    try:
        state = graph.run(state, start=start, checkpoint=save_state, on_enter=on_enter, max_steps=max_steps,
                          interrupt_before={"build", "next_task"} if review else set())
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
    finally:
        ctx.close()
    return _result(state)
