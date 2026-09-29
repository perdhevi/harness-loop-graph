# Chapter G — Fixing a finished run

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter F: Persistence & benchmark](../../chapters/F-benchmark-regression/README.md) · [Index](../../README.md)

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

---

[← Chapter F: Persistence & benchmark](../../chapters/F-benchmark-regression/README.md) · [Index](../../README.md)
