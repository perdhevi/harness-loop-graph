"""Stage 3 tests — tool registry, argument validation and local tools."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.local import LocalToolSource  # noqa: E402
from tools.registry import ToolRegistry, ToolSpec, validate_args  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "force": {"type": "boolean"},
        "opts": {"type": "object"},
        "items": {"type": "array"},
    },
    "required": ["path"],
}


class FakeSource:
    """Stands in for a second source (e.g. MCP in Stage 4)."""

    def __init__(self, name="remote_echo"):
        self.name = name
        self.calls = []

    def list_tools(self):
        return [ToolSpec(self.name, "Echo text back.", {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})]

    def call(self, name, args):
        self.calls.append((name, args))
        if args["text"] == "boom":
            raise RuntimeError("server went away")
        return {"echo": args["text"]}


def write_tools(entries) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(entries, f)
    f.close()
    return Path(f.name)


class ValidateArgsTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(validate_args(SCHEMA, {"path": "a", "count": 2, "ratio": 0.5, "force": True,
                                                "opts": {}, "items": []}), [])

    def test_missing_unknown_and_types(self):
        problems = validate_args(SCHEMA, {"count": "2", "force": 1, "colour": "red"})
        self.assertIn("missing required 'path'", problems)
        self.assertIn("'count' should be integer, got str", problems)
        self.assertIn("'force' should be boolean, got int", problems)
        self.assertTrue(any(p.startswith("unknown argument 'colour'") for p in problems))

    def test_bool_is_not_a_number(self):
        self.assertEqual(validate_args(SCHEMA, {"path": "a", "count": True}),
                         ["'count' should be integer, got bool"])
        self.assertEqual(validate_args(SCHEMA, {"path": "a", "ratio": False}),
                         ["'ratio' should be number, got bool"])


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.reg.add_source(LocalToolSource(ROOT / "tools.json"))

    def test_describe(self):
        text = self.reg.describe()
        self.assertIn("- calculate(expression: string) — ", text)
        self.assertIn("- current_time(timezone?: string) — ", text)

    def test_call_local(self):
        self.assertEqual(self.reg.call("calculate", {"expression": "2 + 3"}), "5")
        self.assertRegex(self.reg.call("current_time", {"timezone": "Asia/Jakarta"}), r"\+07:00$")
        self.assertRegex(self.reg.call("current_time", {}), r"\+00:00$")

    def test_errors_are_observations(self):
        self.assertIn("unknown tool 'nope'", self.reg.call("nope", {}))
        self.assertIn("missing required 'expression'", self.reg.call("calculate", {}))
        self.assertIn("unknown argument 'expr'", self.reg.call("calculate", {"expr": "1"}))
        self.assertTrue(self.reg.call("calculate", {"expression": "1/0"})
                        .startswith("Error: calculate failed: ZeroDivisionError"))
        self.assertTrue(self.reg.call("current_time", {"timezone": "Mars/Base"})
                        .startswith("Error: current_time failed:"))

    def test_second_source_drops_in(self):
        fake = FakeSource()
        self.reg.add_source(fake)
        self.assertIn("- remote_echo(text: string) — Echo text back.", self.reg.describe())
        self.assertEqual(self.reg.call("remote_echo", {"text": "hi"}), '{"echo": "hi"}')
        self.assertIn("server went away", self.reg.call("remote_echo", {"text": "boom"}))
        # validation happens before the source is called
        self.assertIn("missing required 'text'", self.reg.call("remote_echo", {}))
        self.assertEqual(len(fake.calls), 2)

    def test_name_clash_fails_at_startup(self):
        with self.assertRaises(ValueError):
            self.reg.add_source(FakeSource(name="calculate"))


class LocalSourceTests(unittest.TestCase):
    def test_handler_must_be_inside_tools_package(self):
        path = write_tools([{"name": "shell", "description": "", "handler": "os:system"}])
        with self.assertRaises(ValueError):
            LocalToolSource(path)

    def test_bad_handler_fails_at_load(self):
        path = write_tools([{"name": "x", "description": "", "handler": "tools.builtin:does_not_exist"}])
        with self.assertRaises(ValueError):
            LocalToolSource(path)

    def test_adding_a_tool_is_one_entry(self):
        path = write_tools([{
            "name": "add", "description": "Add via calculate.",
            "inputSchema": {"type": "object", "properties": {"expression": {"type": "string"}},
                            "required": ["expression"]},
            "handler": "tools.builtin:calculate",
        }])
        reg = ToolRegistry()
        reg.add_source(LocalToolSource(path))
        self.assertEqual(reg.call("add", {"expression": "40 + 2"}), "42")


if __name__ == "__main__":
    unittest.main()
