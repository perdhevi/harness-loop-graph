"""Chapter B tests — project map, lesson scoring, and lessons carried from one build to the next."""

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
from memory import LessonStore, error_signature, project_map, tokens  # noqa: E402

DAY = 86400


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


def write(path, content):
    return json.dumps({"thought": "w", "action": "workspace.write_file", "args": {"path": path, "content": content}})


def final(note):
    return json.dumps({"thought": "d", "final": note})


def plan(title, *tasks):
    return json.dumps({"title": title, "summary": "s", "tasks": list(tasks)})


def task(tid, done_when, deps=()):
    return {"id": tid, "title": f"task {tid}", "description": "d", "files": [], "depends_on": list(deps),
            "done_when": done_when}


def verdict(v, feedback="", problems=(), met=True):
    return json.dumps({"verdict": v, "requirements": [{"requirement": "works", "met": met, "evidence": "x"}],
                       "problems": list(problems), "feedback": feedback, "summary": v})


BAD = "import json\n\ndef load(path):\n    return json.loads(open(path).read())\n"
GOOD = "import json\n\ndef load(path):\n    try:\n        return json.loads(open(path).read())\n    except FileNotFoundError:\n        return []\n"
CHECK = {"command": "python -c \"import store; assert store.load('missing.json') == []\""}


# ---------------------------------------------------------------- units

class ProjectMapTests(unittest.TestCase):
    def test_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "todo.py").write_text("DB = 1\n\ndef add(items, text):\n    pass\n\nclass Store:\n"
                                        "    def get(self, key): pass\n    def put(self, key, value): pass\n")
            (ws / "broken.py").write_text("def x(:\n")
            (ws / "README.md").write_text("\n# To-do CLI\nmore\n")
            (ws / "__pycache__").mkdir()
            (ws / "__pycache__" / "todo.pyc").write_bytes(b"\x00")
            text = project_map(ws)
            self.assertIn("- todo.py (", text)
            self.assertIn("    def add(items, text)", text)
            self.assertIn("    class Store [get, put]", text)
            self.assertIn("(syntax error line 1)", text)
            self.assertIn('- README.md (18 bytes) — "# To-do CLI"', text)
            self.assertNotIn("pycache", text)
            small = project_map(ws, max_chars=60)
            self.assertIn("(not detailed, map limit reached)", small)


class SignatureTests(unittest.TestCase):
    def test_signature_keeps_error_lines_and_ignores_paths(self):
        a = ("exit code: 1\n--- stdout ---\n--- stderr ---\nTraceback (most recent call last):\n"
             '  File "/tmp/run-1/workspace/store.py", line 4, in load\n'
             "FileNotFoundError: [Errno 2] No such file or directory: 'missing.json'\n")
        b = a.replace("/tmp/run-1/", "/home/x/runs/other/").replace("line 4", "line 40")
        self.assertIn("FileNotFoundError", error_signature(a))
        self.assertEqual(tokens(error_signature(a)), tokens(error_signature(b)))


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_000 * DAY
        self.store = LessonStore(Path(self.tmp.name) / "lessons.json", clock=lambda: self.now)

    def add(self, key, age_days=0, **kw):
        with mock.patch.object(self.store, "clock", lambda: self.now - age_days * DAY):
            return self.store.add("fix", key, check="c", error=key, fix="f", **kw)

    def test_relevance_orders_results(self):
        self.add("ModuleNotFoundError: No module named pytest")
        self.add("FileNotFoundError: No such file or directory missing.json")
        top = self.store.recall("fix", "FileNotFoundError No such file or directory data.json")
        self.assertEqual(top[0]["key"], "FileNotFoundError: No such file or directory missing.json")
        self.assertEqual(len(top), 1)           # the pytest lesson falls under the relative cutoff

    def test_unrelated_store_returns_nothing(self):
        self.add("ModuleNotFoundError: No module named pytest")
        self.assertEqual(self.store.recall("fix", "ZeroDivisionError division by zero"), [])

    def test_decay_halves_per_half_life(self):
        self.add("KeyError missing key name", age_days=0)
        self.add("KeyError missing key name", age_days=30)
        self.add("KeyError missing key name", age_days=300)
        scores = [sc for sc, _, _ in self.store.score_all("fix", "KeyError missing key name")]
        self.assertAlmostEqual(scores[1] / scores[0], 0.5, places=6)          # 30 days = one half-life
        self.assertAlmostEqual(scores[2] / scores[0], 0.5 ** 10, places=9)    # 300 days = ten half-lives
        self.assertGreater(scores[2], 0)                                       # faded, not gone

    def test_track_record(self):
        good = self.add("TypeError unsupported operand")
        bad = self.add("TypeError unsupported operand")
        self.store.mark([good["id"], bad["id"]], shown=True)
        self.store.mark([good["id"]], helped=True)
        self.store.mark([bad["id"]], shown=True)
        ranked = [x["id"] for _, _, x in self.store.score_all("fix", "TypeError unsupported operand")]
        self.assertEqual(ranked, [good["id"], bad["id"]])

    def test_recall_chars_limit(self):
        for _ in range(10):
            self.add("ValueError invalid literal for int", )
        self.store.recall_chars = 400
        self.assertLess(len(self.store.recall("fix", "ValueError invalid literal for int")), 10)


# ---------------------------------------------------------------- across builds

class AcrossBuildsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.set_config()
        for target, value in [("ROOT", self.root), ("CONFIG_PATH", self.root / "config.json")]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)

    def set_config(self, **memory):
        config = json.loads((ROOT / "config.json").read_text())
        config["memory"].update(memory)
        (self.root / "config.json").write_text(json.dumps(config))

    def build(self, *a, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return main.run_build(*a, verbose_graph=False, **kw)

    def lessons(self):
        path = self.root / "memory" / "lessons.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_fix_lesson_carries_to_next_build(self):
        # build 1: the missing-file crash is found by the check and fixed
        self.build("notes app", judge=False, model=FakeModel([
            plan("Notes", task("T1", CHECK)),
            write("store.py", BAD), final("store.load reads json"),
            write("store.py", GOOD), final("load() returns [] when the file does not exist (catch FileNotFoundError)"),
        ]))
        lessons = self.lessons()
        self.assertEqual([x["kind"] for x in lessons], ["fix"])
        self.assertIn("FileNotFoundError", lessons[0]["error"])
        self.assertIn("catch FileNotFoundError", lessons[0]["fix"])

        # build 2: a different project hits the same kind of failure
        model = FakeModel([
            plan("Bookmarks", task("T1", CHECK)),
            write("store.py", BAD), final("done"),
            write("store.py", GOOD), final("handled the missing file"),
        ])
        s = self.build("bookmarks app", judge=False, model=model)
        self.assertEqual(s["status"], "finished")
        fix_prompt = model.calls[3]["messages"][0]["content"]
        self.assertIn("LESSONS FROM PAST BUILDS", fix_prompt)
        self.assertIn("catch FileNotFoundError", fix_prompt)
        first = self.lessons()[0]
        self.assertEqual((first["shown"], first["helped"]), (1, 1))
        self.assertEqual(len(self.lessons()), 2)                    # build 2 added its own lesson

    def test_no_lesson_when_nothing_failed(self):
        self.build("x", judge=False, model=FakeModel([plan("X", task("T1", {"file": "a.py"})),
                                                     write("a.py", "x = 1\n"), final("ok")]))
        self.assertEqual(self.lessons(), [])

    def test_review_lesson_reaches_planner_for_similar_requests_only(self):
        req = "Build a Python CLI to-do app with add, list and done"
        self.build(req, model=FakeModel([
            plan("Todo", task("T1", {"file": "todo.py"})), write("todo.py", "x = 1\n"), final("todo"),
            verdict("revise", feedback="Add the done command", problems=["done is missing"], met=False),
            json.dumps({"changes": "c", "tasks": [task("R1-1", {"file": "done.py"}, ["T1"])]}),
            write("done.py", "y = 1\n"), final("done cmd"),
            verdict("accept"),
        ]))
        self.assertEqual([x["kind"] for x in self.lessons()], ["review"])

        similar = FakeModel([plan("Todo", task("T1", {"file": "todo.py"})), write("todo.py", "x = 1\n"),
                             final("todo"), verdict("accept")])
        s = self.build("Python to-do list CLI: add, list, done commands", model=similar)
        self.assertEqual(s["status"], "accepted")
        planner_msg = similar.calls[0]["messages"][0]["content"]
        self.assertIn("LESSONS FROM PAST BUILDS", planner_msg)
        self.assertIn("done is missing", planner_msg)
        review = self.lessons()[0]
        self.assertEqual((review["shown"], review["helped"]), (1, 1))

        other = FakeModel([plan("Weather", task("T1", {"file": "w.py"})), write("w.py", "z = 1\n"),
                           final("w"), verdict("accept")])
        self.build("Fetch the weather forecast for Jakarta", model=other)
        self.assertNotIn("LESSONS FROM PAST BUILDS", other.calls[0]["messages"][0]["content"])

    def test_later_task_sees_names_from_earlier_task(self):
        model = FakeModel([
            plan("Calc", task("T1", {"file": "calc.py"}), task("T2", {"file": "cli.py"}, ["T1"])),
            write("calc.py", "def add(a, b):\n    return a + b\n"), final("calc"),
            write("cli.py", "from calc import add\n"), final("cli"),
        ])
        self.build("calc", judge=False, model=model)
        t2_prompt = model.calls[3]["messages"][0]["content"]
        self.assertIn("    def add(a, b)", t2_prompt)

    def test_memory_off(self):
        self.set_config(enabled=False)
        model = FakeModel([
            plan("Calc", task("T1", {"file": "calc.py"}), task("T2", {"file": "cli.py"}, ["T1"])),
            write("calc.py", "def add(a, b):\n    return a + b\n"), final("calc"),
            write("cli.py", "x = 1\n"), final("cli"),
        ])
        self.build("calc", judge=False, model=model)
        self.assertNotIn("def add", model.calls[3]["messages"][0]["content"])
        self.assertFalse((self.root / "memory").exists())

    def test_memory_command(self):
        store = main.make_store(json.loads((self.root / "config.json").read_text()))
        store.add("fix", "ModuleNotFoundError No module named pytest", check="python -m pytest -q",
                  error="x", fix="installed nothing; used unittest instead")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main.main(["memory", "--query", "No module named pytest", "--kind", "fix"])
        self.assertIn("→", out.getvalue())
        self.assertIn("python -m pytest -q", out.getvalue())


if __name__ == "__main__":
    unittest.main()
