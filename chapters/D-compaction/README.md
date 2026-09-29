# Chapter D — Compaction

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter C: Context management](../../chapters/C-context-management/README.md) · [Index](../../README.md) · Chapter E: Sensors →

## Read this when

Long tasks forget what they already tried, or history gets cut off once it no longer fits.

## Needs

Chapter C (context management) — compaction is triggered by its token budget.

## Goal

Summarise or trim older turns when the budget fills, so tasks and builds can run long.

## What gets built

- Compaction trigger based on budget pressure
- Summariser that replaces old turns with a summary (what was tried, what failed, current state)
- Record of what was compacted

## Rules

- Triggered by a pressure fraction, not a single hard threshold
- Recent turns and the task's goal are never compacted

## Stays untouched

- Loop shape
- Context builder interface

## Done when

- [x] A task that needs 30+ steps finishes without losing its goal or repeating old attempts
- [x] Compaction events appear in the trace

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `compaction.py` | new | `CompactingModel` (a model wrapper), `digest()` (extractive summary), optional model-written summary |
| `pipeline.py` | changed | `run_task` and `build` wrap the model per loop; a `compaction` trace event and a `[compact]` line |
| `prompts.json` | + `compact_note`, `compact_system` | |
| `config.json` | + `compaction` section (see below) | |
| `trace_view.py` | small change | Compactions shown in the timeline and in `--steps` |
| `loop.py`, `context.py`, `graph.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Decision: compact in a model wrapper, not in the loop

The loop owns its `messages` list and passes it to `model.complete(system, messages)` on every step. `CompactingModel` sits in that call:
- The loop's own list keeps growing untouched. It is the full record, and the transcript has it too.
- **What is sent** to the model is a compacted view of that list.

This is the same outside-in approach as tracing (Chapter A), so the loop stays exactly as it was in Stage 02.

```
loop messages:  [task prompt] [a1] [o1] [a2] [o2] … [a9] [o9] [a10] [o10]
sent view:      [task prompt + summary of steps 1–8]              [a9] [o9] [a10] [o10]
                 ▲ never compacted                                 ▲ most recent steps, verbatim
```

- The **task prompt** (the first message) is never compacted, so the goal is never lost. The summary is added to its end under `compact_note`.
- The **most recent `keep_recent_steps`** (assistant reply + observation pairs) are always sent word for word.
- The cut always falls **before an assistant message**, so the view keeps the user/assistant alternation that the Anthropic API requires.

### When it compacts: pressure fractions, not a fixed step count

```
call_budget = window_tokens × call_fraction              (0.85: the rest is left for the reply)
pressure    = estimated tokens(system + view) / call_budget
```

- **Trigger:** `pressure > next_trigger`, which starts at `compact_at` (0.75).
- **Target:** after compacting, `pressure ≤ target` (0.5), or as close as it can get while keeping the recent steps.
- **Hysteresis, relative to where compaction landed:** afterwards, `next_trigger = min(ceiling, max(compact_at, pressure_after + (compact_at − target)))`. Compaction then waits for the same growth gap before running again, even when the fixed part of the call (system prompt, task prompt, summary, recent steps) keeps the target out of reach. The `ceiling` (0.95) keeps calls inside the budget.

**Incremental:** the wrapper remembers how far its summary reaches (`upto`). Each compaction only moves that point forward over newly aged steps.
- **Extractive mode** rebuilds **one** digest over all compacted steps. It's cheap, and the digest is **bounded**: the last 12 actions, 8 failures, 10 commands and 4 thoughts, plus a count of what was left out. So the summary can't grow without limit.
- **Model mode** is truly incremental: the model gets the previous summary plus only the new steps.

### The summary (`digest()`, extractive: no model call, the default)

Built from the compacted steps themselves:

```
Steps 1–8 were compacted. What happened:
- Tried: write_file todo.py; run_command `python -m pytest -q` (exit 1); edit_file todo.py; …
- Failed:
    run_command `python -m pytest -q` → exit 1: FAILED test_todo.py::test_done - assert 1 == 2
    read_file todo_old.py → Error: workspace.read_file failed: McpToolError: no such file
- Files written or edited: todo.py, test_todo.py
- Last result of each command: `python -m pytest -q` → exit 1; `python todo.py list` → exit 0
- Thoughts, most recent last: "done() never saves"; "fix the index"
```

"What was tried", "what failed" and "current state" come straight from the actions and observations. It's cheap, deterministic and testable, and it can't make things up.

**`mode: "model"`** asks the model instead (`compact_system`), giving it the extractive digest plus the raw steps, and uses its reply as the summary. If that call fails or returns nothing, the extractive digest is used. The call is traced like any other, with the role `compactor`.

### Record of what was compacted

- **Trace event `compaction`:** task, steps covered (`from`–`to`), messages and estimated tokens before and after, and mode.
- **Console:** `[compact] T2: steps 1–8 summarised (9.1k → 3.2k tok)`.
- The full, uncompacted history is still in `transcript.jsonl`.

### Config

```json
"compaction": {"enabled": true, "call_fraction": 0.85, "compact_at": 0.75, "target": 0.5, "ceiling": 0.95,
               "keep_recent_steps": 2, "mode": "extractive"}
```

It uses `context.window_tokens` from Chapter C. With `enabled: false`, the full history is sent, as before.

### Out of scope

Compacting planner and judge conversations (they're short). Compacting the first message (Chapter C's budget handles that).

## How to run

```bash
python main.py build "…"                     # on by default (config: compaction.enabled)
python main.py trace runs/<id> --steps       # "compacted×N" per task, and one "compact steps a–b" line each
python -m unittest discover -s tests -v     # 183 tests, no model needed
```

## Verified

**CLI, a 34-step task in a 3,000-token window** (call budget 2,550 tokens):
- 9 compactions, each covering 3–4 steps.
- Every call stayed within budget: the first was 812 tokens, the peak 2,180 and the last 1,997. Without compaction, the last call would have been about 7,000.
- The **task prompt was in every call**.
- The early failure (step 4: `python scan_all.py` → exit 2, file doesn't exist) was **still in the summary at step 34**. That's what stops a model from trying it again.
- `transcript.jsonl` still has all 34 steps word for word.

The tests also check:
- the user/assistant alternation stays intact
- the 2 most recent steps are sent word for word
- only newly aged steps are compacted
- below the trigger, messages pass through unchanged
- a huge history with only "recent" steps isn't compacted
- model mode uses the model's summary, and falls back to the extractive one if the call fails
- a 300-step digest stays under 3,000 characters
- with compaction off, history grows past the budget

### Problems this chapter found (and fixed)

| Problem | Found by | Fix |
|---|---|---|
| Exit codes were never detected in the digest: the loop prefixes observations with `Observation: ` | test: the early failure missing from the summary | That prefix is stripped before the digest is built |
| The summary grew without limit: each compaction added another "Steps x–y" block, until compactions covered one step each | test (34 steps) | Extractive mode rebuilds **one bounded** digest over all compacted steps |
| **Thrashing:** in a small window the 0.5 target can't be reached, so every new step re-triggered compaction (23 compactions, 14 of them a single step) | the CLI run's trace | Hysteresis is relative to where compaction landed, with a ceiling |
| A failure without an error keyword showed only `exit code: 1` | test | Failure lines show `exit N:` plus the most informative line of the output |

### Known limits

- In **very small windows** the fixed part of a call (system prompt, tools, task prompt, summary) is most of the budget. The CLI run landed at about 0.67 after each compaction, so each one frees less. The real fix is a larger `window_tokens`, which Chapter C's note on `num_ctx` also covers.
- The extractive digest is literal. When every command is different (as in the log run), "Tried" and "Last result" repeat each other. Model mode writes a tighter summary but costs a model call per compaction.
- A digest line can contain long absolute paths from tool errors. They're cut to 160 characters.

## Commit

```
chapter D: compaction — CompactingModel view (goal + summary + recent steps), pressure hysteresis, bounded digest
```

---

[← Chapter C: Context management](../../chapters/C-context-management/README.md) · [Index](../../README.md) · Chapter E: Sensors →
