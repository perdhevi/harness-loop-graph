"""Stage 7 tests — the graph runner, state checkpoints, and resuming stopped builds."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from graph import END, Graph, GraphError  # noqa: E402
from pipeline import RunStopped  # noqa: E402
from planner import PlanError  # noqa: E402
from state import load_state  # noqa: E402


@dataclass
class S:
    value: int = 0
    next: str | None = None
    history: list = field(default_factory=list)


def linear() -> Graph:
    g = Graph(entry="a")
    g.node("a", lambda s: setattr(s, "value", s.value + 1))
    g.node("b", lambda s: setattr(s, "value", s.value * 10))
    g.edge("a", "b").edge("b", END)
    return g


class GraphTests(unittest.TestCase):
    def test_linear_run_and_checkpoints(self):
        saved = []
        s = linear().run(S(), checkpoint=lambda st: saved.append((st.next, list(st.history))))
        self.assertEqual((s.value, s.history, s.next), (10, ["a", "b"], None))
        self.assertEqual(saved, [("a", []), ("b", ["a"]), (None, ["a", "b"])])

    def test_branch(self):
        g = Graph(entry="start")
        g.node("start", lambda s: None).node("big", lambda s: None).node("small", lambda s: None)
        g.branch("start", lambda s: "big" if s.value > 5 else "small", {"big", "small"})
        g.edge("big", END).edge("small", END)
        self.assertEqual(g.run(S(value=9)).history, ["start", "big"])
        self.assertEqual(g.run(S(value=1)).history, ["start", "small"])

    def test_router_must_return_declared_target(self):
        g = Graph(entry="a")
        g.node("a", lambda s: None).node("b", lambda s: None)
        g.branch("a", lambda s: "c", {"b"}).edge("b", END)
        with self.assertRaisesRegex(GraphError, "returned 'c'"):
            g.run(S())

    def test_validate(self):
        g = Graph(entry="a")
        g.node("a", lambda s: None).node("b", lambda s: None)
        g.edge("a", "zzz")
        with self.assertRaises(GraphError) as ctx:
            g.validate()
        self.assertIn("edge to unknown node 'zzz'", str(ctx.exception))
        self.assertIn("node 'b' needs exactly one edge or branch", str(ctx.exception))

    def test_interrupt_and_resume(self):
        g = linear()
        s = g.run(S(), interrupt_before={"b"})
        self.assertEqual((s.value, s.next, s.history), (1, "b", ["a"]))
        s = g.run(s, start=s.next, interrupt_before={"b"})   # starting at an interrupt node runs it
        self.assertEqual((s.value, s.next, s.history), (10, None, ["a", "b"]))

    def test_exception_leaves_next_on_failed_node(self):
        g = Graph(entry="a")
        g.node("a", lambda s: None).node("b", lambda s: 1 / 0)
        g.edge("a", "b").edge("b", END)
        s = S()
        with self.assertRaises(ZeroDivisionError):
            g.run(s)
        self.assertEqual((s.next, s.history), ("b", ["a"]))

    def test_max_steps(self):
        g = Graph(entry="a")
        g.node("a", lambda s: None).edge("a", "a")
        with self.assertRaisesRegex(GraphError, "within 5 steps"):
            g.run(S(), max_steps=5)


# ---------------------------------------------------------------- pipeline

class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append([dict(m) for m in messages])
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


PLAN = json.dumps({"title": "Greeter", "summary": "Greets.", "tasks": [
    {"id": "T1", "title": "app", "description": "d", "files": ["app.py"], "depends_on": [],
     "done_when": {"file": "app.py"}}]})


def write(path, content="x = 1\n"):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": content}})


def final():
    return json.dumps({"thought": "d", "final": "done"})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(main, "ROOT", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def only_run(self) -> Path:
        runs = list((Path(self.tmp.name) / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        return runs[0]

    def test_state_after_full_build(self):
        s = self.build("greeter", model=FakeModel([PLAN, write("app.py"), final()]))
        st = load_state(s["run_dir"])
        self.assertEqual((st.status, st.next), ("finished", None))
        self.assertEqual(st.history, ["intake", "plan", "next_task", "run_task", "next_task", "finish"])
        self.assertEqual(st.plan["title"], "Greeter")
        self.assertEqual(json.loads((Path(s["run_dir"]) / "summary.json").read_text())["status"], "finished")

    def test_crash_while_planning_then_resume(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("greeter", model=FakeModel([ConnectionError("ollama is down")]))
        run_dir = self.only_run()
        self.assertEqual(ctx.exception.run_dir, str(run_dir))
        st = load_state(run_dir)
        self.assertEqual((st.next, st.history), ("plan", ["intake"]))

        s = self.build(None, from_run=str(run_dir), model=FakeModel([PLAN, write("app.py"), final()]))
        self.assertEqual(s["status"], "finished")
        self.assertEqual(load_state(run_dir).history,
                         ["intake", "plan", "next_task", "run_task", "next_task", "finish"])

    def test_ctrl_c_during_build_then_resume(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("greeter", model=FakeModel([PLAN, write("half.py"), KeyboardInterrupt()]))
        self.assertIsInstance(ctx.exception.cause, KeyboardInterrupt)
        run_dir = self.only_run()
        st = load_state(run_dir)
        self.assertEqual((st.next, st.status, st.current_task), ("run_task", "interrupted", "T1"))
        plan = json.loads((run_dir / "plan.json").read_text())
        self.assertEqual(plan["tasks"][0]["status"], "in_progress")

        model = FakeModel([write("app.py"), final()])
        s = self.build(None, from_run=str(run_dir), model=model)
        self.assertEqual(s["status"], "finished")
        first = model.calls[0][0]["content"]
        self.assertIn("being resumed", first)
        self.assertIn("- half.py (6 bytes)", first)
        events = [json.loads(line) for line in (run_dir / "transcript.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("event") == "resume" for e in events))
        self.assertEqual(sorted(f["path"] for f in s["files"]), ["app.py", "half.py"])

    def test_model_error_during_task_is_resumable(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("greeter", model=FakeModel([PLAN, ConnectionError("gone")]))
        st = load_state(ctx.exception.run_dir)
        self.assertEqual((st.next, st.status), ("run_task", "error"))
        s = self.build(None, from_run=ctx.exception.run_dir, model=FakeModel([write("app.py"), final()]))
        self.assertEqual(s["status"], "finished")

    def test_finished_run_is_not_rebuilt(self):
        s = self.build("greeter", model=FakeModel([PLAN, write("app.py"), final()]))
        with self.assertRaisesRegex(PlanError, "already been built"):
            self.build(None, from_run=s["run_dir"], model=FakeModel([]))

    def test_review_pause_is_in_state(self):
        s = self.build("greeter", review=True, model=FakeModel([PLAN]))
        st = load_state(s["run_dir"])
        self.assertEqual((s["status"], st.next, st.status), ("planned", "next_task", "planned"))


if __name__ == "__main__":
    unittest.main()
