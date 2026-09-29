"""Chapter A tests — trace events, wrappers as a side channel, resume sessions, the viewer."""

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
from pipeline import RunStopped  # noqa: E402
from trace_view import load_events, render  # noqa: E402
from tracing import Tracer  # noqa: E402


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


PLAN = json.dumps({"title": "Greeter", "summary": "Greets.", "tasks": [
    {"id": "T1", "title": "app", "description": "d", "files": ["app.py"], "depends_on": [],
     "done_when": {"command": "python app.py"}},
    {"id": "T2", "title": "readme", "description": "d", "files": ["README.md"], "depends_on": ["T1"],
     "done_when": {"file": "README.md"}}]})
VERDICT = json.dumps({"verdict": "accept", "requirements": [{"requirement": "greets", "met": True, "evidence": "app.py"}],
                      "problems": [], "feedback": "", "summary": "ok"})


def write(path, content="print('hi')\n"):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": content}})


def run(cmd):
    return json.dumps({"thought": "r", "action": "workspace.run_command", "args": {"command": cmd}})


def final(note="done"):
    return json.dumps({"thought": "d", "final": note})


def script():
    return [PLAN, write("app.py"), run("python app.py"), "not json", final("app.py prints hi"),
            write("README.md", "# hi\n"), final("readme"), VERDICT]


class TracingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, *a, trace=True, **kw):
        tmp = Path(self.tmp.name) / ("on" if trace else "off")
        tmp.mkdir(exist_ok=True)
        config = json.loads((ROOT / "config.json").read_text())
        config["trace"]["enabled"] = trace
        (tmp / "config.json").write_text(json.dumps(config))
        with mock.patch.object(main, "ROOT", tmp), mock.patch.object(main, "CONFIG_PATH", tmp / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def test_events_cover_the_build(self):
        s = self.build("greeter", model=FakeModel(script()))
        self.assertEqual(s["status"], "accepted")
        events = load_events(s["run_dir"])
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "session")
        self.assertEqual(types.count("node_start"), types.count("node_end"))

        nodes = [e["node"] for e in events if e["type"] == "node_end"]
        self.assertEqual(nodes, ["intake", "plan", "next_task", "run_task", "verify", "next_task", "run_task",
                                 "verify", "next_task", "final_check", "judge", "finish"])

        models = [e for e in events if e["type"] == "model"]
        self.assertEqual([(m["role"], m["task"]) for m in models],
                         [("planner", None)] + [("task", "T1")] * 4 + [("task", "T2")] * 2 + [("judge", None)])
        self.assertTrue(all(m["approx_tokens_in"] > 0 and m["duration_ms"] >= 0 for m in models))

        tools = [e for e in events if e["type"] == "tool"]
        runs = [t for t in tools if t["tool"] == "workspace.run_command"]
        self.assertTrue(all(t["server"] == "workspace" for t in tools))
        self.assertIn("python app.py", [t["command"] for t in runs])       # model's run + harness checks
        self.assertTrue(all(t["ok"] for t in tools))

        steps = [e for e in events if e["type"] == "step"]
        self.assertEqual(len(steps), 6)
        self.assertEqual(sum(1 for x in steps if x["parse_error"]), 1)
        checks = [(e["phase"], e["target"], e["ok"]) for e in events if e["type"] == "check"]
        self.assertEqual(checks, [("verify", "python app.py", True), ("verify", "README.md", True),
                                  ("final", "python app.py", True), ("final", "README.md", True)])
        self.assertEqual([e["verdict"] for e in events if e["type"] == "verdict"], ["accept"])

        plan_end = next(e for e in events if e["type"] == "node_end" and e["node"] == "plan")
        self.assertEqual(plan_end["diff"]["status"], ["new", "planned"])
        self.assertEqual(plan_end["diff"]["plan"], "changed")

    def test_tracing_changes_nothing(self):
        on = self.build("greeter", model=FakeModel(script()), trace=True)
        off = self.build("greeter", model=FakeModel(script()), trace=False)
        self.assertFalse((Path(off["run_dir"]) / "trace.jsonl").exists())
        timing = {"duration_s", "run_dir", "id", "report"}

        def clean(d):
            return json.loads(json.dumps({k: v for k, v in d.items() if k not in timing}).replace(
                Path(on["run_dir"]).name, "RUN").replace(Path(off["run_dir"]).name, "RUN"))
        self.assertEqual(clean(on), clean(off))

        def plan_without_timing(run_dir):
            p = json.loads((Path(run_dir) / "plan.json").read_text())
            for t in p["tasks"]:
                t.pop("duration_s", None)
            return p
        self.assertEqual(plan_without_timing(on["run_dir"]), plan_without_timing(off["run_dir"]))

    def test_resume_starts_a_new_session(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("greeter", model=FakeModel([PLAN, write("app.py"), KeyboardInterrupt()]))
        run_dir = ctx.exception.run_dir
        s = self.build(None, from_run=run_dir, model=FakeModel([final("ok"), write("README.md"), final(), VERDICT]))
        self.assertEqual(s["status"], "accepted")
        events = load_events(run_dir)
        sessions = [e for e in events if e["type"] == "session"]
        self.assertEqual([(x["session"], x["resumed_at"]) for x in sessions], [(1, None), (2, "run_task")])
        interrupted = [e for e in events if e["type"] == "model" and e["error"]]
        self.assertEqual(interrupted[0]["error"].split(":")[0], "KeyboardInterrupt")
        text = render(events)
        self.assertIn("session 2  (resumed at run_task)", text)
        self.assertIn("ERROR KeyboardInterrupt", text)               # Ctrl-C: node_end still written

    def test_hard_kill_shows_an_open_node(self):
        """kill -9 leaves a node_start with no node_end; the viewer says so."""
        events = [{"type": "session", "session": 1, "run_id": "r", "resumed_at": None, "ms": 0},
                  {"type": "node_start", "node": "run_task", "task": "T1", "session": 1, "ms": 5}]
        self.assertIn("(session ended inside a node: interrupted or crashed)", render(events))

    def test_viewer(self):
        s = self.build("greeter", model=FakeModel(script()))
        text = render(load_events(s["run_dir"]))
        for piece in ["session 1", "run_task T1", "check ✓ python app.py", "verdict accept 1/1",
                      "by task", "by tool", "workspace.write_file", "by role (model calls)", "planner", "judge",
                      "slowest"]:
            self.assertIn(piece, text)
        detailed = render(load_events(s["run_dir"]), steps=True)
        self.assertIn("step 3   parse error: no JSON object found", detailed)
        self.assertIn("↳ workspace.run_command", detailed)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["trace", s["run_dir"]]), 0)
        self.assertIn("by task", out.getvalue())


class TracerUnitTests(unittest.TestCase):
    def test_wrappers_reraise_and_record(self):
        t = Tracer()
        with tempfile.TemporaryDirectory() as tmp:
            t.bind(tmp, run_id="r")

            class Boom:
                def complete(self, system, messages):
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                t.wrap_model(Boom()).complete("s", [{"role": "user", "content": "x"}])
            events = load_events(tmp)
            self.assertEqual(events[-1]["type"], "model")
            self.assertTrue(events[-1]["error"].startswith("KeyboardInterrupt"))

    def test_unwritable_trace_never_breaks_the_build(self):
        t = Tracer()
        t.bind("/definitely/not/a/dir", run_id="r")
        t.event("node_start")                      # must not raise
        self.assertIsNotNone(t.broken)
        wrapped = t.wrap_model(FakeModel(["reply"]))
        self.assertEqual(wrapped.complete("s", []), "reply")

    def test_events_before_bind_are_buffered(self):
        t = Tracer()
        t.event("node_start")
        with tempfile.TemporaryDirectory() as tmp:
            t.bind(tmp, run_id="r")
            types = [e["type"] for e in load_events(tmp)]
            self.assertEqual(types, ["session", "node_start"])


if __name__ == "__main__":
    unittest.main()
