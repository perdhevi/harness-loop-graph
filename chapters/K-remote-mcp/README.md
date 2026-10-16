# Chapter K — Remote MCP servers

**Part 2 · Reference chapter** · **Status:** ✅ done (tested against a fake server and the official MCP Python SDK server — confirm with the service you use)

[← Chapter J: Reviewing and fixing existing code](../../chapters/J-review-existing/README.md) · [Index](../../README.md) · [Chapter L: A local web UI →](../../chapters/L-web-ui/README.md)

## Read this when

A tool you want lives on a remote MCP service (a URL), not in a local program the harness can start.

## Needs

Stage 04 (the MCP client and `mcp.json`).

## Goal

Connect to MCP servers over **Streamable HTTP**, the transport remote MCP services use, next to the local stdio servers, without changing the registry, the loop or anything above them.

## What gets built

- `tools/mcp_http.py` — `HttpMcpClient`: each JSON-RPC message is an HTTP POST to the server's URL; a reply is either one JSON body or an SSE stream (server requests such as `ping` on the stream are answered, notifications are ignored); `Mcp-Session-Id` and `MCP-Protocol-Version` are sent back on every request; `close()` ends the session with DELETE
- `tools/mcp_client.py` — `McpSession`, the part both transports share (handshake, `tools/list` with paging, `tools/call`, answers to server requests); `load_servers()` reads `"url"` entries and fills `${ENV_VAR}` in their headers
- `runtime.py` — `build_registry()` picks the transport per server
- `main.py doctor` — lists each server with its transport; remote servers must connect for the check to pass
- `mcp.json` — a disabled `remote-example` entry to copy from
- `tests/fake_http_mcp_server.py` (JSON and SSE replies, sessions, bearer auth, a slow tool) and `tests/test_mcp_http.py`

## Rules

- Same interface as stdio: remote tools appear as `<server>.<tool>` and go through the same registry, argument checks, output limiter, tracing and tool-name resolution
- Secrets stay out of `mcp.json`: headers say `${MY_TOKEN}`, and a missing variable is a clear error before anything connects
- Every failure is a readable `McpError` that names the server: unreachable, timeout, HTTP 401/403 (with a hint about the Authorization header), session ended by the server (HTTP 404)
- Standard library only (`urllib`); the system's proxy settings (`HTTPS_PROXY`) apply

## Done when

- [x] A remote server's tools are listed and called through the registry, next to the local workspace server
- [x] Replies work as plain JSON and as an SSE stream with a server `ping` before the result
- [x] Bearer tokens come from environment variables; missing or wrong tokens give clear errors
- [x] Timeouts, unreachable servers and expired sessions are reported, not hung on
- [x] Works against the official MCP Python SDK server (v2) in its SSE, JSON and stateless modes

## How to run

In `mcp.json`:

```json
"docs": {
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer ${DOCS_MCP_TOKEN}"},
  "timeout_s": 60,
  "enabled": true
}
```

```bash
set DOCS_MCP_TOKEN=…                      # Windows (PowerShell: $env:DOCS_MCP_TOKEN="…"); export on Linux/macOS
python main.py doctor                     # mcp servers: workspace (stdio), docs (http https://…)
python main.py --list-tools               # docs.* tools next to workspace.*
python -m unittest discover -s tests -v   # 274 tests, no model needed
```

A server without auth needs only `"url"`.

## Verified

Against the official MCP Python SDK (`mcp` 2.3.0, `MCPServer` with `streamable-http`), in its default SSE mode, `json_response=True` and `stateless_http=True`:

```
== sse server: sdk-demo protocol: 2025-06-18 session: True
- sdk.add(a: integer, b: integer) — Add two numbers.
add -> 42
shout -> HÉLLO ✓
broken -> Error: sdk.broken failed: McpToolError: Error executing tool broken
closed
```

## Known limits

- **No OAuth sign-in flow.** Bearer tokens and API keys in headers cover most hosted services. For a service that needs a browser sign-in, use a stdio bridge: `{"command": "npx", "args": ["-y", "mcp-remote", "https://…/mcp"]}`.
- **The older HTTP+SSE transport** (protocol 2024-11-05: a GET event stream plus a separate POST endpoint) isn't supported; the same bridge covers it.
- No reconnect: if the server ends the session mid-run, the run stops with a clear error and can be resumed with `--resume`.
- **Remote tools are someone else's code.** The workspace server's protections (path confinement, command allow-list, no shell) apply only to it. A remote tool's output goes into the model's prompt like any observation, so connect only servers you'd trust with what the model sends them.

## Commit

```
chapter K: remote MCP servers over Streamable HTTP — shared session, HTTP client, env-var headers, doctor
```

---

[← Chapter J: Reviewing and fixing existing code](../../chapters/J-review-existing/README.md) · [Index](../../README.md) · [Chapter L: A local web UI →](../../chapters/L-web-ui/README.md)
