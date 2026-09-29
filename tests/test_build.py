"""Stage 5 tests — run folders, transcript and summary, with the real workspace server."""

import contextlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build import CountingModel, execute_build, slugify, start_run  # noqa: E402
from runtime import build_registry, load_json, prompt_text  # noqa: E402

FIXED = datetime(2026, 9, 29, 1, 2, 3, tzinfo=timezone.utc)


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def act(tool, **args):
    return json.dumps({"thought": f"use {tool}", "action": tool, "args": args})


def final(text):
    return json.dumps({"thought": "done", "final": text})


class RunFolderTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Build a Python CLI to-do app with add/list/done"), "build-a-python-cli-to-do")
        self.assertEqual(slugify("!!!"), "build")

    def test_start_run_layout_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            r1 = start_run(runs, "Make a calculator", {"model": "m"}, now=FIXED)
            r2 = start_run(runs, "Make a calculator", now=FIXED)
            self.assertEqual(r1.id, "20260929-010203-make-a-calculator")
            self.assertEqual(r2.id, "20260929-010203-make-a-calculator-2")
            self.assertTrue(r1.workspace.is_dir())
            self.assertEqual(list(r1.workspace.iterdir()), [])
            req = json.loads(r1.request_file.read_text())
            self.assertEqual((req["request"], req["model"]), ("Make a calculator", "m"))


class ExecuteBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = load_json(ROOT / "config.json")
        self.prompts = load_json(ROOT / "prompts.json")
        self.run = start_run(Path(self.tmp.name) / "runs", "Build a greeter", now=FIXED)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.registry = build_registry(self.config, self.run.workspace, self.stack)
        self.system = prompt_text(self.prompts, "react_system") + "\n\n" + prompt_text(self.prompts, "build_rules")

    def build(self, replies, max_iterations=10):
        return execute_build(self.run, FakeModel(replies), self.registry, self.system, "Request: greeter",
                             max_iterations=max_iterations, format_reminder="bad ({error})")

    def transcript(self):
        return [json.loads(line) for line in self.run.transcript.read_text().splitlines()]

    def test_successful_build(self):
        s = self.build([
            act("workspace.write_file", path="greet.py", content="print('hi')\n"),
            act("workspace.write_file", path="README.md", content="# Greeter\nRun: python greet.py\n"),
            "not json",
            act("workspace.run_command", command="python greet.py"),
            final("Created greet.py and README.md. Run: python greet.py"),
        ])
        self.assertEqual(s["status"], "finished")
        self.assertEqual(s["steps"], 5)
        self.assertEqual(s["malformed"], 1)
        self.assertEqual(s["tool_calls"], {"workspace.write_file": 2, "workspace.run_command": 1})
        self.assertEqual([f["path"] for f in s["files"]], ["README.md", "greet.py"])
        self.assertEqual(s["model_calls"], 5)
        self.assertGreater(s["approx_tokens_in"], 0)
        self.assertEqual(json.loads(self.run.summary_file.read_text())["status"], "finished")
        lines = self.transcript()
        self.assertEqual(len(lines), 5)
        self.assertIn("hi", lines[3]["observation"])
        self.assertEqual(lines[4]["final"], "Created greet.py and README.md. Run: python greet.py")

    def test_max_iterations(self):
        s = self.build([act("workspace.list_dir")] * 3, max_iterations=3)
        self.assertEqual((s["status"], s["answer"], s["steps"]), ("max_iterations", None, 3))

    def test_model_error_keeps_partial_results(self):
        s = self.build([
            act("workspace.write_file", path="half.py", content="x = 1\n"),
            ConnectionError("model went away"),
        ])
        self.assertEqual(s["status"], "error")
        self.assertIn("model went away", s["error"])
        self.assertEqual([f["path"] for f in s["files"]], ["half.py"])
        self.assertEqual(len(self.transcript()), 1)
        self.assertTrue(self.run.summary_file.exists())

    def test_files_skip_caches(self):
        s = self.build([
            act("workspace.write_file", path="app.py", content=""),
            act("workspace.write_file", path="__pycache__/app.pyc", content=""),
            final("ok"),
        ])
        self.assertEqual([f["path"] for f in s["files"]], ["app.py"])


class CountingModelTests(unittest.TestCase):
    def test_counts(self):
        m = CountingModel(FakeModel(["abcd", "efgh"]))
        m.complete("sys", [{"role": "user", "content": "12345"}])
        m.complete("sys", [{"role": "user", "content": "1"}])
        self.assertEqual((m.calls, m.chars_in, m.chars_out), (2, 12, 8))


if __name__ == "__main__":
    unittest.main()
