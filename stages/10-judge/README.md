# Stage 10 — Judge: does it match the request?

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 09: Verification & fix loop](../../stages/09-verification/README.md) · [Index](../../README.md) · [Part 2: Reference chapters →](../../README.md#part-2--reference-chapters)

## Goal

Add a separate LLM pass that compares the finished project against the request and spec, and decides what happens next.

## Problem it solves

Tests can pass on an app that ignores half the request.

## What gets built

- Judge rubric in JSON: requirement coverage, check results, code quality, run instructions
- Verdicts: `accept`, `revise` (back to planner with feedback), `escalate` (hand to a human with a report)
- Judge wired as the exit of the graph, using check results as input (and sensor signals, once the Sensors chapter is in place)
- Delivery on `accept`: README with how to run, final summary

## Rules

- The judge is separate from the builder's reasoning
- Revise rounds are bounded
- Hard evidence (failing checks) can't be overruled by the judge's opinion

## Stays untouched

- Loop shape (judge sits on the exit edge)
- Adapter interface

## Done when

- [x] A build that skips a requested feature gets `revise` with that feature named
- [x] Every run ends with a recorded verdict and, on accept, a README

## How to run

```bash
python main.py build "Build a Python CLI to-do app with add/list/done and pytest tests"
cat runs/<id>/REPORT.md            # verdict, requirements, tasks, checks, how to run
python main.py build --no-judge "…"   # Stage 09 behaviour
python -m unittest discover -s tests -v   # 137 tests, no model needed
```

## Verified

The complete Part 1 pipeline ran on the request from Stage 05 (*"Build a Python CLI to-do app with add/list/done and pytest tests"*):

1. The planner made 3 tasks.
2. **T2's tests had a bug** (a new task expected to be `[x]`). The check failed with `exit 1`, and one fix attempt made it pass.
3. The final checks passed. The **judge found that `done` was missing** (2 of 3 requirements met) and gave feedback for `revise`.
4. The reviser added `R1-1` (the `done` command plus a test) and `R1-2` (docs). Both passed their checks, and `SPEC.md` gained a `## Revision 1` section.
5. The judge's second round met 4 of 4 requirements: `accept`, status `accepted`, exit 0, and `REPORT.md` written.
6. The delivered app works: `3 passed`, and `add`, `list` and `done` behave correctly.

The tests also cover:
- A lenient judge that accepted while tasks had failed was overruled to `revise`. The reviser retried the failed task, the task it had blocked was unblocked, and both finished.
- When revision rounds ran out, the result became `escalate`, and `REPORT.md` shows the unmet requirement and the last feedback.
- A judge that never produces valid JSON leads to `escalate` ("the judge could not produce a valid verdict").
- `--no-judge` behaves exactly as Stage 09. The earlier stages' tests run this way.

### Known limits

- The judge uses **the same model** as the builder. Giving it a different provider in `config.json` would make it more independent; that's a small follow-up.
- The judge sees at most `evidence_chars` of code. On bigger projects it judges from a partial view, and the evidence marks what was left out. Chapter C (context management) is where selecting what to show gets smarter.
- The summary's `model_calls` and token totals count **task** calls only. Planner, judge and reviser calls are logged in `planner.jsonl` and `judge.jsonl` but not added up. (Chapter A's `main.py trace` shows all of them, by role.)
- A revision can't reopen a `done` task. It adds a new task instead. That's deliberate, since it keeps the history of what passed.

## Commit

```
stage 10: judge — evidence-based verdict, hard rules, revise via planner, escalate + REPORT.md
```

## Leads to

Part 1 is complete: a request goes in, a checked app comes out. From here, read the Part 2 chapters as problems show up.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `judge.py` | new | `collect_evidence()`, `make_verdict()`, `apply_rules()`, `render_report()` |
| `planner.py` | + `revise_plan()` | Turns judge feedback into plan changes: retry tasks and/or add new ones |
| `pipeline.py` | changed | New `judge` and `revise` nodes; `final_check` routes to `judge`; `REPORT.md` is written at `finish` |
| `state.py` | + `verdicts`, `revisions` | |
| `prompts.json` | + `judge_system`, `judge_request`, `judge_fix`, `revise_system`, `revise_request` | |
| `config.json` | + `judge.enabled`, `max_revisions` (2), `max_attempts` (2), `evidence_chars`, `file_chars` | |
| `main.py` | changed | `--no-judge`; the summary shows the verdict; new statuses and exit codes |
| `loop.py`, `graph.py`, `checks.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### The graph (end of Part 1)

```
… next_task ⇄ run_task → verify … ─▶ final_check ─┬─ judge off ─────────────────────▶ finish
                                                  └─▶ judge ─┬─ accept ─────────────▶ finish
                                                        ▲    ├─ escalate ───────────▶ finish
                                                        │    └─ revise ─▶ revise ─▶ next_task …
                                                        └──────────── (next round) ──────┘
```

### Evidence the judge sees

The judge makes **one call with no tools**, and it is a separate role from the builder. It sees:

1. the request, and the current `SPEC.md`
2. every task with its status, hand-off note and last check (command, exit code)
3. the final check results
4. the workspace file list, and the **contents** of the text files. Each file is cut at `file_chars` (12,000) and the total at `evidence_chars` (60,000). Binary files and skipped folders are left out, and anything cut is marked

### Verdict format

```json
{
  "verdict": "accept | revise | escalate",
  "requirements": [
    {"requirement": "list tasks", "met": true, "evidence": "todo.py: cmd_list(); test_list passes"}
  ],
  "problems": ["README does not say how to run the tests"],
  "feedback": "what to change (required for revise)",
  "summary": "one paragraph for the requester"
}
```

An invalid reply is sent back with the problems listed, for up to `max_attempts` calls in total. If there's still no valid verdict, the result is `escalate` ("the judge could not produce a verdict").

### Hard rules (the harness applies these after the judge answers)

The judge's opinion never outranks evidence:

| The judge says `accept`, but… | The harness changes it to |
|---|---|
| a task is `failed` or `blocked` | `revise` |
| a final check failed | `revise` |
| a requirement in its own list has `met: false` | `revise` |
| the requirements list is empty | `revise` (an accept must show its evidence) |

Every `revise`, whether the judge chose it or the rules did, becomes `escalate` once `max_revisions` rounds have been used. Every override is recorded, with the reason, in `verdict.overrides`.

### Revise

`revise_plan()` makes one planner call with **its own prompt**. The call sees the request, the spec, the plan with statuses, the judge's feedback and problems, and the workspace files. It returns:

```json
{"changes": "one line on what will change",
 "retry": ["T2"],
 "tasks": [{"id": "R1-1", "title": "…", "description": "…", "files": [], "depends_on": ["T1"], "done_when": {…}}]}
```

- `retry`: `failed`/`blocked` tasks go back to `pending`. Their fix attempts reset, and their check history is kept. Tasks blocked by a retried task become `pending` too.
- `tasks`: new tasks, added to the end. Their ids must not clash with existing ids, and they may depend on any task.
- The combined plan is checked with the same `validate_plan()` rules (at most `2 × max_tasks` tasks), with retries on problems.
- A `## Revision N` section (changes, feedback, new tasks) is **added to the end** of `SPEC.md`, so human edits above it are kept.

### Report (`runs/<id>/REPORT.md`, written at `finish` for every judged build)

The report contains:
- the request and the verdict
- the judge's summary
- a requirements table (met or not, with evidence)
- problems and any overrides
- the tasks with their status and checks
- the final checks, and the file list
- **how to run**, taken from the workspace `README.md` if there is one

For an `escalate`, the report is the hand-off to a human.

### Statuses and exit codes

| Status | Meaning | Exit |
|---|---|---|
| `accepted` | judge accepted, and every rule held | 0 |
| `escalated` | judge escalated, revisions ran out, or no valid verdict | 3 |
| `finished` / `partial` | `--no-judge` (Stage 09 behaviour) | 0 / 2 |

### Out of scope

Human-in-the-loop answers to an escalation (the report is where that starts). Scoring several judges against each other. Judging `--no-plan` builds, which have nothing to check against.

---

[← Stage 09: Verification & fix loop](../../stages/09-verification/README.md) · [Index](../../README.md) · [Part 2: Reference chapters →](../../README.md#part-2--reference-chapters)
