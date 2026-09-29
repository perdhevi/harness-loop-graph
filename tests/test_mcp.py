"""Stage 4 tests — MCP client, workspace server, and both wired into the registry.

These start real subprocesses; no model is needed.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.mcp_client import (McpClient, McpError, McpToolError,  # noqa: E402
                              McpToolSource, load_servers)
from tools.registry import ToolRegistry  # noqa: E402

SERVER = str(ROOT / "servers" / "workspace_server.py")
FAKE = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def start_workspace(root: Path, allow="python,python3") -> McpClient:
    c = McpClient("workspace", sys.executable, [SERVER, "--root", str(root), "--allow", allow], timeout_s=20)
    c.initialize()
    return c


class WorkspaceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "ws"
        cls.outside = Path(cls.tmp.name) / "secret.txt"
        cls.outside.write_text("do not read")
        cls.client = start_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def call(self, name, **args):
        return self.client.call_tool(name, args)

    def test_handshake_and_tools(self):
        self.assertEqual(self.client.server_info["name"], "harness-workspace")
        names = {t["name"] for t in self.client.list_tools()}
        self.assertEqual(names, {"read_file", "write_file", "edit_file", "list_dir", "run_command"})

    def test_write_read_edit(self):
        self.assertIn("created src/app.py", self.call("write_file", path="src/app.py", content="x = 1\ny = 1\n"))
        self.assertIn("overwrote", self.call("write_file", path="src/app.py", content="x = 1\ny = 2\n"))
        self.assertEqual(self.call("read_file", path="src/app.py"), "x = 1\ny = 2\n")
        self.assertIn("edited", self.call("edit_file", path="src/app.py", old="y = 2", new="y = 3"))
        self.assertIn("y = 3", self.call("read_file", path="src/app.py"))

    def test_edit_must_be_unique(self):
        self.call("write_file", path="dup.txt", content="a\na\n")
        with self.assertRaisesRegex(McpToolError, "appears 2 times"):
            self.call("edit_file", path="dup.txt", old="a", new="b")
        with self.assertRaisesRegex(McpToolError, "not found"):
            self.call("edit_file", path="dup.txt", old="zzz", new="b")

    def test_list_dir(self):
        self.call("write_file", path="pkg/mod.py", content="")
        self.call("write_file", path="pkg/__pycache__/mod.pyc", content="")
        listing = self.call("list_dir", path=".", recursive=True).splitlines()
        self.assertIn("pkg/", listing)
        self.assertIn("pkg/mod.py", listing)
        self.assertFalse(any("__pycache__" in x for x in listing))

    def test_paths_cannot_escape(self):
        for bad in ["../secret.txt", str(self.outside), "pkg/../../secret.txt"]:
            with self.subTest(path=bad), self.assertRaisesRegex(McpToolError, "outside the workspace"):
                self.call("read_file", path=bad)
        with self.assertRaisesRegex(McpToolError, "outside the workspace"):
            self.call("write_file", path="../evil.txt", content="x")
        self.assertFalse((self.root.parent / "evil.txt").exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlink_cannot_escape(self):
        (self.root / "link").symlink_to(self.outside)
        with self.assertRaisesRegex(McpToolError, "outside the workspace"):
            self.call("read_file", path="link")

    def test_run_command(self):
        self.call("write_file", path="hello.py", content="print('hi from workspace')\n")
        out = self.call("run_command", command="python hello.py")
        self.assertIn("exit code: 0", out)
        self.assertIn("hi from workspace", out)

    def test_same_size_edit_is_not_hidden_by_bytecode_cache(self):
        """Found in Stage 09: a same-size rewrite within one second ran stale .pyc code."""
        self.call("write_file", path="m.py", content="def f():\n    return 1 + 1\n")
        self.call("write_file", path="use_m.py", content="import m\nprint(m.f())\n")
        self.assertIn("\n2\n", self.call("run_command", command="python use_m.py"))
        self.call("write_file", path="m.py", content="def f():\n    return 1 - 1\n")   # same size
        self.assertIn("\n0\n", self.call("run_command", command="python use_m.py"))
        self.assertFalse((self.root / "__pycache__").exists())

    def test_long_output_keeps_head_and_tail(self):
        """Found in Chapter C: the server used to keep only the head, losing the end of test output."""
        out = self.call("run_command", command="python -c \"print('FIRST'); [print('x' * 80) for _ in range(2000)]; print('LAST')\"")
        self.assertIn("FIRST", out)
        self.assertIn("LAST", out)
        self.assertIn("chars in the middle]", out)

    def test_run_command_reports_failure_exit_code(self):
        out = self.call("run_command", command="python -c \"import sys; sys.exit(4)\"")
        self.assertIn("exit code: 4", out)

    def test_command_allow_list_and_no_shell(self):
        with self.assertRaisesRegex(McpToolError, "'rm' is not allowed"):
            self.call("run_command", command="rm -rf .")
        # '>' is just an argument without a shell: no file gets created
        self.call("run_command", command="python -c \"print(1)\" > redirected.txt")
        self.assertFalse((self.root / "redirected.txt").exists())

    def test_command_timeout(self):
        with self.assertRaisesRegex(McpToolError, "timed out after 1s"):
            self.call("run_command", command="python -c \"import time; time.sleep(5)\"", timeout_s=1)

    def test_unknown_tool_is_protocol_error(self):
        with self.assertRaisesRegex(McpError, "unknown tool"):
            self.call("delete_everything")


class ClientFailureTests(unittest.TestCase):
    def start_fake(self, timeout_s=5):
        c = McpClient("fake", sys.executable, [FAKE], timeout_s=timeout_s)
        c.initialize()
        self.addCleanup(c.close)
        return c

    def test_pagination_and_content_blocks(self):
        c = self.start_fake()
        self.assertEqual([t["name"] for t in c.list_tools()], ["echo", "crash", "hang", "ping_me"])
        self.assertEqual(c.call_tool("echo", {"text": "hi"}), "hi\n[image content]")

    def test_answers_server_ping(self):
        c = self.start_fake()
        reply = json.loads(c.call_tool("ping_me", {}))
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": "srv-1", "result": {}})

    def test_crash_is_reported_not_hung(self):
        c = self.start_fake(timeout_s=10)
        t0 = time.monotonic()
        with self.assertRaises(McpError) as ctx:
            c.call_tool("crash", {})
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("server exited", str(ctx.exception))
        time.sleep(0.2)
        with self.assertRaisesRegex(McpError, "not running|exited"):
            c.call_tool("echo", {"text": "again"})

    def test_crash_message_has_stderr(self):
        c = self.start_fake()
        with self.assertRaises(McpError):
            c.call_tool("crash", {})
        time.sleep(0.2)
        with self.assertRaises(McpError) as ctx:
            c.call_tool("echo", {"text": "x"})
        self.assertIn("boom: fake server crashed", str(ctx.exception))
        self.assertIn("exit code 3", str(ctx.exception))

    def test_hang_times_out(self):
        c = self.start_fake(timeout_s=1)
        with self.assertRaisesRegex(McpError, "timed out after 1"):
            c.call_tool("hang", {})

    def test_missing_program(self):
        with self.assertRaisesRegex(McpError, "could not start"):
            McpClient("ghost", "definitely-not-a-real-program-xyz", [])


class RegistryIntegrationTests(unittest.TestCase):
    def test_namespaced_tools_through_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = start_workspace(Path(tmp))
            self.addCleanup(client.close)
            reg = ToolRegistry()
            reg.add_source(McpToolSource(client))
            self.assertIn("- workspace.write_file(path: string, content: string)", reg.describe())
            self.assertIn("created a.txt", reg.call("workspace.write_file", {"path": "a.txt", "content": "A"}))
            self.assertEqual(reg.call("workspace.read_file", {"path": "a.txt"}), "A")
            # registry validation runs before the server is called
            self.assertIn("missing required 'content'", reg.call("workspace.write_file", {"path": "b.txt"}))
            # tool errors from the server become observations
            obs = reg.call("workspace.read_file", {"path": "../x"})
            self.assertTrue(obs.startswith("Error: workspace.read_file failed: McpToolError: "))

    def test_crashed_server_is_an_observation(self):
        client = McpClient("fake", sys.executable, [FAKE], timeout_s=5)
        client.initialize()
        self.addCleanup(client.close)
        reg = ToolRegistry()
        reg.add_source(McpToolSource(client))
        obs = reg.call("fake.crash", {})
        self.assertTrue(obs.startswith("Error: fake.crash failed: McpError:"), obs)


class LoadServersTests(unittest.TestCase):
    def test_placeholders_and_enabled(self):
        servers = load_servers(ROOT / "mcp.json", "some/ws")
        self.assertIn("workspace", servers)
        self.assertNotIn("filesystem", servers)          # disabled by default
        ws = servers["workspace"]
        self.assertEqual(ws["command"], sys.executable)
        self.assertIn(str(Path("some/ws").resolve()), ws["args"])


if __name__ == "__main__":
    unittest.main()
