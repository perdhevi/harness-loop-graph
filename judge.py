"""Stage 10 — the judge: does the finished project match the request?

A separate, tool-less model call reads the evidence (request, spec, tasks,
checks, the workspace's text files) and returns a JSON verdict. The harness
then applies hard rules: evidence (failed tasks, failed checks, unmet
requirements) always outranks the judge's opinion.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from build import SKIP_DIRS
from loop import ParseError, _first_json_object

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
VERDICTS = ("accept", "revise", "escalate")


class JudgeError(RuntimeError):
    pass


# ---------------------------------------------------------------- evidence

def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def workspace_contents(workspace: Path, *, file_chars: int = 12000, total_chars: int = 60000) -> str:
    """Text files of the project, each cut at file_chars, all together at total_chars."""
    parts, used, skipped = [], 0, []
    for p in sorted(workspace.rglob("*")):
        rel = p.relative_to(workspace)
        if not p.is_file() or any(x in SKIP_DIRS for x in rel.parts):
            continue
        data = p.read_bytes()
        if not _is_text(data):
            skipped.append(f"{rel.as_posix()} (binary, {len(data)} bytes)")
            continue
        text = data.decode("utf-8", errors="replace")
        if len(text) > file_chars:
            text = text[:file_chars] + f"\n… [cut: {len(text) - file_chars} more chars]"
        block = f"----- {rel.as_posix()} -----\n{text}\n"
        if used + len(block) > total_chars:
            skipped.append(f"{rel.as_posix()} (left out: evidence limit reached)")
            continue
        parts.append(block)
        used += len(block)
    if skipped:
        parts.append("----- not shown -----\n" + "\n".join(skipped) + "\n")
    return "".join(parts) or "(the workspace is empty)"


def tasks_evidence(plan: dict) -> str:
    lines = []
    for t in plan["tasks"]:
        lines.append(f"{t['id']} {t['title']} — {t['status']}")
        if t.get("handoff"):
            lines.append(f"    hand-off: {t['handoff']}")
        if t.get("checks"):
            c = t["checks"][-1]
            code = "" if c.get("exit_code") is None else f", exit {c['exit_code']}"
            lines.append(f"    last check: {c['kind']} {c['target']} → {'pass' if c['ok'] else 'FAIL'}{code}")
        if t.get("error"):
            lines.append(f"    error: {t['error']}")
    return "\n".join(lines)


def final_checks_evidence(final_checks: list | None) -> str:
    if not final_checks:
        return "(none)"
    seen = {}
    for c in final_checks:
        seen.setdefault((c["kind"], c["target"]), c)
    return "\n".join(f"{'pass' if c['ok'] else 'FAIL'}: {c['kind']} {t}" for (_, t), c in seen.items())


# ---------------------------------------------------------------- verdict

def validate_verdict(obj) -> tuple[dict, list[str]]:
    errors = []
    if not isinstance(obj, dict):
        return {}, ["the verdict must be a JSON object"]
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"'verdict' must be one of {', '.join(VERDICTS)}")
    reqs = obj.get("requirements", [])
    if not isinstance(reqs, list):
        errors.append("'requirements' must be a list")
        reqs = []
    clean_reqs = []
    for i, r in enumerate(reqs, 1):
        if not isinstance(r, dict) or not isinstance(r.get("requirement"), str) or not isinstance(r.get("met"), bool):
            errors.append(f"requirement #{i} needs 'requirement' (string) and 'met' (true/false)")
            continue
        clean_reqs.append({"requirement": r["requirement"], "met": r["met"], "evidence": str(r.get("evidence", ""))})
    problems = obj.get("problems", [])
    if not isinstance(problems, list) or not all(isinstance(p, str) for p in problems):
        errors.append("'problems' must be a list of strings")
        problems = []
    feedback = obj.get("feedback", "")
    if verdict == "revise" and (not isinstance(feedback, str) or not feedback.strip()):
        errors.append("'feedback' is required when the verdict is revise")
    summary = obj.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("'summary' must be a non-empty string")
    return {"verdict": verdict, "requirements": clean_reqs, "problems": problems,
            "feedback": feedback if isinstance(feedback, str) else "", "summary": summary if isinstance(summary, str) else ""}, errors


def make_verdict(model, prompts: dict[str, str], evidence: dict[str, str], *, max_attempts: int = 2,
                 log: Callable[[dict], None] | None = None) -> dict:
    """Ask the judge. Returns a validated verdict, or an escalate verdict if it never produces one."""
    message = prompts["judge_request"]
    for key, value in evidence.items():
        message = message.replace("{" + key + "}", value)
    messages = [{"role": "user", "content": message}]
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        raw = model.complete(prompts["judge_system"], messages)
        messages.append({"role": "assistant", "content": raw if raw.strip() else "(empty reply)"})
        try:
            obj = _first_json_object(_THINK_RE.sub("", raw or ""))
        except ParseError as e:
            errors = [f"no JSON object in the reply ({e})"]
        else:
            verdict, errors = validate_verdict(obj)
            if not errors:
                if log:
                    log({"attempt": attempt, "raw": raw, "ok": True})
                return verdict
        if log:
            log({"attempt": attempt, "raw": raw, "errors": errors})
        messages.append({"role": "user", "content": prompts["judge_fix"].replace(
            "{errors}", "\n".join(f"- {e}" for e in errors))})
    return {"verdict": "escalate", "requirements": [], "problems": [f"the judge could not produce a valid verdict: {'; '.join(errors)}"],
            "feedback": "", "summary": "No valid verdict from the judge; a human needs to review this build.",
            "judge_failed": True}


def apply_rules(verdict: dict, plan: dict, final_checks: list | None, *, revisions_used: int,
                max_revisions: int) -> dict:
    """Evidence outranks opinion. Returns the verdict with any overrides recorded."""
    v = dict(verdict)
    overrides = []
    if v["verdict"] == "accept":
        reasons = []
        bad_tasks = [t["id"] for t in plan["tasks"] if t["status"] in ("failed", "blocked")]
        if bad_tasks:
            reasons.append(f"tasks not done: {', '.join(bad_tasks)}")
        bad_checks = sorted({c["target"] for c in (final_checks or []) if not c["ok"]})
        if bad_checks:
            reasons.append(f"final checks failed: {', '.join(bad_checks)}")
        unmet = [r["requirement"] for r in v["requirements"] if not r["met"]]
        if unmet:
            reasons.append(f"requirements marked not met: {'; '.join(unmet)}")
        if not v["requirements"]:
            reasons.append("accept listed no requirements as evidence")
        if reasons:
            overrides.append({"from": "accept", "to": "revise", "reason": "; ".join(reasons)})
            v["verdict"] = "revise"
            v["feedback"] = (v.get("feedback") or "") + ("\n" if v.get("feedback") else "") + \
                "Harness: " + "; ".join(reasons)
    if v["verdict"] == "revise" and revisions_used >= max_revisions:
        overrides.append({"from": "revise", "to": "escalate",
                          "reason": f"no revision rounds left ({revisions_used}/{max_revisions} used)"})
        v["verdict"] = "escalate"
    v["overrides"] = overrides
    return v


# ---------------------------------------------------------------- report

def render_report(*, request: str, run_id: str, status: str, verdict: dict | None, plan: dict,
                  final_checks: list | None, files: list[dict], readme: str | None, revisions: int) -> str:
    L = [f"# Build report — {plan['title']}", "", f"- **Run:** `{run_id}`", f"- **Request:** {request}",
         f"- **Status:** **{status}**", f"- **Revision rounds:** {revisions}", ""]
    if verdict:
        L += ["## Verdict", "", f"**{verdict['verdict']}** — {verdict.get('summary', '')}", ""]
        for o in verdict.get("overrides", []):
            L.append(f"> Harness override: {o['from']} → {o['to']} ({o['reason']})")
        if verdict.get("overrides"):
            L.append("")
        if verdict.get("requirements"):
            L += ["## Requirements", "", "| Requirement | Met | Evidence |", "|---|---|---|"]
            for r in verdict["requirements"]:
                L.append(f"| {r['requirement']} | {'✅' if r['met'] else '❌'} | {r['evidence']} |")
            L.append("")
        if verdict.get("problems"):
            L += ["## Problems", ""] + [f"- {p}" for p in verdict["problems"]] + [""]
        if verdict.get("feedback") and verdict["verdict"] != "accept":
            L += ["## Feedback", "", verdict["feedback"], ""]
    L += ["## Tasks", "", "| ID | Task | Status | Last check |", "|---|---|---|---|"]
    for t in plan["tasks"]:
        c = t.get("checks", [])[-1:] or [None]
        c = c[0]
        check = "—" if not c else f"{'✅' if c['ok'] else '❌'} `{c['target']}`"
        L.append(f"| {t['id']} | {t['title']} | {t['status']} | {check} |")
    L += ["", "## Final checks", "", final_checks_evidence(final_checks), "",
          "## Files", ""] + [f"- `{f['path']}` ({f['bytes']} bytes)" for f in files] + [""]
    L += ["## How to run", ""]
    L += [readme.strip(), ""] if readme else ["(no README.md in the workspace)", ""]
    return "\n".join(L)
