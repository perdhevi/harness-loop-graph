# Stage 09 — Verification & fix loop

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 08: Task-by-task execution](../../stages/08-task-execution/README.md) · [Index](../../README.md) · Stage 10: Judge: does it match the request? →

## Goal

After each task — and at the end — run real checks (tests, build, lint, smoke run) and feed failures back into the task until it passes or runs out of attempts.

## Problem it solves

"Done" is the model's opinion. Broken code gets marked finished.

## What gets built

- Verify node that runs each task's `done_when` command
- Final checks for the whole project: every task's check is run again at the end, to catch a later task breaking an earlier one
- Failure output fed back into the task loop as an observation
- Bounded fix attempts per task

## Rules

- Checks are run by the harness, not by trusting the model's report
- Evidence (exit codes, test results) outranks opinion everywhere from here on

## Stays untouched

- Loop shape
- Task execution flow (verify sits after it)

## Done when

- [x] A task with a deliberately broken test gets fixed and re-verified
- [x] A task that can't be fixed stops at its attempt limit and is marked `failed` with the last error
- [x] The finished to-do app passes its full test suite, run by the harness

## How to run

```bash
python main.py build "word and line counter CLI with tests"
# watch for [check] ✓/✗ lines and "fix attempt n/2"
cat runs/<id>/plan.json            # every check, with exit code and output, per task
python -m unittest discover -s tests -v   # 121 tests, no model needed
```

## Verified

- **The Stage 05 lesson, closed:**
  - The model wrote `stats.py` with a bug and said *"all tests pass"*. The harness ran `python -m pytest -q` and got `exit 1`, with `FAILED test_stats.py::test_lines - assert 3 == 2`.
  - T1 went back in fix mode. The fix prompt contained the claim, the command, the exit code and the pytest output.
  - The model fixed `count()`, the check passed, T2 ran, and the final checks passed. Result: `finished`.
- **A broken test file:** in an earlier run, my scripted test file had a syntax error. pytest exited with code 2 because it couldn't collect the tests. The model claimed success three times. The harness refused all three, failed T1 after 2 fix attempts, and blocked T2.
- **Regression:** T2 rewrote a module so that T1's test failed. Each task had passed its own check at the time, but `final_check` caught T1 and the build ended `partial` with `regression in T1`.
- **Refused commands:** a `done_when` of `ls`, which isn't on the allow-list, is a failed check that says why. It can never pass.
- **Resuming a fix attempt:** after Ctrl-C during a fix attempt, the resumed prompt contains both the fix context and the resume note.

### A bug this stage found (fixed in the workspace server)

With verification running right after edits, one test kept failing after a correct fix. The cause was Python's bytecode cache: `__pycache__/*.pyc` counts as fresh when the source file's modification time (whole seconds) and size match. The fix changed `a - b` to `a + b` (same size) within the same second, so Python kept running the **old** code.

The workspace server now runs every command with `PYTHONDONTWRITEBYTECODE=1`. `test_same_size_edit_is_not_hidden_by_bytecode_cache` fails without that line and passes with it. In the step-by-step history, that line is already part of Stage 04's server, where it belongs.

### Known limits

- The checks are only as good as the plan's `done_when`. `{"file": "README.md"}` proves the file exists, not that it's any good. Stage 10 (the judge) looks at whether the result matches the request.
- Commands in `done_when` run with the workspace server's allow-list, so a plan that needs another tool (e.g. `make`) fails its checks until that tool is allowed in `mcp.json`.

## Commit

```
stage 9: harness-run checks — verify node, fix attempts, final regression checks
```

## Leads to

The code runs and the tests pass — but nobody has checked it against what the requester actually asked for.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `checks.py` | new | `run_check()`: runs a `done_when` and returns `ok`, `exit_code`, `output` |
| `pipeline.py` | changed | New `verify` and `final_check` nodes; `run_task` also handles fix attempts |
| `prompts.json` | + `fix_request`; `task_rules` gets one extra line | |
| `config.json` | + `verify.max_fix_attempts` (2), `verify.timeout_s` (120), `verify.output_chars` (3000) | |
| `main.py` | small change | The summary shows check results; "not verified" appears only with `--no-plan` |
| `loop.py`, `graph.py`, `planner.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### The graph

```
next_task ─┬─▶ run_task ─▶ verify ─┬─ passed, or failed with no attempts left ─▶ next_task
           │       ▲               └─ failed, attempts left ───────────┐
           │       └───────────────────────── fix ◀────────────────────┘
           └─ nothing left ─▶ final_check ─▶ finish
```

### How a check runs (`checks.py`)

| `done_when` | How it's checked |
|---|---|
| `{"file": "README.md"}` | The harness checks that the file exists in the workspace, using the same path rules as the planner |
| `{"command": "python -m pytest -q"}` | It's run through **the workspace MCP server's `run_command`**, called by the harness, not the model. The same allow-list, no-shell rule and timeout apply. It passes only on `exit code: 0` |

A refused or broken command (a program that isn't allowed, a timeout, a missing server) is a **failed check** with the reason as its output. A check can't pass by accident.

### Task statuses (updated)

`run_task` no longer marks a task `done`:

| The loop… | `run_task` sets | then `verify`… |
|---|---|---|
| gave a final answer | `in_progress`, and saves the answer as the hand-off note | runs the check |
| ran out of steps | `failed` | does nothing |

`verify` then does one of three things:
- **The check passes:** the task becomes `done` with `verified: true`.
- **It fails, and there are attempts left:** `fix_attempts` goes up by one, and the task goes back to `run_task` in **fix mode**.
- **It fails, and there are no attempts left:** the task becomes `failed`, with `error: "check failed after N fix attempts: <command> → exit 1"`.

Every check is recorded on the task in `checks: [{attempt, ok, kind, target, exit_code, output}]`, with the output shortened to `output_chars`.

### Fix mode

In fix mode, `run_task` starts a **fresh** loop. Its message is the normal task prompt plus `fix_request`, which contains:
- what the model claimed in its hand-off note
- the check the harness ran and its exit code
- the end of the check's output
- "fix attempt N of M"

`task_rules` gets one new line: *"The harness runs the done-when check itself after you finish. Saying the task is done does not make it done."*

A resumed task is now spotted with an `attempt_open` flag. It's set when `run_task` starts and cleared when it returns. A resumed **fix** attempt still carries its fix context.

### Final checks (`final_check`)

Once no task is left to run, the checks of all `done` tasks are **run again**. Identical commands run only once. This catches a later task breaking an earlier one, for example T3 renaming a function that T2's tests import. The results go into the summary as `final_checks`.

| Status | When |
|---|---|
| `finished` | every task is `done` **and** every final check passes |
| `partial` | a task is `failed` or `blocked`, **or** a final check fails (a regression) |

### Out of scope

Deciding whether the app matches the *request* (Stage 10, the judge). Stricter checks than `done_when` (lint, coverage). Checking builds made with `--no-plan`, which have no `done_when` and stay "not verified".

---

[← Stage 08: Task-by-task execution](../../stages/08-task-execution/README.md) · [Index](../../README.md) · Stage 10: Judge: does it match the request? →
