"""build --rerun — run dropped, failed or blocked tasks of a finished run again, then judge it again (Chapter G)."""

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
from planner import PlanError, reopen_tasks  # noqa: E402
from state import load_state, save_state  # noqa: E402
from test_judge import FakeModel, final, plan, task, verdict, write  # noqa: E402


def hangman_plan():
    """The shape of a real run: T3 was dropped in round 1 (T4 with it), R1-1 replaced part of it, R2-1 failed."""
    t = lambda tid, status, deps=(), **kw: {"id": tid, "title": f"task {tid}", "description": "d", "files": [],  # noqa: E731
                                            "depends_on": list(deps), "done_when": {"file": f"{tid}.py"},
                                            "status": status, **kw}
    return {"title": "Hangman", "summary": "s", "tasks": [
        t("T1", "done"), t("T2", "done", ["T1"]),
        t("T3", "dropped", ["T2"], dropped_in=1), t("T4", "dropped", ["T3"], dropped_in=1),
        t("R1-1", "done"), t("R2-1", "failed", error="ran out of steps", fix_pending=True, fix_attempts=2),
        t("R2-2", "blocked", ["R2-1"], error="blocked by R2-1"),
    ]}


def escalated_run(build) -> Path:
    """A finished run whose T2 and T3 were dropped (their files never written), judged escalate.

    `build(request, model=…)` runs a build and returns its summary.
    """
    s = build("calc", model=FakeModel([
        plan(task("T1", {"file": "calc.py"}), task("T2", {"file": "cli.py"}, ["T1"]),
             task("T3", {"file": "README.md"}, ["T2"])),
        write("calc.py"), final(), write("cli.py"), final(), write("README.md"), final(),
        verdict("accept")]))
    run = Path(s["run_dir"])
    plan_file = run / "plan.json"
    p = json.loads(plan_file.read_text(encoding="utf-8"))
    for t in p["tasks"][1:]:
        t.update(status="dropped", dropped_in=1, verified=False)
        (run / "workspace" / t["done_when"]["file"]).unlink()
    plan_file.write_text(json.dumps(p), encoding="utf-8")
    st = load_state(run)
    st.plan, st.status, st.revisions = p, "escalated", 1
    st.verdicts.append(json.loads(verdict("escalate")) | {"source": "judge", "overrides": [], "round": 1})
    save_state(st)
    return run


RERUN_T2 = [write("cli.py"), final("cli back"), write("README.md"), final("readme back"),
            verdict("accept", reqs=[("adds numbers", True), ("has a CLI", True)])]


class ReopenTests(unittest.TestCase):
    def test_reopens_the_task_and_its_unfinished_dependents(self):
        p = hangman_plan()
        self.assertEqual(reopen_tasks(p, ["T3"]), ["T3", "T4"])
        by_id = {t["id"]: t for t in p["tasks"]}
        for tid in ("T3", "T4"):
            t = by_id[tid]
            self.assertEqual((t["status"], t["reopened"], t["fix_attempts"], t["verified"]), ("pending", 1, 0, False))
            self.assertNotIn("dropped_in", t)
        self.assertEqual([by_id[x]["status"] for x in ("T1", "T2", "R1-1", "R2-1")], ["done", "done", "done", "failed"])
        self.assertEqual(reopen_tasks(p := hangman_plan(), ["R2-1"]), ["R2-1", "R2-2"])
        self.assertNotIn("error", p["tasks"][5])
        self.assertNotIn("fix_pending", p["tasks"][5])

    def test_without_dependents(self):
        p = hangman_plan()
        self.assertEqual(reopen_tasks(p, ["T3"], dependents=False), ["T3"])
        self.assertEqual(p["tasks"][3]["status"], "dropped")

    def test_refusals_change_nothing(self):
        for ids, kw, msg in [
            (["T4"], {}, r"T4 needs T3 \(dropped\): re-run T3 too"),
            (["R2-2"], {}, r"R2-2 needs R2-1 \(failed\)"),
            (["T2"], {}, "T2 is done; only dropped, failed or blocked .*--fix"),
            (["T9"], {}, "no task 'T9'"),
            ([" "], {}, "say which task"),
        ]:
            with self.subTest(ids=ids):
                p = hangman_plan()
                with self.assertRaisesRegex(PlanError, msg):
                    reopen_tasks(p, ids, **kw)
                self.assertEqual(p, hangman_plan())
        p = hangman_plan()
        self.assertEqual(reopen_tasks(p, ["T4", "T3"], dependents=False), ["T3", "T4"])     # named together: fine


class RerunBuildTests(unittest.TestCase):
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

    def escalated_run(self) -> Path:
        return escalated_run(self.build)

    def test_rerun_runs_the_dropped_tasks_and_is_judged_again(self):
        run = self.escalated_run()
        model = FakeModel(list(RERUN_T2))
        s = self.build(None, from_run=str(run), rerun=["T2"], model=model)
        self.assertEqual(s["status"], "accepted")
        self.assertEqual([(t["id"], t["status"]) for t in s["tasks"]], [("T1", "done"), ("T2", "done"), ("T3", "done")])
        self.assertTrue((run / "workspace" / "cli.py").exists() and (run / "workspace" / "README.md").exists())
        st = load_state(run)
        self.assertEqual([v.get("source", "judge") for v in st.verdicts][-2:], ["human", "judge"])
        self.assertEqual(st.verdicts[-2]["feedback"], "re-run T2, T3")
        self.assertEqual(st.history[st.history.index("finish") + 1], "next_task")
        self.assertIn("**Re-opened:** T2, T3", (run / "SPEC.md").read_text(encoding="utf-8"))
        events = [json.loads(x) for x in (run / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn({"asked": ["T2"], "reopened": ["T2", "T3"]},
                      [{k: e[k] for k in ("asked", "reopened")} for e in events if e.get("event") == "rerun"])
        self.assertEqual(json.loads((run / "plan.json").read_text(encoding="utf-8"))["tasks"][1]["reopened"], 1)

    def test_rerun_without_judge_is_not_labelled_by_the_old_verdict(self):
        run = self.escalated_run()
        s = self.build(None, from_run=str(run), rerun=["T2"], judge=False,
                       model=FakeModel([write("cli.py"), final(), write("README.md"), final()]))
        self.assertEqual(s["status"], "finished")

    def test_refusals(self):
        run = self.escalated_run()
        with self.assertRaisesRegex(PlanError, "T3 needs T2 \\(dropped\\)"):
            self.build(None, from_run=str(run), rerun=["T3"], model=FakeModel([]))
        with self.assertRaisesRegex(PlanError, "T1 is done"):
            self.build(None, from_run=str(run), rerun=["T1"], model=FakeModel([]))
        planned = self.build("calc", review=True, model=FakeModel([plan(task("T1", {"file": "calc.py"}))]))
        with self.assertRaisesRegex(PlanError, "hasn't finished .*--resume"):
            self.build(None, from_run=planned["run_dir"], rerun=["T1"], model=FakeModel([]))
        self.assertEqual(load_state(run).status, "escalated")                    # a refusal changes nothing

    def test_cli(self):
        with mock.patch.object(main, "run_build", return_value={"status": "accepted", "run_dir": "d",
                                                               "plan": {"title": "t", "tasks": 1}}) as rb, \
                mock.patch.object(main, "print_summary"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["build", "--rerun", "runs/x", "T3,T5", "--no-dependents"]), 0)
        kw = rb.call_args.kwargs
        self.assertEqual((kw["from_run"], kw["rerun"], kw["rerun_dependents"]), ("runs/x", ["T3", "T5"], False))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["build", "--rerun", "runs/x"]), 1)
        self.assertIn("Say which task(s) to re-run", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main.main(["build", "--rerun", "runs/x", "--fix", "runs/x", "T3"])


if __name__ == "__main__":
    unittest.main()
