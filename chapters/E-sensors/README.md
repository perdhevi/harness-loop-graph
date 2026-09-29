# Chapter E — Sensors

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter D: Compaction](../../chapters/D-compaction/README.md) · [Index](../../README.md) · Chapter F: Persistence & benchmark →

## Read this when

Tasks go in circles: rewriting the same file, hitting the same test failure, making no progress until they run out of steps.

## Needs

Stage 08 (task execution), Stage 09 (verification). Feeds Stage 10 (judge).

## Goal

Add watchers that observe the build and write signals into state without changing it.

## What gets built

- Repeated-edit detector (same file rewritten again and again)
- Same-failure detector (identical test error N times)
- No-progress detector (steps without file changes or new passing checks)
- Scope-drift detector (task touching files outside its plan)
- Repeated-action detector (the exact same tool call again and again)
- A combined `stuck` score, and three consumers that *read* the signals: a console warning, the judge's evidence, and a note in fix prompts

## Rules

- Sensors only observe and write signals; they never change control flow themselves
- Signals are scores, not booleans

## Stays untouched

- Loop shape
- Tools
- Context builder

## Done when

- [x] A looping task raises a visible signal before hitting its limit
- [x] Signals are available in state, and the judge (Stage 10) can use them

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `sensors.py` | new | `TaskSensors` (observes steps and checks), the detectors, the score curves, `describe()` |
| `pipeline.py` | changed | Feeds each task loop's steps and each check to the task's sensors; saves `task["signals"]`; warning, trace, fix-prompt note |
| `judge.py` | small change | `tasks_evidence()` includes a task's signals when they're notable |
| `planner.py` | small change | `signals` and `sensor_state` are kept when a plan is re-validated |
| `prompts.json` | + `sensor_note` | |
| `config.json` | + `sensors` section | |
| `main.py` | small change | The summary marks tasks that looked stuck |
| `loop.py`, `tools/*`, `context.py`, `compaction.py`, `graph.py`, `model_adapter.py` | **untouched** | |

### What the sensors observe

The inputs are things the harness already has: each **loop step** (action, args, observation), through the step callback like the tracer, and each **check result** from `verify`. Sensors are fed per task. Their raw counters (`sensor_state`) are saved on the task in `plan.json`, so they **add up across fix attempts and resumes**: the same failure in attempt 1 and in the fix attempt counts twice.

| Detector | Counts | Score (0 → 1, smooth) |
|---|---|---|
| `repeated_edit` | writes and edits to the same file | `1 − 0.5^((k−2)/2)`: 2 writes → 0, 4 → 0.5, 6 → 0.75 |
| `same_failure` | the same error signature (Chapter B's `error_signature`) from a failing command or check | `1 − 0.5^(r−1)`: once → 0, twice → 0.5, 3 times → 0.75 |
| `no_progress` | steps since the last **progress** event | `1 − 0.5^((n−3)/4)`: 3 steps → 0, 7 → 0.5, 11 → 0.75 |
| `repeated_action` | identical tool calls (same tool, same args) | `1 − 0.5^((a−1)/1.5)`: once → 0, 3 times → 0.6 |
| `scope_drift` | writes to files outside the task's `files` list (if it has one) | share outside × `(1 − 0.5^outside)` |

**Progress** is one of these:
- a file's content becoming something **it hasn't been before** (by hash)
- a command that failed before now passing
- a passing check

Writing the same bad content again isn't progress.

**Combined score**, a weighted noisy-OR, so several weak signals add up without going over 1:

```
stuck = 1 − (1 − 1.0·same_failure)(1 − 0.9·no_progress)(1 − 0.8·repeated_action)(1 − 0.6·repeated_edit)
drift = scope_drift     (kept separate: drifting isn't the same as being stuck)
```

### Where the signals live

`task["signals"]` in `plan.json` holds the current scores, the `peak_stuck`, and the reasons in words (e.g. `same failure ×3`, `9 steps without progress`). The same signals appear in the summary's task table.

### The rule: sensors don't steer

`TaskSensors` only counts and scores. It never stops a loop, skips a task or changes a status. Three **consumers** read the signals, each clearly separate:

| Consumer | What it does |
|---|---|
| Console + trace | The first time a task's `stuck` passes `warn_at` (0.5): `[sensor] T2 looks stuck (0.68): same failure ×3; 7 steps without progress`. The trace gets a `signals` event per attempt and one at the warning |
| Judge evidence | Tasks with `peak_stuck` or `drift` ≥ 0.3 get a `signals:` line in `tasks_evidence`. The judge sees it and decides; there's no hard rule |
| Fix prompt | If `same_failure` > 0 when a task goes back for a fix, `sensor_note` is added: *"This exact failure has now happened N times. Try a different approach instead of repeating the last one."* |

A test checks the rule: the same scripted build with sensors on and off ends with the same task statuses and the same verdict.

### Config

```json
"sensors": {"enabled": true, "warn_at": 0.5, "evidence_at": 0.3}
```

With `enabled: false`: no signals, no warning, no note.

### Out of scope

- Acting on signals automatically, such as stopping a task early. That would be a new consumer, and it belongs with Chapter F's benchmark numbers, so the threshold is set by evidence rather than by guessing.
- Sensors for the planner and the judge.

## How to run

```bash
python main.py build "…"                   # on by default (config: sensors.enabled)
# watch for:  [sensor] T2 looks stuck (0.65): same failure ×2; same tool call ×2
cat runs/<id>/plan.json                    # task["signals"]: scores, peak_stuck, reasons
python -m unittest discover -s tests -v   # 194 tests, no model needed
```

## Verified

**CLI, a task that goes in circles:** T1 kept rewriting the same broken `calc.py` and running the same failing test.
- **The warning came early:** `[sensor] T1 looks stuck (0.65): same failure ×2; same tool call ×2` fired at about step 4. T1 hit its 20-step limit much later.
- **At the end,** every detector was high: `same failure ×9; 18 steps without progress; same tool call ×10; calc.py written ×10`, with `stuck` at 1.00.
- **The judge's evidence** had the `signals:` line for T1 and none for the blocked T2. The judge escalated.
- **The summary** marks T1 `⚠ looked stuck 1.00`.
- **The trace** shows the scores rising: 0.65 at the warning, then 1.00 at the end of the attempt.

The tests also check:
- **The curves** hit the documented points, rise smoothly and stay below 1. Two weak signals add up in the noisy-OR without going past 1.
- **Progress:** writing the **same content again isn't progress**, new content is, and a command that failed before and now passes is too.
- **Error matching:** the same error from different paths counts as the same failure.
- **Persistence:** the counters carry across attempts (a failure in the loop plus the same failure at the check gives `happened 2 times` in the fix prompt).
- **The rule:** the same scripted build with sensors on and off gives **the same statuses, steps and verdict**. Only the warning, the signals and the note differ.

### Known limits

- `warn_at` of 0.5 fires at the second repeat of a failure. That's early and sometimes noisy, since two identical failures in a row are normal while debugging. The warning is informational, so the cost is low. Chapter F's benchmark is where to tune it.
- Signals don't act on their own. Stopping a stuck task early would save steps, but that's a new consumer, and its threshold should come from benchmark data.
- The error matching is word-based (from Chapter B). Two different errors made of the same words would count as the same failure.

## Commit

```
chapter E: sensors — five smooth detectors + noisy-OR stuck score; warning, judge evidence and fix-prompt note as consumers
```

---

[← Chapter D: Compaction](../../chapters/D-compaction/README.md) · [Index](../../README.md) · Chapter F: Persistence & benchmark →
