"""Stage 9 tests — checks run by the harness, fix attempts, and final regression checks."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from checks import run_check  # noqa: E402
from pipeline import RunStopped  # noqa: E402
from runtime import build_registry, load_json  # noqa: E402


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def plan(*tasks):
    return json.dumps({"title": "Calc", "summary": "A calculator.", "tasks": list(tasks)})


def task(tid, done_when, deps=()):
    return {"id": tid, "title": f"task {tid}", "description": "d", "files": [], "depends_on": list(deps),
            "done_when": done_when}


def write(path, content):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": content}})


def final(note):
    return json.dumps({"thought": "d", "final": note})


GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, 'add(2, 3) should be 5'\nprint('ok')\n"
CHECK = {"command": "python test_calc.py"}


class RunCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tools = build_registry(load_json(ROOT / "config.json"), self.ws, self.stack)

    def test_file_checks(self):
        (self.ws / "README.md").write_text("x")
        self.assertTrue(run_check({"file": "README.md"}, self.ws, self.tools).ok)
        r = run_check({"file": "missing.md"}, self.ws, self.tools)
        self.assertFalse(r.ok)
        self.assertIn("does not exist", r.output)
        self.assertFalse(run_check({"file": "../etc/passwd"}, self.ws, self.tools).ok)

    def test_command_checks(self):
        ok = run_check({"command": "python -c \"print('hi')\""}, self.ws, self.tools)
        self.assertEqual((ok.ok, ok.exit_code), (True, 0))
        bad = run_check({"command": "python -c \"import sys; sys.exit(3)\""}, self.ws, self.tools)
        self.assertEqual((bad.ok, bad.exit_code), (False, 3))
        self.assertEqual(bad.describe(), "`python -c \"import sys; sys.exit(3)\"` → exit 3")

    def test_refused_command_is_a_failed_check(self):
        r = run_check({"command": "ls -la"}, self.ws, self.tools)
        self.assertEqual((r.ok, r.exit_code), (False, None))
        self.assertIn("'ls' is not allowed", r.output)

    def test_output_keeps_the_end(self):
        r = run_check({"command": "python -c \"print('x' * 5000); print('THE END')\""}, self.ws, self.tools,
                      output_chars=200)
        self.assertIn("THE END", r.output)
        self.assertIn("chars cut", r.output)


class VerifyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(main, "ROOT", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **{"judge": False, **kw})   # judge: see test_judge.py

    def plan_of(self, s):
        return {t["id"]: t for t in json.loads((Path(s["run_dir"]) / "plan.json").read_text())["tasks"]}

    def test_false_claim_is_caught_and_fixed(self):
        """The Stage 05 lesson: the model says tests pass; the harness finds out they don't."""
        model = FakeModel([
            plan(task("T1", CHECK)),
            write("calc.py", BAD), write("test_calc.py", TEST), final("calc.py done, tests pass"),
            write("calc.py", GOOD), final("fixed: add used '-' instead of '+'"),
        ])
        s = self.build("calc", model=model)
        self.assertEqual(s["status"], "finished")
        self.assertTrue(s["verified"])
        t1 = self.plan_of(s)["T1"]
        self.assertEqual((t1["status"], t1["verified"], t1["fix_attempts"]), ("done", True, 1))
        self.assertEqual([c["ok"] for c in t1["checks"]], [False, True])
        self.assertEqual(t1["checks"][0]["exit_code"], 1)

        fix_prompt = model.calls[4]["messages"][0]["content"]
        self.assertEqual(len(model.calls[4]["messages"]), 1)             # fresh loop
        self.assertIn("DID NOT PASS ITS CHECK", fix_prompt)
        self.assertIn("Your hand-off note said: calc.py done, tests pass", fix_prompt)
        self.assertIn("`python test_calc.py` → exit 1", fix_prompt)
        self.assertIn("add(2, 3) should be 5", fix_prompt)                # the real failure output
        self.assertIn("fix attempt 1 of 2", fix_prompt)
        self.assertIn("Saying the task is done does not make it done", model.calls[1]["system"])

    def test_fails_after_fix_attempts_and_blocks_dependents(self):
        model = FakeModel([
            plan(task("T1", CHECK), task("T2", {"file": "README.md"}, ["T1"])),
            write("calc.py", BAD), write("test_calc.py", TEST), final("done"),
            final("looked again, it's fine"),
            final("definitely fine"),
        ])
        s = self.build("calc", model=model)
        self.assertEqual(s["status"], "partial")
        tasks = self.plan_of(s)
        self.assertEqual(tasks["T1"]["status"], "failed")
        self.assertEqual(tasks["T1"]["error"], "check failed after 2 fix attempts: `python test_calc.py` → exit 1")
        self.assertEqual(len(tasks["T1"]["checks"]), 3)
        self.assertEqual(tasks["T2"]["status"], "blocked")
        self.assertEqual(model.replies, [])

    def test_final_check_catches_regression(self):
        model = FakeModel([
            plan(task("T1", CHECK), task("T2", {"file": "notes.md"}, ["T1"])),
            write("calc.py", GOOD), write("test_calc.py", TEST), final("calc works"),
            write("calc.py", BAD), write("notes.md", "refactored add"), final("refactored"),   # T2 breaks T1
        ])
        s = self.build("calc", model=model)
        self.assertEqual(s["status"], "partial")
        self.assertTrue(all(t["status"] == "done" for t in s["tasks"]))     # each passed at the time
        bad = [c for c in s["final_checks"] if not c["ok"]]
        self.assertEqual([c["task"] for c in bad], ["T1"])
        self.assertIn("regression in T1: `python test_calc.py` → exit 1", s["error"])

    def test_refused_check_command_fails_the_task(self):
        # Chapter I: the planner now rejects `ls` up front, so put it in by hand, as a reviewer could
        planned = self.build("x", review=True, model=FakeModel([plan(task("T1", {"file": "a.txt"}))]))
        plan_file = Path(planned["run_dir"]) / "plan.json"
        edited = json.loads(plan_file.read_text())
        edited["tasks"][0]["done_when"] = {"command": "ls"}
        plan_file.write_text(json.dumps(edited))
        s = self.build(None, from_run=planned["run_dir"], model=FakeModel([final("a"), final("b"), final("c")]))
        t1 = self.plan_of(s)["T1"]
        self.assertEqual(t1["status"], "failed")
        self.assertIn("'ls' is not allowed", t1["checks"][0]["output"])

    def test_resumed_fix_attempt_keeps_fix_context(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("calc", model=FakeModel([
                plan(task("T1", CHECK)),
                write("calc.py", BAD), write("test_calc.py", TEST), final("done"),
                KeyboardInterrupt(),                                     # during the fix attempt
            ]))
        model = FakeModel([write("calc.py", GOOD), final("fixed")])
        s = self.build(None, from_run=ctx.exception.run_dir, model=model)
        self.assertEqual(s["status"], "finished")
        first = model.calls[0]["messages"][0]["content"]
        self.assertIn("DID NOT PASS ITS CHECK", first)
        self.assertIn("being resumed", first)


if __name__ == "__main__":
    unittest.main()
