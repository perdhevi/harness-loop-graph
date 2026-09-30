# Chapter J — Reviewing and fixing existing code

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted model and a sample project with a planted bug — confirm with a real model)

[← Chapter I: Robust replies and plans](../../chapters/I-robust-replies/README.md) · [Index](../../README.md)

## Read this when

You have a project the harness didn't build, and you want it reviewed, fixed or extended, without the harness touching your folder until you decide.

## Needs

Stage 10 (plan → tasks → checks → judge). Chapter B for the project map, Chapter C for large files, Chapter H for a separate reviewer model, Chapter I for the safety nets.

## Goal

Take an existing folder and a request, find the problems with evidence, fix them through the normal task loop, prove that nothing that worked before broke, and hand back a patch.

## How it runs

```
intake ─▶ survey ─▶ review ─▶ plan ─▶ next_task ⇄ run_task ⇄ verify ─▶ final_check ─▶ judge ─▶ finish
  │         │         │                                                  │                     │
  │         │         └─ REVIEW.md (read-only)                           │                     └─ CHANGES.patch
  │         └─ the project's own tests, before any change                └─ …and again: if they passed before, they must pass now
  └─ copies your project into runs/<id>/workspace and runs/<id>/original
```

- **intake** copies the project twice: `workspace/` is where tasks work, and `original/` stays untouched so the patch can be made against it. `.git`, `node_modules`, virtual environments, caches and build output are skipped; `review.ignore` adds your own patterns. Too many files or bytes stops the run with a clear message.
- **survey** runs the project's tests: `--test` if you give one, otherwise `npm test`, pytest or unittest, whichever the project looks like it uses.
- **review** is a ReAct loop for the new `reviewer` role, with **read-only** tools: it can read files and run commands, but `write_file` and `edit_file` answer "not available while reviewing". It ends with findings (file, line, severity, problem, evidence, suggested fix), saved as `REVIEW.md`. With `--only-review`, the run stops here.
- **plan** uses a planner prompt for existing code: change what the request and findings need, keep the rest, prefer checks that reproduce the bug, and don't use `{"file": …}` checks (the files already exist). The findings and the baseline are added to `SPEC.md`.
- **final_check** re-runs the project's tests. If they passed before, they're a regression check like any task's; if they failed before, the result is reported to the judge (before → after) but doesn't block.
- **finish** writes `CHANGES.patch`, a git-style diff from `original/` to `workspace/`, and adds a "Review and changes" section to `REPORT.md`.
- **apply** (a separate command, or `--apply`) copies the changes back into your folder. See below.

## Putting the changes back

```bash
python main.py apply runs/<id> --dry-run      # what would change: M changed, A added, D deleted
python main.py apply runs/<id>                # write them into the original folder
python main.py apply --undo runs/<id>         # put the folder back the way it was
python main.py review PATH "…" --apply        # review, fix, and apply in one go, if the run is accepted
```

- **Only runs that ended well**: `accepted`, or `finished` with `--no-judge`. `--force` applies any run you've checked yourself. With `--apply`, a run that didn't end well is left alone, and the summary says how to apply it later.
- **Only the files the run changed** are written, added or deleted. Everything else in your folder (`.git`, `.venv`, notes) is left alone.
- **No silent overwrites.** A file you changed after the review copied it is a conflict: nothing at all is written, and the conflicts are listed.
- **Backup first.** Every file that is changed or deleted is saved in `runs/<id>/backup/`. `--undo` restores them and removes the added files; it refuses if you edited those files again after apply (`--force` restores anyway).
- **Recorded.** `summary.json` gets an `applied` entry (when, where, which files), so a run isn't applied twice by accident.

## What gets built

- `apply.py` — `plan_apply()`, `apply_run()`, `undo_apply()`
- `review.py` — `import_project()`, `detect_tests()`, `ReadOnlyTools`, `parse_review()`, `render_review()`, `make_patch()`
- `pipeline.py` — `survey` and `review` nodes; `intake`, `plan`, `final_check`, `judge` and `finish` know about an existing project
- `main.py` — `review PATH "request" [--only-review] [--test CMD] [--no-judge] [--yes] [--apply]`; `apply RUN [--dry-run] [--undo] [--force]`; summary lines for the review, tests before/after and the patch
- `models.py`, `config.json` — the `reviewer` role; a `review` section (`max_iterations`, `map_chars`, `max_files`, `max_bytes`, `ignore`, `test_command`)
- `prompts.json` — `reviewer_rules`, `review_request`, `planner_system_existing`, `existing_notes`
- `state.py` — `baseline`, `baseline_after`, `review`
- `tests/review_sample/` (a project whose `average()` is off by one), `tests/test_review.py`, `tests/test_apply.py`

## Rules

- **Your folder is only read** during the run. Changes reach it only through `apply` (or `git apply CHANGES.patch`), and never over edits you made in the meantime
- The reviewer can't change files; a finding needs evidence (a failing test, an error it produced, the exact line)
- Tests that passed before must still pass
- Everything after `plan` is the normal pipeline, so `--fix`, `--resume`, the judge, per-role models and Chapter I's checks all work on review runs

## Done when

- [x] The sample's bug is found, fixed through a task with a real check, the project's tests go from failing to passing, and the judge accepts
- [x] `CHANGES.patch` applies with `git apply` to the original folder, and its tests then pass
- [x] The original folder is byte-for-byte unchanged after the run
- [x] A change that breaks tests that passed before makes the run `partial` with "regression in baseline"
- [x] `--only-review` writes `REVIEW.md` and changes nothing
- [x] `apply` writes only the run's files, refuses when you changed one of them since the import, backs up, and `--undo` restores the folder exactly

## How to run

```bash
python main.py review C:\path\to\project "list crashes on an empty todo.json"
python main.py review C:\path\to\project --only-review "check error handling"
python main.py review C:\path\to\project --test "python -m pytest -q tests" "…"   # when detection guesses wrong

# then, if you like the result:
python main.py apply runs\<id>

python -m unittest discover -s tests -v   # 262 tests, no model needed
```

`CHANGES.patch` is still written for every run, if you'd rather review the diff or apply it with `git apply` (which works in any folder, not only git repositories).

To use a stronger model for reading code, set the `reviewer` role in `config.json`, e.g. `"reviewer": {"model": "qwen3:8b", "num_ctx": 24576}`.

## Verified

Command-line run against the sample, with a fake Ollama server (reviewer and worker on one model, planner and judge on another):

```
[import] 3 files (596 bytes) from …/project — your folder is not changed
[survey] project tests before any change: ✗ `python -m unittest -q` → exit 1
[review] 1 finding(s): 1 high, 0 medium, 0 low → …/REVIEW.md
[plan] Fix average — 1 tasks
[check] T1 ✓ `python -m unittest -q test_stats` → exit 0
[final] project tests: ✓ `python -m unittest -q` → exit 0 (before: failed)
[judge] accept  (1/1 requirements met)
[changes] 1 file(s): 1 changed, 0 added, 0 deleted → …/CHANGES.patch
```

## Known limits

- Only programs on the workspace server's allow list can run (python, pip, pytest, node, npm). Other languages can be reviewed and edited, but not tested, so their fixes aren't verified.
- On a small model with a 16k window, point the review at a folder or a concrete problem. The reviewer reads files on demand, but its findings on a large codebase will be shallow.
- Tests that need a network, a database or packages that aren't installed will fail in the baseline too; the run reports that rather than guessing.

## Commit

```
chapter J: review and fix existing code — import a copy, baseline tests, read-only reviewer, CHANGES.patch
```

---

[← Chapter I: Robust replies and plans](../../chapters/I-robust-replies/README.md) · [Index](../../README.md)
