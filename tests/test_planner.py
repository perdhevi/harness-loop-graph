"""Stage 6 tests — plan validation, the planner conversation, and plan → build wiring."""

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
from planner import (PlanError, load_plan, make_plan, render_spec,  # noqa: E402
                     render_tasks, save_plan, validate_plan)
from runtime import load_json, prompt_text  # noqa: E402

PROMPTS = {k: prompt_text(load_json(ROOT / "prompts.json"), k) for k in load_json(ROOT / "prompts.json")}


def task(tid, deps=(), done=None, files=("app.py",), title=None):
    return {"id": tid, "title": title or f"task {tid}", "description": "do it", "files": list(files),
            "depends_on": list(deps), "done_when": done or {"command": "python -c \"import app\""}}


def plan_obj(tasks=None, **over):
    obj = {"title": "Greeter", "summary": "Greets people.", "features": ["greet"], "tech": ["Python"],
           "constraints": [], "out_of_scope": [], "assumptions": [],
           "tasks": tasks if tasks is not None else [task("T1"), task("T2", ["T1"], {"file": "README.md"},
                                                                     ["README.md"])]}
    obj.update(over)
    return obj


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


class ValidateTests(unittest.TestCase):
    def test_valid_plan_gets_status(self):
        plan, errors = validate_plan(plan_obj())
        self.assertEqual(errors, [])
        self.assertEqual([t["status"] for t in plan["tasks"]], ["pending", "pending"])

    def test_dependency_order(self):
        plan, errors = validate_plan(plan_obj([task("A", ["C"]), task("B"), task("C", ["B"])]))
        self.assertEqual(errors, [])
        self.assertEqual([t["id"] for t in plan["tasks"]], ["B", "C", "A"])

    def test_problems_are_listed(self):
        bad = plan_obj([
            task("T1", ["T9"]),
            task("T1"),
            task("T2", files=["/etc/passwd", "../x.py"]),
            {"id": "T3", "title": "", "done_when": {"command": "x", "file": "y"}},
            {"id": "T4", "title": "t", "done_when": {}},
        ], title="", tech="python")
        _, errors = validate_plan(bad)
        text = "\n".join(errors)
        for expected in ["'title' must be a non-empty string", "'tech' must be a list of strings",
                         "task T1: duplicate id", "depends on unknown task 'T9'",
                         "'/etc/passwd' must be relative", "'../x.py' must not contain '..'",
                         "task T3: 'title' must be a non-empty string",
                         "task T3: 'done_when' must have exactly one", "task T4: 'done_when' must have exactly one"]:
            self.assertIn(expected, text)

    def test_cycle_and_limits(self):
        _, errors = validate_plan(plan_obj([task("A", ["B"]), task("B", ["A"])]))
        self.assertTrue(any("cycle" in e for e in errors))
        _, errors = validate_plan(plan_obj([task(f"T{i}") for i in range(20)]), max_tasks=5)
        self.assertIn("too many tasks (20); use at most 5", errors)
        _, errors = validate_plan(plan_obj([]))
        self.assertIn("'tasks' must be a non-empty list", errors)


class MakePlanTests(unittest.TestCase):
    def test_valid_first_time_with_think_block(self):
        model = FakeModel(["<think>plan it</think>```json\n" + json.dumps(plan_obj()) + "\n```"])
        log = []
        plan = make_plan(model, PROMPTS, "greeter", log=log.append)
        self.assertEqual(plan["title"], "Greeter")
        self.assertTrue(log[0]["ok"])
        self.assertIn("At most 15 tasks", model.calls[0]["system"])      # placeholder filled in
        self.assertIn("at most 3 short questions", model.calls[0]["system"])

    def test_errors_are_fed_back(self):
        model = FakeModel(["no json at all", json.dumps(plan_obj([task("T1", ["nope"])])),
                           json.dumps(plan_obj())])
        log = []
        plan = make_plan(model, PROMPTS, "greeter", log=log.append)
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertIn("depends on unknown task 'nope'", model.calls[2]["messages"][-1]["content"])
        self.assertEqual([("errors" in e) for e in log], [True, True, False])

    def test_questions_answered(self):
        model = FakeModel([json.dumps({"questions": ["CLI or web?", "Which language?"]}),
                           json.dumps(plan_obj())])
        asked = []
        plan = make_plan(model, PROMPTS, "make me an app",
                         ask=lambda qs: asked.extend(qs) or ["CLI", "Python"])
        self.assertEqual(asked, ["CLI or web?", "Which language?"])
        self.assertEqual(plan["clarifications"], [{"q": "CLI or web?", "a": "CLI"},
                                                  {"q": "Which language?", "a": "Python"}])
        self.assertIn("A: CLI", model.calls[1]["messages"][-1]["content"])

    def test_questions_without_asker_become_assumptions(self):
        model = FakeModel([json.dumps({"questions": ["CLI or web?"]}), json.dumps(plan_obj())])
        plan = make_plan(model, PROMPTS, "make me an app", ask=None)
        self.assertIn("No answers are available", model.calls[1]["messages"][-1]["content"])
        self.assertIn("assumption", plan["clarifications"][0]["a"])

    def test_only_one_round_of_questions(self):
        q = json.dumps({"questions": ["?"]})
        model = FakeModel([q, q, json.dumps(plan_obj())])
        make_plan(model, PROMPTS, "x", ask=lambda qs: ["a"])
        self.assertIn("already asked once", model.calls[2]["messages"][-1]["content"])

    def test_gives_up(self):
        with self.assertRaisesRegex(PlanError, "no valid plan after 2 attempts"):
            make_plan(FakeModel(["nope", "still nope"]), PROMPTS, "x", max_attempts=2)


class FilesTests(unittest.TestCase):
    def test_spec_and_roundtrip(self):
        plan, _ = validate_plan(plan_obj())
        plan["clarifications"] = [{"q": "CLI?", "a": "yes"}]
        spec = render_spec(plan, "build a greeter")
        self.assertIn("# Greeter", spec)
        self.assertIn("| T2 | task T2 | `README.md` | T1 | `README.md` exists |", spec)
        self.assertIn("**Q:** CLI?", spec)
        self.assertIn("T2. task T2 (after T1)", render_tasks(plan))
        with tempfile.TemporaryDirectory() as tmp:
            save_plan(Path(tmp), plan, "build a greeter")
            loaded, text = load_plan(Path(tmp))
            self.assertEqual(loaded["tasks"], plan["tasks"])
            self.assertEqual(loaded["clarifications"], plan["clarifications"])
            self.assertEqual(text, spec)

    def test_broken_edit_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "plan.json").write_text(json.dumps(plan_obj([task("T1", ["T1"])])))
            with self.assertRaisesRegex(PlanError, "depends on itself"):
                load_plan(d)
            (d / "plan.json").write_text("{not json")
            with self.assertRaisesRegex(PlanError, "not valid JSON"):
                load_plan(d)


def final(text):
    return json.dumps({"thought": "done", "final": text})


def write(path, content):
    return json.dumps({"thought": "write", "action": "workspace.write_file",
                       "args": {"path": path, "content": content}})


class BuildFlowTests(unittest.TestCase):
    """main.run_build with a fake model; runs go to a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(main, "ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def quiet(self, fn, *a, **kw):
        if fn is main.run_build:
            kw.setdefault("judge", False)        # Stage 6–9 behaviour; the judge has its own tests
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **kw)

    def test_review_then_build_from_edited_plan(self):
        planned = self.quiet(main.run_build, "Build a greeter", review=True,
                             model=FakeModel([json.dumps(plan_obj())]))
        self.assertEqual(planned["status"], "planned")
        run_dir = Path(planned["run_dir"])
        self.assertTrue((run_dir / "SPEC.md").exists())
        self.assertFalse((run_dir / "summary.json").exists())

        # a human edits the plan and the spec
        plan = json.loads((run_dir / "plan.json").read_text())
        plan["tasks"][0]["title"] = "Greeter module (edited by reviewer)"
        (run_dir / "plan.json").write_text(json.dumps(plan))
        spec = run_dir / "SPEC.md"
        spec.write_text(spec.read_text() + "\nReviewer note: greet in Indonesian too.\n")

        # Stage 8: one loop per task — T1 writes a file and finishes, T2 finishes
        model = FakeModel([write("app.py", "print('halo')\n"), final("app.py prints halo"),
                           write("README.md", "# Greeter\n"), final("readme done")])
        s = self.quiet(main.run_build, None, from_run=str(run_dir), model=model)
        self.assertEqual(s["status"], "finished")
        self.assertEqual(s["plan"], {"title": "Greeter", "tasks": 2})
        first = model.calls[0]["messages"][0]["content"]
        self.assertIn("Greeter module (edited by reviewer)", first)
        self.assertIn("Reviewer note: greet in Indonesian too.", first)
        second_task = model.calls[2]["messages"][0]["content"]
        self.assertIn("Done when: `README.md` exists", second_task)
        self.assertIn("app.py prints halo", second_task)     # T1's hand-off note

        with self.assertRaisesRegex(PlanError, "already been built"):
            self.quiet(main.run_build, None, from_run=str(run_dir), model=FakeModel([]))

    def test_no_plan_is_stage5(self):
        model = FakeModel([final("done")])
        s = self.quiet(main.run_build, "Build a greeter", no_plan=True, model=model)
        self.assertIsNone(s["plan"])
        self.assertFalse((Path(s["run_dir"]) / "plan.json").exists())
        self.assertTrue(model.calls[0]["messages"][0]["content"].endswith("Start building."))

    def test_plan_failure_is_recorded(self):
        s = self.quiet(main.run_build, "x", model=FakeModel(["a", "b", "c"]))
        self.assertEqual(s["status"], "plan_failed")
        run_dir = Path(s["run_dir"])
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["status"], "plan_failed")
        self.assertEqual(len((run_dir / "planner.jsonl").read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
