"""Stage 10 tests — verdicts, hard rules, revise rounds, escalation and the report."""

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
from judge import apply_rules, make_verdict, validate_verdict, workspace_contents  # noqa: E402
from planner import _apply_revision  # noqa: E402
from runtime import load_json, prompt_text  # noqa: E402
from state import load_state  # noqa: E402

PROMPTS = {k: prompt_text(load_json(ROOT / "prompts.json"), k) for k in load_json(ROOT / "prompts.json")}


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


def verdict(v, reqs=(("adds numbers", True),), feedback="", problems=()):
    return json.dumps({"verdict": v, "requirements": [{"requirement": r, "met": m, "evidence": "calc.py"}
                                                      for r, m in reqs],
                       "problems": list(problems), "feedback": feedback, "summary": f"judge says {v}"})


def plan(*tasks):
    return json.dumps({"title": "Calc", "summary": "Adds numbers.", "tasks": list(tasks)})


def task(tid, done_when, deps=()):
    return {"id": tid, "title": f"task {tid}", "description": "d", "files": [], "depends_on": list(deps),
            "done_when": done_when}


def write(path, content="x = 1\n"):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": content}})


def final(note="done"):
    return json.dumps({"thought": "d", "final": note})


def look():
    return json.dumps({"thought": "look", "action": "workspace.list_dir", "args": {}})


def P(tasks, **kw):
    return {"tasks": [{"id": t, "status": st, "title": t} for t, st in tasks], **kw}


# ---------------------------------------------------------------- units

class VerdictTests(unittest.TestCase):
    def test_validation(self):
        _, errors = validate_verdict({"verdict": "maybe", "requirements": [{"requirement": "x"}],
                                      "summary": ""})
        text = "\n".join(errors)
        self.assertIn("'verdict' must be one of accept, revise, escalate", text)
        self.assertIn("requirement #1 needs", text)
        self.assertIn("'summary' must be a non-empty string", text)
        _, errors = validate_verdict({"verdict": "revise", "summary": "s"})
        self.assertIn("'feedback' is required when the verdict is revise", errors)

    def test_invalid_twice_escalates(self):
        v = make_verdict(FakeModel(["no json", '{"verdict": "accept"}']), PROMPTS,
                         {"request": "r", "spec": "", "tasks": "", "final_checks": "", "files": ""})
        self.assertEqual(v["verdict"], "escalate")
        self.assertTrue(v["judge_failed"])

    def test_think_block_and_evidence_in_prompt(self):
        model = FakeModel(["<think>hmm</think>" + verdict("accept")])
        v = make_verdict(model, PROMPTS, {"request": "ADD NUMBERS", "spec": "S", "tasks": "T1 done",
                                          "final_checks": "pass: x", "files": "----- calc.py -----"})
        self.assertEqual(v["verdict"], "accept")
        sent = model.calls[0]["messages"][0]["content"]
        for piece in ["ADD NUMBERS", "T1 done", "pass: x", "----- calc.py -----"]:
            self.assertIn(piece, sent)
        self.assertIn("You did not build this project", model.calls[0]["system"])


class RuleTests(unittest.TestCase):
    ok_plan = P([("T1", "done")])

    def rules(self, v, plan=None, final_checks=None, used=0, max_rev=2):
        base = json.loads(verdict("accept")) if isinstance(v, str) and v == "accept" else v
        return apply_rules(base, plan or self.ok_plan, final_checks, revisions_used=used, max_revisions=max_rev)

    def test_clean_accept_stays(self):
        v = self.rules("accept")
        self.assertEqual((v["verdict"], v["overrides"]), ("accept", []))

    def test_evidence_overrides_accept(self):
        cases = [
            (P([("T1", "done"), ("T2", "failed")]), None, "tasks not done: T2"),
            (None, [{"ok": False, "target": "python -m pytest -q"}], "final checks failed: python -m pytest -q"),
        ]
        for plan_, checks, reason in cases:
            with self.subTest(reason=reason):
                v = self.rules("accept", plan=plan_, final_checks=checks)
                self.assertEqual(v["verdict"], "revise")
                self.assertIn(reason, v["overrides"][0]["reason"])
                self.assertIn("Harness:", v["feedback"])

    def test_unmet_or_missing_requirements_override_accept(self):
        v = self.rules(json.loads(verdict("accept", reqs=[("adds", True), ("subtracts", False)])))
        self.assertIn("requirements marked not met: subtracts", v["overrides"][0]["reason"])
        v = self.rules(json.loads(verdict("accept", reqs=[])))
        self.assertIn("listed no requirements", v["overrides"][0]["reason"])

    def test_revise_becomes_escalate_when_rounds_are_used(self):
        v = self.rules(json.loads(verdict("revise", feedback="add tests")), used=2, max_rev=2)
        self.assertEqual(v["verdict"], "escalate")
        v = self.rules("accept", plan=P([("T1", "failed")]), used=2, max_rev=2)   # accept → revise → escalate
        self.assertEqual([o["to"] for o in v["overrides"]], ["revise", "escalate"])


class EvidenceTests(unittest.TestCase):
    def test_workspace_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "a.py").write_text("print('a')\n")
            (ws / "big.py").write_text("x" * 500)
            (ws / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
            (ws / "__pycache__").mkdir()
            (ws / "__pycache__" / "a.pyc").write_bytes(b"\x00")
            (ws / "z.txt").write_text("y" * 300)
            text = workspace_contents(ws, file_chars=100, total_chars=250)
            self.assertIn("----- a.py -----\nprint('a')", text)
            self.assertIn("[cut: 400 more chars]", text)
            self.assertIn("logo.png (binary", text)
            self.assertIn("z.txt (left out: evidence limit reached)", text)
            self.assertNotIn("__pycache__", text)


class RevisionMergeTests(unittest.TestCase):
    plan = {"title": "t", "summary": "s", "tasks": [
        {"id": "T1", "title": "a", "status": "failed", "fix_attempts": 2, "error": "check failed",
         "checks": [{"ok": False}], "depends_on": [], "done_when": {"file": "a"}, "files": []},
        {"id": "T2", "title": "b", "status": "blocked", "error": "blocked by T1", "depends_on": ["T1"],
         "done_when": {"file": "b"}, "files": []},
        {"id": "T3", "title": "c", "status": "done", "depends_on": [], "done_when": {"file": "c"}, "files": []}]}

    def test_retry_resets_task_and_unblocks(self):
        merged, errors = _apply_revision(self.plan, {"retry": ["T1"]})
        self.assertEqual(errors, [])
        t1, t2, t3 = merged["tasks"]
        self.assertEqual((t1["status"], t1["fix_attempts"], "error" in t1), ("pending", 0, False))
        self.assertEqual(len(t1["checks"]), 1)                  # history kept
        self.assertEqual(t2["status"], "pending")
        self.assertEqual(t3["status"], "done")
        self.assertEqual(self.plan["tasks"][0]["status"], "failed")   # original untouched

    def test_rules(self):
        _, errors = _apply_revision(self.plan, {"retry": ["T3", "T9"]})
        self.assertTrue(any("task 'T3' is done" in e for e in errors))
        self.assertTrue(any("unknown task 'T9'" in e for e in errors))
        _, errors = _apply_revision(self.plan, {})
        self.assertTrue(any("change something" in e for e in errors))


# ---------------------------------------------------------------- flows

class JudgeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.config = json.loads((ROOT / "config.json").read_text())
        self.config["build"]["task_max_iterations"] = 3
        self.config["judge"]["max_revisions"] = 1
        (tmp / "config.json").write_text(json.dumps(self.config))
        for target, value in [("ROOT", tmp), ("CONFIG_PATH", tmp / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def test_accept_writes_report(self):
        model = FakeModel([
            plan(task("T1", {"file": "calc.py"}), task("T2", {"file": "README.md"}, ["T1"])),
            write("calc.py", "def add(a, b):\n    return a + b\n"), final("add() in calc.py"),
            write("README.md", "# Calc\n\nRun: python -c \"import calc; print(calc.add(2, 3))\"\n"), final("readme"),
            verdict("accept", reqs=[("adds two numbers", True), ("has run instructions", True)]),
        ])
        s = self.build("a calculator that adds", model=model)
        self.assertEqual(s["status"], "accepted")
        self.assertEqual(s["verdict"]["verdict"], "accept")
        judge_prompt = model.calls[-1]["messages"][0]["content"]
        self.assertIn("def add(a, b):", judge_prompt)                 # the judge saw the code
        self.assertIn("T1 task T1 — done", judge_prompt)
        report = (Path(s["run_dir"]) / "REPORT.md").read_text()
        self.assertIn("**accepted**", report)
        self.assertIn("| adds two numbers | ✅ | calc.py |", report)
        self.assertIn('Run: python -c "import calc', report)           # how to run, from the README
        self.assertEqual(load_state(s["run_dir"]).history[-3:], ["final_check", "judge", "finish"])

    def test_revise_adds_task_then_accept(self):
        model = FakeModel([
            plan(task("T1", {"file": "calc.py"})),
            write("calc.py"), final("calc.py"),
            verdict("revise", reqs=[("adds", True), ("has a README", False)], feedback="Add a README.md"),
            json.dumps({"changes": "add README", "retry": [],
                        "tasks": [task("R1-1", {"file": "README.md"}, ["T1"])]}),
            write("README.md", "# Calc\n"), final("readme added"),
            verdict("accept", reqs=[("adds", True), ("has a README", True)]),
        ])
        s = self.build("calc", model=model)
        self.assertEqual((s["status"], s["revisions"]), ("accepted", 1))
        run_dir = Path(s["run_dir"])
        st = load_state(run_dir)
        self.assertEqual([v["verdict"] for v in st.verdicts], ["revise", "accept"])
        plan_now = json.loads((run_dir / "plan.json").read_text())
        self.assertEqual([(t["id"], t["status"]) for t in plan_now["tasks"]], [("T1", "done"), ("R1-1", "done")])
        spec = (run_dir / "SPEC.md").read_text()
        self.assertIn("## Revision 1", spec)
        self.assertIn("**Why:** Add a README.md", spec)
        self.assertIn("| R1-1 | task R1-1 | T1 | `README.md` exists |", spec)
        self.assertIn("New task ids must start with R1-", model.calls[4]["messages"][0]["content"])
        self.assertIn("README.md", model.calls[-1]["messages"][0]["content"])   # second judge saw the new file

    def test_accept_with_failed_task_is_overridden_and_retried(self):
        model = FakeModel([
            plan(task("T1", {"file": "calc.py"}), task("T2", {"file": "README.md"}, ["T1"])),
            look(), look(), look(),                                    # T1 runs out of steps → failed, T2 blocked
            verdict("accept"),                                         # judge is too lenient
            json.dumps({"changes": "retry T1", "retry": ["T1"], "tasks": []}),
            write("calc.py"), final("calc"),                           # T1 retried
            write("README.md"), final("readme"),                       # T2 unblocked
            verdict("accept", reqs=[("adds", True)]),
        ])
        s = self.build("calc", model=model)
        self.assertEqual(s["status"], "accepted")
        first = load_state(s["run_dir"]).verdicts[0]
        self.assertEqual(first["verdict"], "revise")
        self.assertIn("tasks not done: T1, T2", first["overrides"][0]["reason"])
        self.assertEqual([t["status"] for t in s["tasks"]], ["done", "done"])

    def test_revisions_run_out_then_escalate(self):
        model = FakeModel([
            plan(task("T1", {"file": "calc.py"})),
            write("calc.py"), final("calc"),
            verdict("revise", reqs=[("subtracts", False)], feedback="Add subtract()"),
            json.dumps({"changes": "add subtract", "tasks": [task("R1-1", {"file": "sub.py"}, ["T1"])]}),
            write("sub.py"), final("sub"),
            verdict("revise", reqs=[("subtracts", False)], feedback="subtract is still wrong"),
        ])
        s = self.build("calc", model=model)
        self.assertEqual(s["status"], "escalated")
        v = s["verdict"]
        self.assertEqual(v["overrides"][-1]["to"], "escalate")
        self.assertIn("no revision rounds left (1/1 used)", v["overrides"][-1]["reason"])
        report = (Path(s["run_dir"]) / "REPORT.md").read_text()
        self.assertIn("**escalated**", report)
        self.assertIn("| subtracts | ❌ |", report)
        self.assertIn("subtract is still wrong", report)
        self.assertEqual(model.replies, [])

    def test_no_valid_verdict_escalates(self):
        s = self.build("calc", model=FakeModel([
            plan(task("T1", {"file": "calc.py"})), write("calc.py"), final(),
            "I think it's fine", "still not json",
        ]))
        self.assertEqual(s["status"], "escalated")
        self.assertTrue(s["verdict"]["judge_failed"])
        self.assertTrue((Path(s["run_dir"]) / "REPORT.md").exists())

    def test_no_judge_is_stage9(self):
        s = self.build("calc", judge=False, model=FakeModel([plan(task("T1", {"file": "c.py"})),
                                                              write("c.py"), final()]))
        self.assertEqual(s["status"], "finished")
        self.assertNotIn("verdict", s)
        self.assertFalse((Path(s["run_dir"]) / "REPORT.md").exists())


if __name__ == "__main__":
    unittest.main()
