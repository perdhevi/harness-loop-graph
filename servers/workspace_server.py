"""Stage 4 — workspace MCP server.

A minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), written
by hand with the standard library. It gives an agent file tools and a
command runner, all confined to one root folder.

    python servers/workspace_server.py --root ./workspace --allow python,pytest

Safety: paths can't escape the root, commands run without a shell and must
start with an allowed program, and every command has a timeout. This is NOT
a sandbox — an allowed `python` can still run arbitrary code. Use a scratch
folder, and a container when you need a real boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SERVER_INFO = {"name": "harness-workspace", "version": "0.4.0"}
DEFAULT_PROTOCOL = "2025-06-18"
READ_LIMIT = 100_000
OUTPUT_LIMIT = 100_000   # per stream; the harness's OutputLimiter (Chapter C) decides what the model sees
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}


class ToolError(Exception):
    """A tool-level failure: reported to the model as isError=true."""


# ---------------------------------------------------------------- tool logic

class Workspace:
    def __init__(self, root: Path, allow: set[str]):
        self.root = root.resolve()
        self.allow = allow

    def _path(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path '{rel}' is outside the workspace")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    def read_file(self, path: str) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        data = p.read_bytes()
        text = data[:READ_LIMIT].decode("utf-8", errors="replace")
        if len(data) > READ_LIMIT:
            text += f"\n… [truncated: showing {READ_LIMIT} of {len(data)} bytes]"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.is_dir():
            raise ToolError(f"'{path}' is a directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return f"{'overwrote' if existed else 'created'} {self._rel(p)} ({len(content.encode('utf-8'))} bytes)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path}")
        if not old:
            raise ToolError("'old' must not be empty")
        text = p.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"'old' text not found in {path}")
        if count > 1:
            raise ToolError(f"'old' text appears {count} times in {path}; include more context so it is unique")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {self._rel(p)}"

    def list_dir(self, path: str = ".", recursive: bool = False) -> str:
        base = self._path(path)
        if not base.is_dir():
            raise ToolError(f"no such directory: {path}")
        entries: list[str] = []

        def walk(d: Path):
            for child in sorted(d.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
                if child.name in SKIP_DIRS:
                    continue
                entries.append(self._rel(child) + ("/" if child.is_dir() else ""))
                if recursive and child.is_dir() and not child.is_symlink():
                    walk(child)
                if len(entries) >= 1000:
                    return

        walk(base)
        return "\n".join(entries) if entries else "(empty)"

    def run_command(self, command: str, timeout_s: int = 60) -> str:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"could not parse command: {e}")
        if not argv:
            raise ToolError("empty command")
        program = Path(argv[0]).name
        name = program.lower()
        if name.endswith(".exe"):                  # Windows: python.exe → python
            name = name[:-4]
        if name not in self.allow:
            raise ToolError(f"'{program}' is not allowed. Allowed: {', '.join(sorted(self.allow))}")
        timeout_s = max(1, min(int(timeout_s), 300))
        try:
            # No bytecode cache: Python validates .pyc files by mtime (whole seconds) and
            # size, so a same-size edit within one second would silently run the old code.
            # PYTHONUTF8: a child Python prints ✓ or — without crashing on a Windows cp1252 pipe
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.run(argv, cwd=self.root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {timeout_s}s: {command}")
        except FileNotFoundError:
            raise ToolError(f"program not found: {argv[0]}")

        def clip(s: str) -> str:
            # keep the start AND the end: errors and test summaries are usually at the end
            if len(s) <= OUTPUT_LIMIT:
                return s
            half = OUTPUT_LIMIT // 2
            return s[:half] + f"\n… [truncated {len(s) - 2 * half} chars in the middle]\n" + s[-half:]

        return f"exit code: {proc.returncode}\n--- stdout ---\n{clip(proc.stdout)}\n--- stderr ---\n{clip(proc.stderr)}"


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"name": "read_file", "description": "Read a text file from the workspace.",
     "inputSchema": _schema({"path": {"type": "string", "description": "path relative to the workspace"}}, ["path"])},
    {"name": "write_file", "description": "Create or overwrite a file (parent folders are created).",
     "inputSchema": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "edit_file", "description": "Replace one exact, unique occurrence of 'old' with 'new' in a file.",
     "inputSchema": _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                            ["path", "old", "new"])},
    {"name": "list_dir", "description": "List files and folders (folders end with '/').",
     "inputSchema": _schema({"path": {"type": "string"}, "recursive": {"type": "boolean"}}, [])},
    {"name": "run_command",
     "description": "Run a command in the workspace root without a shell (no pipes or &&). "
                    "Returns exit code, stdout and stderr.",
     "inputSchema": _schema({"command": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["command"])},
]


# ---------------------------------------------------------------- protocol

class Server:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None or mid is None:    # a response or a notification: nothing to send back
            return None
        try:
            result = self._dispatch(method, msg.get("params") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                    "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params.get("name"), params.get("arguments") or {})
        raise _RpcError(-32601, f"method not found: {method}")

    def _call(self, name: str, args: dict) -> dict:
        fn = getattr(self.ws, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            raise _RpcError(-32602, f"unknown tool: {name}")
        try:
            text, is_error = fn(**args), False
        except ToolError as e:
            text, is_error = str(e), True
        except TypeError as e:
            text, is_error = f"bad arguments: {e}", True
        except OSError as e:
            text, is_error = f"{type(e).__name__}: {e}", True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--allow", default="python,python3,pytest")
    a = ap.parse_args()
    # Windows: piped stdin/stdout default to the locale encoding (cp1252); the protocol is UTF-8
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    root = Path(a.root)
    root.mkdir(parents=True, exist_ok=True)
    server = Server(Workspace(root, {x.strip() for x in a.allow.split(",") if x.strip()}))
    print(f"[workspace-server] root={root.resolve()}", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
