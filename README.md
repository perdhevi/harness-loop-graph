# harness-loop-graph

An agent harness that takes a request in plain language — *"build me a CLI to-do app with tests"* — and builds the application it asks for: plans it, writes it, runs it, fixes it and checks it against the request.

Its architecture follows the same approach as Support Harness: a thin model adapter, a locked ReAct loop, and every other concern added around it.

The project is organised like a book in two parts:

- **Part 1 — Build pipeline.** Ten stages, built in order. Each adds one concern, is specced before it's coded, and lands as one commit. At the end, a request goes in and a checked app comes out.
- **Part 2 — Reference chapters.** Concerns that sit *around* the loop without changing it: memory, context, compaction and so on. Read one when its symptom shows up; each chapter lists what it needs from Part 1.

## Quick start

```bash
# Local model
ollama pull qwen3:8b
python main.py doctor            # checks settings, workspace server, model and its reply format
python main.py build "Build a Python CLI to-do app with add/list/done and pytest tests"
python main.py "What is (17 * 23) + 4, and is it prime?"

# Or Anthropic: set "provider": "anthropic" in config.json, then
export ANTHROPIC_API_KEY=...

# Then read the result
cat runs/<id>/REPORT.md
python main.py build --fix runs/<id> "list crashes on an empty file"   # change a finished run
python main.py trace runs/<id>

# Measure: replay the benchmark and compare with your baseline
python main.py bench benchmarks/core.json --label qwen3-8b --save-baseline

# Tests (no model needed)
python -m unittest discover -s tests -v
```

Standard-library Python 3.10+ only; nothing to install. (Node.js is only needed if you enable the optional official filesystem MCP server.)

## Principles

- **Harness ≠ model client.** The adapter is only the HTTP wrapper; the loop, tools, planning, verification, memory and judging are the harness.
- **One concern per stage, one commit per stage.**
- **Spec before code.** Existing signatures and schemas are preserved unless the spec says otherwise.
- **Data in JSON, not Python.** Config, tools, MCP servers, plans, rubrics and benchmarks are all data.
- **Fractions over hard thresholds.** Smooth scores and budgets instead of binary cutoffs.
- **Build to need.** Add a concern when its problem shows up, not before.
- **Build to delete.** Borrow patterns from frameworks and protocols; write the implementation yourself.

## Part 1 — Build pipeline

Build these in order.

| # | Stage | Adds | Status |
|---|---|---|---|
| 01 | [Model adapter](stages/01-model-adapter/README.md) | Send one prompt to a model and get one answer back | ✅ done |
| 02 | [ReAct loop](stages/02-react-loop/README.md) | Turn the single call into a loop: reason → act → observe → repeat, with a max-iterations guard | ✅ done |
| 03 | [Tool registry & dispatch](stages/03-tool-registry/README.md) | Give the loop one place to find tools and call them by name, with local Python tools defined in JSON | ✅ done |
| 04 | [MCP tools & workspace](stages/04-mcp-workspace/README.md) | Connect to MCP servers and expose their tools through the same registry — starting with our own workspace server that lets the loop write files and run commands in one project folder | ✅ done |
| 05 | [Request → first build](stages/05-first-build/README.md) | Take a request in plain language, create a fresh project workspace, and let the loop build it end to end with the workspace tools | ✅ done |
| 06 | [Planner: request → spec → tasks](stages/06-planner/README.md) | Before writing code, turn the request into a short spec and an ordered task list, saved in the run folder | ✅ done |
| 07 | [Graph & state](stages/07-graph-state/README.md) | Define one typed state object and wire the steps as a graph: intake → plan → build → finish | ✅ done |
| 08 | [Task-by-task execution](stages/08-task-execution/README.md) | Execute the plan one task at a time, each in its own ReAct loop with a fresh, task-scoped conversation | ✅ done |
| 09 | [Verification & fix loop](stages/09-verification/README.md) | After each task — and at the end — run real checks (tests, build, lint, smoke run) and feed failures back into the task until it passes or runs out of attempts | ✅ done |
| 10 | [Judge: does it match the request?](stages/10-judge/README.md) | Add a separate LLM pass that compares the finished project against the request and spec, and decides what happens next | ✅ done |

### What a build looks like at the end of Part 1 ✅

Part 1 is complete: `python main.py build "<request>"` plans, builds task by task, checks every task, has the result judged against the request, revises if needed, and writes `runs/<id>/REPORT.md`.

```
request ─▶ intake ─▶ plan ─▶ task ─▶ verify ─▶ (next task … ) ─▶ judge ─┬─ accept   → app + README
                      │        ▲        │                                ├─ revise   → back to plan
                      │        └─ fix ◀─┘ (failed check, bounded)       └─ escalate → report to human
                 SPEC.md + plan.json
```

## Part 2 — Reference chapters

Read and build these when their symptom appears. Order is a suggestion, apart from the dependencies listed.

| Ch. | Chapter | Read this when… | Needs | Status |
|---|---|---|---|---|
| A | [Tracing](chapters/A-tracing/README.md) | You can't tell what happened in a run, or you're debugging with print statements. | Stage 02 (ReAct loop) | ✅ done |
| B | [Memory](chapters/B-memory/README.md) | Later tasks ignore decisions made by earlier ones, or the same error and dead end come back build after build. | Stage 07 (graph & state), Stage 08 (task execution) | ✅ done |
| C | [Context management](chapters/C-context-management/README.md) | Prompts grow too large, or are filled with whole files and long test logs that have nothing to do with the current task. | Stage 07 (graph & state) | ✅ done |
| D | [Compaction](chapters/D-compaction/README.md) | Long tasks forget what they already tried, or history gets cut off once it no longer fits. | Chapter C (context management) — compaction is triggered by its token budget | ✅ done |
| E | [Sensors](chapters/E-sensors/README.md) | Tasks go in circles: rewriting the same file, hitting the same test failure, making no progress until they run out of steps. | Stage 08 (task execution), Stage 09 (verification) | ✅ done |
| F | [Persistence & benchmark](chapters/F-benchmark-regression/README.md) | You changed the harness and can't tell whether it got better or worse. | Stage 10 (judge) for verdicts | ✅ done |
| G | [Fixing a finished run](chapters/G-fix-run/README.md) | A build finished, but something is missing or wrong, and you want to change that run instead of starting over. | Stage 10 (judge) | ✅ done |
| H | [A model per role](chapters/H-role-models/README.md) | One model can't do every job well: good at code but bad at plans or verdicts, or too slow to use everywhere. | Stage 10 (judge) | ✅ done |
| I | [Robust replies and plans](chapters/I-robust-replies/README.md) | Runs fail on the model's format: parse errors, revisions that escalate, tasks that keep saying "done" until they run out of steps. | Stages 02, 04, 06, 10 | ✅ done |

Part 2 is complete: every chapter is built and tested. Chapters A–E change how a build runs; they are on by default and each can be switched off in `config.json`. Chapter F adds the `runs` and `bench` commands. Chapter G adds `build --fix`. Chapter H lets each role (planner, task, judge, reviser, compactor) use its own model; without a `roles` setting, one model does everything. Chapter I makes replies and plans from small models far more robust.

**Fixed dependencies inside Part 2:** C (context management) before D (compaction). A (tracing) is worth reading as soon as Stage 02 is done.

## How tools flow

```
ReAct loop ──▶ ToolRegistry ──┬──▶ LocalToolSource   (tools.json + Python functions)
                              └──▶ McpToolSource     (mcp.json → stdio JSON-RPC)
                                       ├── workspace server  (ours: files, edit, run_command)
                                       └── filesystem server (official, optional)
```

## Layout

```
harness-loop-graph/
├── README.md                 ← this index
├── stages/                   ← Part 1, built in order
│   ├── 01-model-adapter/
│   │   ├── README.md         ← stage reference (spec + done-when)
│   │   └── ARTICLE.md        ← long-form write-up
│   ├── 02-react-loop/
│   └── … 10-judge/
└── chapters/                 ← Part 2, read when needed
    ├── A-tracing/
    ├── B-memory/
    └── … F-benchmark-regression/
```

Stage READMEs: **Goal · Problem · What gets built · Rules · Untouched · Done when · Leads to**.  
Chapter READMEs: **Read this when · Needs · Goal · What gets built · Rules · Untouched · Done when**.

## Status legend

- ✅ done — implemented and committed
- 🔨 in progress — spec agreed, code being written
- ⬜ planned — outline only; the full spec is written before coding starts
