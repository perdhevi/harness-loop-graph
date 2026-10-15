"""Chapter K — MCP over Streamable HTTP, against tests/fake_http_mcp_server.py."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_http_mcp_server import FakeHttpMcp  # noqa: E402
from loop import run_loop  # noqa: E402
from runtime import build_registry, load_json  # noqa: E402
from tools.mcp_client import McpError, McpToolError, McpToolSource, load_servers  # noqa: E402
from tools.mcp_http import HttpMcpClient  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402


class Server(unittest.TestCase):
    sse = False
    token = None

    def setUp(self):
        self.fake = FakeHttpMcp(sse=self.sse, token=self.token)
        self.addCleanup(self.fake.close)

    def client(self, **kw) -> HttpMcpClient:
        c = HttpMcpClient("remote", self.fake.url, timeout_s=kw.pop("timeout_s", 10), **kw)
        self.addCleanup(c.close)
        c.initialize()
        return c


class JsonReplyTests(Server):
    def test_handshake_session_and_headers(self):
        c = self.client()
        self.assertEqual(c.server_info["name"], "fake-http")
        self.assertIn(c.session_id, self.fake.sessions)
        self.assertEqual([t["name"] for t in c.list_tools()], ["echo", "add", "fail", "slow", "whoami"])
        posts = [r for r in self.fake.requests if r["method"] == "POST"]
        init, initialized, listing = posts[0], posts[1], posts[2]
        self.assertNotIn("Mcp-Session-Id", init["headers"])
        self.assertEqual(initialized["body"], {"jsonrpc": "2.0", "method": "notifications/initialized"})
        for r in (initialized, listing):
            self.assertEqual(r["headers"]["Mcp-Session-Id"], c.session_id)
        self.assertEqual(listing["headers"]["Mcp-Protocol-Version"], "2025-06-18")
        self.assertIn("text/event-stream", listing["headers"]["Accept"])

    def test_tools_through_the_registry(self):
        reg = ToolRegistry()
        reg.add_source(McpToolSource(self.client()))
        self.assertIn("remote.add(a: number, b: number)", reg.describe())
        self.assertEqual(reg.call("remote.add", {"a": 2, "b": 3}), "5")
        self.assertEqual(reg.call("echo", {"text": "héllo ✓"}), "(ran as remote.echo; use that exact name next time)\nhéllo ✓")
        self.assertIn("Error: remote.fail failed: McpToolError: the remote tool failed", reg.call("remote.fail", {}))

    def test_loop_uses_a_remote_tool(self):
        reg = ToolRegistry()
        reg.add_source(McpToolSource(self.client()))
        replies = iter([json.dumps({"thought": "t", "action": "remote.add", "args": {"a": 40, "b": 2}}),
                        json.dumps({"thought": "t", "final": "42"})])
        model = type("M", (), {"complete": lambda self, s, m: next(replies)})()
        r = run_loop(model, "{tools}", "add", reg)
        self.assertEqual((r.status, r.steps[0].observation, r.answer), ("final", "42", "42"))

    def test_close_ends_the_session(self):
        c = self.client()
        sid = c.session_id
        c.close()
        self.assertEqual(self.fake.ended, [sid])
        with self.assertRaisesRegex(McpError, "connection .* is closed"):
            c.list_tools()

    def test_tool_error_and_timeout(self):
        c = self.client(timeout_s=0.5)
        with self.assertRaisesRegex(McpToolError, "the remote tool failed"):
            c.call_tool("fail", {})
        with self.assertRaisesRegex(McpError, r"tools/call timed out after 0.5s"):
            c.call_tool("slow", {"seconds": 2})

    def test_expired_session(self):
        c = self.client()
        self.fake.end_session(c.session_id)
        with self.assertRaisesRegex(McpError, "the server ended the session"):
            c.list_tools()

    def test_unreachable(self):
        c = HttpMcpClient("gone", "http://127.0.0.1:9/mcp", timeout_s=2)
        with self.assertRaisesRegex(McpError, r"\[gone\] could not reach http://127.0.0.1:9/mcp"):
            c.initialize()


class SseReplyTests(Server):
    sse = True

    def test_reply_in_an_event_stream_after_a_ping(self):
        c = self.client()
        self.assertEqual(c.call_tool("echo", {"text": "via sse"}), "via sse")
        self.assertTrue(self.fake.ping_answered.is_set())                    # the client answered the server's ping
        replies = [r for r in self.fake.requests if r["body"] and r["body"].get("id") == "ping-1"]
        self.assertEqual(replies[0]["headers"]["Mcp-Session-Id"], c.session_id)
        self.assertEqual(len(c.list_tools()), 5)                              # tools/list over SSE too


class AuthTests(Server):
    token = "s3cret"

    def test_missing_or_wrong_token(self):
        with self.assertRaisesRegex(McpError, "HTTP 401 .* add an Authorization header in mcp.json"):
            HttpMcpClient("remote", self.fake.url).initialize()
        with self.assertRaisesRegex(McpError, "HTTP 401 .* check the Authorization header"):
            HttpMcpClient("remote", self.fake.url, headers={"Authorization": "Bearer nope"}).initialize()

    def test_token_from_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "mcp.json"
            cfg.write_text(json.dumps({"servers": {"remote": {
                "url": self.fake.url, "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}, "timeout_s": 7}}}))
            with mock.patch.dict(os.environ, {"TEST_MCP_TOKEN": "s3cret"}):
                srv = load_servers(cfg, d)["remote"]
            self.assertEqual(srv, {"url": self.fake.url, "headers": {"Authorization": "Bearer s3cret"}, "timeout_s": 7})
            c = HttpMcpClient("remote", srv["url"], headers=srv["headers"])
            self.addCleanup(c.close)
            self.assertEqual(c.initialize()["serverInfo"]["name"], "fake-http")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(McpError, r"\[remote\] header Authorization needs the environment "
                                                      "variable TEST_MCP_TOKEN, which isn't set"):
                    load_servers(cfg, d)


class RegistryTests(Server):
    def test_local_and_remote_servers_together(self):
        mcp = load_json(ROOT / "mcp.json")
        mcp["servers"]["docs"] = {"url": self.fake.url, "timeout_s": 10}
        mcp["servers"]["off"] = {"url": "http://127.0.0.1:9/never", "enabled": False}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "mcp.json").write_text(json.dumps(mcp))
            config = {"tools": {"local": "tools.json", "mcp": str(Path(d) / "mcp.json")}}
            with contextlib.ExitStack() as stack:
                reg = build_registry(config, Path(d), stack)
                names = [t.name for t in reg.list_tools()]
                self.assertIn("workspace.write_file", names)                     # stdio, as before
                self.assertIn("docs.echo", names)                                # http
                self.assertNotIn("off.echo", names)
                self.assertEqual(reg.call("docs.echo", {"text": "hi"}), "hi")
                self.assertIn("created", reg.call("workspace.write_file", {"path": "a.txt", "content": "x"}))
            self.assertEqual(len(self.fake.ended), 1)                            # the run's stack closed the session

    def test_list_tools_command(self):
        import main
        mcp = load_json(ROOT / "mcp.json")
        mcp["servers"]["docs"] = {"url": self.fake.url}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "mcp.json").write_text(json.dumps(mcp))
            config = load_json(ROOT / "config.json")
            config["tools"]["mcp"] = str(Path(d) / "mcp.json")
            (Path(d) / "config.json").write_text(json.dumps(config))
            with mock.patch.object(main, "CONFIG_PATH", Path(d) / "config.json"), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main.main(["--list-tools", "--workspace", d]), 0)
        self.assertIn("- docs.add(a: number, b: number) — Add two numbers.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
