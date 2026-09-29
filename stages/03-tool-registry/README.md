# Stage 03 — Tool registry & dispatch

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 02: ReAct loop](../../stages/02-react-loop/README.md) · [Index](../../README.md) · Stage 04: MCP tools & workspace →

## Goal

Give the loop one place to find tools and call them by name, with local Python tools defined in JSON.

## Problem it solves

Tools are hardcoded inside the loop.

## What gets built

- `tools.json` — name, description, parameter schema
- `ToolRegistry` with a `ToolSource` interface: `list_tools()` and `call(name, args)`
- `LocalToolSource` mapping names to Python functions
- Dispatcher that validates args and returns results as observations
- Tool descriptions rendered into the system prompt

## Rules

- Unknown tools and tool exceptions come back as observations, never crashes
- Tool schemas are data, not code
- The registry doesn't care where a tool lives — that's what makes Stage 04 a drop-in

## Stays untouched

- The loop's shape: reason → act → observe, the action format, the iteration guard and `LoopResult`
- The adapter

## Done when

- [x] Adding a local tool means one JSON entry and one function
- [x] A failing tool shows up in the trace and the loop continues
- [x] Bad or invented arguments are caught before the tool runs
- [x] A second tool source can be added without touching the loop (proved in tests with a fake source)

## How to run

```bash
python main.py --list-tools
python main.py "What time is it in Jakarta and how many hours until midnight?"
python -m unittest discover -s tests -v     # 39 tests, no model needed
```

To add a tool: write a function in `tools/builtin.py`, add one entry to `tools.json`.

## Tool-name resolution

Small models drop or re-spell the server prefix. `ToolRegistry.resolve()` maps `write_file`, `workspace_write_file`, `workspace/write_file` and `functions.write_file` to `workspace.write_file` **only when exactly one tool matches**. The observation starts with `(ran as workspace.write_file; use that exact name next time)`. Ambiguous or unknown names are still errors.

## Commit

```
stage 3: tool registry with MCP-shaped JSON tool definitions and arg validation
```

## Leads to

Tools can only be Python functions inside this repo, and none of them can touch a project folder yet.

## Spec

### The one change to the loop

In Stage 02, `loop.py` rendered the tool list and dispatched calls itself, from a plain `dict` of tools. This stage moves that plumbing out of the loop and into the registry. `run_loop()` now takes a **tool box**, meaning any object that has these two methods:

```python
describe() -> str                # tool list for the system prompt
call(name: str, args: dict) -> str   # always returns an observation; never raises
```

The loop's shape stays the same. What changes is where tools come from, and this is the only time the loop will need to change for that.

### Files

| File | Change | Purpose |
|---|---|---|
| `tools/registry.py` | new | `ToolSpec`, `ToolSource` protocol, `ToolRegistry`, argument validation |
| `tools/local.py` | new | `LocalToolSource`: loads `tools.json` and resolves each handler |
| `tools/builtin.py` | new | Python functions for local tools (`calculate` moved here, `current_time` added) |
| `tools.json` | new | Local tool definitions |
| `loop.py` | changed | Takes a tool box; `Tool`, `render_tools` and `dispatch` removed |
| `stub_tools.py` | **deleted** | Replaced by the three files above |
| `main.py` | changed | Builds the registry; new `--list-tools` flag |
| `config.json` | + `tools.local` | Path to `tools.json` |
| `model_adapter.py` | **untouched** | |

### Tool definition format

`tools.json` uses the **same shape MCP uses** (`name`, `description`, `inputSchema` as JSON Schema), so Stage 04's MCP tools and local tools are described identically. The only extra field is `handler`:

```json
{
  "name": "calculate",
  "description": "Evaluate an arithmetic expression, e.g. '(17 * 23) + 4'.",
  "inputSchema": {
    "type": "object",
    "properties": { "expression": { "type": "string", "description": "the expression" } },
    "required": ["expression"]
  },
  "handler": "tools.builtin:calculate"
}
```

- `handler` must point inside the `tools.` package. `tools.json` can't be used to call `os:system`.
- Handlers are resolved when the tools are loaded, so a typo fails at startup instead of halfway through a run.

### Interfaces

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON Schema object

class ToolSource(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...
    def call(self, name: str, args: dict) -> Any: ...   # may raise

class ToolRegistry:
    def add_source(self, source: ToolSource) -> None      # name clash → ValueError at startup
    def list_tools(self) -> list[ToolSpec]
    def describe(self) -> str
    def call(self, name: str, args: dict) -> str           # never raises
```

### What `ToolRegistry.call` guarantees

1. If the tool name is unknown, it returns `Error: unknown tool 'x'. Available tools: …`
2. The arguments are validated against `inputSchema` before the tool runs:
   - Required arguments must be present.
   - Unknown arguments are rejected. Models invent parameter names, and rejecting them makes that visible.
   - Types are checked: `string`, `integer`, `number`, `boolean`, `object`, `array`. `true` doesn't count as an integer.
3. If the source raises, it returns `Error: <tool> failed: <type>: <message>`.
4. A non-string result is serialised to JSON.

Validation lives in the registry, not in each source, so MCP tools get the same checks for free.

### Prompt rendering

```
- calculate(expression: string) — Evaluate an arithmetic expression, e.g. '(17 * 23) + 4'.
- current_time(timezone?: string) — Current date and time as ISO 8601. …
```

A `?` after a name marks an optional argument.

### Out of scope

MCP (Stage 04), tools that touch files or run commands (Stage 04), and truncating long tool output (Chapter C).

---

[← Stage 02: ReAct loop](../../stages/02-react-loop/README.md) · [Index](../../README.md) · Stage 04: MCP tools & workspace →
