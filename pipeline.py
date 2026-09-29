"""The build as a graph.

Stage 7:  intake → plan → build → finish, with state checkpointed to
          runs/<id>/state.json before every node, so a stopped run can resume.
Stage 8:  with a plan, `build` is replaced by a task loop:
          plan → next_task ⇄ run_task → finish. Each task runs in its own
          ReAct loop; task status and hand-off notes live in plan.json.
Stage 9:  run_task → verify: the harness runs each task's done_when itself;
          a failed check sends the task back to run_task in fix mode.
          final_check re-runs every check before finish.
Stage 10: final_check → judge: a separate model call compares the project with
          the request; revise sends feedback back through the planner,
          escalate hands a REPORT.md to a human.
Chapter H (--fix): a finished run re-enters at revise with a person's feedback in place
          of a judge verdict, then goes through tasks, checks and the judge again.
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
from judge import (apply_rules, final_checks_evidence, make_verdict, render_report, tasks_evidence,
                   workspace_contents)
from graph import END, Graph
from loop import Step
from planner import PlanError, load_plan, make_plan, render_tasks, revise_plan, revision_section, save_plan
from runtime import build_registry
from state import BuildState, load_state, save_state
from tracing import NullTracer, RoleModel
from compaction import CompactingModel
from replies import NormalizingModel
from sensors import TaskSensors
from memory import error_signature, project_map, render_fix_lessons, render_review_lessons
from trace_view import load_events, metrics as trace_metrics
from context import (CHARS_PER_TOKEN, OutputLimiter, Section, allocate, estimate_tokens, rank_files,
                     relevant_files)

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
    tracer: object = field(default_factory=NullTracer)      # Chapter A
    memory: object = None                                   # Chapter B: LessonStore, or None when off
    models: dict = field(default_factory=dict)               # Chapter H: role → model; missing roles use `model`
    _registry: object = None

    def model_for(self, role: str):
        return self.models.get(role) or self.model

    def registry(self, workspace: Path):
        """One set of MCP servers per run, shared by every task."""
        if self._registry is None:
            tools = build_registry(self.config, workspace, self.stack)
            cfg = self.config.get("context", {})
            if cfg.get("enabled", True):                 # Chapter C: long outputs → head + tail + pointer
                tools = OutputLimiter(tools, Path(workspace).parent / "outputs",
                                      max_chars=cfg.get("max_observation_chars", 4000))
            self._registry = self.tracer.wrap_tools(tools)   # trace records what the model actually saw
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
                   "steps": t.get("steps", 0), "error": t.get("error"),
                   "stuck": (t.get("signals") or {}).get("peak_stuck")} for t in plan["tasks"]],
        **{k: totals[k] for k in ("steps", "malformed")},
        "tool_calls": dict(tools),
        "files": list_files(run.workspace),
        "duration_s": round(duration, 2),
        **{k: totals[k] for k in ("model_calls", "approx_tokens_in", "approx_tokens_out")},
    }


def human_verdict(feedback: str, round_no: int) -> dict:
    """A person's fix request, shaped like a judge verdict so the revise node can use it."""
    return {"verdict": "revise", "source": "human", "requirements": [], "problems": [],
            "feedback": feedback.strip(), "summary": "", "overrides": [], "round": round_no}


def append_index(ctx: Context, s: BuildState) -> None:
    """Chapter F: one line per finished run, for comparing across run folders."""
    summ = s.summary or {}
    m = summ.get("metrics") or {}
    tasks = (s.plan or {}).get("tasks", [])
    row = {
        "id": s.run_id, "ts": _now(), "request": s.request[:300],
        "provider": ctx.meta.get("provider"), "model": ctx.meta.get("model"),
        "status": s.status, "verdict": (summ.get("verdict") or {}).get("verdict"),
        "tasks": len(tasks), "tasks_done": sum(1 for t in tasks if t.get("status") == "done"),
        "steps": summ.get("steps"), "revisions": s.revisions, "duration_s": summ.get("duration_s"),
        "model_calls": m.get("model_calls"), "tokens_in": m.get("tokens_in"), "tokens_out": m.get("tokens_out"),
        "compactions": m.get("compactions"), "sensor_warnings": m.get("sensor_warnings"),
        "stuck_max": max(((t.get("signals") or {}).get("peak_stuck") or 0 for t in tasks), default=0),
        "bench": s.options.get("tags"),
    }
    try:
        ctx.runs_dir.mkdir(parents=True, exist_ok=True)
        with open(ctx.runs_dir / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass                                   # the index is a convenience; never fail a build over it


# ---------------------------------------------------------------- nodes

def build_graph(ctx: Context) -> Graph:
    cfg_build = ctx.config.get("build", {})
    cfg_plan = ctx.config.get("plan", {})
    cfg_verify = ctx.config.get("verify", {})
    cfg_judge = ctx.config.get("judge", {})
    cfg_memory = ctx.config.get("memory", {})
    cfg_context = ctx.config.get("context", {})
    cfg_compaction = ctx.config.get("compaction", {})
    cfg_sensors = ctx.config.get("sensors", {})
    sensors_on = cfg_sensors.get("enabled", True)
    warn_at = cfg_sensors.get("warn_at", 0.5)
    P = ctx.prompts
    max_tasks = cfg_plan.get("max_tasks", 12)
    load_limit = max_tasks * 2              # revisions may add tasks (Stage 10)
    max_fix = cfg_verify.get("max_fix_attempts", 2)
    max_revisions = cfg_judge.get("max_revisions", 2)

    def loop_model(label: str):
        """Per loop: normalize other tool-call formats (e.g. Gemma's call:…{…}), then compact (Chapter D)."""
        base = ctx.model_for("task")
        if ctx.config.get("replies", {}).get("normalize", True):
            def on_normalize(how: str, raw: str) -> None:
                ctx.tracer.event("normalized", format=how, raw=raw[:300])
            base = NormalizingModel(base, on_normalize=on_normalize)
        if not cfg_compaction.get("enabled", True):
            return base

        def on_compact(e: dict) -> None:
            ctx.tracer.event("compaction", **e)
            ctx.say(f"[compact] {label}: steps {e['from_step']}–{e['to_step']} summarised "
                    f"({e['tokens_before'] / 1000:.1f}k → {e['tokens_after'] / 1000:.1f}k tok)")
        return CompactingModel(
            base, window_tokens=cfg_context.get("window_tokens", 16000),
            call_fraction=cfg_compaction.get("call_fraction", 0.85), compact_at=cfg_compaction.get("compact_at", 0.75),
            target=cfg_compaction.get("target", 0.5), ceiling=cfg_compaction.get("ceiling", 0.95),
            keep_recent_steps=cfg_compaction.get("keep_recent_steps", 2),
            note=P["compact_note"], mode=cfg_compaction.get("mode", "extractive"),
            summarizer=RoleModel(ctx.model_for("compactor"), ctx.tracer, "compactor"), summarizer_system=P["compact_system"],
            on_compact=on_compact)

    def report_signals(task: dict, sig: dict, phase: str) -> None:
        """Chapter E consumer: warn once when a task first looks stuck; always trace."""
        warn = sig["stuck"] >= warn_at and not task.get("sensor_warned")
        if warn:
            task["sensor_warned"] = True
            ctx.say(f"[sensor] {task['id']} looks stuck ({sig['stuck']:.2f}): {'; '.join(sig['reasons']) or '—'}")
        ctx.tracer.event("signals", phase=phase, warning=warn,
                         **{k: v for k, v in sig.items() if k != "reasons"}, reasons=sig["reasons"])

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
        notes = None
        if ctx.memory is not None:                      # Chapter B: review lessons for similar requests
            lessons = ctx.memory.recall("review", s.request)
            if lessons:
                notes = P["lessons_plan"].replace("{lessons}", render_review_lessons(lessons))
                s.lessons_shown = [x["id"] for x in lessons]
                ctx.memory.mark(s.lessons_shown, shown=True)
                ctx.say(f"[memory] {len(lessons)} review lesson(s) from past builds → planner")
        ctx.say("[plan] asking the planner …")
        try:
            p = make_plan(ctx.model_for("planner"), P, s.request, ask=ctx.ask,
                          max_attempts=cfg_plan.get("max_attempts", 3), max_tasks=max_tasks,
                          max_questions=cfg_plan.get("max_questions", 3),
                          log=lambda e: _append_jsonl(run_dir / "planner.jsonl", e), notes=notes)
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
            run, loop_model("build"), ctx.registry(run.workspace), P["react_system"] + "\n\n" + P["build_rules"], first,
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

        # ---- gather the pieces of the first message (Chapters B and C decide how they're shown)
        files_text = (project_map(run.workspace, max_chars=cfg_memory.get("map_chars", 4000))
                      if ctx.memory is not None else _files_listing(run.workspace))
        fix_text = lessons_text = resume_text = ""
        last = task["checks"][-1] if fixing else None
        if fixing:
            fix_text = (P["fix_request"]
                        .replace("{handoff}", task.get("handoff") or "(none)")
                        .replace("{check}", _describe_check(last))
                        .replace("{output}", last["output"] or "(no output)")
                        .replace("{n}", str(task.get("fix_attempts", 1)))
                        .replace("{max}", str(max_fix)))
            if sensors_on:                              # Chapter E consumer: say when it's the same failure again
                repeats = TaskSensors(task, run.workspace).max_same_failure()
                if repeats >= 2:
                    fix_text += "\n" + P["sensor_note"].replace("{n}", str(repeats))
            if ctx.memory is not None:                  # Chapter B: fixes that worked for similar failures
                lessons = ctx.memory.recall("fix", error_signature(last["output"] or ""))
                if lessons:
                    lessons_text = P["lessons_fix"].replace("{lessons}", render_fix_lessons(lessons))
                    ids = [x["id"] for x in lessons]
                    task["lessons_shown"] = list(dict.fromkeys((task.get("lessons_shown") or []) + ids))
                    ctx.memory.mark(ids, shown=True)
                    ctx.say(f"[memory] {len(lessons)} fix lesson(s) from past builds → {task['id']}")
        if resuming:
            resume_text = P["build_resume_note"].replace("{files}", _files_listing(run.workspace))
            _append_jsonl(run.transcript, {"event": "resume", "task": task["id"], "at": _now()})

        task_text = (P["task_block"]
                     .replace("{task_id}", task["id"]).replace("{task_title}", task["title"])
                     .replace("{task_description}", task["description"] or "(no description)")
                     .replace("{task_files}", ", ".join(task["files"]) or "(not specified)")
                     .replace("{task_done_when}", _done_when(task["done_when"])))
        pieces = {"request": s.request, "spec": (s.spec or "").strip(), "plan_status": plan_status(s.plan, task["id"]),
                  "handoffs": handoffs(s.plan), "files": files_text}

        relevant_block = ""
        if cfg_context.get("enabled", True):            # Chapter C: one budget, shared by fraction
            shares = cfg_context.get("shares", {})
            query = " ".join([task["title"], task["description"] or "", last["output"] if last else ""])
            ranked = rank_files(run.workspace, listed=task["files"], query=query,
                                mentions=(last["output"] or "") if last else "")
            full_relevant, _ = relevant_files(ranked, 10 ** 9)
            strategies = {"handoffs": "tail", "fix": "tail"}
            sections = [Section("task", task_text, required=True)]
            for name, text in [*pieces.items(), ("relevant_files", full_relevant if ranked else ""),
                               ("fix", fix_text), ("lessons", lessons_text), ("resume", resume_text)]:
                sections.append(Section(name, text, shares.get(name, 0.05), strategies.get(name, "head")))
            skeleton = len(P["task_request"]) + len(P["relevant_files_block"])
            budget_chars = int(cfg_context.get("window_tokens", 16000) * cfg_context.get("first_message_fraction", 0.4)
                               * CHARS_PER_TOKEN) - skeleton
            alloc = allocate(sections, max(0, budget_chars))
            texts = alloc.texts
            shown: list[str] = []
            if ranked:      # re-render whole files into the relevant_files allocation (not a blind cut)
                texts["relevant_files"], shown = relevant_files(ranked, alloc.given["relevant_files"])
                relevant_block = P["relevant_files_block"].replace("{relevant_files}", texts["relevant_files"])
            stats = alloc.stats(sections)
            ctx.tracer.event("context", budget_tokens=budget_chars // CHARS_PER_TOKEN,
                             used_tokens=estimate_tokens("".join(texts.values())),
                             sections=stats["sections"], files_shown=shown, attempt="fix" if fixing else "first")
        else:
            texts = {**pieces, "task": task_text, "fix": fix_text, "lessons": lessons_text, "resume": resume_text}

        first = (P["task_request"]
                 .replace("{request}", texts["request"]).replace("{spec}", texts["spec"])
                 .replace("{plan_status}", texts["plan_status"]).replace("{handoffs}", texts["handoffs"])
                 .replace("{files}", texts["files"]).replace("{relevant_files_block}", relevant_block)
                 .replace("{task_block}", texts["task"]))
        for extra in ("fix", "lessons", "resume"):
            if texts[extra]:
                first += "\n\n" + texts[extra]

        on_step = ctx.on_step
        sensors = TaskSensors(task, run.workspace) if sensors_on else None
        if sensors is not None:                         # Chapter E: observe every step (never steer)
            def on_step(step, _base=ctx.on_step):
                sensors.observe_step(step)
                sig = sensors.scores()
                if sig["stuck"] >= warn_at and not task.get("sensor_warned"):
                    report_signals(task, sig, "step")
                if _base:
                    _base(step)
        try:
            result = execute_build(
                run, loop_model(task["id"]), ctx.registry(run.workspace),
                P["react_system"] + "\n\n" + P["task_rules"], first,
                max_iterations=cfg_build.get("task_max_iterations", 20),
                format_reminder=P["format_reminder"], on_step=on_step,
                label={"task": task["id"], "fix": task.get("fix_attempts", 0)} if fixing else {"task": task["id"]},
                write_summary=False)
        finally:
            if sensors is not None:
                report_signals(task, sensors.save(), "attempt")
                _save_plan_json(run.dir, s.plan)

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
        ctx.tracer.event("check", phase="verify", kind=res.kind, target=res.target, ok=res.ok,
                         exit_code=res.exit_code)
        if sensors_on:                                  # Chapter E: a check is an observation too
            sensors = TaskSensors(task, run.workspace)
            sensors.observe_check(res.ok, res.output or "")
            report_signals(task, sensors.save(), "check")
        if res.ok:
            task["status"], task["verified"] = "done", True
            task.pop("error", None)
            task.pop("fix_pending", None)
            ctx.say(f"[check] {task['id']} ✓ {res.describe()}")
            if ctx.memory is not None:                  # Chapter B: remember what fixed it
                failed = [c for c in task["checks"][:-1] if not c["ok"]]
                if failed and task.get("fix_attempts", 0) > 0:
                    lesson = ctx.memory.add_fix(failure_output=failed[-1]["output"] or "", check=failed[-1]["target"],
                                                fix_note=task.get("handoff") or "", task=task["title"],
                                                request=s.request, run_id=s.run_id)
                    ctx.say(f"[memory] lesson {lesson['id']} saved: fix for `{failed[-1]['target']}`")
                ctx.memory.mark(task.pop("lessons_shown", []), helped=True)
        elif task.get("fix_attempts", 0) < max_fix:
            task["fix_attempts"] = task.get("fix_attempts", 0) + 1
            task["fix_pending"] = True
            ctx.say(f"[check] {task['id']} ✗ {res.describe()} → fix attempt {task['fix_attempts']}/{max_fix}")
        else:
            task["status"], task["verified"] = "failed", False
            task["error"] = f"check failed after {task.get('fix_attempts', 0)} fix attempts: {res.describe()}"
            task.pop("fix_pending", None)
            task.pop("lessons_shown", None)             # shown, didn't help: `shown` already counted
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
                ctx.tracer.event("check", phase="final", kind=res.kind, target=res.target, ok=res.ok,
                                 exit_code=res.exit_code)
            results.append({"task": task["id"], **seen[key]})
        s.final_checks = results
        _append_jsonl(run.transcript, {"event": "final_checks", "ok": all(r["ok"] for r in results),
                                       "count": len(seen)})

    def judge(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        s.plan, s.spec = load_plan(run.dir, load_limit)
        evidence = {
            "request": s.request,
            "spec": (s.spec or "").strip(),
            "tasks": tasks_evidence(s.plan, cfg_sensors.get("evidence_at", 0.3)),
            "final_checks": final_checks_evidence(s.final_checks),
            "files": workspace_contents(run.workspace, file_chars=cfg_judge.get("file_chars", 12000),
                                        total_chars=cfg_judge.get("evidence_chars", 60000)),
        }
        ctx.say(f"[judge] reviewing the project (round {s.revisions + 1}) …")
        v = make_verdict(ctx.model_for("judge"), P, evidence, max_attempts=cfg_judge.get("max_attempts", 2),
                         log=lambda e: _append_jsonl(run.dir / "judge.jsonl", {"round": s.revisions, **e}))
        v = apply_rules(v, s.plan, s.final_checks, revisions_used=s.revisions - s.revision_base,
                        max_revisions=max_revisions)
        v["round"] = s.revisions
        s.verdicts.append(v)
        met = sum(1 for r in v["requirements"] if r["met"])
        ctx.say(f"[judge] {v['verdict']}  ({met}/{len(v['requirements'])} requirements met)")
        if ctx.memory is not None and v["verdict"] == "revise" and not v["overrides"]:   # Chapter B
            lesson = ctx.memory.add_review(request=s.request, feedback=v.get("feedback", ""),
                                           problems=v.get("problems", []), run_id=s.run_id)
            ctx.say(f"[memory] lesson {lesson['id']} saved: review feedback")
        ctx.tracer.event("verdict", verdict=v["verdict"], met=met, total=len(v["requirements"]),
                         overrides=[f"{o['from']}→{o['to']}" for o in v["overrides"]], round=s.revisions)
        for o in v["overrides"]:
            ctx.say(f"[judge] harness override: {o['from']} → {o['to']} ({o['reason']})")

    def revise(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        verdict = s.verdicts[-1]
        round_no = s.revisions + 1
        ctx.say(f"[revise] round {round_no}: planning changes …")
        try:
            new_plan, record = revise_plan(
                ctx.model_for("reviser"), P, request=s.request, plan=s.plan, spec=s.spec or "",
                plan_status=plan_status(s.plan), verdict=verdict, files=_files_listing(run.workspace),
                round_no=round_no, max_attempts=cfg_plan.get("max_attempts", 3), max_tasks=max_tasks,
                log=lambda e: _append_jsonl(run.dir / "planner.jsonl", e))
        except PlanError as e:
            verdict["overrides"].append({"from": "revise", "to": "escalate", "reason": f"revision failed: {e}"})
            verdict["verdict"] = "escalate"
            ctx.say(f"[revise] failed → escalate ({e})")
            return
        s.revisions = round_no
        s.plan, s.final_checks = new_plan, None
        _save_plan_json(run.dir, new_plan)
        with open(run.dir / "SPEC.md", "a", encoding="utf-8") as f:
            f.write("\n" + revision_section(record, verdict, new_plan))
        s.spec = (run.dir / "SPEC.md").read_text(encoding="utf-8")
        _append_jsonl(run.transcript, {"event": "revision", **record})
        ctx.say(f"[revise] {record['changes'] or 'plan updated'} — retry: {', '.join(record['retry']) or 'none'}; "
                f"new: {', '.join(record['added']) or 'none'}")

    def finish(s: BuildState) -> None:
        run = open_run(Path(s.run_dir))
        if s.plan is not None and s.status != "plan_failed":
            s.plan, _ = load_plan(run.dir, load_limit)
            all_done = all(t["status"] == "done" for t in s.plan["tasks"])
            regressions = [c for c in (s.final_checks or []) if not c["ok"]]
            last = s.verdicts[-1] if s.verdicts else None
            judged = last is not None and last.get("source") != "human"     # a --fix without a judge after it
            if judged:
                s.status = "accepted" if last["verdict"] == "accept" else "escalated"
                if ctx.memory is not None and s.status == "accepted" and s.lessons_shown:
                    ctx.memory.mark(s.lessons_shown, helped=True)
                    s.lessons_shown = []
            elif last is not None and last["verdict"] == "escalate":      # the fix couldn't be planned
                s.status = "escalated"
            else:
                s.status = "finished" if all_done and not regressions else "partial"
            s.summary = aggregate(run, s.plan, s.status)
            s.summary["verified"] = True
            s.summary["final_checks"] = s.final_checks or []
            if regressions:
                note = "; ".join(f"regression in {c['task']}: {_describe_check(c)}" for c in regressions)
                s.summary["error"] = "; ".join(x for x in [s.summary["error"], note] if x)
            if last is not None and not judged and last["overrides"]:
                s.summary["error"] = "; ".join(x for x in [s.summary["error"], last["overrides"][-1]["reason"]] if x)
            if judged:
                s.summary["verdict"] = s.verdicts[-1]
                s.summary["revisions"] = s.revisions
                readme = run.workspace / "README.md"
                report = render_report(
                    request=s.request, run_id=s.run_id, status=s.status, verdict=s.verdicts[-1], plan=s.plan,
                    final_checks=s.final_checks, files=s.summary["files"],
                    readme=readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else None,
                    revisions=s.revisions)
                (run.dir / "REPORT.md").write_text(report, encoding="utf-8")
                s.summary["report"] = str(run.dir / "REPORT.md")
            s.error = s.summary["error"]
        elif s.summary is None:     # ended before building (plan_failed)
            s.summary = {"id": s.run_id, "status": s.status, "error": s.error, "plan": _plan_info(s.plan)}
        # Chapter F: totals from the trace, and one row in runs/index.jsonl
        try:
            s.summary["metrics"] = trace_metrics(load_events(run.dir))
        except FileNotFoundError:
            s.summary["metrics"] = None
        run.summary_file.write_text(json.dumps(s.summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        append_index(ctx, s)

    def after_plan(s: BuildState) -> str:
        if s.status == "plan_failed":
            return "finish"
        return "build" if s.plan is None else "next_task"

    def after_verify(s: BuildState) -> str:
        task = next(t for t in s.plan["tasks"] if t["id"] == s.current_task)
        return "run_task" if task.get("fix_pending") else "next_task"

    def after_final_check(s: BuildState) -> str:
        return "judge" if s.options.get("judge", True) else "finish"

    g = Graph(entry="intake")
    for name, fn in [("intake", intake), ("plan", plan), ("build", build),
                     ("next_task", next_task), ("run_task", run_task), ("verify", verify),
                     ("final_check", final_check), ("judge", judge), ("revise", revise),
                     ("finish", finish)]:
        g.node(name, ctx.tracer.wrap_node(name, fn))
    g.edge("intake", "plan")
    g.branch("plan", after_plan, {"build", "next_task", "finish"})
    g.edge("build", "finish")
    g.branch("next_task", lambda s: "run_task" if s.current_task else "final_check",
             {"run_task", "final_check"})
    g.edge("run_task", "verify")
    g.branch("verify", after_verify, {"run_task", "next_task"})
    g.branch("final_check", after_final_check, {"judge", "finish"})
    g.branch("judge", lambda s: "revise" if s.verdicts[-1]["verdict"] == "revise" else "finish",
             {"revise", "finish"})
    g.branch("revise", lambda s: "next_task" if s.verdicts[-1]["verdict"] == "revise" else "finish",
             {"next_task", "finish"})
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
    judge: bool | None = None,
    tags: dict | None = None,
    fix: str | None = None,
    on_enter: Callable[[str, BuildState], None] | None = None,
) -> dict:
    """Start a new build, resume one from its state.json, or (`fix`) revise a finished one."""
    if resume_dir and fix is not None:
        state = load_state(resume_dir)
        if not fix.strip():
            raise PlanError("--fix needs a description of what to change")
        if state.plan is None:
            raise PlanError(f"{state.run_dir} was built without a plan (--no-plan); --fix needs a plan to revise. "
                            "Start a new run instead")
        if state.next or state.status in RESUMABLE_ENDED | {"planned", "plan_failed"}:
            raise PlanError(f"{state.run_dir} hasn't finished (status: {state.status}). "
                            "Finish it first with --resume (or --from-run for a reviewed plan)")
        state.plan, state.spec = load_plan(Path(state.run_dir), ctx.config.get("plan", {}).get("max_tasks", 12) * 2)
        state.verdicts.append(human_verdict(fix, state.revisions))
        state.revision_base = state.revisions + 1         # the judge gets its full budget again
        state.error = None
        start = "revise"
        if judge is not None:
            state.options["judge"] = judge
        ctx.say(f"[run] {state.run_dir}  (fixing: {' '.join(fix.split())[:80]})")
    elif resume_dir:
        state = load_state(resume_dir)
        if state.next:
            start = state.next
        elif state.status in RESUMABLE_ENDED:
            start = "build" if state.plan is None else "next_task"
        else:
            raise PlanError(f"{state.run_dir} has already been built (status: {state.status}); "
                            "start a new run instead")
        state.options["review"] = review
        if judge is not None:
            state.options["judge"] = judge
        if max_iterations:
            state.options["max_iterations"] = max_iterations
        ctx.say(f"[run] {state.run_dir}  (resuming at '{start}')")
    else:
        judge_on = ctx.config.get("judge", {}).get("enabled", True) if judge is None else judge
        state = BuildState(request=request, options={"review": review, "no_plan": no_plan,
                                                     "max_iterations": max_iterations, "judge": judge_on,
                                                     "tags": tags})
        start = None

    # Chapter A: wrap from outside — the loop, nodes and tools don't know they're traced
    tracer = ctx.tracer
    wrapped: dict[int, object] = {}                     # models shared by several roles are wrapped once

    def wrap(m):
        return wrapped.setdefault(id(m), tracer.wrap_model(m))
    ctx.model = wrap(ctx.model)
    ctx.models = {role: wrap(m) for role, m in ctx.models.items()}
    if tracer.enabled:
        user_on_step = ctx.on_step

        def on_step(step):
            tracer.step(step)
            if user_on_step:
                user_on_step(step)
        ctx.on_step = on_step
    if resume_dir:
        tracer.bind(state.run_dir, run_id=state.run_id, resumed_at=start)

    graph = build_graph(ctx)
    # per task: next_task + run_task + verify, plus (run_task + verify) per fix attempt;
    # up to 2 × max_tasks after revisions, and one full pass per revision round
    max_tasks = ctx.config.get("plan", {}).get("max_tasks", 12)
    max_fix = ctx.config.get("verify", {}).get("max_fix_attempts", 2)
    rounds = ctx.config.get("judge", {}).get("max_revisions", 2) + 1
    max_steps = rounds * (10 + 2 * max_tasks * (3 + 2 * max_fix))
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
