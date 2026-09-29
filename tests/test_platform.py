"""Fixes found when the first build didn't create files on Windows with Ollama.

1. Windows pipes default to cp1252: UTF-8 file content was corrupted or crashed the workspace server.
2. Ollama's small default context (4,096 tokens on < ~23 GB VRAM) silently drops the start of long prompts.
3. qwen3 "thinks" by default; replies can end up in message.thinking with empty content.
"""

import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from model_adapter import OllamaAdapter  # noqa: E402
from tools.mcp_client import McpClient, McpToolError  # noqa: E402

SERVER = str(ROOT / "servers" / "workspace_server.py")


class WindowsEncodingTests(unittest.TestCase):
    """The server runs with cp1252 pipes, as it would on Windows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = McpClient("workspace", sys.executable, [SERVER, "--root", str(self.root), "--allow", "python"],
                           env={"PYTHONIOENCODING": "cp1252"}, timeout_s=15)
        self.addCleanup(self.c.close)
        self.c.initialize()

    def test_non_ascii_content_is_written_intact(self):
        for name, text in [("dash.md", "# To-do CLI — simple\n"), ("cyr.md", "Ёлка ✓\n"), ("id.md", "Selamat datang, Raditya 🙂\n")]:
            self.c.call_tool("write_file", {"path": name, "content": text})
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), text)
            self.assertEqual(self.c.call_tool("read_file", {"path": name}), text)

    def test_server_survives_bytes_cp1252_cannot_decode(self):
        self.c.call_tool("write_file", {"path": "a.md", "content": "Ё"})          # UTF-8 D0 81: 0x81 is undefined in cp1252
        self.assertIn("created b.md", self.c.call_tool("write_file", {"path": "b.md", "content": "still alive"}))

    def test_child_python_can_print_unicode(self):
        self.c.call_tool("write_file", {"path": "show.py", "content": "print('done ✓ — Ёлка')\n"})
        out = self.c.call_tool("run_command", {"command": "python show.py"})
        self.assertIn("exit code: 0", out)
        self.assertIn("done ✓ — Ёлка", out)

    def test_exe_suffix_passes_the_allow_list(self):
        with self.assertRaises(McpToolError) as ctx:
            self.c.call_tool("run_command", {"command": "python.exe -c \"print(1)\""})
        self.assertNotIn("not allowed", str(ctx.exception))                    # on Linux: "program not found"


class OllamaAdapterTests(unittest.TestCase):
    def serve(self, reply: dict):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                out = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        self.addCleanup(srv.server_close)
        return f"http://127.0.0.1:{srv.server_address[1]}", seen

    def test_sends_num_ctx_and_think(self):
        url, seen = self.serve({"message": {"role": "assistant", "content": "{\"final\": \"ok\"}"}})
        a = OllamaAdapter({"base_url": url, "model": "qwen3:8b", "num_ctx": 16384, "think": False})
        self.assertEqual(a.complete("sys", [{"role": "user", "content": "hi"}]), '{"final": "ok"}')
        self.assertEqual(seen[0]["options"], {"num_ctx": 16384})
        self.assertIs(seen[0]["think"], False)

    def test_leaves_defaults_alone_when_unset(self):
        url, seen = self.serve({"message": {"role": "assistant", "content": "x"}})
        OllamaAdapter({"base_url": url, "model": "m"}).complete("s", [])
        self.assertNotIn("options", seen[0])
        self.assertNotIn("think", seen[0])

    def test_thinking_only_reply_is_not_lost(self):
        url, _ = self.serve({"message": {"role": "assistant", "content": "", "thinking": "{\"final\": \"from thinking\"}"}})
        a = OllamaAdapter({"base_url": url, "model": "qwen3:8b"})
        self.assertEqual(a.complete("s", []), '{"final": "from thinking"}')


class ThinkNotSupportedTests(unittest.TestCase):
    def test_retries_without_think(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                if "think" in body:
                    out, code = json.dumps({"error": '"gemma4:e4b" does not support thinking'}).encode(), 400
                else:
                    out, code = json.dumps({"message": {"role": "assistant", "content": "ok"}}).encode(), 200
                self.send_response(code)
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        a = OllamaAdapter({"base_url": f"http://127.0.0.1:{srv.server_address[1]}", "model": "gemma4:e4b", "think": False})
        self.assertEqual(a.complete("s", []), "ok")
        self.assertEqual(a.complete("s", []), "ok")
        self.assertEqual(["think" in b for b in seen], [True, False, False])     # asked once, then remembered


class SettingsCheckTests(unittest.TestCase):
    def config(self, num_ctx, window):
        c = json.loads((ROOT / "config.json").read_text())
        c["provider"] = "ollama"
        c["providers"]["ollama"].pop("num_ctx", None)
        if num_ctx:
            c["providers"]["ollama"]["num_ctx"] = num_ctx
        c.setdefault("context", {})["window_tokens"] = window
        return c

    def test_warnings(self):
        self.assertIn("not set", main.check_context_settings(self.config(None, 16000), say=lambda m: None)[0])
        self.assertIn("larger than", main.check_context_settings(self.config(4096, 16000), say=lambda m: None)[0])
        self.assertEqual(main.check_context_settings(self.config(16384, 16000), say=lambda m: None), [])

    def test_shipped_config_is_consistent(self):
        c = json.loads((ROOT / "config.json").read_text())
        self.assertGreaterEqual(c["providers"]["ollama"]["num_ctx"], c.get("context", {}).get("window_tokens", 16000))


if __name__ == "__main__":
    unittest.main()


class DoctorTests(unittest.TestCase):
    def fake_ollama(self, chat_reply: str):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                out = json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                out = json.dumps({"message": {"role": "assistant", "content": chat_reply}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = json.loads((ROOT / "config.json").read_text())
        c["provider"] = "ollama"
        c["providers"]["ollama"]["base_url"] = f"http://127.0.0.1:{srv.server_address[1]}"
        path = Path(tmp.name) / "config.json"
        path.write_text(json.dumps(c))
        return path

    def doctor(self, reply):
        from unittest import mock
        with mock.patch.object(main, "CONFIG_PATH", self.fake_ollama(reply)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main.main(["doctor"])
        return code, out.getvalue()

    def test_model_that_uses_the_tool(self):
        code, out = self.doctor('{"thought": "t", "action": "workspace.write_file", '
                                '"args": {"path": "hello.txt", "content": "hi"}}')
        self.assertEqual(code, 0, out)
        self.assertIn("✓ reply is a valid JSON action that calls write_file", out)

    def test_model_that_answers_with_code_instead(self):
        code, out = self.doctor("Sure! Here is the file:\n```\nhi\n```")
        self.assertEqual(code, 1)
        self.assertIn("✗ reply is a valid JSON action", out)

    def test_model_that_gives_a_final_answer_instead(self):
        code, out = self.doctor('{"thought": "easy", "final": "Create hello.txt with: hi"}')
        self.assertEqual(code, 1)
        self.assertIn("the model answered instead of using a tool", out)
