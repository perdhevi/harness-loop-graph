# Stage 02 — ReAct loop

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 01: Model adapter](../../stages/01-model-adapter/README.md) · [Index](../../README.md) · Stage 03: Tool registry & dispatch →

## Goal

Turn the single call into a loop: reason → act → observe → repeat, with a max-iterations guard. This loop is the engine every later stage builds around, and it is locked after this stage.

## Problem it solves

The model answers once and cannot take actions or react to results.

## What gets built

- Loop runner with a `reason` step, an `act` step and a `finish` exit
- Action format: the model replies with a JSON action (`tool` + `args`) or a final answer
- Action parser that feeds malformed output back to the model instead of crashing
- Max-iterations guard and a trace of each step

## Rules

- Loop shape is fixed after this stage; later stages attach around it, not inside it
- Actions are parsed from text, so tools from any source (local or MCP) work without changing the adapter
- Own runner, not LangGraph (see Spec)

## Stays untouched

- `model_adapter.py` and its `complete()` signature

## Done when

- [x] A request runs several reason/act/observe cycles and stops on a final answer
- [x] Malformed actions come back as an observation, not a crash
- [x] Hitting the iteration limit ends cleanly with a reason
- [x] One stub tool is enough to prove the loop

## How to run

```bash
python main.py "What is (17 * 23) + 4, and is it prime?"
python main.py --max-iterations 4 "..."
python main.py --once "..."                 # Stage 01 single call
python -m unittest discover -s tests -v     # 26 tests, no model needed
```

## Other models' tool-call formats (found with Gemma 4)

On a real run with Gemma 4, the file tool was never called. Models fall back to the format they were trained on, e.g. Gemma 4's `<|tool_call>call:write_file{path:<|"|>a.py<|"|>,…}<tool_call|>`, or `{"name": …, "arguments": …}`. Every such reply failed to parse, so no tool ever ran.

`replies.py` fixes this. `NormalizingModel` wraps the model from the outside, so the loop itself is untouched. It converts the Gemma call syntax, Qwen 3.5's XML-style `<tool_call><function=…><parameter=…>` calls, and common JSON shapes (OpenAI `function`, Anthropic `tool_use`, `name`/`arguments`, `tool`/`args`, `final_answer`, and Hermes-style JSON inside `<tool_call>` tags) into this stage's action format. Canonical replies pass through byte for byte, and unrecognised text still gets the normal format reminder. From Chapter A on, each conversion also appears in the trace as `normalized×N`. Switch it off with `replies.normalize: false`.

## Commit

```
stage 2: ReAct loop with JSON actions, stub calculate tool, iteration guard
```

## Leads to

The loop can act, but only with a hardcoded stub tool.

## Spec

### Decision: own runner, not LangGraph

The loop is a plain Python function. No framework, still standard library only. Each step is written as a small function (`reason`, `act`) so a graph framework could replace the runner in Stage 07 without rewriting the steps.

### Files

| File | Change | Purpose |
|---|---|---|
| `loop.py` | new | `run_loop()`, `parse_action()`, `Tool`, `Step`, `LoopResult` |
| `stub_tools.py` | new | One stub tool: `calculate(expression)` — safe arithmetic only |
| `prompts.json` | new | ReAct system prompt (with a `{tools}` slot) and the format reminder |
| `config.json` | + `loop.max_iterations` | Iteration budget as data |
| `main.py` | updated | Runs the loop by default; `--once` keeps the Stage 01 behaviour |
| `replies.py` | new | `normalize_reply()` and `NormalizingModel`: other models' tool-call formats → this stage's action format |
| `tests/test_replies.py` | new | The formats above, and canonical replies left untouched |
| `model_adapter.py` | **untouched** | |
| `tests/test_loop.py` | new | Loop and parser tests with a scripted fake model |

### Action format

Each model turn must be exactly **one JSON object**:

```json
{"thought": "why I need this", "action": "calculate", "args": {"expression": "17 * 23"}}
```

```json
{"thought": "why I'm done", "final": "The answer is 391."}
```

The parser is forgiving about *wrapping* and strict about *content*:

- It strips `<think>…</think>` blocks (reasoning models such as qwen3) and ```` ```json ```` fences.
- It takes the first JSON object found in the reply.
- The object must have either `final` or `action`. `args` defaults to `{}` and must be an object.

### Loop

```
messages = [user: request]
repeat up to max_iterations:
    reply  = model.complete(system, messages)        # reason
    messages += [assistant: reply]
    parse reply
      ├─ final     → stop, status "final"
      ├─ action    → observation = dispatch(tool, args)   # act
      └─ malformed → observation = format reminder
    messages += [user: "Observation: " + observation]
stop, status "max_iterations"
```

- The message list always alternates user → assistant (the Anthropic API requires it).
- A malformed reply uses up one iteration. That way a model that never follows the format still stops.
- Dispatch never raises. Unknown tools, wrong arguments and tool exceptions all become `Error: …` observations.
- Every step fires an `on_step` callback. `main.py` prints it; the loop itself does no printing.

### Result

`LoopResult(status, answer, steps, messages)`. `status` is `"final"` or `"max_iterations"`. `steps` holds, for each step: the raw reply, thought, action, args, observation, final answer and any parse error.

### Out of scope

A tool registry and tools defined in JSON (Stage 03), real workspace tools (Stage 04), and the model's built-in tool-calling APIs.

---

[← Stage 01: Model adapter](../../stages/01-model-adapter/README.md) · [Index](../../README.md) · Stage 03: Tool registry & dispatch →
