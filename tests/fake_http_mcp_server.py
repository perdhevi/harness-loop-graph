"""A small Streamable HTTP MCP server for Chapter K's tests (runs in a thread).

Tools: echo(text), add(a, b), fail(), slow(seconds), whoami().
Options: sse=True answers requests as an SSE stream (with a ping and a progress notification
before the result); token="…" requires Authorization: Bearer …; sessions are always issued.
Everything the client sent is kept in `requests` for assertions.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
import uuid

TOOLS = [
    {"name": "echo", "description": "Echo text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "fail", "description": "Always reports an error.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Waits before answering.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "whoami", "description": "Says which session this is.", "inputSchema": {"type": "object", "properties": {}}},
]


class FakeHttpMcp:
    def __init__(self, *, sse: bool = False, token: str | None = None):
        self.sse, self.token = sse, token
        self.requests: list[dict] = []          # {"method", "headers", "body"}
        self.sessions: set[str] = set()
        self.ended: list[str] = []
        self.ping_answered = threading.Event()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                     # the client gave up waiting (timeout tests)

            def do_DELETE(self):
                fake.requests.append({"method": "DELETE", "headers": dict(self.headers), "body": None})
                sid = self.headers.get("Mcp-Session-Id")
                fake.sessions.discard(sid)
                fake.ended.append(sid)
                self._send(200)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"method": "POST", "headers": dict(self.headers), "body": body})
                if fake.token and self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, b'{"error": "unauthorized"}')
                    return
                if "method" not in body:                     # the client's reply to our ping
                    if body.get("id") == "ping-1" and body.get("result") == {}:
                        fake.ping_answered.set()
                    self._send(202)
                    return
                method = body["method"]
                if method == "initialize":
                    sid = uuid.uuid4().hex
                    fake.sessions.add(sid)
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake-http", "version": "1"}}
                    self._send(200, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode(),
                               extra={"Mcp-Session-Id": sid})
                    return
                sid = self.headers.get("Mcp-Session-Id")
                if sid not in fake.sessions:
                    self._send(404, b'{"error": "unknown session"}')
                    return
                if "id" not in body:                         # notifications/initialized and friends
                    self._send(202)
                    return
                result = fake.handle(method, body.get("params") or {}, sid)
                reply = {"jsonrpc": "2.0", "id": body["id"], **result}
                if not fake.sse:
                    self._send(200, json.dumps(reply).encode())
                    return
                events = []
                if method == "tools/call":                  # a server request and a notification first
                    events.append({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
                    events.append({"jsonrpc": "2.0", "method": "notifications/progress",
                                   "params": {"progressToken": 1, "progress": 0.5}})
                stream = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events)
                stream += ": keep-alive comment\n\n"
                stream += "event: message\ndata: " + json.dumps(reply) + "\n\n"
                self._send(200, stream.encode(), ctype="text/event-stream")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def handle(self, method: str, params: dict, sid: str) -> dict:
        if method == "tools/list":
            return {"result": {"tools": TOOLS}}
        if method != "tools/call":
            return {"error": {"code": -32601, "message": f"no method {method}"}}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            return {"result": {"content": [{"type": "text", "text": args.get("text", "")}]}}
        if name == "add":
            return {"result": {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}}
        if name == "fail":
            return {"result": {"content": [{"type": "text", "text": "the remote tool failed"}], "isError": True}}
        if name == "slow":
            time.sleep(float(args.get("seconds", 1)))
            return {"result": {"content": [{"type": "text", "text": "done"}]}}
        if name == "whoami":
            return {"result": {"content": [{"type": "text", "text": sid}]}}
        return {"error": {"code": -32602, "message": f"unknown tool {name}"}}

    def end_session(self, sid: str) -> None:
        self.sessions.discard(sid)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
