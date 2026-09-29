# Chapter C — Context management

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter B: Memory](../../chapters/B-memory/README.md) · [Index](../../README.md) · [Chapter D: Compaction →](../../chapters/D-compaction/README.md)

## Read this when

Prompts grow too large, or are filled with whole files and long test logs that have nothing to do with the current task.

## Needs

Stage 07 (graph & state). Works best after Chapter B (memory), since memory is one of the budgeted sections.

## Goal

Assemble each prompt from state, memory and the relevant files within a token budget.

## What gets built

- Context builder with named sections
- Fractional budget per section (spec, plan, hand-offs, file map, relevant files, fix output, lessons); the task itself is never cut
- Relevant-file selection by score
- Long tool output truncated with a pointer back to the full text

## Rules

- Budgets are fractions of the total, not fixed counts
- The builder is the only place **task** prompts are assembled (planner and judge keep their own limits; see Out of scope)

## Stays untouched

- Loop shape
- Adapter interface

## Done when

- [x] Prompt size stays under budget on a project with 20+ files
- [x] Each section's share is visible in the trace

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `context.py` | new | `estimate_tokens()`, `allocate()`, `shrink()`, `rank_files()`, `relevant_files()`, `OutputLimiter` |
| `pipeline.py` | changed | `run_task` builds its first message through the builder; `Context.registry()` adds the `OutputLimiter`; a `context` trace event |
| `prompts.json` | changed | `task_request` gets `{relevant_files}`; the fix, lessons and resume blocks become budgeted sections |
| `config.json` | + `context` section (see below) | |
| `trace_view.py` | small change | `--steps` shows how each task's prompt was split up |
| `loop.py`, `graph.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Two problems, two parts

| Problem | Part |
|---|---|
| A task's **first message** grows with the project: spec, plan, hand-offs, map, fix output, lessons | **Budgeted sections** plus **relevant files** |
| **Tool results** can be huge: `read_file` up to 100 KB, `run_command` up to 2 × 8 KB, and every one stays in the history | **Output limiter** with a pointer back to the full text |

(A loop's growing history is Chapter D.)

### Budget

```
first_message_budget = window_tokens × first_message_fraction      e.g. 16000 × 0.4 = 6400 tokens ≈ 25,600 chars
```

Tokens are estimated as characters ÷ 4, the same estimate the rest of the harness uses.

### Sections and their shares

| Section | Share | If cut, keeps | Notes |
|---|---|---|---|
| `task` | — | never cut | id, title, description, files, done-when |
| `request` | 0.05 | head | |
| `spec` | 0.15 | head | |
| `plan_status` | 0.05 | head | |
| `handoffs` | 0.15 | **tail** | the most recent notes matter most |
| `files` (map) | 0.10 | head | |
| `relevant_files` | 0.30 | per file | see below |
| `fix` | 0.12 | **tail** | the end of test output is where the failure is |
| `lessons` | 0.05 | head | |
| `resume` | 0.03 | head | |

### Allocation (`allocate()`): water-filling, no hard cut-offs

1. Required sections (`task`) are subtracted from the budget first.
2. Each other section is offered `share / (sum of shares still in play) × budget left`.
3. Sections that need **less** than their offer take what they need, and **the surplus is shared out again** among the rest, by the same shares.
4. Step 3 repeats until nothing changes. Sections still asking for more than their offer are cut to it.

So on a small project nothing is cut. On a big one, the cuts land on the big sections in proportion to their shares, never to zero.

### Cutting (`shrink()`)

| Strategy | Keeps |
|---|---|
| `head` | the start |
| `tail` | the end |
| `middle` | the start and the end |

A cut is always marked, e.g. `[… 12,345 chars cut from spec]`.

### Relevant files (new section)

Showing current file contents saves the model from spending steps on `read_file`. Every text file in the workspace is **scored** (`rank_files()`):

```
score = 1.0   if the task lists it in "files"
      + 0.8   if its name appears in the fix output (traceback, pytest failure)
      + 0.5 × relevance(task title + description + fix output, file content)   TF-IDF cosine (from Chapter B)
```

Files with a score above zero are added best-first. Each is cut (`middle`) to at most half of the section's budget, so one huge file can't crowd out the rest. Files that don't fit are listed by name.

### Output limiter (`OutputLimiter`, wraps the registry)

- A result longer than `max_observation_chars` (4000) is saved in full to `runs/<id>/outputs/0007.txt`. The model gets the start and the end, plus a pointer:

  ```
  [output 0007 cut: showing the first 1500 and last 1500 of 23,456 chars.
   Call harness.read_output with {"id": "0007", "offset": 1500} to read more.]
  ```

- **`harness.read_output(id, offset?, limit?)`** is a new tool that reads slices of saved outputs. It is always available when the limiter is on.
- Checks (Stage 09) go through the same registry. Their exit code is on the first line, which a cut never removes.
- Tracing wraps outside the limiter, so the trace records what the model actually saw.

### Visible in the trace

`run_task` emits a `context` event per attempt, with the budget and each section's `wanted`, `given` and `cut` characters, plus the relevant files shown. `main.py trace --steps` prints it as:

```
context  6400 tok budget · used 3120 · cut: fix 1,204 chars · files shown: todo.py, test_todo.py
```

### Config

```json
"context": {"enabled": true, "window_tokens": 16000, "first_message_fraction": 0.4,
            "max_observation_chars": 4000,
            "shares": {"request": 0.05, "spec": 0.15, "plan_status": 0.05, "handoffs": 0.15, "files": 0.10,
                       "relevant_files": 0.30, "fix": 0.12, "lessons": 0.05, "resume": 0.03}}
```

Set `window_tokens` to match your model. qwen3:8b in Ollama defaults to a much smaller context window than the model supports, so check `num_ctx`.

With `enabled: false`, everything behaves as in Chapter B: no limiter and no budget.

### Out of scope

- The loop's growing history (Chapter D).
- The planner and judge prompts, which keep their own limits (the judge has `evidence_chars`). Moving them onto the builder is a follow-up.
- Real tokenizers.

## How to run

```bash
python main.py build "…"                       # on by default (config: context.enabled)
python main.py trace runs/<id> --steps         # one "context …" line per task attempt
ls runs/<id>/outputs/                          # full text of every cut tool output
python -m unittest discover -s tests -v       # 174 tests, no model needed
```

Set `context.window_tokens` to your model's real context size.

## Verified

- **CLI, 22 modules, window of 4,000 tokens (1,517-token budget):** T2's first message used 1,498 tokens. The trace line shows what was cut: `files 1,075 chars, relevant_files 80 chars`. The four modules T2 needs were shown in full (`u03`, `u08`, `u13`, `u18`). The smaller sections (plan, hand-offs, task) were untouched.
- **Output limiter:** `python report.py` printed 38,047 characters. The model saw 3,849: the start, the end (including `REPORT DONE`), and the pointer. `harness.read_output {"id": "0001", "offset": 20000}` returned exactly that slice, and the full text is in `outputs/0001.txt`.
- **Tests:**
  - **Allocation:** a small section's unused share goes to the big one (500 + 400 → 900); cuts follow the shares (60/20/20 of 3,000 gives 1,800/600/600); required sections are never cut.
  - **With 25 files:** T2's prompt stays within 6,400 characters and within the budget per the trace.
  - **Fix prompts:** the end of a 4,000-line failure (`THE REAL FAILURE`) survives the cut.
  - **Context off:** Chapter B behaviour.

### Problems this chapter found (and fixed)

| Where | Problem | Fix |
|---|---|---|
| Workspace server (Stage 04) | Long output was cut to its **first** 8 KB, dropping the end, which is where errors and test summaries are | It now keeps the start **and** the end. The per-stream cap went up to 100 KB, since the limiter now decides what the model sees |
| `relevant_files()` | With one relevant file, the "half the section per file" cap left half the room unused | A single file may use the whole section |
| `relevant_files()` | The "not shown: …" line wasn't counted, so the budget overshot by about 70 chars | Room for that line is reserved |
| `harness.read_output` | An offset past the end returned an empty slice | It now returns a clear error |

### Known limits

- **History inside a task's loop still grows.** The trace shows T2's four calls at about 11k tokens in total. Keeping that in check is Chapter D (compaction).
- Planner and judge prompts aren't budgeted by the builder yet.
- Checks also go through the limiter. Their full output is saved in `outputs/` too, which can mean several copies of the same test run.
- Token counts are estimates (characters ÷ 4).

## Commit

```
chapter C: context budget — fractional sections (water-filling), relevant files, output limiter + harness.read_output
```

---

[← Chapter B: Memory](../../chapters/B-memory/README.md) · [Index](../../README.md) · [Chapter D: Compaction →](../../chapters/D-compaction/README.md)
