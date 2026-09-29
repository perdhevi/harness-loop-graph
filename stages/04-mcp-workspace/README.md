# Stage 04 — MCP tools & workspace

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model and real MCP servers — confirm with a real model)

[← Stage 03: Tool registry & dispatch](../../stages/03-tool-registry/README.md) · [Index](../../README.md) · [Stage 05: Request → first build →](../../stages/05-first-build/README.md)

## Goal

Connect to MCP servers and expose their tools through the same registry — starting with our own workspace server that lets the loop write files and run commands in one project folder.

## Problem it solves

The loop has no hands: it can't create files, run code or see what happened.

## What gets built

- `mcp.json` — list of servers (command, args, env, enabled)
- `McpToolSource` — a minimal MCP client over stdio (JSON-RPC 2.0): `initialize`, `tools/list`, `tools/call`
- Tool names namespaced by server (e.g. `workspace.write_file`)
- `servers/workspace_server.py` — our own MCP server: `read_file`, `write_file`, `edit_file`, `list_dir`, `run_command`, all confined to one workspace folder
- Optional: the official filesystem server (`@modelcontextprotocol/server-filesystem` via `npx`) to prove third-party servers plug in too

## Rules

- Client written by hand first (build to delete); the official `mcp` SDK is the fallback if protocol details get in the way
- MCP tools go through the same `ToolSource` interface as local tools — the loop can't tell them apart
- `run_command` uses an allow-list (e.g. `python`, `pip`, `pytest`, `node`, `npm`) and a timeout; paths can't escape the workspace
- Server processes start with the run and shut down cleanly when it ends
- Running model-written code is risky: the workspace is the boundary for now; a container can wrap the server later

## Stays untouched

- The ReAct loop
- The adapter
- The registry interface from Stage 03

## Done when

- [x] `python main.py --list-tools` shows local and MCP tools side by side
- [x] The loop can write a file, run it, and read the output back
- [x] Writing to `../outside.txt` or running a non-allowed command is refused and reported as an observation
- [x] A crashed MCP server shows up as a tool error, not a hung run

## How to run

```bash
python main.py --list-tools
python main.py --workspace ./scratch "Write greet.py that greets a name from the command line, and run it"
python -m unittest discover -s tests -v     # 65 tests, no model needed (starts real server processes)
```

To try the official filesystem server as well, set `"enabled": true` for `filesystem` in `mcp.json` (needs Node.js / `npx`). Its tools then appear as `filesystem.*`, next to `workspace.*`.

## Verified

- Our workspace server: all five tools, blocking of paths that escape the workspace (including via symlinks), the command allow-list, no shell, and timeouts.
- Client failure modes: a server crash is reported with its exit code and stderr, a hung server times out, the server's `ping` gets answered, and paged `tools/list` results are followed.
- **Interop:** the hand-written client talks to the official `@modelcontextprotocol/server-filesystem` (protocol `2025-06-18`, 14 tools). Reads work, and that server's own path check blocks `/etc`.
- An end-to-end run through `main.py` had a (scripted) model write `greet.py`, hit a `NameError`, fix it with `edit_file`, get blocked from `..` and from `ls`, then run the program successfully. The server process was gone afterwards.

### Fixes found in later stages (included in this stage's code)

- **Windows:** the MCP client now sends ASCII-only JSON (`\uXXXX` escapes). The server reads and writes UTF-8, and runs commands with `PYTHONUTF8=1`, so a cp1252 pipe can't corrupt `—` or `✓` or crash the server. `python.exe` passes the allow-list as `python`. Covered by `tests/test_platform.py`.
- Chapter C: long output used to be cut to its **first** 8 KB. The server now keeps the start and the end, with a 100 KB cap per stream. The harness's output limiter decides what the model actually sees.
- Stage 09 found that commands needed `PYTHONDONTWRITEBYTECODE=1`. Without it, a same-size edit made within one second could run stale bytecode. See the Stage 09 README.

## Commit

```
stage 4: hand-written MCP stdio client + workspace MCP server (files, edit, run_command)
```

## Leads to

The loop has real tools. Next it needs to take a request and turn it into a project.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `tools/mcp_client.py` | new | `McpClient` (stdio JSON-RPC 2.0) and `McpToolSource` (plugs into the registry) |
| `servers/workspace_server.py` | new | Our MCP server: file tools and `run_command`, confined to one folder |
| `mcp.json` | new | Which servers to start, and how |
| `main.py` | changed | `--workspace DIR`, starts MCP servers, shuts them down on exit |
| `prompts.json` | changed | One line: paths are relative to the workspace |
| `loop.py`, `tools/registry.py`, `model_adapter.py` | **untouched** | |

Everything stays standard-library Python: both the client and the server are written by hand.

### `mcp.json`

```json
{
  "servers": {
    "workspace": {
      "command": "{python}",
      "args": ["servers/workspace_server.py", "--root", "{workspace}",
               "--allow", "python,python3,pip,pytest,node,npm"],
      "timeout_s": 120,
      "enabled": true
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "{workspace}"],
      "enabled": false
    }
  }
}
```

`{python}` becomes the interpreter running the harness. `{workspace}` becomes the absolute workspace path.

### MCP client (`McpClient`)

- It starts the server as a subprocess. Messages are **newline-delimited JSON-RPC 2.0** on stdin/stdout, and stderr is kept for error messages.
- A reader thread routes each response to the request waiting for it (matched by `id`). It answers a server `ping`, and ignores notifications.
- The handshake sends `initialize` (the protocol version, the client's name, no capabilities), then `notifications/initialized`.
- `list_tools()` sends `tools/list` and follows `nextCursor` if the list is paged.
- `call_tool(name, args)` sends `tools/call`:
  - Text content blocks are joined into one string. Other block types become a placeholder like `[image content]`.
  - If `isError` is true, it raises `McpToolError`, which the registry turns into `Error: … failed: …`.
- **Failure modes** all raise `McpError`, and none of them hang:
  - a request times out
  - the server process exits (the error message includes the tail of its stderr)
  - the server returns a JSON-RPC error
- `close()` closes stdin, waits briefly, then terminates the server and, if that fails, kills it.

### MCP tool source (`McpToolSource`)

- Tool names get the server's name as a prefix, so `write_file` becomes `workspace.write_file`. Two servers can then offer tools with the same name.
- Each tool's `inputSchema` is used as-is, so the registry's argument checks cover MCP tools too.

### Workspace server tools

| Tool | Args | Behaviour |
|---|---|---|
| `read_file` | `path` | Returns the text, cut off at 100 KB with a note |
| `write_file` | `path`, `content` | Creates or overwrites the file, and any missing folders |
| `edit_file` | `path`, `old`, `new` | Replaces exactly one occurrence. Errors if `old` appears 0 times or more than once |
| `list_dir` | `path?`, `recursive?` | Folders end in `/`. Skips `.git`, `node_modules`, `__pycache__`, `.venv` |
| `run_command` | `command`, `timeout_s?` | Runs in the workspace root. Returns the exit code, stdout and stderr, each cut off at 8 KB |

### Safety rules

- **Paths:** every path is resolved (including symlinks) and must stay inside the root. `../x`, `/etc/passwd` or a symlink pointing outside are refused.
- **Commands:**
  - The command is split with `shlex` and run **without a shell**, so `|`, `&&`, `>` and `$(…)` have no effect.
  - The first word must be on the allow-list.
  - The default timeout is 60 s, with a maximum of 300 s.
- **The allow-list isn't a sandbox.** `python -c "…"` can still do anything your user account can. The allow-list stops accidental commands; the real boundary is a container, which comes later. Until then, point `--workspace` at a scratch folder, and use a virtualenv so `pip install` doesn't touch your global Python.
- Refusals are returned as tool errors (`isError: true`), so the model sees why a call failed.

### Out of scope

A `runs/` folder per request (Stage 05), HTTP/SSE transports, MCP resources and prompts, and running inside a container.

---

[← Stage 03: Tool registry & dispatch](../../stages/03-tool-registry/README.md) · [Index](../../README.md) · [Stage 05: Request → first build →](../../stages/05-first-build/README.md)
