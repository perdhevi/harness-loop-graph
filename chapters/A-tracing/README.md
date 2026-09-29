# Chapter A — Tracing

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[Index](../../README.md) · Chapter B: Memory →

## Read this when

You can't tell what happened in a run, or you're debugging with print statements.

## Needs

Stage 02 (ReAct loop). Richer once Stage 07 (graph & state) exists.

> Useful from Stage 02 onward — the one chapter worth reading early.

## Goal

Emit structured traces for every loop step, node transition and tool call, including MCP calls.

## What gets built

- Trace event format (node, task, timestamp, state diff, tool + server, tokens, latency)
- Pretty stdout renderer
- Trace hooks around the model, the tool registry, each graph node and each loop step (as wrappers: the loop itself is not edited)

## Rules

- Tracing is a side channel; removing it changes no behaviour
- Built in-house rather than adopting an observability platform

## Stays untouched

- Loop shape
- All node interfaces

## Done when

- [x] Any build can be read step by step from its trace
- [x] Token use, latency and tool time per task are visible

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `tracing.py` | new | `Tracer` (writes events), `NullTracer`, `TracingModel`, `TracingTools`, `wrap_node()` |
| `trace_view.py` | new | Reads `trace.jsonl` and prints a timeline plus totals |
| `pipeline.py` | changed | `Context` gets a `tracer`; `run_pipeline` wraps the model, the registry, the step callback and every node; `verify` and `judge` each emit one event |
| `main.py` | changed | New `trace` subcommand: `python main.py trace runs/<id> [--steps]` |
| `config.json` | + `trace.enabled` (true) | |
| `loop.py`, `graph.py`, `build.py`, `checks.py`, `judge.py`, `planner.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Where the hooks attach

Everything is wrapped **from outside**, so no stage's code changes shape:

```
run_pipeline
 ├─ ctx.model      → TracingModel(model)        every model call: role, latency, chars → ~tokens, error
 ├─ ctx.registry() → TracingTools(registry)      every tool call: tool, server, latency, ok/error, sizes
 ├─ ctx.on_step    → tracer.step + original      every loop step: n, action, parse error, final
 └─ graph nodes    → wrap_node(name, fn)          node start/end: duration, state diff, error
```

`TracingModel` doesn't know *who* is calling it (planner, task loop, judge, reviser). The tracer keeps track of the current node and task, so each model call is tagged with them: a model call during `run_task` for T2 is a task call for T2, and one during `judge` is a judge call.

### Event format (`runs/<id>/trace.jsonl`, one JSON object per line)

Every event has:

| Field | Meaning |
|---|---|
| `type` | `session`, `node_start`, `node_end`, `model`, `tool`, `step`, `check`, `verdict` |
| `ts` | wall-clock time (UTC, ISO) |
| `ms` | milliseconds since this session started |
| `session` | 1 for the first run, and +1 after each `--resume` |
| `node`, `task` | where it happened (may be null) |

Additional fields by type:

| Type | Fields |
|---|---|
| `session` | `run_id`, `resumed_at` (the node), `pid` |
| `node_end` | `duration_ms`, `diff` (changed small state fields, e.g. `{"status": ["planned", "building"]}`), `error` |
| `model` | `duration_ms`, `chars_in`, `chars_out`, `approx_tokens_in`, `approx_tokens_out`, `error` |
| `tool` | `tool`, `server` (the prefix before the `.`, or `local`), `duration_ms`, `ok` (false if the observation starts with `Error:`), `args_chars`, `result_chars` |
| `step` | `n`, `action`, `parse_error`, `final` (bool) |
| `check` | `kind`, `target`, `ok`, `exit_code`, `phase` (`verify` / `final`) |
| `verdict` | `verdict`, `met`, `total`, `overrides` |

The state diff leaves out the large fields (`plan`, `spec`, `summary`, `history`, `verdicts`) and records only that they changed.

### Viewer (`python main.py trace runs/<id>`)

```
session 1  (20260929-…)
  0.00s  intake          3 ms
  0.00s  plan          1.20 s   model×1 ~1.9k→0.3k tok
  1.21s  next_task       2 ms
  1.21s  run_task  T1  4.10 s   model×3 ~5.2k→0.4k  tools×2 (workspace 2)
  5.31s  verify    T1    90 ms  check ✓ python -m pytest -q
  …
by task   T1  4.2 s  model 3.9 s  tools 0.2 s  ~5.2k→0.4k tok  3 steps
by tool   workspace.write_file ×3  12 ms avg … workspace.run_command ×4  310 ms avg
by role   task 11 calls 12.3 s · planner 1 · judge 2 · reviser 1
slowest   model  2.1 s  judge (round 1)
          tool   1.8 s  workspace.run_command `python -m pytest -q` (T2)
```

`--steps` also lists every loop step under its node.

### The side-channel guarantee

- The wrappers pass arguments and results through unchanged. Exceptions (including `KeyboardInterrupt`) are recorded, then **re-raised**.
- Writing an event never raises. If the trace file can't be written, the tracer switches itself off and the build carries on.
- `trace.enabled: false` uses `NullTracer`, and nothing is wrapped.
- Tested: the same scripted build with tracing on and off produces the same `plan.json` and the same summary, apart from the durations.

### Out of scope

Real token counts (the adapter returns text only; ~ counts are characters ÷ 4). Question mode (`main.py "…"`) isn't traced; it's for quick checks. Exporting to OpenTelemetry or similar: the event format is plain enough to convert later.

## How to run

```bash
python main.py build "…"                 # tracing is on by default (config: trace.enabled)
python main.py trace runs/<id>           # timeline + totals by task, tool and role
python main.py trace runs/<id> --steps   # also every loop step and tool call
python -m unittest discover -s tests -v  # 145 tests, no model needed
```

## Verified

A full replay of the Stage 10 to-do build, with made-up model delays, produced 126 events. The viewer showed:
- the whole story on one screen: T2's failed check (`✗ python -m pytest -q`), the fix attempt, the ✓, the judge's `revise 2/3`, the reviser, the two new tasks, and the `accept 4/4`
- **per task:** time, model time, tool time, ~tokens and steps. T2 took the longest (2.41 s) because of its fix round
- **per tool:** `run_command` ×12, 1.8 s in total. Most of that is the harness's own pytest checks, which is worth knowing when a test suite is slow
- **per role:** planner, task, judge and reviser calls separately. This fills the Stage 10 gap, where the summary counted task calls only

The tests also check:
- every node has a matching start and end event, and every model call is tagged with the right role and task
- `--steps` shows parse errors
- after Ctrl-C, the trace has the interrupted model call (`KeyboardInterrupt`), and `--resume` opens **session 2** "resumed at run_task"
- a `kill -9`, which leaves a node with no end, shows as "session ended inside a node"
- **side channel:** the same scripted build with tracing on and off gives the same summary and `plan.json`, apart from timings
- an unwritable trace file turns tracing off and never stops the build

### Known limits

- The token counts are estimates (characters ÷ 4). Real counts would need the adapter to return the provider's usage numbers, which is a change to `model_adapter.py` for another day.
- A tool call is written to the trace just before the loop step that made it (the step is recorded after its observation). With `--steps` they appear in that order.
- Question mode (`main.py "…"`) isn't traced.

## Commit

```
chapter A: tracing — trace.jsonl from wrapped model/tools/nodes/steps; main.py trace viewer
```

---

[Index](../../README.md) · Chapter B: Memory →
