"""Stage 8 tests — task-by-task execution: status, hand-offs, failures, blocking, resume."""

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
import pipeline  # noqa: E402
from pipeline import RunStopped  # noqa: E402
from state import load_state  # noqa: E402


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


def t(tid, deps=(), title=None, done=None):
    return {"id": tid, "title": title or f"task {tid}", "description": f"do {tid}", "files": [f"{tid.lower()}.py"],
            "depends_on": list(deps), "done_when": done or {"file": f"{tid.lower()}.py"}}


def plan(*tasks):
    return json.dumps({"title": "Demo", "summary": "A demo.", "tasks": list(tasks)})


def write(path):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": "x=1\n"}})


def look():
    return json.dumps({"thought": "look", "action": "workspace.list_dir", "args": {}})


def final(note):
    return json.dumps({"thought": "d", "final": note})


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        config = json.loads((ROOT / "config.json").read_text())
        config["build"]["task_max_iterations"] = 3
        (tmp / "config.json").write_text(json.dumps(config))
        for target, value in [("ROOT", tmp), ("CONFIG_PATH", tmp / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)
        # count how often the MCP registry is built
        self.registry_builds = 0
        real = pipeline.build_registry

        def counting(*a, **kw):
            self.registry_builds += 1
            return real(*a, **kw)
        p = mock.patch.object(pipeline, "build_registry", counting)
        p.start()
        self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def plan_file(self, run_dir):
        return json.loads((Path(run_dir) / "plan.json").read_text())

    def test_tasks_run_in_order_with_handoffs(self):
        model = FakeModel([
            plan(t("T1"), t("T2", ["T1"]), t("T3", ["T2"])),
            write("t1.py"), final("T1 made t1.py with x"),
            write("t2.py"), final("T2 made t2.py"),
            write("t3.py"), final("T3 made t3.py"),
        ])
        s = self.build("demo", model=model)
        self.assertEqual(s["status"], "finished")
        self.assertEqual([x["status"] for x in s["tasks"]], ["done", "done", "done"])
        self.assertEqual(self.registry_builds, 1)                     # one MCP setup for all tasks

        t2_prompt = model.calls[3]["messages"][0]["content"]
        self.assertIn("=== YOUR TASK: T2 — task T2 ===", t2_prompt)
        self.assertIn("[x] T1 task T1\n[>] T2 task T2\n[ ] T3 task T3", t2_prompt)
        self.assertIn("T1 (task T1): T1 made t1.py with x", t2_prompt)
        self.assertIn("- t1.py (4 bytes)", t2_prompt)
        self.assertIn("ONE task", model.calls[3]["system"])
        self.assertEqual(len(model.calls[3]["messages"]), 1)         # fresh conversation per task

        p = self.plan_file(s["run_dir"])
        self.assertEqual(p["tasks"][0]["handoff"], "T1 made t1.py with x")
        self.assertEqual(p["tasks"][0]["steps"], 2)
        self.assertEqual(s["steps"], 6)
        self.assertEqual(s["tool_calls"], {"workspace.write_file": 3})
        self.assertIn("T2 task T2: T2 made t2.py", s["answer"])
        lines = [json.loads(x) for x in (Path(s["run_dir"]) / "transcript.jsonl").read_text().splitlines()]
        self.assertEqual([x["task"] for x in lines if "n" in x], ["T1", "T1", "T2", "T2", "T3", "T3"])
        self.assertEqual(load_state(s["run_dir"]).history,
                         ["intake", "plan"] + ["next_task", "run_task"] * 3 + ["next_task", "finish"])

    def test_failed_task_blocks_dependents_but_not_others(self):
        model = FakeModel([
            plan(t("T1"), t("T2", ["T1"]), t("T3")),
            look(), look(), look(),                # T1 runs out of its 3 steps
            write("t3.py"), final("T3 ok"),
        ])
        s = self.build("demo", model=model)
        self.assertEqual(s["status"], "partial")
        by_id = {x["id"]: x for x in s["tasks"]}
        self.assertEqual(by_id["T1"]["status"], "failed")
        self.assertEqual(by_id["T1"]["error"], "ran out of steps (3)")
        self.assertEqual((by_id["T2"]["status"], by_id["T2"]["error"]), ("blocked", "blocked by T1"))
        self.assertEqual(by_id["T3"]["status"], "done")
        self.assertIn("T1 failed", s["error"])
        self.assertEqual(model.replies, [])                           # T2 never ran

    def test_resume_skips_done_tasks(self):
        with self.assertRaises(RunStopped) as ctx:
            self.build("demo", model=FakeModel([
                plan(t("T1"), t("T2", ["T1"])),
                write("t1.py"), final("T1 done"),
                write("t2_part.py"), KeyboardInterrupt(),
            ]))
        run_dir = ctx.exception.run_dir
        self.assertEqual([x["status"] for x in self.plan_file(run_dir)["tasks"]], ["done", "in_progress"])
        self.assertEqual(load_state(run_dir).next, "run_task")

        model = FakeModel([write("t2.py"), final("T2 done")])
        s = self.build(None, from_run=run_dir, model=model)
        self.assertEqual(s["status"], "finished")
        first = model.calls[0]["messages"][0]["content"]
        self.assertIn("YOUR TASK: T2", first)                          # T1 was not re-run
        self.assertIn("being resumed", first)
        self.assertIn("- t2_part.py", first)
        # Known limit: an interrupted attempt never reports its numbers, so only the
        # resumed attempt's 2 steps are counted. The transcript still has all 3.
        self.assertEqual(self.plan_file(run_dir)["tasks"][1]["steps"], 2)
        steps = [json.loads(x) for x in (Path(run_dir) / "transcript.jsonl").read_text().splitlines()]
        self.assertEqual(sum(1 for x in steps if x.get("task") == "T2" and "n" in x), 3)

    def test_plan_edit_between_runs_is_used(self):
        s = self.build("demo", review=True, model=FakeModel([plan(t("T1"), t("T2", ["T1"]))]))
        pf = Path(s["run_dir"]) / "plan.json"
        p = json.loads(pf.read_text())
        p["tasks"][1]["title"] = "Renamed by reviewer"
        pf.write_text(json.dumps(p))
        model = FakeModel([write("t1.py"), final("T1 ok"), write("t2.py"), final("T2 ok")])
        self.build(None, from_run=s["run_dir"], model=model)
        self.assertIn("YOUR TASK: T2 — Renamed by reviewer", model.calls[2]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
