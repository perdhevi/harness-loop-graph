"""Stage 2/3 tests — the ReAct loop against a scripted fake model, with tools from the registry.

Run:  python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loop import ParseError, parse_action, run_loop  # noqa: E402
from tools.builtin import calculate  # noqa: E402
from tools.local import LocalToolSource  # noqa: E402
from tools.registry import ToolRegistry, ToolSpec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def local_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.add_source(LocalToolSource(ROOT / "tools.json"))
    return r


class DictSource:
    """A tiny in-memory tool source for tests."""

    def __init__(self, fns: dict):
        self.fns = fns

    def list_tools(self):
        return [ToolSpec(n, "test tool", {"type": "object", "properties": {}}) for n in self.fns]

    def call(self, name, args):
        return self.fns[name](**args)

SYSTEM = "Tools:\n{tools}\nReply with one JSON object."


class FakeModel:
    """Returns scripted replies in order and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        return self.replies.pop(0)


def j(**kw):
    return json.dumps(kw)


class ParseActionTests(unittest.TestCase):
    def test_tool_action(self):
        a = parse_action(j(thought="t", action="calculate", args={"expression": "1+1"}))
        self.assertEqual(a, {"thought": "t", "action": "calculate", "args": {"expression": "1+1"}})

    def test_final(self):
        self.assertEqual(parse_action(j(thought="done", final="42"))["final"], "42")

    def test_non_string_final_is_serialised(self):
        self.assertEqual(parse_action(j(final={"a": 1}))["final"], '{"a": 1}')

    def test_strips_think_and_fences(self):
        text = '<think>let me think {not json}</think>\n```json\n{"action": "calculate", "args": {"expression": "2*3"}}\n```'
        self.assertEqual(parse_action(text)["action"], "calculate")

    def test_skips_leading_non_json_braces(self):
        text = 'Sure {here} is it: {"final": "ok"}'
        self.assertEqual(parse_action(text)["final"], "ok")

    def test_missing_args_defaults_to_empty(self):
        self.assertEqual(parse_action(j(action="x"))["args"], {})

    def test_errors(self):
        for bad in ["no json here", j(thought="only thought"), j(action=""), j(action="x", args=[1])]:
            with self.subTest(bad=bad), self.assertRaises(ParseError):
                parse_action(bad)


class LoopTests(unittest.TestCase):
    def test_tool_then_final(self):
        model = FakeModel([
            j(thought="need math", action="calculate", args={"expression": "17 * 23"}),
            j(thought="done", final="391"),
        ])
        seen = []
        r = run_loop(model, SYSTEM, "what is 17*23?", local_registry(), on_step=seen.append)
        self.assertEqual((r.status, r.answer), ("final", "391"))
        self.assertEqual(r.steps[0].observation, "391")
        self.assertEqual([s.n for s in seen], [1, 2])
        # the model saw the observation on its second call
        self.assertEqual(model.calls[1]["messages"][-1], {"role": "user", "content": "Observation: 391"})
        # tools were rendered into the system prompt
        self.assertIn("- calculate(expression: string)", model.calls[0]["system"])

    def test_messages_alternate_roles(self):
        model = FakeModel([
            "garbage",
            j(action="calculate", args={"expression": "1+1"}),
            j(final="2"),
        ])
        r = run_loop(model, SYSTEM, "q", local_registry())
        roles = [m["role"] for m in r.messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user", "assistant"])

    def test_malformed_reply_becomes_observation(self):
        model = FakeModel(["I think the answer is 4", j(final="4")])
        r = run_loop(model, SYSTEM, "2+2?", local_registry(), format_reminder="BAD FORMAT ({error})")
        self.assertEqual(r.status, "final")
        self.assertEqual(r.steps[0].error, "no JSON object found")
        self.assertEqual(r.steps[0].observation, "BAD FORMAT (no JSON object found)")

    def test_unknown_tool(self):
        model = FakeModel([j(action="search_web", args={"q": "x"}), j(final="ok")])
        r = run_loop(model, SYSTEM, "q", local_registry())
        self.assertIn("unknown tool 'search_web'", r.steps[0].observation)
        self.assertIn("calculate", r.steps[0].observation)

    def test_bad_args_and_tool_exception(self):
        model = FakeModel([
            j(action="calculate", args={"expr": "1+1"}),              # wrong arg name
            j(action="calculate", args={"expression": "__import__('os')"}),  # rejected by tool
            j(final="gave up"),
        ])
        r = run_loop(model, SYSTEM, "q", local_registry())
        self.assertTrue(r.steps[0].observation.startswith("Error: bad arguments"))
        self.assertTrue(r.steps[1].observation.startswith("Error: calculate failed: ValueError"))
        self.assertEqual(r.status, "final")

    def test_max_iterations(self):
        model = FakeModel([j(action="calculate", args={"expression": "1"})] * 3)
        r = run_loop(model, SYSTEM, "loop forever", local_registry(), max_iterations=3)
        self.assertEqual((r.status, r.answer, len(r.steps)), ("max_iterations", None, 3))
        self.assertEqual(len(model.calls), 3)

    def test_tool_returning_non_string(self):
        tools = ToolRegistry()
        tools.add_source(DictSource({"info": lambda: {"ok": True}}))
        model = FakeModel([j(action="info"), j(final="done")])
        r = run_loop(model, SYSTEM, "q", tools)
        self.assertEqual(r.steps[0].observation, '{"ok": true}')


class CalculateTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(calculate("(17 * 23) + 4"), "395")
        self.assertEqual(calculate("-2 ** 3"), "-8")

    def test_rejects_non_arithmetic(self):
        for expr in ["__import__('os')", "a + 1", "9 ** 9 ** 9", "[1, 2]"]:
            with self.subTest(expr=expr), self.assertRaises(ValueError):
                calculate(expr)


if __name__ == "__main__":
    unittest.main()
