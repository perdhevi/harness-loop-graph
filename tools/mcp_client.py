"""Stage 4 — a minimal MCP client over stdio, and a tool source for the registry.

Protocol: newline-delimited JSON-RPC 2.0 on the server's stdin/stdout.
Lifecycle: initialize → notifications/initialized → tools/list, tools/call … → close.

Written by hand with the standard library (build to delete). If protocol
details start getting in the way, the official `mcp` SDK is the fallback:
McpToolSource's interface wouldn't change.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from tools.registry import ToolSpec

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "harness-loop-graph", "version": "0.4.0"}
_EOF = object()


class McpError(RuntimeError):
    """Transport or protocol failure: timeout, dead server, JSON-RPC error."""


class McpToolError(RuntimeError):
    """The tool ran and reported isError=true."""


class McpClient:
    def __init__(self, name: str, command: str, args: list[str], *,
                 env: dict | None = None, cwd: str | Path | None = None, timeout_s: float = 60):
        self.name = name
        self.timeout_s = timeout_s
        self.server_info: dict = {}
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._eof = threading.Event()
        try:
            self._proc = subprocess.Popen(
                [command, *args], cwd=cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, **(env or {})}, text=True, encoding="utf-8", bufsize=1,
            )
        except FileNotFoundError as e:
            raise McpError(f"[{name}] could not start server: {e}") from e
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ------------------------------------------------------------ lifecycle

    def initialize(self) -> dict:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = result.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ tools

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict, timeout_s: float | None = None) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout_s)
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content]")
        if not parts and "structuredContent" in result:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts)
        if result.get("isError"):
            raise McpToolError(text or "tool reported an error")
        return text

    # ------------------------------------------------------------ transport

    def _send(self, msg: dict) -> None:
        # ASCII on the wire (non-ASCII as \uXXXX): decodes the same under any pipe encoding —
        # on Windows a server's stdin defaults to cp1252, which corrupts or crashes on UTF-8 bytes
        line = json.dumps(msg, ensure_ascii=True) + "\n"
        with self._write_lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise McpError(self._dead_message(f"could not write to server: {e}")) from e

    def _request(self, method: str, params: dict, timeout_s: float | None = None) -> dict:
        if self._proc.poll() is not None:
            raise McpError(self._dead_message("server is not running"))
        with self._id_lock:
            self._next_id += 1
            mid = self._next_id
        box: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[mid] = box
        try:
            if self._eof.is_set():   # reader already saw the server go away
                raise McpError(self._dead_message(f"server exited before {method}"))
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            try:
                reply = box.get(timeout=timeout_s or self.timeout_s)
            except queue.Empty:
                raise McpError(f"[{self.name}] {method} timed out after {timeout_s or self.timeout_s}s")
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)
        if reply is _EOF:
            raise McpError(self._dead_message(f"server exited during {method}"))
        if "error" in reply:
            err = reply["error"]
            raise McpError(f"[{self.name}] {method} failed ({err.get('code')}): {err.get('message')}")
        return reply.get("result", {})

    def _read_stdout(self) -> None:
        try:
            self._read_stdout_lines()
        except (ValueError, OSError):   # pipe closed by close()
            pass
        # stdout closed: the server is gone; wake everyone who is waiting
        self._eof.set()
        with self._pending_lock:
            boxes = list(self._pending.values())
        for box in boxes:
            try:
                box.put_nowait(_EOF)
            except queue.Full:
                pass

    def _read_stdout_lines(self) -> None:
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_tail.append(f"(non-JSON on stdout) {line[:200]}")
                continue
            if "method" in msg:
                self._handle_server_message(msg)
                continue
            with self._pending_lock:
                box = self._pending.get(msg.get("id"))
            if box is not None:
                box.put(msg)

    def _handle_server_message(self, msg: dict) -> None:
        if "id" not in msg:
            return  # notification (logging, progress, list_changed): ignored for now
        if msg["method"] == "ping":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": f"client does not support {msg['method']}"}}
        try:
            self._send(reply)
        except McpError:
            pass

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr:
                self._stderr_tail.append(line.rstrip())
        except (ValueError, OSError):
            pass

    def _dead_message(self, what: str) -> str:
        code = self._proc.poll()
        tail = " | ".join(list(self._stderr_tail)[-5:])
        status = f"exit code {code}" if code is not None else "still running"
        return f"[{self.name}] {what} ({status})" + (f". stderr: {tail}" if tail else "")


class McpToolSource:
    """Exposes one MCP server's tools to the registry as '<server>.<tool>'."""

    def __init__(self, client: McpClient):
        self.client = client
        self._specs = [
            ToolSpec(
                name=f"{client.name}.{t['name']}",
                description=t.get("description", ""),
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for t in client.list_tools()
        ]

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, name: str, args: dict) -> Any:
        prefix = self.client.name + "."
        return self.client.call_tool(name[len(prefix):] if name.startswith(prefix) else name, args)


def load_servers(path: str | Path, workspace: str | Path) -> dict[str, dict]:
    """Read mcp.json, keep enabled servers, fill in {python} and {workspace}."""
    with open(path, encoding="utf-8") as f:
        servers = json.load(f).get("servers", {})
    ws = str(Path(workspace).resolve())

    def fill(s: str) -> str:
        return s.replace("{python}", sys.executable).replace("{workspace}", ws)

    out = {}
    for name, cfg in servers.items():
        if not cfg.get("enabled", True):
            continue
        out[name] = {
            "command": fill(cfg["command"]),
            "args": [fill(a) for a in cfg.get("args", [])],
            "env": {k: fill(v) for k, v in cfg.get("env", {}).items()},
            "timeout_s": cfg.get("timeout_s", 60),
        }
    return out
