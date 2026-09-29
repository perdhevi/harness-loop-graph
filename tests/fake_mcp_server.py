"""A misbehaving MCP server for client tests.

Tools:
  echo      — returns its text
  crash     — writes to stderr and exits with code 3
  hang      — never answers
  ping_me   — sends the client a ping request first, then reports the client's reply
Also returns tools/list in two pages to exercise nextCursor.
"""

import json
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def tool(name):
    return {"name": name, "description": f"{name} tool",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}


for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None or method is None:
        continue
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "tools/list":
        if params.get("cursor") == "page2":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [tool("hang"), tool("ping_me")]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [tool("echo"), tool("crash")],
                                                            "nextCursor": "page2"}})
    elif method == "tools/call":
        name = params["name"]
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": params["arguments"].get("text", "")},
                            {"type": "image", "data": "", "mimeType": "image/png"}]}})
        elif name == "crash":
            sys.stderr.write("boom: fake server crashed\n")
            sys.stderr.flush()
            sys.exit(3)
        elif name == "hang":
            pass
        elif name == "ping_me":
            send({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
            reply = json.loads(sys.stdin.readline())
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(reply)}]}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
