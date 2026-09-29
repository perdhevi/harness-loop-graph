"""Chapter E tests — sensor curves, detectors, persistence, and the three consumers."""

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
from judge import tasks_evidence  # noqa: E402
from loop import Step  # noqa: E402
from sensors import (TaskSensors, combine, no_progress, repeated_action, repeated_edit,  # noqa: E402
                     same_failure, scope_drift)


class CurveTests(unittest.TestCase):
    def test_documented_points(self):
        self.assertEqual([repeated_edit(k) for k in (1, 2, 4, 6)], [0, 0, 0.5, 0.75])
        self.assertEqual([same_failure(r) for r in (1, 2, 3)], [0, 0.5, 0.75])
        self.assertEqual([no_progress(n) for n in (3, 7, 11)], [0, 0.5, 0.75])
        self.assertAlmostEqual(repeated_action(3), 0.6, places=2)
        self.assertEqual(scope_drift(0, 5), 0)
        self.assertEqual(scope_drift(1, 1), 0.5)

    def test_smooth_and_bounded(self):
        values = [no_progress(n) for n in range(0, 60)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(0 <= v < 1 for v in values))

    def test_combine_is_a_noisy_or(self):
        self.assertEqual(combine({}), 0)
        one = combine({"same_failure": 0.5})
        two = combine({"same_failure": 0.5, "no_progress": 0.5})
        self.assertGreater(two, one)                             # weak signals add up…
        self.assertLess(combine({k: 0.99 for k in ("same_failure", "no_progress", "repeated_action",
                                                   "repeated_edit")}), 1)   # …without going over 1


def step(n, action=None, args=None, observation=None, final=None, error=None):
    return Step(n=n, raw="", action=action, args=args or {}, observation=observation, final=final, error=error)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.task = {"id": "T1", "files": ["app.py"]}

    def write(self, sensors, n, path, content):
        (self.ws / path).write_text(content)
        sensors.observe_step(step(n, "workspace.write_file", {"path": path, "content": content},
                                  f"created {path}"))

    def test_same_content_again_is_not_progress(self):
        s = TaskSensors(self.task, self.ws)
        self.write(s, 1, "app.py", "x = 1\n")
        self.assertEqual(s.since_progress, 0)
        for n in range(2, 7):
            self.write(s, n, "app.py", "x = 1\n")          # same bytes: an edit, but no progress
        sc = s.scores()
        self.assertEqual(s.since_progress, 5)
        self.assertEqual(sc["repeated_edit"], 0.75)           # 6 writes
        self.assertGreater(sc["repeated_action"], 0.9)        # identical call ×6
        self.write(s, 7, "app.py", "x = 2\n")                 # new content: progress
        self.assertEqual(s.since_progress, 0)

    def test_failures_and_recovery(self):
        s = TaskSensors(self.task, self.ws)
        fail = "exit code: 1\n--- stderr ---\nTraceback\n  File \"/tmp/a/app.py\", line 3\nNameError: name 'x' is not defined"
        for n in range(1, 4):
            s.observe_step(step(n, "workspace.run_command", {"command": "python app.py"}, fail.replace("/a/", f"/r{n}/")))
        sc = s.scores()
        self.assertEqual(sc["same_failure"], 0.75)            # same signature ×3, paths ignored
        self.assertIn("same failure ×3", sc["reasons"])
        s.observe_step(step(4, "workspace.run_command", {"command": "python app.py"}, "exit code: 0\n"))
        self.assertEqual(s.since_progress, 0)                 # failed before, passes now

    def test_scope_drift_and_malformed_replies(self):
        s = TaskSensors(self.task, self.ws)
        self.write(s, 1, "app.py", "a")
        self.write(s, 2, "other.py", "b")
        self.assertGreater(s.scores()["drift"], 0)
        self.assertIn("1 write(s) outside the task's files", s.scores()["reasons"])
        s.observe_step(step(3, error="no JSON object found"))
        self.assertEqual(s.since_progress, 1)

    def test_counters_persist_across_attempts(self):
        s = TaskSensors(self.task, self.ws)
        s.observe_check(False, "AssertionError: add(2, 3) should be 5")
        s.save()
        again = TaskSensors(self.task, self.ws)               # e.g. the fix attempt, or after a resume
        again.observe_check(False, "AssertionError: add(2, 3) should be 5")
        self.assertEqual(again.save()["same_failure"], 0.5)
        self.assertEqual(json.loads(json.dumps(self.task))["signals"]["same_failure"], 0.5)   # JSON-safe

    def test_judge_evidence_only_when_notable(self):
        plan = {"tasks": [
            {"id": "T1", "title": "a", "status": "done", "signals": {"peak_stuck": 0.1, "drift": 0, "reasons": []}},
            {"id": "T2", "title": "b", "status": "failed",
             "signals": {"peak_stuck": 0.82, "drift": 0.0, "reasons": ["same failure ×3"]}}]}
        text = tasks_evidence(plan)
        self.assertNotIn("T1 a — done\n    signals", text)
        self.assertIn("signals: looked stuck 0.82 (peak), drift 0.00 — same failure ×3", text)


# ---------------------------------------------------------------- in a build

class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


def act(tool, **args):
    return json.dumps({"thought": "a", "action": tool, "args": args})


def final(note="done"):
    return json.dumps({"thought": "d", "final": note})


BAD = "def add(a, b):\n    return a - b\n"
GOOD = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, 'add(2, 3) should be 5'\n"


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, replies, sensors=True, judge=False, **cfg):
        config = json.loads((ROOT / "config.json").read_text())
        config["build"]["task_max_iterations"] = cfg.get("max_steps", 12)
        config["sensors"]["enabled"] = sensors
        (self.root / f"config-{sensors}.json").write_text(json.dumps(config))
        model = FakeModel(replies)
        out = io.StringIO()
        with mock.patch.object(main, "ROOT", self.root / ("on" if sensors else "off")), \
                mock.patch.object(main, "CONFIG_PATH", self.root / f"config-{sensors}.json"), \
                contextlib.redirect_stdout(out):
            s = main.run_build("calc", model=model, verbose_graph=False, judge=judge)
        return s, model, out.getvalue()

    def plan(self, *tasks):
        return json.dumps({"title": "Calc", "summary": "s", "tasks": list(tasks)})

    def test_looping_task_warns_before_its_limit(self):
        loop = [act("workspace.write_file", path="calc.py", content=BAD),
                act("workspace.run_command", command="python test_calc.py")] * 6
        s, model, out = self.build([
            self.plan({"id": "T1", "title": "calc", "description": "d", "files": ["calc.py"], "depends_on": [],
                       "done_when": {"command": "python test_calc.py"}}),
            act("workspace.write_file", path="test_calc.py", content=TEST), *loop])
        self.assertEqual(s["tasks"][0]["status"], "failed")                  # it ran out of steps…
        warn_at = out.index("[sensor] T1 looks stuck")
        self.assertLess(warn_at, out.index("[task] T1 → failed"))           # …but was flagged first
        warning_line = out[warn_at:].splitlines()[0]
        self.assertIn("same failure ×", warning_line)
        plan = json.loads((Path(s["run_dir"]) / "plan.json").read_text())
        self.assertGreaterEqual(plan["tasks"][0]["signals"]["peak_stuck"], 0.5)
        self.assertGreaterEqual(s["tasks"][0]["stuck"], 0.5)
        from trace_view import load_events
        warnings = [e for e in load_events(s["run_dir"]) if e["type"] == "signals" and e["warning"]]
        self.assertEqual(len(warnings), 1)                                    # warned once

    def test_fix_prompt_says_it_is_the_same_failure(self):
        s, model, out = self.build([
            self.plan({"id": "T1", "title": "calc", "description": "d", "files": ["calc.py", "test_calc.py"],
                       "depends_on": [], "done_when": {"command": "python test_calc.py"}}),
            act("workspace.write_file", path="calc.py", content=BAD),
            act("workspace.write_file", path="test_calc.py", content=TEST),
            act("workspace.run_command", command="python test_calc.py"),          # fails once in the loop
            final("done"),                                                        # …and again at the check
            act("workspace.write_file", path="calc.py", content=GOOD), final("fixed")])
        self.assertEqual(s["status"], "finished")
        fix_first = model.calls[5]["messages"][0]["content"]
        self.assertIn("this exact failure has now happened 2 times", fix_first)

    def test_sensors_do_not_change_control_flow(self):
        replies = lambda: [                                                   # noqa: E731
            self.plan({"id": "T1", "title": "calc", "description": "d", "files": ["calc.py"], "depends_on": [],
                       "done_when": {"command": "python test_calc.py"}},
                      {"id": "T2", "title": "readme", "description": "d", "files": ["README.md"],
                       "depends_on": ["T1"], "done_when": {"file": "README.md"}}),
            act("workspace.write_file", path="test_calc.py", content=TEST),
            act("workspace.write_file", path="calc.py", content=BAD), final("done"),
            final("still done"), final("really done"),                         # T1 fails after 2 fixes
        ]
        on, _, out_on = self.build(replies(), sensors=True)
        off, _, out_off = self.build(replies(), sensors=False)
        self.assertEqual([(t["id"], t["status"], t["steps"]) for t in on["tasks"]],
                         [(t["id"], t["status"], t["steps"]) for t in off["tasks"]])
        self.assertEqual(on["status"], off["status"])
        self.assertIn("[sensor]", out_on)
        self.assertNotIn("[sensor]", out_off)
        plan_off = json.loads((Path(off["run_dir"]) / "plan.json").read_text())
        self.assertNotIn("signals", plan_off["tasks"][0])


if __name__ == "__main__":
    unittest.main()
