# Chapter F — Persistence & benchmark

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter E: Sensors](../../chapters/E-sensors/README.md) · [Index](../../README.md)

## Read this when

You changed the harness and can't tell whether it got better or worse.

## Needs

Stage 10 (judge) for verdicts. Much more useful with Chapter A (tracing).

## Goal

Save every run, and replay a fixed set of build requests to measure the harness and catch regressions.

## What gets built

- Run store: every run folder already keeps its trace, plan, verdict, report and workspace; each finished run now also adds one line to `runs/index.jsonl`
- `benchmarks/` — requests of rising difficulty, each with an acceptance command
- Replay command that reports pass rate, iterations, tokens and time against the last baseline

## Rules

- Comparison uses pass rates and score deltas, not exact text matches
- Baselines are versioned alongside the code
- The same benchmark runs against local and cloud models for comparison

## Stays untouched

- Everything in Part 1

## Done when

- [x] One command replays the benchmark and reports changes
- [x] A change that lowers pass rate is flagged

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `bench.py` | new | `load_suite()`, `run_acceptance()`, `run_suite()`, `compare()`, `render_results()`, `render_compare()` |
| `trace_view.py` | + `metrics()` | Per-role model calls and tokens, tool calls and errors, compactions and sensor warnings, all from `trace.jsonl` |
| `pipeline.py` | small change | `finish` adds `metrics` to the summary and appends a row to `runs/index.jsonl` |
| `main.py` | changed | New `runs` and `bench` subcommands; `run_build(config=…)` so a benchmark can override provider and model |
| `benchmarks/core.json` | new | The starter suite: 4 requests of rising difficulty |
| `benchmarks/accept/*.py` | new | One acceptance script per case, written by a human, never shown to the builder |
| `benchmarks/baselines/` | new, **committed** | One baseline per suite and label |
| `benchmarks/results/` | new, ignored | Every benchmark run's results |
| everything in Part 1 | **untouched** | |

### Run store and index

Every run folder already holds everything about the run: `request.json`, `plan.json`, `SPEC.md`, `trace.jsonl`, `transcript.jsonl`, `summary.json`, `REPORT.md`, `workspace/`.

What was missing is a way to compare runs **across** folders. `finish` now appends one line to `runs/index.jsonl`:

```json
{"id": "…", "ts": "…", "request": "…", "provider": "ollama", "model": "qwen3:8b", "status": "accepted",
 "verdict": "accept", "tasks": 3, "tasks_done": 3, "steps": 14, "revisions": 0, "duration_s": 41.2,
 "model_calls": {"planner": 1, "task": 12, "judge": 1}, "tokens_in": 18400, "tokens_out": 1900,
 "compactions": 0, "sensor_warnings": 0, "stuck_max": 0.12, "bench": {"suite": "core", "case": "todo-cli"}}
```

`python main.py runs [--last N]` prints the index as a table.

### Suite format (`benchmarks/core.json`)

```json
{"name": "core", "judge": true,
 "cases": [
   {"id": "greet", "difficulty": 1,
    "request": "Write greet.py: `python greet.py Ana` prints `Hello, Ana!` …",
    "acceptance": {"script": "accept/greet.py"}}
 ]}
```

**The acceptance script is ground truth, independent of the build:**
- It's written by a human, so the builder's own plan and checks don't grade the builder.
- It runs **after** the build, in a **copy** of the final workspace (`runs/<id>/acceptance/`), so the delivered project isn't touched.
- It runs through the **same workspace MCP server**, with the same allow-list, no shell and timeout.
- It passes only on exit code 0.

Each script is tested against a **reference solution** (`tests/bench_reference/`), so a broken acceptance script can't pass or fail builds by mistake.

### The starter suite

| Case | Difficulty | Asks for |
|---|---|---|
| `greet` | 1 | `greet.py <name>` → `Hello, <name>!`, with `world` as the default |
| `word-stats` | 2 | `stats.py <file>` → `<words> words, <lines> lines` |
| `todo-cli` | 3 | `todo.py add/list/done` with JSON storage and an exact `list` format |
| `csv-report` | 4 | `report.py <csv>` → total per region, sorted high to low, 2 decimals |

Each request states the exact output format, so a script can check it without guessing.

### Replay: `python main.py bench benchmarks/core.json`

| Option | Meaning |
|---|---|
| `--label NAME` | Names this results set and its baseline, e.g. `qwen3-8b` or `sonnet` |
| `--cases a,b` | Run only these cases |
| `--repeat N` | Run each case N times (LLMs vary), giving a pass **rate** per case |
| `--provider P --model M` | Override `config.json` for this benchmark: **the same suite, local vs cloud** |
| `--save-baseline` | Save these results as the baseline for this suite and label |
| `--tolerance X` | A pass-rate drop of up to X isn't a regression. Default 0; with `--repeat 3`, use e.g. 0.34 to allow one flaky run |
| `--no-judge` | Skip the judge (faster, and closer to Stage 09) |

The output is `benchmarks/results/<suite>-<label>-<timestamp>.json`, holding every row plus totals:
- the pass rate (from the acceptance scripts)
- the accepted rate (judge verdicts)
- per case: average steps, tokens in and out, time and model calls

### Comparison against the baseline (`benchmarks/baselines/<suite>-<label>.json`)

| Change | How it's judged | Effect |
|---|---|---|
| A case's pass rate **drops** by more than `--tolerance` | **regression** | exit code 1 |
| The overall pass rate drops by more than `--tolerance` | **regression** | exit code 1 |
| A case's average steps, tokens or time rises by more than 25% | **warning** (`costlier` / `slower`) | shown, doesn't fail |
| …falls by more than 25% | **improvement** | shown |
| A case missing from the baseline | **new** | shown |

Pass rates and relative changes are compared, never text. Model output varies too much from run to run for exact matches to mean anything.

### Out of scope

- Running cases in parallel. Builds share nothing, so it's easy to add later.
- Statistical tests on repeated runs.
- A dashboard: the results are plain JSON.

## How to run

```bash
# record a baseline for your local model, then for a cloud model
python main.py bench benchmarks/core.json --label qwen3-8b --save-baseline
python main.py bench benchmarks/core.json --provider anthropic --model claude-sonnet-5 --label sonnet --save-baseline

# after changing the harness: replay and compare (exit code 1 on a regression)
python main.py bench benchmarks/core.json --label qwen3-8b
python main.py bench benchmarks/core.json --label qwen3-8b --repeat 3 --tolerance 0.34   # allow one flaky run

python main.py runs --last 20            # every finished run, from runs/index.jsonl
python -m unittest discover -s tests -v  # 204 tests, no model needed
```

Commit `benchmarks/baselines/*.json`. `benchmarks/results/` is in `.gitignore`.

## Verified

**CLI, two core cases (`greet`, `word-stats`):**
1. **Recording a baseline:** both cases **PASS**, the table printed, and the baseline was saved to `benchmarks/baselines/core-demo.json`.
2. **Replay after a change that breaks line counting** (`count('\n') + 1` instead of `splitlines()`):
   - `word-stats` was still **accepted by the judge**, and its build passed its own check (`python stats.py stats.py`).
   - The **acceptance script failed it**: `expected '6 words, 4 lines'`.
   - Result: `✗ word-stats: pass rate 100% → 0%`, `✗ overall: 100% → 50%`, `result: REGRESSION`, exit code **1**.

   This is the reason for independent acceptance scripts. The build's own checks and the judge are both part of the system being measured; the acceptance script isn't.
3. **The run index:** `main.py runs` listed all four runs with model, status, tasks, steps, tokens and the `core/<case>` tag.

The tests also check:
- **All four acceptance scripts pass their reference solutions and fail an empty workspace.** Writing the reference for `csv-report` exposed an ambiguity in its request (should a region with no valid rows print as `0.00`?). The request now says it isn't printed, and the reference had a subtle `defaultdict` bug, now fixed.
- **Copy, not the original:** acceptance runs in a copy, and the delivered workspace never gets `_accept.py`.
- **Cost changes:** more steps or tokens gives a `costlier` **warning**, not a failure. The reverse gives an improvement.
- **`--repeat 2`:** one pass and one fail give a 50% rate, which is a regression at tolerance 0 but not at 0.5.
- **A crashing build** counts as a failed case, and the benchmark itself carries on.
- **`--provider` / `--model`** reach the builds, and are recorded in the results and baseline.
- **The index** rows carry model calls per role (e.g. `{"planner": 1, "task": 2}`) and the benchmark tag.

### Known limits

- Real LLM runs vary. With `--repeat 1`, one unlucky run looks like a regression. Use `--repeat 3 --tolerance 0.34` (or similar) before trusting a comparison.
- The core suite is small and Python-only. It's a starting point: add cases from the kinds of apps you actually ask for, with their own acceptance scripts and reference solutions.
- Cases run one after another. A full suite on a local 8B model will take a while.

## Commit

```
chapter F: run index + bench — suite with human acceptance scripts, baselines, regression flags, provider/model matrix
```

---

[← Chapter E: Sensors](../../chapters/E-sensors/README.md) · [Index](../../README.md)
