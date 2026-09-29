"""Chapter F tests — run index, acceptance scripts, suite replay, baselines and regression detection."""

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
from bench import SuiteError, compare, load_suite, render_compare, run_suite  # noqa: E402
from runtime import load_json  # noqa: E402

REF = Path(__file__).resolve().parent / "bench_reference"
CORE = ROOT / "benchmarks" / "core.json"


class AcceptanceScriptTests(unittest.TestCase):
    """The acceptance scripts are the ground truth, so they get tested too."""

    def run_script(self, case_id, files_from=None):
        suite = load_suite(CORE)
        case = next(c for c in suite["cases"] if c["id"] == case_id)
        with tempfile.TemporaryDirectory() as tmp:
            if files_from:
                for f in files_from.iterdir():
                    shutil.copy(f, tmp)
            shutil.copy(CORE.parent / case["acceptance"]["script"], Path(tmp) / "_accept.py")
            return subprocess.run([sys.executable, "_accept.py"], cwd=tmp, capture_output=True, text=True, timeout=60)

    def test_reference_solutions_pass(self):
        for case in load_suite(CORE)["cases"]:
            with self.subTest(case=case["id"]):
                r = self.run_script(case["id"], REF / case["id"])
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_workspace_fails(self):
        for case in load_suite(CORE)["cases"]:
            with self.subTest(case=case["id"]):
                self.assertNotEqual(self.run_script(case["id"]).returncode, 0)

    def test_suite_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text(json.dumps({"name": "x", "cases": [{"id": "a", "request": "r",
                                                             "acceptance": {"script": "nope.py"}}]}))
            with self.assertRaisesRegex(SuiteError, "acceptance script not found"):
                load_suite(p)


# ---------------------------------------------------------------- scripted builds

class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        return self.replies.pop(0)


def act(tool, **args):
    return json.dumps({"thought": "a", "action": tool, "args": args})


def final(note="done"):
    return json.dumps({"thought": "d", "final": note})


def one_task_plan(fname, check):
    return json.dumps({"title": "t", "summary": "s", "tasks": [
        {"id": "T1", "title": "write it", "description": "d", "files": [fname], "depends_on": [],
         "done_when": {"command": check}}]})


GREET_OK = (REF / "greet" / "greet.py").read_text()
STATS_OK = (REF / "word-stats" / "stats.py").read_text()
STATS_BAD = "import sys\ntext = open(sys.argv[1]).read()\nprint(f'{len(text.split())} words')\n"


def script_for(case, broken=False, extra_steps=0):
    if case == "greet":
        return [one_task_plan("greet.py", "python greet.py Ana"),
                *[act("workspace.list_dir")] * extra_steps,
                act("workspace.write_file", path="greet.py", content=GREET_OK), final("greet.py")]
    return [one_task_plan("stats.py", "python -c \"import stats\" x"),
            act("workspace.write_file", path="stats.py", content=STATS_BAD if broken else STATS_OK),
            act("workspace.write_file", path="x", content="a b\n"),
            final("stats.py")]


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # a small suite with two real cases from core
        bench_dir = self.root / "bench"
        (bench_dir / "accept").mkdir(parents=True)
        core = json.loads(CORE.read_text())
        for f in ("greet.py", "word_stats.py"):
            shutil.copy(CORE.parent / "accept" / f, bench_dir / "accept" / f)
        small = {"name": "mini", "judge": False, "cases": [c for c in core["cases"] if c["id"] in ("greet", "word-stats")]}
        self.suite_path = bench_dir / "mini.json"
        self.suite_path.write_text(json.dumps(small))
        self.config = load_json(ROOT / "config.json")
        for target, value in [("ROOT", self.root)]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def build_fn(self, scripts):
        """build_fn for run_suite: picks the scripted model by case id (from the tags)."""
        def fn(request, **kw):
            replies = scripts[kw["tags"]["case"]]
            if isinstance(replies, Exception):
                raise replies
            with contextlib.redirect_stdout(io.StringIO()):
                return main.run_build(request, model=FakeModel(replies), config=self.config, quiet=True,
                                      verbose_graph=False, **kw)
        return fn

    def bench(self, scripts, **kw):
        suite = load_suite(self.suite_path)
        return run_suite(suite, build_fn=self.build_fn(scripts), config=self.config, label="test",
                         say=lambda m: None, **kw)

    def test_suite_run_passes_and_writes_results(self):
        r = self.bench({"greet": script_for("greet"), "word-stats": script_for("word-stats")})
        self.assertEqual(r["overall"]["pass_rate"], 1.0)
        self.assertEqual([x["case"] for x in r["rows"]], ["greet", "word-stats"])
        self.assertTrue(all(x["passed"] for x in r["rows"]))
        self.assertTrue(Path(r["_path"]).exists())
        self.assertEqual(r["cases"]["greet"]["steps"], 2)
        # acceptance ran in a copy: the delivered workspace has no _accept.py
        run_dir = self.root / "runs" / r["rows"][0]["run_id"]
        self.assertFalse((run_dir / "workspace" / "_accept.py").exists())
        self.assertTrue((run_dir / "acceptance" / "_accept.py").exists())

    def test_regression_is_flagged(self):
        base = self.bench({"greet": script_for("greet"), "word-stats": script_for("word-stats")})
        now = self.bench({"greet": script_for("greet"), "word-stats": script_for("word-stats", broken=True)})
        self.assertFalse(now["rows"][1]["passed"])
        self.assertIn("expected '6 words, 4 lines'", now["rows"][1]["acceptance"]["output"])
        report = compare(now, base)
        self.assertFalse(report["ok"])
        self.assertIn("word-stats: pass rate 100% → 0%", report["regressions"])
        self.assertIn("overall: pass rate 100% → 50%", report["regressions"])
        self.assertIn("result: REGRESSION", render_compare(report, "mini-test.json"))

    def test_cost_changes_are_warnings_not_failures(self):
        base = self.bench({"greet": script_for("greet"), "word-stats": script_for("word-stats")}, only=["greet"])
        now = self.bench({"greet": script_for("greet", extra_steps=3), "word-stats": []}, only=["greet"])
        report = compare(now, base)
        self.assertTrue(report["ok"])
        self.assertTrue(any(w.startswith("greet: costlier — steps 2 → 5") for w in report["warnings"]))
        back = compare(base, now)
        self.assertTrue(any("steps 5 → 2" in i for i in back["improvements"]))

    def test_repeat_gives_rates_and_tolerance(self):
        good, bad = script_for("word-stats"), script_for("word-stats", broken=True)
        calls = iter([good, bad])

        def fn(request, **kw):
            with contextlib.redirect_stdout(io.StringIO()):
                return main.run_build(request, model=FakeModel(next(calls)), config=self.config, quiet=True,
                                      verbose_graph=False, **kw)
        r = run_suite(load_suite(self.suite_path), build_fn=fn, config=self.config, label="rep", repeat=2,
                      only=["word-stats"], say=lambda m: None)
        self.assertEqual(r["cases"]["word-stats"]["pass_rate"], 0.5)
        base = {"cases": {"word-stats": {**r["cases"]["word-stats"], "pass_rate": 1.0}},
                "overall": {"pass_rate": 1.0}}
        self.assertFalse(compare(r, base)["ok"])
        self.assertTrue(compare(r, base, tolerance=0.5)["ok"])

    def test_crashing_build_is_a_failed_case(self):
        r = self.bench({"greet": RuntimeError("model unreachable"), "word-stats": script_for("word-stats")})
        self.assertEqual(r["rows"][0]["status"], "error")
        self.assertFalse(r["rows"][0]["passed"])
        self.assertTrue(r["rows"][1]["passed"])

    def test_run_index(self):
        self.bench({"greet": script_for("greet"), "word-stats": script_for("word-stats")})
        rows = [json.loads(x) for x in (self.root / "runs" / "index.jsonl").read_text().splitlines()]
        self.assertEqual([r["bench"]["case"] for r in rows], ["greet", "word-stats"])
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["model_calls"], {"planner": 1, "task": 2})
        self.assertGreater(rows[0]["tokens_in"], 0)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(main, "CONFIG_PATH", ROOT / "config.json"):
            main.main(["runs"])
        self.assertIn("mini/greet", out.getvalue())

    def test_cli_exit_code_and_overrides(self):
        seen = []

        def fake_run_build(request, **kw):
            seen.append(kw["config"]["providers"][kw["config"]["provider"]]["model"])
            case = kw["tags"]["case"]
            broken = case == "word-stats" and len(seen) > 2
            with contextlib.redirect_stdout(io.StringIO()):
                return main.__dict__["_real_run_build"](request, model=FakeModel(script_for(case, broken=broken)),
                                                       **{k: v for k, v in kw.items() if k != "ask"})
        main._real_run_build = main.run_build
        self.addCleanup(lambda: delattr(main, "_real_run_build"))
        args = [str(self.suite_path), "--label", "cli", "--provider", "anthropic", "--model", "claude-test"]
        with mock.patch.object(main, "run_build", fake_run_build), mock.patch.object(main, "CONFIG_PATH", ROOT / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            first = main.main(["bench", *args, "--save-baseline"])
            second = main.main(["bench", *args])
        self.assertEqual((first, second), (0, 1))
        self.assertEqual(set(seen), {"claude-test"})
        self.assertIn("baseline : saved", out.getvalue())
        self.assertIn("✗ word-stats: pass rate 100% → 0%", out.getvalue())
        base = json.loads((self.suite_path.parent / "baselines" / "mini-cli.json").read_text())
        self.assertEqual((base["provider"], base["model"]), ("anthropic", "claude-test"))


if __name__ == "__main__":
    unittest.main()
