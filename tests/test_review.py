"""Chapter J — review and fix an existing project, against tests/review_sample (average() has a planted bug)."""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import review as review_mod  # noqa: E402
from loop import parse_action  # noqa: E402
from review import (ReadOnlyTools, ReviewError, detect_tests, import_project, make_patch,  # noqa: E402
                    parse_review)
from state import load_state  # noqa: E402
from tools.registry import ToolRegistry, ToolSpec  # noqa: E402
from trace_view import load_events  # noqa: E402

SAMPLE = ROOT / "tests" / "review_sample"
TESTS = "python -m unittest -q"
BUG = "return sum(values) / len(values) - 1"
FIXED = "return sum(values) / len(values)"


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


def j(**kw):
    return json.dumps(kw)


def act(tool, **args):
    return j(thought="t", action=f"workspace.{tool}", args=args)


def findings(*items, summary="average() is off by one"):
    return j(thought="done", final={"summary": summary, "findings": list(items)})


F1 = {"id": "F1", "file": "stats.py", "line": 5, "severity": "high", "problem": "average() subtracts 1",
      "evidence": "test_average fails: 2.0 != 3", "fix": "remove the - 1"}
PLAN = j(title="Fix average", summary="Fix the off-by-one in average().", tasks=[
    {"id": "T1", "title": "Fix average()", "description": "Remove the - 1", "files": ["stats.py"],
     "depends_on": [], "done_when": {"command": "python -m unittest -q test_stats"}}])
ACCEPT = j(verdict="accept", requirements=[{"requirement": "average is correct", "met": True,
                                            "evidence": "test_average passes"}], problems=[], feedback="", summary="ok")


def copy_sample(dest: Path) -> Path:
    shutil.copytree(SAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = copy_sample(self.tmp / "project")
        config = json.loads((ROOT / "config.json").read_text())
        config["build"]["task_max_iterations"] = 4
        config["review"]["max_iterations"] = 5
        (self.tmp / "config.json").write_text(json.dumps(config))
        for target, value in [("ROOT", self.tmp), ("CONFIG_PATH", self.tmp / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def run_review(self, replies, **kw):
        kw.setdefault("test_command", TESTS)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            s = main.run_build(kw.pop("request", "average() gives wrong results"), model=FakeModel(replies),
                               verbose_graph=False, source=str(self.project), **kw)
            main.print_summary(s)
        return s, out.getvalue()


class ReviewFlowTests(Harness):
    def test_review_fix_and_patch(self):
        before = {p.name: p.read_bytes() for p in self.project.iterdir()}
        s, out = self.run_review([
            act("read_file", path="stats.py"),
            act("write_file", path="stats.py", content="x"),          # blocked: the review only reads
            act("run_command", command=TESTS),
            findings(F1),
            PLAN,
            act("edit_file", path="stats.py", old=BUG, new=FIXED), j(thought="d", final="fixed average"),
            ACCEPT,
        ])
        self.assertEqual(s["status"], "accepted", out)
        run = Path(s["run_dir"])

        # the review
        review_md = (run / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("| F1 | high | `stats.py:5` | average() subtracts 1 |", review_md)
        self.assertIn("FAILED (exit 1)", review_md)                              # the baseline, before any change
        steps = [json.loads(x) for x in (run / "transcript.jsonl").read_text().splitlines()]
        blocked = [x for x in steps if x.get("phase") == "review" and x.get("action") == "workspace.write_file"]
        self.assertIn("not available while reviewing", blocked[0]["observation"])
        self.assertIn(BUG, (run / "original" / "stats.py").read_text())        # the copy the patch is made against

        # the planner saw the findings and the baseline
        spec = (run / "SPEC.md").read_text(encoding="utf-8")
        self.assertIn("## Review findings", spec)
        self.assertIn("F1 [high] stats.py:5", spec)

        # tests before and after, and the patch
        self.assertEqual((s["baseline"]["ok"], s["baseline_after"]["ok"]), (False, True))
        self.assertEqual(s["changes"], {"changed": ["stats.py"], "added": [], "deleted": []})
        patch = (run / "CHANGES.patch").read_text(encoding="utf-8")
        self.assertIn(f"-    {BUG}", patch)
        self.assertIn(f"+    {FIXED}", patch)
        self.assertIn("apply    : python main.py apply", out)
        self.assertIn("## Review and changes", (run / "REPORT.md").read_text(encoding="utf-8"))

        # the user's folder is unchanged until the patch is applied; then the tests pass
        self.assertEqual({p.name: p.read_bytes() for p in self.project.iterdir()}, before)
        subprocess.run(["git", "apply", str(run / "CHANGES.patch")], cwd=self.project, check=True)
        self.assertIn(FIXED, (self.project / "stats.py").read_text())
        res = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=self.project, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # trace roles
        roles = {e["role"] for e in load_events(run) if e["type"] == "model"}
        self.assertTrue({"reviewer", "planner", "task", "judge"} <= roles, roles)
        self.assertEqual(load_state(run).history[:4], ["intake", "survey", "review", "plan"])

    def test_only_review_changes_nothing(self):
        s, out = self.run_review([act("read_file", path="stats.py"), findings(F1)], only_review=True)
        self.assertEqual((s["status"], s["findings"]), ("reviewed", 1))
        run = Path(s["run_dir"])
        self.assertTrue((run / "REVIEW.md").exists())
        self.assertFalse((run / "CHANGES.patch").exists())
        self.assertFalse((run / "plan.json").exists())
        self.assertIn("Reviewed: 1 finding(s). Nothing was changed.", out)

    def test_project_tests_that_passed_must_still_pass(self):
        (self.project / "stats.py").write_text((self.project / "stats.py").read_text().replace(BUG, FIXED))
        plan = j(title="Tidy", summary="s", tasks=[
            {"id": "T1", "title": "Tidy largest()", "description": "d", "files": ["stats.py"], "depends_on": [],
             "done_when": {"command": "python -c \"import stats\""}}])
        s, out = self.run_review([
            findings(summary="nothing serious"),
            plan,
            act("edit_file", path="stats.py", old="return max(values)", new="return min(values)"),   # breaks it
            j(thought="d", final="tidied"),
        ], judge=False)
        self.assertEqual(s["status"], "partial", out)
        self.assertIn("regression in baseline", s["error"])
        self.assertTrue(s["baseline"]["ok"])
        self.assertFalse(s["baseline_after"]["ok"])

    def test_missing_folder(self):
        with contextlib.redirect_stdout(io.StringIO()):
            s = main.run_build("x", model=FakeModel([]), verbose_graph=False, source=str(self.tmp / "nope"))
        self.assertEqual(s["status"], "import_failed")
        self.assertIn("not a folder", s["error"])

    def test_cli(self):
        with mock.patch.object(main, "run_build", return_value={"status": "reviewed", "findings": 0, "review": "r",
                                                               "run_dir": "d", "source": "p"}) as rb, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["review", str(self.project), "--only-review", "--test", TESTS, "check", "it"]), 0)
        kw = rb.call_args.kwargs
        self.assertEqual((kw["source"], kw["only_review"], kw["test_command"]),
                         (str(self.project.resolve()), True, TESTS))
        self.assertEqual(rb.call_args.args[0], "check it")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["review", str(self.tmp / "nope"), "x"]), 1)
        self.assertIn("not a folder", out.getvalue())


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_import_skips_noise_and_respects_limits(self):
        src = copy_sample(self.tmp / "src")
        for junk in [".git/HEAD", "node_modules/x/index.js", "__pycache__/stats.cpython-311.pyc", ".venv/bin/python"]:
            (src / junk).parent.mkdir(parents=True, exist_ok=True)
            (src / junk).write_text("junk")
        (src / "notes.log").write_text("skip me")
        run = self.tmp / "run"
        info = import_project(src, run, ignore=["*.log"])
        self.assertEqual(info["files"], 3)
        self.assertEqual(sorted(p.name for p in (run / "workspace").rglob("*") if p.is_file()),
                         ["README.md", "stats.py", "test_stats.py"])
        self.assertEqual(sorted(p.name for p in (run / "original").rglob("*") if p.is_file()),
                         ["README.md", "stats.py", "test_stats.py"])
        with self.assertRaisesRegex(ReviewError, "more than 2 files"):
            import_project(src, self.tmp / "run2", max_files=2)
        with self.assertRaisesRegex(ReviewError, "larger than 10 bytes"):
            import_project(src, self.tmp / "run3", max_bytes=10)

    def test_detect_tests(self):
        ws = copy_sample(self.tmp / "ws")
        with mock.patch.object(review_mod.importlib.util, "find_spec", return_value=None):
            self.assertEqual(detect_tests(ws), "python -m unittest discover -q")
        with mock.patch.object(review_mod.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(detect_tests(ws), "python -m pytest -q")
        (ws / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js"}}))
        self.assertEqual(detect_tests(ws), "npm test")
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIsNone(detect_tests(empty))

    def test_patch_covers_added_deleted_and_no_newline(self):
        a, b = self.tmp / "a", self.tmp / "b"
        for root in (a, b):
            (root / "pkg").mkdir(parents=True)
        (a / "pkg" / "m.py").write_text("one\ntwo\n")
        (b / "pkg" / "m.py").write_text("one\nTWO\n")
        (a / "old.txt").write_text("bye\n")
        (b / "new.txt").write_text("hi")                                     # no newline at the end
        patch, stats = make_patch(a, b)
        self.assertEqual(stats, {"changed": ["pkg/m.py"], "added": ["new.txt"], "deleted": ["old.txt"]})
        self.assertIn("\\ No newline at end of file", patch)
        target = self.tmp / "target"
        shutil.copytree(a, target)
        (self.tmp / "p.patch").write_text(patch, newline="\n")
        subprocess.run(["git", "apply", str(self.tmp / "p.patch")], cwd=target, check=True)
        self.assertEqual((target / "pkg" / "m.py").read_text(), "one\nTWO\n")
        self.assertEqual((target / "new.txt").read_text(), "hi")
        self.assertFalse((target / "old.txt").exists())
        self.assertEqual(make_patch(a, a), ("", {"changed": [], "added": [], "deleted": []}))

    def test_read_only_tools(self):
        class Src:
            def list_tools(self):
                return [ToolSpec(n, "d", {"type": "object", "properties": {}})
                        for n in ("workspace.read_file", "workspace.write_file", "workspace.edit_file",
                                  "workspace.run_command")]

            def call(self, name, args):
                return f"ran {name}"
        reg = ToolRegistry()
        reg.add_source(Src())
        ro = ReadOnlyTools(reg)
        self.assertNotIn("write_file", ro.describe())
        self.assertIn("workspace.read_file", ro.describe())
        self.assertIn("not available while reviewing", ro.call("write_file", {}))      # short names too
        self.assertIn("not available while reviewing", ro.call("workspace.edit_file", {}))
        self.assertEqual(ro.call("workspace.run_command", {}), "ran workspace.run_command")

    def test_parse_review(self):
        answer = parse_action(findings({"problem": "minor", "severity": "LOW"}, F1, {"problem": ""}))["final"]
        r = parse_review(answer)
        self.assertEqual([f["id"] for f in r["findings"]], ["F1", "F1"])   # the unnamed one is numbered by position
        self.assertEqual([f["severity"] for f in r["findings"]], ["high", "low"])       # most severe first
        self.assertEqual(r["summary"], "average() is off by one")
        plain = parse_review("I looked around and average() seems wrong.")
        self.assertEqual((plain["findings"], plain["structured"]), ([], False))
        self.assertIn("seems wrong", plain["summary"])


if __name__ == "__main__":
    unittest.main()
