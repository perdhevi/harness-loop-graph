"""Stage 6 — planner: request → spec → tasks.

One tool-less model call turns the request into a JSON plan. The harness
validates it (feeding errors back for another attempt), then writes
plan.json and a human-readable SPEC.md into the run folder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Callable

from loop import ParseError, _first_json_object

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
LIST_FIELDS = ("features", "tech", "constraints", "out_of_scope", "assumptions")

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
    elif len(raw_tasks) > max_tasks:
        errors.append(f"too many tasks ({len(raw_tasks)}); use at most {max_tasks}")

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
            errors.append(f"{where}: 'done_when' must have exactly one of 'command' or 'file'")
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
            "status": "pending",
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

def _parse(text: str) -> dict:
    return _first_json_object(_THINK_RE.sub("", text or ""))


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
) -> dict:
    """Call the planner until it returns a valid plan. Raises PlanError if it can't."""
    system = prompts["planner_system"].replace("{max_tasks}", str(max_tasks)) \
                                      .replace("{max_questions}", str(max_questions))
    first = prompts["planner_request"].replace("{request}", request)
    messages = [{"role": "user", "content": first}]
    clarifications: list[dict] = []
    asked = False
    last_errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        raw = model.complete(system, messages)
        messages.append({"role": "assistant", "content": raw if raw.strip() else "(empty reply)"})
        entry = {"attempt": attempt, "raw": raw}
        try:
            obj = _parse(raw)
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
