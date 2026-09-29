"""Other models' tool-call formats (found with Gemma 4: the file tool was never called)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import parse_action  # noqa: E402
from replies import normalize_reply  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
