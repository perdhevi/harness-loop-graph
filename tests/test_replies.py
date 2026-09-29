"""Other models' tool-call formats (found with Gemma 4: the file tool was never called)."""

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
from loop import parse_action  # noqa: E402
from replies import normalize_reply  # noqa: E402
from tools.registry import ToolRegistry, ToolSpec  # noqa: E402

Q = '<|"|>'


def gemma(name, **args):
    body = ",".join(f"{k}:{Q}{v}{Q}" if isinstance(v, str) else f"{k}:{json.dumps(v)}" for k, v in args.items())
    return f"<|tool_call>call:{name}{{{body}}}<tool_call|>"


class NormalizeTests(unittest.TestCase):
    def action(self, text):
        out, how = normalize_reply(text)
        return parse_action(out), how

    def test_gemma_call_with_tricky_content(self):
        content = 'import json\nprint({"a": [1, 2]}, "x, y: z")\n'
        a, how = self.action("Let me write it.\n" + gemma("write_file", path="todo.py", content=content))
        self.assertEqual(how, "gemma-call")
        self.assertEqual((a["action"], a["args"]), ("write_file", {"path": "todo.py", "content": content}))
        self.assertEqual(a["thought"], "Let me write it.")

    def test_gemma_variants(self):
        a, _ = self.action('call:workspace.run_command{command:"python -m pytest -q",timeout_s:60}')
        self.assertEqual(a["args"], {"command": "python -m pytest -q", "timeout_s": 60})
        a, _ = self.action("call:list_dir{recursive:true}")
        self.assertEqual(a["args"], {"recursive": True})
        a, _ = self.action("call:list_dir{}")
        self.assertEqual((a["action"], a["args"]), ("list_dir", {}))

    def test_qwen_xml_call(self):
        text = ("I'll write the file.\n<tool_call>\n<function=write_file>\n<parameter=path>\ngreet.py\n</parameter>\n"
                "<parameter=content>\nimport sys\nprint(f'Hello, {sys.argv[1]}!')\n</parameter>\n</function>\n</tool_call>")
        a, how = self.action(text)
        self.assertEqual(how, "qwen-xml")
        self.assertEqual(a["action"], "write_file")
        # the format puts each value on its own line; that newline is a delimiter, not content
        self.assertEqual(a["args"], {"path": "greet.py", "content": "import sys\nprint(f'Hello, {sys.argv[1]}!')"})
        self.assertEqual(a["thought"], "I'll write the file.")
        a, _ = self.action("<tool_call><function=run_command><parameter=command>python -m pytest -q</parameter>"
                           "<parameter=timeout_s>60</parameter></function></tool_call>")
        self.assertEqual(a["args"], {"command": "python -m pytest -q", "timeout_s": 60})

    def test_hermes_json_in_tool_call_tags(self):
        a, how = self.action('<tool_call>\n{"name": "list_dir", "arguments": {"recursive": true}}\n</tool_call>')
        self.assertEqual((a["action"], a["args"], how), ("list_dir", {"recursive": True}, "json-alias"))

    def test_json_aliases(self):
        cases = [
            ('{"name": "write_file", "arguments": {"path": "a", "content": "b"}}', "write_file"),
            ('{"tool": "read_file", "args": {"path": "a"}}', "read_file"),
            ('{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"a\\"}"}}', "read_file"),
            ('{"type": "tool_use", "name": "read_file", "input": {"path": "a"}}', "read_file"),
            ('{"tool_calls": [{"function": {"name": "list_dir", "arguments": "{}"}}]}', "list_dir"),
        ]
        for text, name in cases:
            with self.subTest(text=text):
                a, how = self.action(text)
                self.assertEqual((a["action"], how), (name, "json-alias"))
        a, _ = self.action('{"final_answer": "all done"}')
        self.assertEqual(a["final"], "all done")

    def test_left_alone(self):
        canonical = '{"thought": "t", "action": "workspace.write_file", "args": {"path": "a", "content": "b"}}'
        self.assertEqual(normalize_reply(canonical), (canonical, None))
        final = '{"thought": "t", "final": "done"}'
        self.assertEqual(normalize_reply(final), (final, None))
        for text in ["Here is the code: print(1)", "call:write_file{path:" + Q + "unterminated"]:
            self.assertEqual(normalize_reply(text), (text, None))


class ResolveTests(unittest.TestCase):
    class Src:
        def __init__(self, *names):
            self.names = names

        def list_tools(self):
            return [ToolSpec(n, "d", {"type": "object", "properties": {}}) for n in self.names]

        def call(self, name, args):
            return f"called {name}"

    def test_unique_short_names_resolve(self):
        reg = ToolRegistry()
        reg.add_source(self.Src("workspace.write_file", "workspace.read_file", "calculate"))
        for name in ["write_file", "workspace_write_file", "workspace/write_file", "functions.write_file", "Write_File"]:
            with self.subTest(name=name):
                self.assertEqual(reg.resolve(name), "workspace.write_file")
        out = reg.call("write_file", {})
        self.assertTrue(out.startswith("(ran as workspace.write_file; use that exact name next time)"))
        self.assertIn("called workspace.write_file", out)

    def test_ambiguous_or_unknown_names_are_errors(self):
        reg = ToolRegistry()
        reg.add_source(self.Src("workspace.write_file", "filesystem.write_file"))
        self.assertIsNone(reg.resolve("write_file"))
        self.assertIn("unknown tool 'write_file'", reg.call("write_file", {}))
        self.assertIn("unknown tool 'delete_everything'", reg.call("delete_everything", {}))


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        return self.replies.pop(0)


class GemmaBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, normalize=True):
        config = json.loads((ROOT / "config.json").read_text())
        config["replies"]["normalize"] = normalize
        config["build"]["task_max_iterations"] = 4
        (self.root / "config.json").write_text(json.dumps(config))
        plan = json.dumps({"title": "Greeter", "summary": "s", "tasks": [
            {"id": "T1", "title": "greet.py", "description": "d", "files": ["greet.py"], "depends_on": [],
             "done_when": {"command": "python greet.py"}}]})
        replies = [plan,
                   "I'll create the script.\n" + gemma("write_file", path="greet.py", content="print('Hello — world ✓')\n"),
                   gemma("run_command", command="python greet.py"),
                   '{"final_answer": "greet.py prints Hello — world ✓"}']
        replies += [gemma("write_file", path="greet.py", content="x")] * 6      # spare replies if nothing parses
        with mock.patch.object(main, "ROOT", self.root), mock.patch.object(main, "CONFIG_PATH", self.root / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()):
            return main.run_build("greeter", model=FakeModel(replies), verbose_graph=False, judge=False)

    def test_gemma_style_model_creates_files(self):
        s = self.build()
        self.assertEqual(s["status"], "finished")
        run = Path(s["run_dir"])
        self.assertEqual((run / "workspace" / "greet.py").read_text(encoding="utf-8"), "print('Hello — world ✓')\n")
        steps = [json.loads(x) for x in (run / "transcript.jsonl").read_text().splitlines() if '"n"' in x]
        self.assertEqual([x["action"] for x in steps[:2]], ["write_file", "run_command"])
        self.assertIn("ran as workspace.write_file", steps[0]["observation"])
        self.assertIn("Hello — world ✓", steps[1]["observation"])

    def test_without_normalizing_no_file_is_created(self):
        s = self.build(normalize=False)
        self.assertEqual(s["status"], "partial")
        self.assertFalse((Path(s["run_dir"]) / "workspace" / "greet.py").exists())


class DoctorGemmaTests(unittest.TestCase):
    def test_doctor_explains_the_conversion(self):
        import test_platform
        d = test_platform.DoctorTests("test_model_that_uses_the_tool")
        code, out = d.doctor(gemma("write_file", path="hello.txt", content="hi"))
        d.doCleanups()
        self.assertEqual(code, 0, out)
        self.assertIn("raw reply: <|tool_call>call:write_file", out)
        self.assertIn("converted from gemma-call", out)


if __name__ == "__main__":
    unittest.main()
