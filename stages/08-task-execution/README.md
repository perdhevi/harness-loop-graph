# Stage 08 — Task-by-task execution

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 07: Graph & state](../../stages/07-graph-state/README.md) · [Index](../../README.md) · Stage 09: Verification & fix loop →

## Goal

Execute the plan one task at a time, each in its own ReAct loop with a fresh, task-scoped conversation.

## Problem it solves

One giant conversation mixes all tasks together; early mistakes and old file contents crowd out the current work.

## What gets built

- Task picker: next task whose dependencies are done
- Per-task loop with its own message history and iteration budget
- Task status written back to `plan.json` (`pending` → `in_progress` → `done` / `failed`, or `blocked` when a dependency failed)
- Hand-off note from each task for the ones that follow

## Rules

- The ReAct loop itself is reused unchanged — this stage only changes what goes in and out of it
- A task only sees the spec, its own task, the hand-off notes and the files it needs

## Stays untouched

- Loop shape
- Planner output format

## Done when

- [x] The to-do app is built as a series of tasks, each visible in `plan.json`
- [x] Resuming a run skips tasks already done
- [x] One failing task doesn't wipe out the progress of the others

## How to run

```bash
python main.py build "temperature converter CLI with tests"
# Ctrl-C during any task, then:
python main.py build --resume runs/<id>        # continues the interrupted task; done tasks are skipped
cat runs/<id>/plan.json                        # status, hand-off note and counts per task
python -m unittest discover -s tests -v       # 112 tests, no model needed
```

## Verified

- **Kill and resume through the CLI with 3 tasks:**
  - T1 finished, then a real SIGINT arrived during T2. `plan.json` showed `T1 done, T2 in_progress, T3 pending`, and the state had `next: run_task`.
  - `--resume` picked up at `run_task` for T2 (T1 wasn't run again), then ran T3. All three ended `done`, and the generated tests pass (`2 passed`).
- **Hand-offs and context:** T2's prompt contained T1's hand-off note, the plan with status marks (`[x] T1`, `[>] T2`, `[ ] T3`), and the files T1 created. Each task started a fresh conversation (one message), and all tasks shared **one** set of MCP servers.
- **Failure and blocking:** T1 ran out of its 3-step budget and became `failed`. T2, which depends on T1, became `blocked by T1` and never ran. T3, which is independent, still ran and became `done`. The build ended as `partial` (exit code 2).
- **Edits between runs:** a task renamed in `plan.json` after `--review` showed up in that task's prompt.
- **Updated tests:** the tests for Stages 06 and 07 were changed to match per-task execution. The graph node names changed, and replies are now supplied per task.

### Known limits

- An **interrupted attempt's numbers aren't counted**: the steps done before Ctrl-C don't reach the task's `steps` total. `transcript.jsonl` still has every step.
- **"Done" is still the model's word**, now for each task separately. A task whose `done_when` command would fail is marked `done` anyway. Stage 09 runs the checks.

## Commit

```
stage 8: task-by-task execution — next_task/run_task loop, task status + hand-offs in plan.json
```

## Leads to

Tasks get marked done because the model says so, not because anything was checked.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `pipeline.py` | changed | New `next_task` and `run_task` nodes that loop; `finish` totals up the tasks; one MCP registry kept open for the whole run |
| `build.py` | small change | `execute_build()` gets `label` (tags each transcript line with its task) and `write_summary` (off for single tasks) |
| `planner.py` | small change | `validate_plan()` keeps a task's `status` and its progress fields instead of resetting them to `pending`, so a resume remembers what's done |
| `state.py` | + `current_task` | |
| `prompts.json` | + `task_rules`, `task_request` | The prompt for one task |
| `config.json` | + `build.task_max_iterations` (default 20) | Step budget **per task** |
| `main.py` | small change | The summary prints a per-task table; `partial` status → exit code 2 |
| `loop.py`, `graph.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### The graph

```
intake ─▶ plan ─┬─ plan_failed ────────────────────────────────▶ finish ─▶ END
                ├─ no plan (--no-plan) ─▶ build (Stage 05–07) ─▶ finish
                └─ plan ─▶ next_task ─┬─ a task is ready ─▶ run_task ─┐
                              ▲       └─ nothing left ───▶ finish     │
                              └───────────────────────────────────────┘
```

`--review` now pauses before `next_task`. This is the first cycle in the graph, and `max_steps` (50) covers 12 tasks with room to spare.

### Task status lifecycle (saved in `plan.json` after every change)

```
pending ─▶ in_progress ─┬─▶ done      the task loop gave a final answer
                        └─▶ failed    it ran out of steps (task_max_iterations)
pending ─▶ blocked                    a dependency is failed or blocked
```

- **`next_task`** reloads `plan.json` from disk, so edits made between runs are used. It marks as `blocked` any pending task that depends on a failed or blocked task. It then picks the first task that is `in_progress` (being resumed) or `pending` with all dependencies `done`, and stores it in `state.current_task`.
- **`run_task`** sets the task to `in_progress`, saves the plan, and runs **one ReAct loop just for this task**. That loop has a fresh conversation, the per-task step budget, and each transcript line tagged `"task": "T2"`.
  - When the loop finishes, the task becomes `done`, and its final answer is saved as the task's **hand-off note**.
  - If it runs out of steps, the task becomes `failed` (with the reason), and the build moves on. Tasks that depend on it will become `blocked`.
  - A **model or server error** doesn't fail the task. It stops the run, and `state.next` stays at `run_task`, so a resume retries the same task.
  - Ctrl-C stops the run the same way. The task stays `in_progress`.
- Per-task numbers are **added up across attempts** and kept on the task: steps, malformed replies, tool calls, model calls, time, rough token count.

### What one task sees (`task_request`)

1. the request
2. the text of `SPEC.md`
3. the plan with every task's status (`[x]` done, `[>]` current, `[ ]` pending, `[!]` failed, `[-]` blocked)
4. the hand-off notes from finished tasks, in order
5. the files currently in the workspace, with their sizes
6. its own task: title, description, files, done-when
7. if it's being resumed, the `build_resume_note`

`task_rules` replaces `build_rules` for task loops:
- do only this task
- build on what's already in the workspace, and read files before changing them
- no `pip`/`npm install`
- make sure the done-when condition holds
- end with a short hand-off note: what was done, which files, and names or commands the next tasks need

### Final summary (`finish`)

| Status | When |
|---|---|
| `finished` | every task is `done` |
| `partial` | at least one task is `failed` or `blocked` |

The summary also has a `tasks` table (id, title, status, steps, error), totals added up across tasks, the workspace file list, and an `answer` made from the hand-off notes.

### Resume rules (updated)

| State on disk | Where the run continues |
|---|---|
| `next` is set | at `next`, for example `run_task` for the task that was running |
| no `next`, status `error` or `interrupted` | at `next_task` (or `build` with `--no-plan`) |
| `finished`, `partial`, `max_iterations`, `plan_failed` | refused |

Tasks that are already `done` are never run again.

### Out of scope

Checking `done_when` with a real command run (Stage 09). For now, "done" still means the model said so, one task at a time. Retrying failed tasks, and re-planning (Stage 10).

---

[← Stage 07: Graph & state](../../stages/07-graph-state/README.md) · [Index](../../README.md) · Stage 09: Verification & fix loop →
