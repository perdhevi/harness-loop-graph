"""Chapter K — MCP over Streamable HTTP, for remote MCP servers.

Same session as stdio (handshake, tools/list, tools/call), different transport:

- every JSON-RPC message is an HTTP POST to one endpoint URL,
  with Accept: application/json, text/event-stream
- a request's reply is either one JSON body, or an SSE stream that may carry
  server requests (ping) and notifications before the reply
- notifications get 202 Accepted and no body
- the server may hand out an Mcp-Session-Id on initialize; it's sent back on every
  later request, along with MCP-Protocol-Version; close sends DELETE

Headers (e.g. Authorization) come from mcp.json, with ${ENV_VAR} filled in by load_servers,
so tokens stay out of the file. OAuth sign-in and the older HTTP+SSE transport are not
covered; for those, a stdio bridge such as `npx mcp-remote <url>` still works.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from tools.mcp_client import CLIENT_INFO, McpError, McpSession

USER_AGENT = f"{CLIENT_INFO['name']}/{CLIENT_INFO['version']}"


class HttpMcpClient(McpSession):
    """MCP over Streamable HTTP: the server is a URL."""

    def __init__(self, name: str, url: str, *, headers: dict | None = None, timeout_s: float = 60):
        self.name = name
        self.url = url
        self.timeout_s = timeout_s
        self.headers = dict(headers or {})
        self.server_info: dict = {}
        self.session_id: str | None = None
        self._initialized = False
        self._closed = False
        self._next_id = 0

    # ------------------------------------------------------------ lifecycle

    def initialize(self) -> dict:
        result = super().initialize()
        self._initialized = True
        return result

    def _notify(self, method: str) -> None:
        self._post_message({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.session_id:                      # end the session; servers may answer 405, that's fine
            try:
                req = urllib.request.Request(self.url, method="DELETE", headers=self._headers())
                urllib.request.urlopen(req, timeout=5).close()
            except (urllib.error.URLError, OSError, ValueError):
                pass

    # ------------------------------------------------------------ requests

    def _request(self, method: str, params: dict, timeout_s: float | None = None) -> dict:
        if self._closed:
            raise McpError(f"[{self.name}] the connection to {self.url} is closed")
        timeout = timeout_s or self.timeout_s
        deadline = time.monotonic() + timeout
        self._next_id += 1
        mid = self._next_id
        try:
            resp = self._open({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}, timeout, method)
            with resp:
                if method == "initialize":
                    self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
                kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    reply = self._read_events(resp, mid, method, deadline)
                else:
                    reply = self._from_json_body(resp.read(), mid, method)
        except (socket.timeout, TimeoutError) as e:
            raise McpError(f"[{self.name}] {method} timed out after {timeout}s") from e
        if "error" in reply:
            err = reply["error"]
            raise McpError(f"[{self.name}] {method} failed ({err.get('code')}): {err.get('message')}")
        return reply.get("result", {})

    def _from_json_body(self, body: bytes, mid: int, method: str) -> dict:
        try:
            data = json.loads(body.decode("utf-8")) if body.strip() else None
        except (ValueError, UnicodeDecodeError) as e:
            raise McpError(f"[{self.name}] {method}: the server's reply isn't JSON ({e})") from e
        for msg in data if isinstance(data, list) else [data]:
            if isinstance(msg, dict) and msg.get("id") == mid and ("result" in msg or "error" in msg):
                return msg
        raise McpError(f"[{self.name}] {method}: the server's reply has no response for request {mid}")

    def _read_events(self, resp, mid: int, method: str, deadline: float) -> dict:
        """Read the SSE stream until our reply arrives; answer server requests on the way."""
        data: list[str] = []

        def dispatch(payload: str) -> dict | None:
            try:
                msg = json.loads(payload)
            except ValueError:
                return None
            for m in msg if isinstance(msg, list) else [msg]:
                if not isinstance(m, dict):
                    continue
                if "method" in m:
                    if "id" in m:                # a request from the server, e.g. ping: answer it
                        self._post_message(self._server_reply(m))
                    continue                     # notifications (progress, logging): ignored
                if m.get("id") == mid:
                    return m
            return None

        for raw in resp:
            if time.monotonic() > deadline:
                raise TimeoutError
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:][1:] if line[5:6] == " " else line[5:])
                continue
            if line or not data:                 # event:, id:, retry:, ":" comments; or a blank line with no data
                continue
            found, data = dispatch("\n".join(data)), []
            if found:
                return found
        if data:                                 # the stream ended right after a data line
            found = dispatch("\n".join(data))
            if found:
                return found
        raise McpError(f"[{self.name}] {method}: the server closed the event stream without a response")

    # ------------------------------------------------------------ HTTP

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT, **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self._initialized:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _open(self, msg: dict, timeout: float, what: str):
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={**self._headers(), "Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", errors="replace").strip()
            if e.code in (401, 403):
                hint = ("check the Authorization header in mcp.json and its environment variable"
                        if any(k.lower() == "authorization" for k in self.headers)
                        else "this server needs credentials: add an Authorization header in mcp.json")
                raise McpError(f"[{self.name}] {what}: HTTP {e.code} {e.reason} — {hint}") from e
            if e.code == 404 and self.session_id:
                raise McpError(f"[{self.name}] {what}: the server ended the session (HTTP 404); "
                               "start the run again to reconnect") from e
            raise McpError(f"[{self.name}] {what}: HTTP {e.code} {e.reason}"
                           + (f": {detail}" if detail else "")) from e
        except urllib.error.URLError as e:
            raise McpError(f"[{self.name}] could not reach {self.url}: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:
            raise McpError(f"[{self.name}] {what} timed out after {timeout}s") from e

    def _post_message(self, msg: dict) -> None:
        """A notification or a reply to the server: expect 202 Accepted, nothing to read."""
        with self._open(msg, self.timeout_s, msg.get("method") or "reply") as resp:
            resp.read()
