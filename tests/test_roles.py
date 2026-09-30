"""Chapter H — a model per role, against a fake Ollama server that answers by model name."""

import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from model_adapter import ModelError, OllamaAdapter  # noqa: E402
from models import describe_roles, make_role_models, ollama_models, role_settings  # noqa: E402
from trace_view import load_events, metrics, render  # noqa: E402

PLANNER, WORKER, JUDGE = "qwen3:8b", "gemma4:e4b", "mistral:7b"


def base_config(url="http://127.0.0.1:1"):
    c = json.loads((ROOT / "config.json").read_text())
    c["provider"] = "ollama"
    c["providers"]["ollama"].update(base_url=url, model=PLANNER, num_ctx=16384)
    c["roles"] = {"planner": {}, "reviser": {}, "task": {"model": WORKER, "think": None},
                  "judge": {"model": JUDGE}, "compactor": {}}
    return c


class FakeOllama:
    """/api/chat replies from a queue per model; /api/tags lists the models."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.seen: list[str] = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self._send({"models": [{"name": n} for n in fake.replies]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append(body["model"])
                queue = fake.replies.get(body["model"])
                if not queue:
                    self._send({"error": f"model '{body['model']}' not found"}, 404)
                    return
                self._send({"message": {"role": "assistant", "content": queue.pop(0)}})

            def _send(self, obj, code=200):
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SettingsTests(unittest.TestCase):
    def test_overrides_and_defaults(self):
        c = base_config()
        self.assertEqual(role_settings(c, "planner")[1]["model"], PLANNER)             # empty role → default
        provider, task = role_settings(c, "task")
        self.assertEqual((provider, task["model"], task["think"], task["num_ctx"]), ("ollama", WORKER, None, 16384))
        self.assertEqual(describe_roles(c)["judge"], f"ollama/{JUDGE}")
        c["roles"]["judge"] = {"provider": "anthropic"}
        self.assertEqual(role_settings(c, "judge")[1]["model"], c["providers"]["anthropic"]["model"])
        del c["roles"]
        self.assertEqual(set(describe_roles(c).values()), {f"ollama/{PLANNER}"})     # no roles section: old behaviour

    def test_errors(self):
        c = base_config()
        with self.assertRaisesRegex(ModelError, "unknown role"):
            role_settings(c, "architect")
        c["roles"]["judge"] = {"provider": "openai"}
        with self.assertRaisesRegex(ModelError, "unknown provider"):
            role_settings(c, "judge")

    def test_roles_with_the_same_settings_share_one_adapter(self):
        m = make_role_models(base_config())
        self.assertIs(m["planner"], m["reviser"])
        self.assertIs(m["planner"], m["compactor"])
        self.assertIsInstance(m["task"], OllamaAdapter)
        self.assertEqual((m["task"].model, m["judge"].model, m["planner"].model), (WORKER, JUDGE, PLANNER))
        self.assertEqual({r: x for r, x in make_role_models(base_config(), override="fake").items()},
                         {r: "fake" for r in m})

    def test_num_ctx_is_checked_per_model(self):
        c = base_config()
        c["roles"]["judge"]["num_ctx"] = 4096
        warnings = main.check_context_settings(c, say=lambda m: None)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"{JUDGE} (judge)", warnings[0])
        self.assertEqual(ollama_models(c)[PLANNER]["roles"], ["planner", "reviser", "compactor", "reviewer"])


class RoleBuildTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self.tmp = tmp

    def run_with(self, replies, argv=None):
        fake = FakeOllama(replies)
        self.addCleanup(fake.close)
        config = base_config(fake.url)
        config["build"]["task_max_iterations"] = 4
        (self.tmp / "config.json").write_text(json.dumps(config))
        with mock.patch.object(main, "ROOT", self.tmp), mock.patch.object(main, "CONFIG_PATH", self.tmp / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = main.main(argv) if argv else main.run_build("greeter", verbose_graph=False)
        return fake, result, out.getvalue()

    def test_each_role_uses_its_own_model(self):
        plan = json.dumps({"title": "Greeter", "summary": "Greets.", "tasks": [
            {"id": "T1", "title": "greet.py", "description": "d", "files": ["greet.py"], "depends_on": [],
             "done_when": {"command": "python greet.py"}}]})
        write = json.dumps({"thought": "w", "action": "workspace.write_file",
                            "args": {"path": "greet.py", "content": "print('hi')\n"}})
        verdict = json.dumps({"verdict": "accept", "requirements": [{"requirement": "greets", "met": True,
                                                                     "evidence": "greet.py"}],
                              "problems": [], "feedback": "", "summary": "ok"})
        fake, s, _ = self.run_with({PLANNER: [plan], WORKER: [write, '{"thought": "d", "final": "greet.py"}'],
                                    JUDGE: [verdict]})
        self.assertEqual(s["status"], "accepted")
        self.assertEqual(fake.seen, [PLANNER, WORKER, WORKER, JUDGE])
        self.assertEqual(s["metrics"]["models"], {"planner": [PLANNER], "task": [WORKER], "judge": [JUDGE]})
        self.assertIn(f"({JUDGE})", render(load_events(s["run_dir"])))
        request = json.loads((Path(s["run_dir"]) / "request.json").read_text())
        self.assertEqual((request["model"], request["roles"]["judge"]), (WORKER, f"ollama/{JUDGE}"))
        self.assertEqual(metrics(load_events(s["run_dir"]))["model_calls"], {"planner": 1, "task": 2, "judge": 1})

    def test_doctor_checks_every_model(self):
        write = json.dumps({"thought": "t", "action": "workspace.write_file", "args": {"path": "hello.txt", "content": "hi"}})
        fake, code, out = self.run_with({PLANNER: [], WORKER: [write], JUDGE: []}, argv=["doctor"])
        self.assertEqual(code, 0, out)
        for name, roles in [(PLANNER, "planner, reviser, compactor, reviewer"), (WORKER, "task"), (JUDGE, "judge")]:
            self.assertIn(f"✓ model {name} is pulled ({roles})", out)
        self.assertEqual(fake.seen, [WORKER])                    # the reply-format check uses the task model
        self.assertIn(f"judge     ollama/{JUDGE}", out)

    def test_doctor_reports_a_missing_model(self):
        _, code, out = self.run_with({PLANNER: [], WORKER: []}, argv=["doctor", "--skip-model"])
        self.assertEqual(code, 1)
        self.assertIn(f"✗ model {JUDGE} is pulled (judge)", out)
        self.assertIn(f"ollama pull {JUDGE}", out)


if __name__ == "__main__":
    unittest.main()
