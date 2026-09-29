"""Stage 6 — planner: request → spec → tasks.

One tool-less model call turns the request into a JSON plan. The harness
validates it (feeding errors back for another attempt), then writes
plan.json and a human-readable SPEC.md into the run folder.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Callable

from loop import ParseError, _first_json_object

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
LIST_FIELDS = ("features", "tech", "constraints", "out_of_scope", "assumptions")
TASK_STATUSES = {"pending", "in_progress", "done", "failed", "blocked", "dropped"}   # dropped: Chapter I
# progress fields written by the harness (Stage 8); kept when a plan is re-validated
PROGRESS_FIELDS = ("handoff", "error", "steps", "malformed", "tool_calls", "duration_s",
                   "model_calls", "approx_tokens_in", "approx_tokens_out",
                   "checks", "verified", "fix_attempts", "fix_pending", "attempt_open",   # + Stage 9
                   "lessons_shown",                                                        # + Chapter B
                   "signals", "sensor_state", "sensor_warned",                             # + Chapter E
                   "dropped_in")                                                           # + Chapter I

Asker = Callable[[list[str]], list[str]]   # questions -> answers


class PlanError(RuntimeError):
    pass


# ---------------------------------------------------------------- validation

def _bad_path(p) -> str | None:
    if not isinstance(p, str) or not p.strip():
        return "must be a non-empty string"
    pp = PurePosixPath(p.replace("\\", "/"))
    if pp.is_absolute() or re.match(r"^[A-Za-z]:", p):
        return "must be relative to the workspace"
    if ".." in pp.parts:
        return "must not contain '..'"
    return None


def _order(tasks: list[dict]) -> tuple[list[dict], str | None]:
    """Dependency order, keeping the planner's order where it's free to."""
    by_id = {t["id"]: t for t in tasks}
    done: list[str] = []
    placed: set[str] = set()
    remaining = [t["id"] for t in tasks]
    while remaining:
        for tid in remaining:
            if all(d in placed for d in by_id[tid]["depends_on"]):
                done.append(tid)
                placed.add(tid)
                remaining.remove(tid)
                break
        else:
            return tasks, "tasks have a dependency cycle: " + ", ".join(remaining)
    return [by_id[t] for t in done], None


def validate_plan(obj, max_tasks: int = 15) -> tuple[dict, list[str]]:
    """Return (normalised plan, problems). An empty problem list means it's valid."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {}, ["plan must be a JSON object"]
    plan: dict = {}
    for key in ("title", "summary"):
        v = obj.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"'{key}' must be a non-empty string")
        plan[key] = v.strip() if isinstance(v, str) else ""
    for key in LIST_FIELDS:
        v = obj.get(key, [])
        if v is None:
            v = []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.append(f"'{key}' must be a list of strings")
            v = []
        plan[key] = v
    plan["clarifications"] = obj.get("clarifications", []) or []

    raw_tasks = obj.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        errors.append("'tasks' must be a non-empty list")
        raw_tasks = []
    elif sum(1 for t in raw_tasks if not (isinstance(t, dict) and t.get("status") == "dropped")) > max_tasks:
        active = sum(1 for t in raw_tasks if not (isinstance(t, dict) and t.get("status") == "dropped"))
        errors.append(f"too many tasks ({active}); use at most {max_tasks}"
                      + (" (dropped tasks don't count)" if active < len(raw_tasks) else ""))

    tasks: list[dict] = []
    seen: set[str] = set()
    for i, t in enumerate(raw_tasks, 1):
        where = f"task #{i}"
        if not isinstance(t, dict):
            errors.append(f"{where} must be an object")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not tid.strip():
            errors.append(f"{where}: 'id' must be a non-empty string")
            continue
        tid = tid.strip()
        where = f"task {tid}"
        if tid in seen:
            errors.append(f"{where}: duplicate id")
            continue
        seen.add(tid)
        title = t.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{where}: 'title' must be a non-empty string")
        desc = t.get("description", "")
        files = t.get("files", []) or []
        if not isinstance(files, list):
            errors.append(f"{where}: 'files' must be a list")
            files = []
        for f in files:
            problem = _bad_path(f)
            if problem:
                errors.append(f"{where}: file {f!r} {problem}")
        deps = t.get("depends_on", []) or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            errors.append(f"{where}: 'depends_on' must be a list of task ids")
            deps = []
        dw = t.get("done_when")
        if not isinstance(dw, dict) or len([k for k in ("command", "file") if k in dw]) != 1:
            other = [k for k in dw if k not in ("command", "file")] if isinstance(dw, dict) else []
            hint = (f" (it uses {', '.join(repr(k) for k in other)}; write it as "
                    f'{{"command": "python -m pytest -q"}} or {{"file": "README.md"}})') if other else ""
            errors.append(f"{where}: 'done_when' must have exactly one of 'command' or 'file'{hint}")
            dw = {}
        elif "command" in dw and (not isinstance(dw["command"], str) or not dw["command"].strip()):
            errors.append(f"{where}: done_when.command must be a non-empty string")
        elif "file" in dw and _bad_path(dw["file"]):
            errors.append(f"{where}: done_when.file {_bad_path(dw['file'])}")
        tasks.append({
            "id": tid,
            "title": title.strip() if isinstance(title, str) else "",
            "description": desc if isinstance(desc, str) else str(desc),
            "files": [f for f in files if isinstance(f, str)],
            "depends_on": [d.strip() for d in deps],
            "done_when": {k: v for k, v in dw.items() if k in ("command", "file")},
            "status": t.get("status") if t.get("status") in TASK_STATUSES else "pending",
            **{k: t[k] for k in PROGRESS_FIELDS if k in t},
        })

    for t in tasks:
        for d in t["depends_on"]:
            if d not in seen:
                errors.append(f"task {t['id']}: depends on unknown task '{d}'")
            elif d == t["id"]:
                errors.append(f"task {t['id']}: depends on itself")
    if tasks and not errors:
        tasks, cycle = _order(tasks)
        if cycle:
            errors.append(cycle)
    plan["tasks"] = tasks
    return plan, errors


# ---------------------------------------------------------------- rendering

def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "- (none)"


def _done_when(dw: dict) -> str:
    return f"`{dw['command']}` succeeds" if "command" in dw else f"`{dw['file']}` exists"


def render_spec(plan: dict, request: str) -> str:
    lines = [f"# {plan['title']}", "", f"> **Request:** {request}", "", plan["summary"], ""]
    sections = [("Features", "features"), ("Tech", "tech"), ("Constraints", "constraints"),
                ("Out of scope", "out_of_scope"), ("Assumptions", "assumptions")]
    for heading, key in sections:
        lines += [f"## {heading}", "", _bullets(plan[key]), ""]
    if plan.get("clarifications"):
        lines += ["## Clarifications", ""]
        for c in plan["clarifications"]:
            lines += [f"- **Q:** {c['q']}", f"  **A:** {c['a']}"]
        lines.append("")
    lines += ["## Tasks", "", "| ID | Task | Files | Depends on | Done when |", "|---|---|---|---|---|"]
    for t in plan["tasks"]:
        files = ", ".join(f"`{f}`" for f in t["files"]) or "—"
        deps = ", ".join(t["depends_on"]) or "—"
        lines.append(f"| {t['id']} | {t['title']} | {files} | {deps} | {_done_when(t['done_when'])} |")
    return "\n".join(lines) + "\n"


def render_tasks(plan: dict) -> str:
    out = []
    for t in plan["tasks"]:
        deps = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        files = f" — files: {', '.join(t['files'])}" if t["files"] else ""
        out.append(f"{t['id']}. {t['title']}{deps}{files}\n    {t['description']}\n"
                   f"    done when: {_done_when(t['done_when'])}")
    return "\n".join(out)


# ---------------------------------------------------------------- planning

SHELL_TOKENS = {"&&", "||", "|", ">", ">>", "<", ";", "2>&1", "&"}
_KEYLESS_DONE_WHEN = re.compile(r'("done_when"\s*:\s*\{)\s*("(?:[^"\\]|\\.)*")\s*\}')


def _program(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def command_problems(tasks: list[dict], allowed: set[str] | None = None) -> list[str]:
    """Checks run without a shell (Chapter I): `a && b` or `x > file` can never pass, so reject them early.

    `allowed`: the programs the workspace server may run; a check that starts with anything else
    (echo, cat, ls …) can't even start.
    """
    problems = []
    for t in tasks:
        cmd = (t.get("done_when") or {}).get("command")
        if not isinstance(cmd, str):
            continue
        try:
            words = shlex.split(cmd)
        except ValueError as e:
            problems.append(f"task {t.get('id')}: done_when.command can't be split into words ({e}); check its quotes")
            continue
        if allowed and words and _program(words[0]) not in allowed:
            problems.append(f"task {t.get('id')}: done_when.command starts with '{words[0]}', which checks can't run; "
                            f"use one of: {', '.join(sorted(allowed))} (e.g. python -m pytest -q), and make it test the work")
            continue
        bad = sorted({w for w in words if w in SHELL_TOKENS or w.startswith((">", "2>"))})
        if bad:
            problems.append(f"task {t.get('id')}: done_when.command uses {' '.join(bad)}, but checks run without a "
                            "shell; use one plain command, e.g. python -m pytest -q")
    return problems


def _parse(text: str, want: tuple[str, ...] = ()) -> dict:
    text = _THINK_RE.sub("", text or "")
    text = _KEYLESS_DONE_WHEN.sub(r'\1"command": \2}', text)      # {"python -c …"} → {"command": "python -c …"}
    return _first_json_object(text, want)


def make_plan(
    model,
    prompts: dict[str, str],
    request: str,
    *,
    ask: Asker | None = None,
    max_attempts: int = 3,
    max_tasks: int = 15,
    max_questions: int = 3,
    log: Callable[[dict], None] | None = None,
    notes: str | None = None,
    allowed: set[str] | None = None,
) -> dict:
    """Call the planner until it returns a valid plan. Raises PlanError if it can't.

    `notes` (Chapter B) is extra context appended to the first message, e.g. lessons from past builds.
    """
    system = prompts["planner_system"].replace("{max_tasks}", str(max_tasks)) \
                                      .replace("{max_questions}", str(max_questions))
    first = prompts["planner_request"].replace("{request}", request)
    if notes:
        first += "\n\n" + notes
    messages = [{"role": "user", "content": first}]
    clarifications: list[dict] = []
    asked = False
    last_errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        raw = model.complete(system, messages)
        messages.append({"role": "assistant", "content": raw if raw.strip() else "(empty reply)"})
        entry = {"attempt": attempt, "raw": raw}
        try:
            obj = _parse(raw, ("tasks", "questions", "title"))
        except ParseError as e:
            last_errors = [f"no JSON object in the reply ({e})"]
        else:
            questions = obj.get("questions")
            if questions and not obj.get("tasks"):
                qs = [str(q) for q in questions][:max_questions]
                entry["questions"] = qs
                if asked:
                    last_errors = ["questions were already asked once; return the plan now"]
                else:
                    asked = True
                    answers = ask(qs) if ask else None
                    if answers:
                        clarifications = [{"q": q, "a": a} for q, a in zip(qs, answers)]
                        reply = prompts["planner_answers"].replace(
                            "{answers}", "\n".join(f"Q: {c['q']}\nA: {c['a']}" for c in clarifications))
                    else:
                        clarifications = [{"q": q, "a": "(no answer — assumption recorded)"} for q in qs]
                        reply = prompts["planner_no_answers"]
                    entry["answers"] = [c["a"] for c in clarifications]
                    if log:
                        log(entry)
                    messages.append({"role": "user", "content": reply})
                    continue
            else:
                plan, last_errors = validate_plan(obj, max_tasks)
                last_errors = last_errors or command_problems(plan["tasks"], allowed)
                if not last_errors:
                    plan["clarifications"] = clarifications
                    entry["ok"] = True
                    if log:
                        log(entry)
                    return plan
        entry["errors"] = last_errors
        if log:
            log(entry)
        messages.append({"role": "user", "content": prompts["planner_fix"].replace(
            "{errors}", "\n".join(f"- {e}" for e in last_errors))})

    raise PlanError(f"no valid plan after {max_attempts} attempts: " + "; ".join(last_errors))


# ---------------------------------------------------------------- files

def save_plan(run_dir: Path, plan: dict, request: str) -> None:
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "SPEC.md").write_text(render_spec(plan, request), encoding="utf-8")


def load_plan(run_dir: Path, max_tasks: int = 15) -> tuple[dict, str]:
    """Read plan.json (re-validated) and the current SPEC.md text."""
    plan_file, spec_file = run_dir / "plan.json", run_dir / "SPEC.md"
    if not plan_file.exists():
        raise PlanError(f"no plan.json in {run_dir}")
    try:
        obj = json.loads(plan_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlanError(f"plan.json is not valid JSON: {e}") from e
    clar = obj.get("clarifications", [])
    plan, errors = validate_plan(obj, max_tasks)
    if errors:
        raise PlanError("plan.json has problems:\n" + "\n".join(f"  - {e}" for e in errors))
    plan["clarifications"] = clar
    spec = spec_file.read_text(encoding="utf-8") if spec_file.exists() else ""
    return plan, spec


# ---------------------------------------------------------------- Stage 10: revise

RESETTABLE = {"failed", "blocked"}
DROPPABLE = {"failed", "blocked", "pending"}


def _drop(tasks: list[dict], ids: list[str]) -> list[str]:
    """Mark tasks dropped, plus every unfinished task that depends on one. Returns all dropped ids."""
    dropped = set(ids)
    changed = True
    while changed:
        changed = False
        for t in tasks:
            if t["id"] not in dropped and t["status"] != "done" and any(d in dropped for d in t["depends_on"]):
                dropped.add(t["id"])
                changed = True
    for t in tasks:
        if t["id"] in dropped:
            t["status"] = "dropped"
            for k in ("fix_pending", "error"):
                t.pop(k, None)
    return [t["id"] for t in tasks if t["id"] in dropped]


def _apply_revision(plan: dict, obj: dict) -> tuple[dict, list[str]]:
    """Merge a reviser reply into a copy of the plan. Returns (merged object, problems)."""
    errors = []
    by_id = {t["id"]: t for t in plan["tasks"]}
    retry = obj.get("retry", []) or []
    new_tasks = obj.get("tasks", []) or []
    if not isinstance(retry, list) or not all(isinstance(x, str) for x in retry):
        errors.append("'retry' must be a list of task ids")
        retry = []
    if not isinstance(new_tasks, list):
        errors.append("'tasks' must be a list")
        new_tasks = []
    drop = obj.get("drop", []) or []
    if not isinstance(drop, list) or not all(isinstance(x, str) for x in drop):
        errors.append("'drop' must be a list of task ids")
        drop = []
    for tid in drop:
        if tid not in by_id:
            errors.append(f"drop: unknown task '{tid}'")
        elif by_id[tid]["status"] not in DROPPABLE:
            errors.append(f"drop: task '{tid}' is {by_id[tid]['status']}; only failed, blocked or pending tasks can be dropped")
    retry = [x for x in retry if x not in drop]
    new_ids = {t.get("id") for t in new_tasks if isinstance(t, dict)}
    retry = [x for x in retry if x not in new_ids]      # Chapter I: new task ids listed under retry too — just drop them
    done_checks = {json.dumps(t["done_when"], sort_keys=True): t["id"] for t in plan["tasks"] if t["status"] == "done"}
    for t in new_tasks:
        if isinstance(t, dict) and isinstance(t.get("done_when"), dict):
            same = done_checks.get(json.dumps(t["done_when"], sort_keys=True))
            if same and "test" not in str(t["done_when"].get("command", "")):   # a test suite grows; that's fine
                errors.append(f"task {t.get('id')}: its done_when is the same check as {same}, which already passes, "
                              "so it can't show the new work is done; give it a check that tests the change")
    if not retry and not new_tasks and not drop:
        errors.append("change something: list task ids under 'retry', new tasks under 'tasks', "
                      "and/or replaced tasks under 'drop'")
    for tid in retry:
        if tid not in by_id:
            errors.append(f"retry: unknown task '{tid}'")
        elif by_id[tid]["status"] not in RESETTABLE:
            errors.append(f"retry: task '{tid}' is {by_id[tid]['status']}; only failed or blocked tasks can be retried "
                          "(add a new task to change finished work)")
    merged = json.loads(json.dumps(plan))           # deep copy
    merged["_dropped"] = _drop(merged["tasks"], [x for x in drop if x in by_id and by_id[x]["status"] in DROPPABLE])
    for t in merged["tasks"]:
        if t["id"] in retry or (retry and t["status"] == "blocked"):
            t["status"] = "pending"
            t["fix_attempts"] = 0
            t["verified"] = False
            for k in ("fix_pending", "error"):
                t.pop(k, None)
            t["attempt_open"] = False
    for t in new_tasks:
        if isinstance(t, dict):
            t = {**t, "status": "pending"}
        merged["tasks"].append(t)
    return merged, errors


def revise_plan(model, prompts: dict[str, str], *, request: str, plan: dict, spec: str, plan_status: str,
                verdict: dict, files: str, round_no: int, max_attempts: int = 3, max_tasks: int = 15,
                log: Callable[[dict], None] | None = None, allowed: set[str] | None = None) -> tuple[dict, dict]:
    """Turn judge feedback into plan changes. Returns (new plan, change record)."""
    retryable = ", ".join(t["id"] for t in plan["tasks"] if t["status"] in RESETTABLE) or "(none: add new tasks)"
    active = sum(1 for t in plan["tasks"] if t["status"] != "dropped")
    message = (prompts["revise_request"]
               .replace("{retryable}", retryable)
               .replace("{slots}", f"{active} of {max_tasks * 2}")
               .replace("{request}", request).replace("{spec}", spec.strip())
               .replace("{plan_status}", plan_status)
               .replace("{feedback}", verdict.get("feedback") or "(none)")
               .replace("{problems}", "\n".join(f"- {p}" for p in verdict.get("problems", [])) or "(none)")
               .replace("{files}", files).replace("{prefix}", f"R{round_no}-"))
    messages = [{"role": "user", "content": message}]
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        raw = model.complete(prompts["revise_system"], messages)
        messages.append({"role": "assistant", "content": raw if raw.strip() else "(empty reply)"})
        try:
            obj = _parse(raw, ("changes", "retry", "tasks", "drop"))
        except ParseError as e:
            errors = [f"no JSON object in the reply ({e})"]
        else:
            merged, errors = _apply_revision(plan, obj)
            if not errors:
                dropped = merged.pop("_dropped", [])
                for t in merged["tasks"]:
                    if t["id"] in dropped:
                        t["dropped_in"] = round_no
                new_plan, errors = validate_plan(merged, max_tasks * 2)
                known = {t["id"] for t in plan["tasks"]}
                errors = errors or command_problems([t for t in new_plan["tasks"] if t["id"] not in known], allowed)
                if not errors:
                    new_plan["clarifications"] = plan.get("clarifications", [])
                    record = {"round": round_no, "changes": str(obj.get("changes", "")),
                              "retry": [x for x in (obj.get("retry") or []) if x in known and x not in dropped],
                              "dropped": dropped,
                              "added": [t["id"] for t in new_plan["tasks"] if t["id"] not in known]}
                    if log:
                        log({"revision": round_no, "attempt": attempt, "raw": raw, "ok": True})
                    return new_plan, record
        if log:
            log({"revision": round_no, "attempt": attempt, "raw": raw, "errors": errors})
        messages.append({"role": "user", "content": prompts.get("revise_fix", prompts["planner_fix"]).replace(
            "{errors}", "\n".join(f"- {e}" for e in errors))})
    raise PlanError(f"no valid revision after {max_attempts} attempts: " + "; ".join(errors))


def revision_section(record: dict, verdict: dict, plan: dict) -> str:
    added = [t for t in plan["tasks"] if t["id"] in record["added"]]
    why = "**Why (fix requested by a person):**" if verdict.get("source") == "human" else "**Why:**"
    lines = [f"## Revision {record['round']}", "", f"{why} {verdict.get('feedback', '').strip()}", "",
             f"**Changes:** {record['changes'] or '(not described)'}", ""]
    if record["retry"]:
        lines += [f"**Retried:** {', '.join(record['retry'])}", ""]
    if record.get("dropped"):
        lines += [f"**Dropped (replaced or no longer needed):** {', '.join(record['dropped'])}", ""]
    if added:
        lines += ["| ID | Task | Depends on | Done when |", "|---|---|---|---|"]
        for t in added:
            lines.append(f"| {t['id']} | {t['title']} | {', '.join(t['depends_on']) or '—'} | {_done_when(t['done_when'])} |")
        lines.append("")
    return "\n".join(lines) + "\n"
