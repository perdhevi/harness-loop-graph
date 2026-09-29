"""build --fix — change a finished run with a person's feedback (revise → tasks → checks → judge)."""

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
sys.path.insert(0, str(ROOT / "tests"))

import main  # noqa: E402
from planner import PlanError  # noqa: E402
from state import load_state  # noqa: E402
from test_judge import FakeModel, final, plan, task, verdict, write  # noqa: E402

FEEDBACK = "Also add a README.md that explains how to run calc.py"


def revision(*tasks, retry=()):
    return json.dumps({"changes": "add what was asked", "retry": list(retry), "tasks": list(tasks)})


class FixTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config = json.loads((ROOT / "config.json").read_text())
        config["build"]["task_max_iterations"] = 3
        config["judge"]["max_revisions"] = 1
        (tmp / "config.json").write_text(json.dumps(config))
        for target, value in [("ROOT", tmp), ("CONFIG_PATH", tmp / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def finished_run(self, judge=True) -> str:
        replies = [plan(task("T1", {"file": "calc.py"})), write("calc.py"), final("calc.py adds")]
        if judge:
            replies.append(verdict("accept"))
        s = self.build("calc", judge=None if judge else False, model=FakeModel(replies))
        self.assertEqual(s["status"], "accepted" if judge else "finished")
        return s["run_dir"]

    def test_fix_adds_a_task_and_is_judged_again(self):
        run_dir = self.finished_run()
        model = FakeModel([
            revision(task("R1-1", {"file": "README.md"}, ["T1"])),
            write("README.md", "# Calc\n"), final("readme written"),
            verdict("accept", reqs=[("adds numbers", True), ("has a README", True)]),
        ])
        s = self.build(None, from_run=run_dir, fix=FEEDBACK, model=model)
        self.assertEqual((s["status"], s["revisions"]), ("accepted", 1))
        self.assertIn(FEEDBACK, model.calls[0]["messages"][0]["content"])             # the reviser saw it
        self.assertEqual([(t["id"], t["status"]) for t in s["tasks"]], [("T1", "done"), ("R1-1", "done")])
        spec = (Path(run_dir) / "SPEC.md").read_text()
        self.assertIn("## Revision 1", spec)
        self.assertIn(f"**Why (fix requested by a person):** {FEEDBACK}", spec)
        st = load_state(run_dir)
        self.assertEqual([v.get("source", "judge") for v in st.verdicts], ["judge", "human", "judge"])
        self.assertIn("revise", st.history[st.history.index("finish") + 1:])
        self.assertIn("has a README", (Path(run_dir) / "REPORT.md").read_text())

    def test_judge_gets_its_own_budget_after_a_fix(self):
        # max_revisions is 1: without its own budget, the judge's revise below would be escalated at once
        run_dir = self.finished_run()
        s = self.build(None, from_run=run_dir, fix=FEEDBACK, model=FakeModel([
            revision(task("R1-1", {"file": "README.md"}, ["T1"])),
            write("README.md"), final(),
            verdict("revise", reqs=[("has a README", False)], feedback="README must show an example"),
            revision(task("R2-1", {"file": "EXAMPLE.md"}, ["R1-1"])),
            write("EXAMPLE.md"), final(),
            verdict("accept", reqs=[("has a README", True)]),
        ]))
        self.assertEqual((s["status"], s["revisions"]), ("accepted", 2))
        self.assertEqual([t["id"] for t in s["tasks"]], ["T1", "R1-1", "R2-1"])

    def test_fix_without_judge_uses_the_checks(self):
        run_dir = self.finished_run(judge=False)
        s = self.build(None, from_run=run_dir, fix=FEEDBACK, model=FakeModel([
            revision(task("R1-1", {"file": "README.md"}, ["T1"])), write("README.md"), final(),
        ]))
        self.assertEqual(s["status"], "finished")
        self.assertNotIn("verdict", s)

    def test_fix_that_cannot_be_planned_escalates(self):
        run_dir = self.finished_run(judge=False)
        s = self.build(None, from_run=run_dir, fix=FEEDBACK, model=FakeModel(["no", "json", "here"]))
        self.assertEqual(s["status"], "escalated")
        self.assertIn("revision failed", s["error"])
        self.assertEqual([t["id"] for t in s["tasks"]], ["T1"])        # nothing changed

    def test_refuses_runs_that_are_not_finished_or_have_no_plan(self):
        with self.assertRaisesRegex(PlanError, "without a plan"):
            s = self.build("calc", no_plan=True, model=FakeModel([write("calc.py"), final()]))
            self.build(None, from_run=s["run_dir"], fix=FEEDBACK, model=FakeModel([]))
        planned = self.build("calc", review=True, model=FakeModel([plan(task("T1", {"file": "calc.py"}))]))
        with self.assertRaisesRegex(PlanError, "hasn't finished .*--resume"):
            self.build(None, from_run=planned["run_dir"], fix=FEEDBACK, model=FakeModel([]))
        run_dir = self.finished_run()
        with self.assertRaisesRegex(PlanError, "needs a description"):
            self.build(None, from_run=run_dir, fix="  ", model=FakeModel([]))

    def test_cli(self):
        run_dir = self.finished_run(judge=False)
        with mock.patch.object(main, "run_build", return_value={"status": "finished"}) as rb, \
                mock.patch.object(main, "print_summary"):
            self.assertEqual(main.main(["build", "--fix", run_dir, "add", "a", "README"]), 0)
        self.assertEqual(rb.call_args.kwargs["fix"], "add a README")
        self.assertEqual(rb.call_args.kwargs["from_run"], run_dir)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main.main(["build", "--fix", run_dir, "--resume", run_dir, "x"])


if __name__ == "__main__":
    unittest.main()
