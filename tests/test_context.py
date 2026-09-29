"""Chapter C tests — budget allocation, relevant files, the output limiter, and prompts under budget."""

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
from context import OutputLimiter, Section, allocate, rank_files, relevant_files, shrink  # noqa: E402
from trace_view import load_events, render  # noqa: E402


# ---------------------------------------------------------------- units

class AllocateTests(unittest.TestCase):
    def test_small_sections_are_not_cut(self):
        a = allocate([Section("a", "x" * 100, 0.5), Section("b", "y" * 100, 0.5)], 1000)
        self.assertEqual((a.texts["a"], a.texts["b"]), ("x" * 100, "y" * 100))

    def test_surplus_flows_to_sections_that_need_it(self):
        secs = [Section("small", "s" * 100, 0.5), Section("big", "b" * 5000, 0.5)]
        a = allocate(secs, 1000)
        self.assertEqual(len(a.texts["small"]), 100)            # needed less than its 500 share
        self.assertEqual(a.given["big"], 900)                    # got the other 400
        self.assertLessEqual(len(a.texts["big"]), 900)

    def test_proportional_cut_and_total_within_budget(self):
        secs = [Section("a", "a" * 10_000, 0.6), Section("b", "b" * 10_000, 0.2), Section("c", "c" * 10_000, 0.2)]
        a = allocate(secs, 3000)
        self.assertEqual((a.given["a"], a.given["b"], a.given["c"]), (1800, 600, 600))
        self.assertLessEqual(sum(len(t) for t in a.texts.values()), 3000)
        self.assertIn("chars cut from a", a.texts["a"])

    def test_required_is_never_cut(self):
        a = allocate([Section("task", "T" * 800, required=True), Section("spec", "s" * 800, 1.0)], 1000)
        self.assertEqual(a.texts["task"], "T" * 800)
        self.assertLessEqual(len(a.texts["spec"]), 200)

    def test_shrink_strategies(self):
        text = "HEAD" + "." * 1000 + "TAIL"
        for strategy, keeps, drops in [("head", "HEAD", "TAIL"), ("tail", "TAIL", "HEAD")]:
            r = shrink(text, 100, strategy, "x")
            self.assertLessEqual(len(r), 100)
            self.assertIn(keeps, r)
            self.assertNotIn(drops, r)
        r = shrink(text, 100, "middle")
        self.assertTrue(r.startswith("HEAD") and r.endswith("TAIL"))


class RelevantFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "todo.py").write_text("def add(items, text):\n    items.append(text)\n")
        (self.ws / "store.py").write_text("import json\n\ndef load(path):\n    return json.load(open(path))\n")
        (self.ws / "colors.css").write_text("body { color: red; }\n")

    def test_listed_and_mentioned_files_rank_first(self):
        ranked = rank_files(self.ws, listed=["todo.py"], query="add items",
                            mentions='File "/tmp/x/workspace/store.py", line 4, in load')
        self.assertEqual([p for _, p, _ in ranked][:2], ["todo.py", "store.py"])
        self.assertNotIn("colors.css", [p for _, p, _ in ranked])       # no reason to show it

    def test_budget_and_per_file_cap(self):
        (self.ws / "big.py").write_text("def big():\n" + "    x = 1\n" * 2000)
        ranked = rank_files(self.ws, listed=["big.py", "todo.py"], query="")
        text, shown = relevant_files(ranked, 1200)
        self.assertLessEqual(len(text), 1300)
        self.assertIn("big.py", shown)
        self.assertIn("chars cut from big.py", text)                       # capped at half the section


class OutputLimiterTests(unittest.TestCase):
    class Echo:
        def list_tools(self):
            return []

        def describe(self):
            return "- echo(text)"

        def call(self, name, args):
            return args["text"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "outputs"
        self.lim = OutputLimiter(self.Echo(), self.out, max_chars=1000)

    def test_short_passes_through(self):
        self.assertEqual(self.lim.call("echo", {"text": "hi"}), "hi")
        self.assertFalse(self.out.exists())

    def test_long_output_is_saved_and_readable(self):
        text = "START" + "".join(f"line {i}\n" for i in range(2000)) + "END"
        seen = self.lim.call("echo", {"text": text})
        self.assertLessEqual(len(seen), 1000 + 200)
        self.assertTrue(seen.startswith("START") and seen.endswith("END"))
        self.assertIn("[output 0001 cut:", seen)
        self.assertEqual((self.out / "0001.txt").read_text(), text)
        part = self.lim.call("harness.read_output", {"id": "0001", "offset": 5, "limit": 50})
        self.assertIn("chars 5–55", part)
        self.assertIn("line 0", part)
        self.assertIn("Next: offset 55", part)
        tail = self.lim.call("harness.read_output", {"id": "0001", "offset": len(text) - 10})
        self.assertIn("(end of output)", tail)

    def test_read_output_errors_and_tool_listing(self):
        self.assertIn("no saved output with id '9999'", self.lim.call("harness.read_output", {"id": "9999"}))
        self.assertIn("unknown argument", self.lim.call("harness.read_output", {"id": "1", "path": "x"}))
        self.lim.call("echo", {"text": "z" * 3000})
        self.assertIn("past the end", self.lim.call("harness.read_output", {"id": "0001", "offset": 5000}))
        self.assertIn("harness.read_output(id: string", self.lim.describe())
        self.assertIn("harness.read_output", [t.name for t in self.lim.list_tools()])

    def test_numbering_continues_after_resume(self):
        self.lim.call("echo", {"text": "x" * 2000})
        again = OutputLimiter(self.Echo(), self.out, max_chars=1000)
        self.assertIn("[output 0002 cut:", again.call("echo", {"text": "y" * 2000}))


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


def plan(*tasks):
    return json.dumps({"title": "Big", "summary": "s", "tasks": list(tasks)})


def task(tid, done_when, files=(), deps=()):
    return {"id": tid, "title": f"task {tid}", "description": "work on the project", "files": list(files),
            "depends_on": list(deps), "done_when": done_when}


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.configure()
        for target, value in [("ROOT", self.root), ("CONFIG_PATH", self.root / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def configure(self, **context):
        config = json.loads((ROOT / "config.json").read_text())
        config["context"].update({"window_tokens": 4000, **context})    # 0.4 × 4000 × 4 = 6400 chars
        config["build"]["task_max_iterations"] = 40                       # T1 writes 25 files
        (self.root / "config.json").write_text(json.dumps(config))

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, judge=False, **kw)

    def test_prompt_stays_under_budget_with_25_files(self):
        writes = [act("workspace.write_file", path=f"pkg/mod{i:02d}.py",
                      content=f"def f{i}(x):\n    return x\n" + "# padding line\n" * 150) for i in range(25)]
        wanted = ["pkg/mod03.py", "pkg/mod05.py", "pkg/mod07.py", "pkg/mod09.py", "pkg/mod11.py", "main.py"]
        model = FakeModel([plan(task("T1", {"file": "pkg/mod00.py"}),
                                task("T2", {"file": "main.py"}, files=wanted, deps=["T1"])),
                           *writes, final("25 modules"),
                           act("workspace.write_file", path="main.py", content="import pkg\n"), final("main")])
        s = self.build("big project", model=model)
        self.assertEqual(s["status"], "finished")
        t2_first = model.calls[27]["messages"][0]["content"]
        self.assertIn("YOUR TASK: T2", t2_first)
        self.assertLessEqual(len(t2_first), 6400)                 # 5 × 2.3 KB of wanted files did not fit
        self.assertIn("----- pkg/mod03.py -----", t2_first)       # listed files are shown, cut to fit
        self.assertIn("def f3(x)", t2_first)

        events = [e for e in load_events(s["run_dir"]) if e["type"] == "context"]
        t2 = events[-1]
        self.assertLessEqual(t2["used_tokens"], t2["budget_tokens"] + 5)
        self.assertGreater(t2["sections"]["relevant_files"]["cut"], 0)
        self.assertEqual(t2["sections"]["task"]["cut"], 0)
        self.assertEqual(t2["sections"]["plan_status"]["cut"], 0)       # small sections keep everything
        self.assertTrue(set(t2["files_shown"]) <= set(wanted))
        self.assertIn("context  1517 tok budget", render(load_events(s["run_dir"]), steps=True))

    def test_single_relevant_file_can_use_the_whole_section(self):
        ranked = [(1.0, "only.py", "def only():\n" + "    pass\n" * 300)]
        text, shown = relevant_files(ranked, 5000)
        self.assertNotIn("chars cut", text)
        self.assertEqual(shown, ["only.py"])

    def test_long_observation_is_cut_and_readable(self):
        noisy = "import sys\nfor i in range(3000):\n    print('row', i)\nprint('THE END')\n"
        model = FakeModel([plan(task("T1", {"file": "noisy.py"})),
                           act("workspace.write_file", path="noisy.py", content=noisy),
                           act("workspace.run_command", command="python noisy.py"),
                           act("harness.read_output", id="0001", offset=2000, limit=300),
                           final()])
        s = self.build("noisy", model=model)
        run_dir = Path(s["run_dir"])
        steps = [json.loads(x) for x in (run_dir / "transcript.jsonl").read_text().splitlines() if '"n"' in x]
        cut = steps[1]["observation"]
        self.assertLessEqual(len(cut), 4000 + 200)
        self.assertIn("[output 0001 cut:", cut)
        self.assertIn("THE END", cut)                                    # the tail survives
        self.assertIn("row 2999", (run_dir / "outputs" / "0001.txt").read_text())
        self.assertIn("[output 0001: chars 2000–2300", steps[2]["observation"])

    def test_fix_prompt_keeps_the_end_of_long_failures(self):
        failing = "for i in range(4000):\n    print('noise', i)\nassert 1 == 2, 'THE REAL FAILURE'\n"
        model = FakeModel([plan(task("T1", {"command": "python check.py"})),
                           act("workspace.write_file", path="check.py", content=failing), final("done"),
                           act("workspace.write_file", path="check.py", content="print('ok')\n"), final("fixed")])
        s = self.build("x", model=model)
        self.assertEqual(s["status"], "finished")
        fix_first = model.calls[3]["messages"][0]["content"]
        self.assertIn("THE REAL FAILURE", fix_first)
        self.assertLessEqual(len(fix_first), 6400)

    def test_context_off(self):
        self.configure(enabled=False)
        model = FakeModel([plan(task("T1", {"file": "noisy.py"}, files=["noisy.py"])),
                           act("workspace.write_file", path="noisy.py",
                               content="for i in range(3000):\n    print('row', i)\n"),
                           act("workspace.run_command", command="python noisy.py"), final()])
        s = self.build("noisy", model=model)
        steps = [json.loads(x) for x in (Path(s["run_dir"]) / "transcript.jsonl").read_text().splitlines()
                 if '"n"' in x]
        self.assertNotIn("[output", steps[1]["observation"])
        self.assertNotIn("RELEVANT FILES", model.calls[1]["messages"][0]["content"])
        self.assertNotIn("harness.read_output", model.calls[1]["system"])


if __name__ == "__main__":
    unittest.main()
