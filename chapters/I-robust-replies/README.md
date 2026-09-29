# Chapter I — Robust replies and plans

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with real replies from a failed run — confirm with a real model)

[← Chapter H: A model per role](../../chapters/H-role-models/README.md) · [Index](../../README.md)

## Read this when

Runs fail on the model's *format*, not on the work: steps end in `parse error`, a revision round escalates with *"no JSON object found"*, a task runs out of steps while saying "the task is complete" over and over, or `edit_file` is retried with the same text.

## Needs

Stage 02 (loop and replies), Stage 04 (workspace server), Stage 06 and Stage 10 (planner and reviser).

## Goal

Make small models reliable by catching their common mistakes early, repairing the obvious ones and explaining the rest precisely, so the next attempt can succeed.

## Where this came from

A real run on a to-do CLI:

| What the log showed | Cause | Fix in this chapter |
|---|---|---|
| Round 6: `no JSON object found` three times with the **same** reply | `"done_when": {"python -c …"}` (a value with no key) made the whole reply invalid, and the error didn't say where | Keyless `done_when` is repaired to `{"command": …}`; any other JSON error says *line, column and a snippet with ⟨here⟩* |
| R6-1: `ran out of steps`, `"final": 16` in `tool_calls`, looked stuck 0.95 | The model finished with `{"action": "final", "args": {"final": …}}`, a call to a tool that doesn't exist | A call to `final/finish/answer/done` is read as a final answer, in every format: the JSON action, Gemma's `call:final{…}`, Qwen's `<function=final>` and JSON aliases (`final-action` in the trace); calling `final` as a tool explains how to finish |
| `edit_file` ×4 with the same `old` | `old` didn't match the file, and the error gave no clue | "not found" shows the closest lines with line numbers, and says when only whitespace differs |
| Checks like `python todo_cli.py list > /dev/null && echo ok` accepted | Commands run without a shell, so these can never pass | New plans and revisions reject `&&`, `\|\|`, `\|`, `>`, `<`, `;` (plans already saved still load) |
| `retry: unknown task 'R1-1'`, three attempts wasted | New task ids listed under `retry` too | They're dropped from `retry`; the reviser is told which tasks it can retry |
| R6-1's check was T1's check, copied | It already passed, so it could never show the new work | A new task can't reuse a *done* task's check (test-suite commands are allowed) |
| Rounds 1–5 on one failing test | The plan kept tasks in memory while `add` and `list` run as separate processes | Planner and reviser rule: state that must survive between commands goes to a file |

## What gets built

- `loop.py` — the parser accepts real line breaks inside strings, picks the first object with the expected keys, falls back to Python-style dicts, and reports *where* JSON broke (or that the reply was cut off). The planner, reviser and judge use it too.
- `replies.py` — `final-action`: `{"action": "final", …}` → a final answer
- `tools/registry.py` — calling `final`, `finish` or `answer` as a tool returns how to finish
- `servers/workspace_server.py` — `edit_file` "not found" shows the closest lines
- `planner.py` — `command_problems()`, keyless `done_when` repair, clearer `done_when` errors, `retry` clean-up, copied-check rule, the reviser's own retry prompt
- `prompts.json` — planner and reviser rules; `revise_request` lists retryable tasks; new `revise_fix`
- `config.json` — Anthropic `max_tokens` 1024 → 8192, so a file or a revision fits in one reply

## Rules

- Repair only what is unambiguous; anything else goes back to the model with a precise reason
- Plans saved by earlier runs keep loading; the stricter checks apply to new plans and new revision tasks
- The loop's shape is unchanged: every fix is in parsing, normalizing, validation or messages

## Done when

- [x] Round 6's real reply parses, and its shell-operator check is sent back with the reason
- [x] A model that ends with `{"action": "final"}` finishes in one step
- [x] `edit_file` misses show the lines to copy
- [x] Retry ids, copied checks and wrong `done_when` keys get specific feedback

## How to run

Nothing to switch on. To see the conversions in a run:

```bash
python main.py trace runs/<id>           # normalized×N (final-action, …) per task
python -m unittest discover -s tests -v   # 237 tests, no model needed
```

## Commit

```
chapter I: robust replies and plans — precise JSON errors, final-as-tool, edit_file hints, shell/retry/copied-check validation
```

---

[← Chapter H: A model per role](../../chapters/H-role-models/README.md) · [Index](../../README.md)
