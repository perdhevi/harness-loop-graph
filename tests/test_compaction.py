"""Chapter D tests — compaction of a task loop's history, from outside the loop."""

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
from compaction import CompactingModel, digest  # noqa: E402
from context import estimate_tokens  # noqa: E402
from trace_view import load_events, render  # noqa: E402


def act(tool, thought="t", **args):
    return json.dumps({"thought": thought, "action": tool, "args": args})


def history(n_steps: int, obs_chars: int = 600) -> list[dict]:
    msgs = [{"role": "user", "content": "=== YOUR TASK: T1 — build it ===\n" + "goal " * 50}]
    for i in range(1, n_steps + 1):
        msgs.append({"role": "assistant", "content": act("workspace.run_command", thought=f"try {i}",
                                                          command=f"python step{i}.py")})
        code = 1 if i == 2 else 0
        msgs.append({"role": "user", "content": f"Observation: exit code: {code}\n--- stdout ---\n"
                     + ("E" if code else "o") * obs_chars + ("\nAssertionError: step 2 broke" if code else "")})
    return msgs


class Recorder:
    def __init__(self, reply="ok"):
        self.sent = []
        self.reply = reply

    def complete(self, system, messages):
        self.sent.append([dict(m) for m in messages])
        return self.reply


def size(msgs):
    return sum(estimate_tokens(m["content"]) for m in msgs)


class DigestTests(unittest.TestCase):
    def test_digest(self):
        steps = [
            (act("workspace.write_file", thought="start", path="app.py", content="x"), "Observation: created app.py"),
            (act("workspace.run_command", thought="run tests", command="python -m pytest -q"),
             "exit code: 1\n--- stdout ---\nFAILED test_app.py::test_x - assert 1 == 2\n"),
            (act("workspace.read_file", path="old.py"), "Error: workspace.read_file failed: McpToolError: no such file"),
            ("I think I'm done", "Your last reply was not a valid action"),
            (act("workspace.edit_file", path="app.py", old="1", new="2"), "edited app.py"),
            (act("workspace.run_command", thought="again", command="python -m pytest -q"), "exit code: 0\n"),
        ]
        d = digest(steps, 3)
        self.assertIn("Steps 3–8 were compacted", d)
        self.assertIn("run_command `python -m pytest -q` (exit 1) ✗", d)
        self.assertIn("FAILED test_app.py::test_x - assert 1 == 2", d)
        self.assertIn("read_file old.py → Error: workspace.read_file failed", d)
        self.assertIn("(step 6: reply was not a valid action)", d)
        self.assertIn("Files written or edited: app.py", d)
        self.assertIn("`python -m pytest -q` → exit 0", d)        # last result wins
        self.assertIn('"again"', d)


class CompactingModelTests(unittest.TestCase):
    def wrapper(self, rec, **kw):
        events = []
        m = CompactingModel(rec, window_tokens=3000, note="=== EARLIER ===\n{summary}", on_compact=events.append, **kw)
        return m, events

    def test_below_pressure_sends_everything(self):
        rec = Recorder()
        m, events = self.wrapper(rec)
        msgs = history(2, obs_chars=100)
        m.complete("sys", msgs)
        self.assertEqual(rec.sent[0], msgs)
        self.assertEqual(events, [])

    def test_compacts_keeps_goal_recent_steps_and_alternation(self):
        rec = Recorder()
        m, events = self.wrapper(rec)
        msgs = history(12)
        m.complete("sys", msgs)
        view = rec.sent[0]
        self.assertEqual(len(events), 1)
        self.assertTrue(view[0]["content"].startswith("=== YOUR TASK: T1"))     # goal kept
        self.assertIn("=== EARLIER ===\nSteps 1–", view[0]["content"])
        self.assertIn("AssertionError: step 2 broke", view[0]["content"])        # what failed is kept
        self.assertEqual(view[-4:], msgs[-4:])                                   # 2 recent steps verbatim
        roles = [x["role"] for x in view]
        self.assertEqual(roles, ["user"] + ["assistant", "user"] * ((len(view) - 1) // 2))
        self.assertLessEqual(size(view), 0.75 * m.call_budget)
        self.assertLess(events[0]["tokens_after"], events[0]["tokens_before"])
        self.assertLessEqual(events[0]["pressure_after"], 0.55)

    def test_incremental_and_hysteresis(self):
        rec = Recorder()
        m, events = self.wrapper(rec)
        msgs = history(12)
        m.complete("sys", msgs)
        upto = m.upto
        msgs = history(13)                                       # one more step: still under the trigger
        m.complete("sys", msgs)
        self.assertEqual((len(events), m.upto), (1, upto))
        msgs = history(22)                                       # enough growth to trigger again
        m.complete("sys", msgs)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["from_step"], events[0]["to_step"] + 1)       # only newly aged steps
        # extractive mode: one bounded digest over all compacted steps, not a growing chain
        self.assertEqual(m.summary.count("were compacted"), 1)
        self.assertIn(f"Steps 1–{events[1]['to_step']} were compacted", m.summary)

    def test_digest_stays_bounded(self):
        steps = [(act("workspace.run_command", thought=f"t{i}", command=f"python s{i}.py"),
                  f"exit code: {i % 2}\n--- stdout ---\nAssertionError: case {i}") for i in range(300)]
        d = digest(steps, 1)
        self.assertLess(len(d), 3000)
        self.assertIn("(288 earlier actions not listed)", d)
        self.assertIn("python s299.py", d)                                       # most recent kept

    def test_nothing_to_compact_yet(self):
        rec = Recorder()
        m, events = self.wrapper(rec, keep_recent_steps=2)
        msgs = history(2, obs_chars=12000)                      # huge, but only 2 steps: both are "recent"
        m.complete("sys", msgs)
        self.assertEqual(events, [])
        self.assertEqual(rec.sent[0], msgs)

    def test_model_mode_and_fallback(self):
        rec = Recorder()

        class Summ:
            def __init__(self, reply=None, fail=False):
                self.reply, self.fail, self.prompts = reply, fail, []

            def complete(self, system, messages):
                self.prompts.append(messages[0]["content"])
                if self.fail:
                    raise ConnectionError("down")
                return self.reply

        s = Summ("Tried step1..step9; step 2 failed with AssertionError.")
        m, events = self.wrapper(rec, mode="model", summarizer=s, summarizer_system="summarise")
        m.complete("sys", history(12))
        self.assertEqual(events[0]["mode"], "model")
        self.assertIn("Tried step1..step9", rec.sent[0][0]["content"])
        self.assertIn("Extracted facts:", s.prompts[0])
        m2, events2 = self.wrapper(Recorder(), mode="model", summarizer=Summ(fail=True))
        m2.complete("sys", history(12))
        self.assertEqual(events2[0]["mode"], "extractive (model summary failed)")


# ---------------------------------------------------------------- a long task in a build

class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


class LongTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, enabled=True):
        config = json.loads((ROOT / "config.json").read_text())
        config["context"]["window_tokens"] = 3000
        config["build"]["task_max_iterations"] = 40
        config["compaction"]["enabled"] = enabled
        (self.root / "config.json").write_text(json.dumps(config))
        plan = json.dumps({"title": "Long", "summary": "s", "tasks": [
            {"id": "T1", "title": "long task", "description": "needs many steps", "files": ["done.txt"],
             "depends_on": [], "done_when": {"file": "done.txt"}}]})
        steps = [act("workspace.run_command", thought=f"probe {i}",
                     command=("python -c \"import sys; print('E' * 500); sys.exit(1)\"" if i == 3 else
                              f"python -c \"print('{chr(97 + i % 26)}' * 600)\""))
                 for i in range(1, 33)]
        model = FakeModel([plan, *steps, act("workspace.write_file", path="done.txt", content="ok\n"),
                           json.dumps({"thought": "d", "final": "done after 33 steps"})])
        with mock.patch.object(main, "ROOT", self.root), mock.patch.object(main, "CONFIG_PATH", self.root / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            s = main.run_build("long", model=model, verbose_graph=False, judge=False)
        return s, model

    def test_34_step_task_finishes_within_budget(self):
        s, model = self.build()
        self.assertEqual(s["status"], "finished")
        self.assertEqual(s["tasks"][0]["steps"], 34)
        task_calls = model.calls[1:]
        call_budget = int(3000 * 0.85)
        for c in task_calls:
            self.assertIn("YOUR TASK: T1", c["messages"][0]["content"])        # goal in every call
            self.assertLessEqual(sum(estimate_tokens(m["content"]) for m in c["messages"]) +
                                 estimate_tokens(c["system"]), call_budget)
        late = task_calls[-1]["messages"][0]["content"]
        self.assertIn("EARLIER STEPS IN THIS TASK", late)
        self.assertIn("exit 1", late)                                         # the early failure is remembered
        events = load_events(s["run_dir"])
        comps = [e for e in events if e["type"] == "compaction"]
        self.assertGreaterEqual(len(comps), 3)
        # no thrashing: in a 3,000-token window the 0.5 target is out of reach, yet compaction
        # still waits for real growth instead of firing on every step
        self.assertLessEqual(len(comps), 12)
        self.assertTrue(any(not e["target_reached"] for e in comps))
        self.assertTrue(all(e["next_trigger"] <= 0.95 for e in comps))
        self.assertEqual(comps[0]["from_step"], 1)
        for a, b in zip(comps, comps[1:]):
            self.assertEqual(b["from_step"], a["to_step"] + 1)                 # no step summarised twice
        text = render(events, steps=True)
        self.assertIn("compacted×", text)
        self.assertIn("compact  steps 1–", text)
        full = [x for x in (Path(s["run_dir"]) / "transcript.jsonl").read_text().splitlines() if '"n"' in x]
        self.assertEqual(len(full), 34)                                       # the full record is untouched

    def test_off_means_history_grows_past_budget(self):
        s, model = self.build(enabled=False)
        biggest = max(sum(estimate_tokens(m["content"]) for m in c["messages"]) for c in model.calls[1:])
        self.assertGreater(biggest, int(3000 * 0.85))
        self.assertNotIn("EARLIER STEPS", model.calls[-1]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
