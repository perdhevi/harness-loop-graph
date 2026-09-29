"""Adapter settings found on a real Windows + Ollama run.

1. Ollama's small default context (4,096 tokens on < ~23 GB VRAM) silently drops the start of long prompts.
2. qwen3 "thinks" by default; replies can end up in message.thinking with empty content.
"""

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model_adapter import OllamaAdapter  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

