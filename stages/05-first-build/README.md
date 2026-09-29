# Stage 05 — Request → first build

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model and the real workspace server — confirm with a real model)

[← Stage 04: MCP tools & workspace](../../stages/04-mcp-workspace/README.md) · [Index](../../README.md) · Stage 06: Planner: request → spec → tasks →

## Goal

Take a request in plain language, create a fresh project workspace, and let the loop build it end to end with the workspace tools.

## Problem it solves

There's no notion of a "build": no project folder per request, no record of what was asked, no summary of what was made.

## What gets built

- `main.py build "<request>"` — creates `runs/<run-id>/` with `request.json` and an empty `workspace/`
- Build prompt: role, request, rules for using the workspace, what "finished" means
- Final answer includes how to run the app
- Run summary: iterations, tools called, files created, time and tokens

## Rules

- The harness code knows nothing about what kind of app is being built — the request is data
- Every run gets a fresh workspace, so runs don't contaminate each other
- No planning yet: the loop goes straight from request to code. This stage is meant to show where that breaks
- Expect small local models to struggle; use the Anthropic provider as the baseline and compare

## Stays untouched

- The ReAct loop
- Registry and MCP client

## Done when

- [x] "Build a Python CLI to-do app with add/list/done and pytest tests" produces a runnable project
- [x] The run folder has the request, the workspace and a summary
- [x] A failed run leaves everything in place for inspection

## How to run

```bash
python main.py build "Build a Python CLI to-do app with add/list/done and pytest tests"
python -m unittest discover -s tests -v     # 80 tests, no model needed
```

Then look in `runs/<id>/`: the project is in `workspace/`, and every step is in `transcript.jsonl`.

## Verified

- An end-to-end `main.py build` with a scripted model wrote `todo.py`, `test_todo.py` and `README.md`, tried the CLI, and finished in 9 steps. The run folder held the request, the workspace, 9 transcript lines and the summary.
- The generated project really works: 4 tests pass, and `add`, `list` and `done` behave as expected.
- A model error partway through still leaves the files, the transcript so far and a summary with `status: error`.

### The lesson this stage was built to show

On the first end-to-end run, **pytest wasn't installed**. Both test runs failed with `No module named pytest`. The model never saw a passing test, yet it said *"Tests pass and the CLI works"*, and the build was recorded as `finished`.

Nothing in the harness checks that claim. That gap is what Stage 09 (verification) closes. Stage 06 (planning) comes first, because the second weakness shows up as soon as requests get bigger: there's no plan to check progress against.

A smaller side effect showed up too: trying the CLI left a `todo.json` in the workspace. Nothing in the harness tells "project files" apart from files the program created while running.

## Troubleshooting: "the build ran, but no files were created"

Run `python main.py doctor` first. It checks every item below in about ten seconds. Then look at the run itself:

```bash
python -c "import json; print(json.load(open('runs/<id>/summary.json'))['files'])"   # what's in the workspace
cat runs/<id>/transcript.jsonl                                                     # every step and tool call
```

The files are in `runs/<id>/workspace/`, next to `main.py`, not in the folder you ran the command from.

| Symptom in the transcript or trace | Cause | Fix |
|---|---|---|
| `write_file` fails, or **every** tool call after one fails with `server exited` / `server is not running` (**Windows**) | Windows pipes default to cp1252. Text such as `—`, `✓` or `Ё` in file content was corrupted, or crashed the workspace server | Fixed in Stage 04: the MCP client sends ASCII-only JSON, and the server reads and writes UTF-8 |
| The first steps look fine, then replies stop being JSON actions, or the model "forgets" the tools | **Ollama silently truncates** prompts longer than its context. The default is **4,096 tokens** on GPUs under ~23 GB of VRAM, and it drops the start (system prompt, tool list, task) | Fixed: the adapter sends `num_ctx` (`providers.ollama.num_ctx`, 16384). Keep `context.window_tokens` at or below it; `doctor` warns if not |
| Replies look like `<|tool_call>call:write_file{…}`, `<tool_call><function=write_file>…` or `{"name": …, "arguments": …}`, the **file tool is never called**, and every step says "not a valid action" (**Gemma 4**, **Qwen 3.5** and others) | The model uses its own trained tool-call format, not the harness's JSON | Fixed: `replies.py` converts these formats, and the registry resolves `write_file` → `workspace.write_file`. `doctor` shows the raw reply and the conversion |
| `final` on step 1 with code in the answer, and 0 files | The model answered instead of using a tool | `doctor` catches this. Try `think: false` (the default now) or a larger model. From Stage 09 on, a check failure sends it back to fix |
| Every reply is `(empty reply)` or a parse error | qwen3's reasoning went into `message.thinking` and `content` was empty | Fixed: `think: false` by default, and a reply with only thinking is no longer lost |
| `HTTP 400 … does not support thinking` | The model has no thinking switch | Fixed: the adapter retries once without `think` and remembers |
| `run_command` → `program not found: python` (**Windows**) | Only the Microsoft Store alias of `python` is on PATH | Install Python from python.org, or put a real `python.exe` first on PATH |

## Commit

```
stage 5: build command — run folders, transcript, summary; straight-to-code builds
```

## Leads to

Straight-to-code works for small apps and falls apart on bigger ones: no plan, no way to check progress. Stage 06 adds the plan.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `runtime.py` | new | Shared setup moved out of `main.py`: `load_json`, `prompt_text`, `build_registry` |
| `build.py` | new | `start_run()` creates the run folder; `execute_build()` runs the loop and writes the transcript and summary |
| `main.py` | changed | New `build` subcommand; the old usage still works. New `doctor` subcommand checks the setup (see Troubleshooting) |
| `prompts.json` | + `build_rules`, `build_request` | Build instructions, as data |
| `config.json` | + `build.max_iterations`, `build.runs_dir` | A build needs a far bigger step budget than a question |
| `loop.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Command

```bash
python main.py build "Build a Python CLI to-do app with add/list/done and pytest tests"
python main.py build --max-iterations 60 "…"
python main.py "question"          # unchanged: Stage 02–04 behaviour
```

### Run folder

```
runs/20260929-010203-build-a-python-cli-to-do/
├── request.json      request, time, provider, model, max_iterations
├── workspace/        the project (the MCP workspace server's root)
├── transcript.jsonl  one line per loop step: thought, action, args, observation, final, error
└── summary.json      status, answer, counts, files, time, approximate size
```

- The run id is `<UTC timestamp>-<slug>`, where the slug is the first few words of the request, lowercased with hyphens. If the id already exists, `-2`, `-3`, … is appended.
- `transcript.jsonl` is appended to **as each step happens**. If a run crashes or is killed, everything up to that point is still there.

### Prompts

The system prompt is the Stage 02 `react_system` followed by `build_rules`, which tell the model to:
- build a complete, working project in the empty workspace, using the workspace tools for everything
- keep the project self-contained, prefer the standard library, and list dependencies instead of installing them (no `pip install` / `npm install`)
- write a `README.md` with how to run the project
- run the program, and its tests if there are any, before finishing
- end with a final answer that lists the files created and the exact commands to run them

The first user message is `build_request`, with `{request}` filled in.

### Summary (`summary.json`)

| Field | Meaning |
|---|---|
| `status` | `finished` (model gave a final answer), `max_iterations`, or `error` |
| `answer` | The model's final answer, if any |
| `error` | Error message when `status` is `error` |
| `steps`, `tool_calls`, `malformed` | Loop counts; `tool_calls` is broken down per tool |
| `files` | Every file in the workspace (skipping `.git`, `node_modules`, caches), with its size |
| `duration_s` | Wall-clock time |
| `model_calls`, `approx_tokens_in`, `approx_tokens_out` | Counted by a thin wrapper around the model (characters ÷ 4). The adapter doesn't change |

`finished` means the model *said* it was done. Nothing is checked yet; that's Stage 09.

### Out of scope

Planning (Stage 06), resuming a run (Stage 07), verification (Stage 09), and exact token counts.

---

[← Stage 04: MCP tools & workspace](../../stages/04-mcp-workspace/README.md) · [Index](../../README.md) · Stage 06: Planner: request → spec → tasks →
