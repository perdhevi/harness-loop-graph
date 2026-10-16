"""build --replan — a person asks for one task to be replaced (e.g. broken into smaller ones) (Chapter G)."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import main  # noqa: E402
from planner import PlanError, revise_plan  # noqa: E402
from runtime import load_json, prompt_text  # noqa: E402
from state import load_state  # noqa: E402
from test_judge import FakeModel, final, task, verdict, write  # noqa: E402
from test_rerun import escalated_run  # noqa: E402

PROMPTS = {k: prompt_text(load_json(ROOT / "prompts.json"), k) for k in load_json(ROOT / "prompts.json")}


def revision(*tasks, retry=(), drop=()):
    return json.dumps({"changes": "split it", "retry": list(retry), "tasks": list(tasks), "drop": list(drop)})


def plan_with_failed_t2():
    t = lambda tid, status, deps=(): {**task(tid, {"file": f"{tid}.py"}, deps), "status": status}  # noqa: E731
    return {"title": "Calc", "summary": "s", "tasks": [t("T1", "done"), t("T2", "failed", ["T1"]),
                                                      t("T3", "pending", ["T2"]), t("T4", "pending", ["T3"])]}


class ReplaceTaskTests(unittest.TestCase):
    def revise(self, replies, plan=None, replace="T2"):
        model = FakeModel(replies)
        new_plan, record = revise_plan(model, PROMPTS, request="calc", plan=plan or plan_with_failed_t2(), spec="",
                                       plan_status="", verdict={"feedback": "split T2"}, files="", round_no=1,
                                       replace=replace)
        return new_plan, record, model

    def test_split_drops_the_task_and_rewires_its_dependents(self):
        new_plan, record, _ = self.revise([revision(task("R1-1", {"file": "a.py"}, ["T1"]),
                                                    task("R1-2", {"file": "b.py"}, ["R1-1"]))])
        by_id = {t["id"]: t for t in new_plan["tasks"]}
        self.assertEqual(by_id["T2"]["status"], "dropped")              # not listed under drop: added for it
        self.assertEqual((by_id["T3"]["status"], by_id["T3"]["depends_on"]), ("pending", ["R1-2"]))
        self.assertEqual(by_id["T4"]["depends_on"], ["T3"])
        self.assertEqual((record["replaced"], record["rewired"], record["added"], record["dropped"]),
                         ("T2", ["T3"], ["R1-1", "R1-2"], ["T2"]))

    def test_two_parallel_new_tasks_are_both_needed(self):
        new_plan, record, _ = self.revise([revision(task("R1-1", {"file": "a.py"}, ["T1"]),
                                                    task("R1-2", {"file": "b.py"}, ["T1"]))])
        self.assertEqual(next(t for t in new_plan["tasks"] if t["id"] == "T3")["depends_on"], ["R1-1", "R1-2"])

    def test_mistakes_are_explained_then_fixed(self):
        good = revision(task("R1-1", {"file": "a.py"}, ["T1"]))
        for bad, msg in [
            (revision(retry=["T2"]), "T2 is being re-planned; don't retry it"),
            (revision(task("R1-1", {"file": "a.py"}, ["T2"])), "can't depend on T2, which it replaces; depend on what T2 "
                                                              "depended on (T1)"),
            (revision(drop=["T3"]), "add the new task(s) that replace T2"),
        ]:
            with self.subTest(msg=msg):
                new_plan, record, model = self.revise([bad, good])
                self.assertIn(msg, model.calls[1]["messages"][-1]["content"])
                self.assertEqual(record["added"], ["R1-1"])

    def test_already_dropped_task_can_be_listed_under_drop(self):
        p = plan_with_failed_t2()
        p["tasks"][1]["status"] = p["tasks"][2]["status"] = "dropped"
        new_plan, record, _ = self.revise([revision(task("R1-1", {"file": "a.py"}, ["T1"]), drop=["T2"])], plan=p)
        t3 = next(t for t in new_plan["tasks"] if t["id"] == "T3")
        self.assertEqual((t3["status"], t3["depends_on"]), ("dropped", ["R1-1"]))   # stays dropped: re-run it later
        self.assertEqual(record["dropped"], [])

    def test_without_replace_nothing_changes(self):
        new_plan, record, _ = self.revise([revision(task("R1-1", {"file": "a.py"}, ["T1"]), drop=["T2"])],
                                          replace=None)
        self.assertNotIn("replaced", record)
        self.assertEqual([t["status"] for t in new_plan["tasks"][1:4]], ["dropped", "dropped", "dropped"])


class ReplanBuildTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        config["build"]["task_max_iterations"] = 3
        config["judge"]["max_revisions"] = 1
        config["memory"]["enabled"] = False
        (tmp / "config.json").write_text(json.dumps(config))
        for target, value in [("ROOT", tmp), ("CONFIG_PATH", tmp / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def test_replan_a_dropped_task_then_build_and_judge(self):
        run = escalated_run(self.build)                         # T2 and T3 dropped in round 1
        model = FakeModel([
            revision(task("R2-1", {"file": "parse.py"}, ["T1"]), task("R2-2", {"file": "cli.py"}, ["R2-1"])),
            write("parse.py"), final(), write("cli.py"), final(),
            verdict("accept"),
        ])
        s = self.build(None, from_run=str(run), replan="T2", fix="split it into a parser and the CLI", model=model)
        self.assertEqual(s["status"], "accepted")
        reviser_saw = model.calls[0]["messages"][0]["content"]
        self.assertIn('Re-plan task T2 ("task T2"): split it into a parser and the CLI', reviser_saw)
        tasks = {t["id"]: t for t in json.loads((run / "plan.json").read_text(encoding="utf-8"))["tasks"]}
        self.assertEqual([tasks[x]["status"] for x in ("T2", "R2-1", "R2-2")], ["dropped", "done", "done"])
        self.assertEqual((tasks["T3"]["status"], tasks["T3"]["depends_on"]), ("dropped", ["R2-2"]))
        spec = (run / "SPEC.md").read_text(encoding="utf-8")
        self.assertIn("**Re-planned:** T2 → R2-1, R2-2 (now depended on by T3)", spec)
        st = load_state(run)
        self.assertEqual(st.verdicts[-2]["replan"], "T2")

        # and the task that depended on it can now be re-run on top of the new tasks
        s = self.build(None, from_run=str(run), rerun=["T3"], model=FakeModel([write("README.md"), final(),
                                                                              verdict("accept")]))
        self.assertEqual((s["status"], [t["status"] for t in s["tasks"]].count("dropped")), ("accepted", 1))

    def test_default_comment_and_refusals(self):
        run = escalated_run(self.build)
        model = FakeModel(["no json"] * 3)
        s = self.build(None, from_run=str(run), replan="T2", model=model)
        self.assertEqual(s["status"], "escalated")                  # no valid revision: escalated with the reason
        self.assertIn("Break it into smaller tasks, each with its own check.", model.calls[0]["messages"][0]["content"])
        for tid, msg in [("T1", "T1 is done; to change finished work, use --fix"), ("T9", "no task 'T9'")]:
            with self.subTest(tid=tid), self.assertRaisesRegex(PlanError, msg):
                self.build(None, from_run=str(run), replan=tid, model=FakeModel([]))

    def test_cli(self):
        with mock.patch.object(main, "run_build", return_value={"status": "accepted", "run_dir": "d"}) as rb, \
                mock.patch.object(main, "print_summary"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["build", "--replan", "runs/x", "T3", "split", "it", "up"]), 0)
        kw = rb.call_args.kwargs
        self.assertEqual((kw["from_run"], kw["replan"], kw["fix"]), ("runs/x", "T3", "split it up"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["build", "--replan", "runs/x"]), 1)
        self.assertIn("Say which task to re-plan", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["build", "--replan", "runs/x", "--rerun", "runs/x", "T3"])


if __name__ == "__main__":
    unittest.main()
