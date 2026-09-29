# Stage 06 — Planner: request → spec → tasks

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 05: Request → first build](../../stages/05-first-build/README.md) · [Index](../../README.md) · [Stage 07: Graph & state →](../../stages/07-graph-state/README.md)

## Goal

Before writing code, turn the request into a short spec and an ordered task list, saved in the run folder.

## Problem it solves

The loop improvises: it forgets requirements, writes files in a bad order, and can't tell how far along it is.

## What gets built

- Plan step that runs before the build loop
- Clarify: if the request is ambiguous, ask the requester (interactive) or record assumptions (non-interactive)
- `runs/<id>/SPEC.md` — what will be built: features, tech choices, constraints, out of scope, assumptions
- `runs/<id>/plan.json` — tasks with `id`, `title`, `files`, `depends_on`, `done_when`, `status`
- `--review` flag to stop after planning so a human can edit the plan

## Rules

- Spec before code — the harness follows the same rule this project does
- The plan is data in the run folder, editable by a human, never hidden in the prompt
- Every task has a checkable `done_when` (a command or a file that must exist)

## Stays untouched

- The ReAct loop
- Tools and MCP client

## Done when

- [x] The to-do app request produces a readable SPEC.md and a valid plan
- [x] An invalid plan is sent back to the model with the errors and fixed, or the run stops with a clear message
- [x] An ambiguous request ("make me a website") triggers questions or written-down assumptions
- [x] Building from an edited plan uses the edits

## How to run

```bash
python main.py build --review "make me a tip calculator"   # plan, then stop
# read / edit runs/<id>/SPEC.md and plan.json
python main.py build --from-run runs/<id>                   # build from that plan
python -m unittest discover -s tests -v                    # 95 tests, no model needed
```

## Verified

- **Vague request, nobody at the terminal:**
  - `build --review "make me a tip calculator"` got two questions back from the planner. With no terminal, it was told to assume.
  - The planner returned a 3-task plan that recorded the assumptions ("CLI, not web", "default tip is 10%"), and both are listed in `SPEC.md`.
  - The planner put T3 before T2 (which T3 depends on); the harness reordered them.
  - `planner.jsonl` holds both attempts.
- **Build from the reviewed plan:** `--from-run` built the project task by task within one loop. The model ran each `done_when` command before moving on, and the result works (`python tip.py 120 15 3` prints `Each person pays 46.00`). Running `--from-run` on the same folder again is refused.
- **Edits are used:** in the tests, a title changed in `plan.json` and a note added to `SPEC.md` both show up in the build prompt. A broken edit (a task that depends on itself, invalid JSON) stops the run before anything is built.
- **Retries:** invalid plans go back to the model with every problem listed. After `max_attempts` the run stops with `status: plan_failed`.

Not tested end to end: typing answers into a real terminal. The code path is the same `ask` function the unit tests drive.

## Commit

```
stage 6: planner — validated JSON plan, SPEC.md, clarify/assume, --review/--from-run
```

## Leads to

A plan exists, but the build still runs as one long loop. Stage 07 gives the pieces a shared state so they can be run as a graph.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `planner.py` | new | `make_plan()`, `validate_plan()`, `render_spec()`, `render_tasks()`, `save_plan()`, `load_plan()` |
| `main.py` | changed | Plans before building; new flags `--yes`, `--review`, `--from-run`, `--no-plan` |
| `prompts.json` | + `planner_system`, `planner_request`, `planner_fix`, `planner_answers`, `build_with_plan` | All planner wording, as data |
| `config.json` | + `plan.max_tasks`, `plan.max_attempts`, `plan.max_questions` | |
| `build.py` | small change | The summary also records the plan's title and task count |
| `loop.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Decision: the plan lives in the run folder, not the workspace

The Stage 06 outline said `workspace/`. It moved to `runs/<id>/` for three reasons: the workspace is the requester's project and shouldn't ship harness files, the model can't quietly rewrite its own plan with `write_file`, and it sits next to `request.json` where a reviewer expects it.

### Flow

```
request ─▶ planner call ─▶ questions? ──yes──▶ ask requester (or "no answers — assume") ─▶ planner call
                 │                                                                            │
                 ▼                                                                            ▼
          validate JSON ◀────────────── fix message with the errors (up to max_attempts) ─────┘
                 │ ok
                 ▼
     write plan.json + SPEC.md ─▶ --review? stop here : build with the plan in the prompt
```

- **The planner is a single model call with no tools.** It returns one JSON object, and the harness validates it and writes the files. The model never writes the plan files itself.
- **Clarify:**
  - If the request is too vague to plan, the planner may return `{"questions": [...]}` (at most `max_questions`). There is one round of questions.
  - **Interactive** (a terminal is attached, no `--yes`): the questions are shown and the answers are sent back.
  - **Non-interactive:** the planner is told "no answers are available; make reasonable assumptions and list them".
  - Questions and answers are saved in `plan.json` under `clarifications`.
- **Validation errors** go back to the model as a message listing every problem, for up to `max_attempts` calls in total. Every attempt, including the raw reply and the errors, is logged in `planner.jsonl`.

### Plan format (what the planner returns)

```json
{
  "title": "CLI to-do app",
  "summary": "One paragraph on what gets built.",
  "features": ["add a task", "list tasks", "mark a task done"],
  "tech": ["Python 3 standard library", "pytest for tests"],
  "constraints": ["single-user, local JSON file"],
  "out_of_scope": ["sync", "priorities"],
  "assumptions": ["tasks are stored in todo.json next to the script"],
  "tasks": [
    {
      "id": "T1",
      "title": "Storage layer",
      "description": "load/save tasks in todo.json",
      "files": ["todo.py"],
      "depends_on": [],
      "done_when": {"command": "python -c \"import todo\""}
    },
    {
      "id": "T4",
      "title": "README",
      "description": "how to run and test",
      "files": ["README.md"],
      "depends_on": ["T1"],
      "done_when": {"file": "README.md"}
    }
  ]
}
```

### Validation rules

- `title` and `summary` must be non-empty strings. The list fields must be lists of strings, and missing lists become `[]`.
- There must be between 1 and `max_tasks` tasks.
- Task `id`s must be unique, non-empty strings.
- Every `depends_on` entry must name an existing task, and there must be **no cycles**. Tasks are stored in dependency order, keeping the planner's order where possible.
- `files` must hold relative paths inside the workspace: no absolute paths and no `..`.
- `done_when` must have exactly one of `command` (a non-empty string) or `file` (a relative path).
- The harness adds `"status": "pending"` to every task. Status only starts to change in Stage 08.

### `SPEC.md`

`SPEC.md` is generated from the plan (title, summary, features, tech, constraints, out of scope, assumptions, clarifications, and a task table). It's meant for humans. The build prompt uses **the file's current text**, so edits to `SPEC.md` count too.

### Building with a plan

The Stage 05 build rules stay in place. The first message becomes `build_with_plan`: the request, the text of `SPEC.md`, and the ordered task list with each task's `done_when`, with an instruction to work through the tasks in order. It's still **one loop for the whole build**; running each task separately is Stage 08.

### Command line

```bash
python main.py build "…"                   # plan, then build (asks questions if the request is vague)
python main.py build --yes "…"             # never ask; the planner records assumptions
python main.py build --review "…"          # plan only, then stop
python main.py build --from-run runs/<id>  # build from a reviewed (possibly edited) plan
python main.py build --no-plan "…"         # Stage 05 behaviour, for comparison
```

`--from-run` validates `plan.json` again. A broken edit is reported and nothing gets built.

### Out of scope

Per-task loops and task status (Stage 08), checking `done_when` (Stage 09), and re-planning after a failure (Stage 10).

---

[← Stage 05: Request → first build](../../stages/05-first-build/README.md) · [Index](../../README.md) · [Stage 07: Graph & state →](../../stages/07-graph-state/README.md)
