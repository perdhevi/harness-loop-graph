# Chapter G — Fixing a finished run

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter F: Persistence & benchmark](../../chapters/F-benchmark-regression/README.md) · [Index](../../README.md) · [Chapter H: A model per role →](../../chapters/H-role-models/README.md)

## Read this when

A build finished, but not the way you wanted: a feature is missing, something crashes, or the judge accepted too early. You want to change that run, not start over.

## Needs

Stage 10 (judge): the fix reuses its `revise` node. Stage 07 (graph & state) to re-enter a saved run.

## Goal

Feed a person's feedback into a finished run and take it through revise → tasks → checks → judge again.

## What gets built

- `main.py build --fix RUN_DIR "what to change"`
- `pipeline.py` — `human_verdict()`; `run_pipeline(fix=…)` re-enters the graph at `revise`; `finish` tells a person's fix apart from a judge verdict
- `state.py` — `revision_base`, so the judge's revision budget restarts after each fix
- `planner.py` — the `SPEC.md` revision section says when a person asked for it
- `tests/test_fix.py`

## Rules

- A fix is shaped like a judge verdict (`"source": "human"`), so the revise node, planner and graph stay unchanged
- Only finished runs with a plan can be fixed; `--resume` stays the way to continue a stopped run
- A fix that can't be planned changes nothing

## Done when

- [x] A fix adds or retries tasks, runs their checks and the judge, and rewrites `summary.json` and `REPORT.md`
- [x] The judge gets its full revision budget after a fix
- [x] Unfinished, unplanned and empty-feedback requests are refused with a clear message

## How to run

`--resume` only continues a run that stopped part-way. To change a run that already **finished** (accepted, escalated or partial), give it your feedback:

```bash
python main.py build --fix runs/<id> "list crashes on an empty todo.json; also add a --json flag"
```

The run re-enters the graph at `revise`, with your text in place of a judge verdict (`"source": "human"`). From there it is the normal path: the reviser turns the feedback into retried and/or new `R<n>-…` tasks, each task runs and is checked, the final checks run again, and the judge reviews the result (skip it with `--no-judge`). `SPEC.md` gets a `## Revision <n>` section marked *fix requested by a person*, and `summary.json` and `REPORT.md` are rewritten.

- The judge gets its full `max_revisions` budget again after each fix, so your request doesn't use up its rounds.
- If the reviser can't turn the feedback into a valid change, nothing is changed and the run ends `escalated` with the reason.
- `--fix` refuses runs that haven't finished (use `--resume` first) and runs built with `--no-plan` (there is no plan to revise).
- Each fix adds tasks. A plan can hold up to twice `plan.max_tasks`, so after many fixes, start a new run.

Tests: `tests/test_fix.py`. `python -m unittest discover -s tests -v` runs 210 tests, no model needed.

## Commit

```
chapter G: fixing a finished run — build --fix feeds a person's feedback through revise, tasks, checks and the judge
```


## Follow-up: re-running tasks (`build --rerun`)

**Read this when** a revision dropped a task that was never replaced, or a task failed, and you want that task built again in the same run: not a new plan, not a new run.

Revisions can only retry *failed* or *blocked* tasks, and only the reviser decides that. `--resume` refuses a finished run. So a dropped task (a real case: *T3, two-player mode*, dropped in round 1 and replaced by nothing) had no way back.

**What gets built**

- `planner.reopen_tasks(plan, ids, dependents=True)`: sets dropped, failed or blocked tasks back to `pending` (fix attempts, errors and `dropped_in` cleared, `reopened` counted). With `dependents`, unfinished tasks that depend on them come too.
- `run_pipeline(…, rerun=[ids])`: writes the plan, adds `## Re-run (requested by a person)` to SPEC.md and a `rerun` event to the transcript, then starts the graph at `next_task`, followed by the tasks, final checks and the judge.
- `main.py build --rerun RUN_DIR T3 [T5 …] [--no-dependents] [--no-judge]`
- The web UI (Chapter L): a **Re-run** button on each dropped, failed or blocked task row. It shows what it will reopen, or why it can't.

**Rules**

- Only finished runs (resume a stopped one first) and only dropped, failed or blocked tasks; finished work is changed with `--fix`.
- A task whose dependency is unfinished and not reopened with it is refused with a sentence saying which task to re-run instead: it would only be blocked again. A refusal changes nothing.
- The judge gets its full revision budget again, as after `--fix`. The re-run is recorded as a person's verdict (`"verdict": "rerun"`), so a run re-run without a judge is labelled by its checks, not by the judge's earlier verdict.

```bash
python main.py build --rerun runs/<id> T3          # T3 and the unfinished tasks after it; then judged again
python main.py build --rerun runs/<id> T3 --no-dependents
```

Tests: `tests/test_rerun.py` (7), and `RerunTests` in `tests/test_gui.py`. `python -m unittest discover -s tests -v` runs 298 tests at that point.

```
chapter G: re-run dropped, failed or blocked tasks — reopen_tasks, build --rerun, judged again; Re-run buttons in the web UI
```


## Follow-up: re-planning one task (`build --replan`)

**Read this when** a task is too big or wrong for a small model ("T3 ran out of steps three times") and you want it replaced by other tasks, such as *break it into smaller ones*, without writing a whole new plan.

`--fix` passes a comment to the reviser, but nothing ties that comment to one task. And dropping a task drops every unfinished task that depends on it, so splitting T3 would also drop T4–T6.

**What gets built**

- `run_pipeline(…, fix=comment, replan="T3")`: a person's fix request that names the task. The reviser is told to replace it, and the comment says how. With no comment, it asks for smaller tasks, each with its own check.
- `planner._apply_revision(…, replace="T3")`: the harness holds the reviser to it:
  - T3 is dropped. If the reviser left it out of `drop`, it's added: that repair is unambiguous.
  - At least one new task must be added. Retrying T3 instead is refused, and so is a new task that depends on T3.
  - Tasks that depended on T3 now depend on the new tasks that finish last (the new tasks no other new task depends on), so they aren't dropped with T3. Tasks dropped in an earlier round stay dropped; re-run them afterwards (`--rerun`).
- The SPEC.md revision section shows `**Re-planned:** T3 → R3-1, R3-2`.
- `main.py build --replan RUN_DIR T3 [comment …]`
- The web UI (Chapter L): a **Re-plan…** button on every unfinished task, with a box for the comment.

**Rules**

- Only finished runs, and only tasks that aren't done. To change finished work, use `--fix` with a comment.
- After the revision, the new tasks run, then the final checks and the judge, exactly as after `--fix`. The judge gets its full budget again.
- If no valid revision is found within the reviser's attempts, the run is escalated with the reason, as for `--fix`.

```bash
python main.py build --replan runs/<id> T3 split it into game logic, the two-player loop, and score saving
python main.py build --replan runs/<id> R2-1                 # "break it into smaller tasks"
```

Tests: `tests/test_replan.py` (8), and `RerunTests` in `tests/test_gui.py`. `python -m unittest discover -s tests -v` runs 307 tests.

```
chapter G: re-plan one task — build --replan replaces it with new tasks, dependents rewired; Re-plan… in the web UI
```

---

[← Chapter F: Persistence & benchmark](../../chapters/F-benchmark-regression/README.md) · [Index](../../README.md) · [Chapter H: A model per role →](../../chapters/H-role-models/README.md)
