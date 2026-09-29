"""Chapter I — robust replies and plans. Most inputs here are real replies from a failed run."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "servers"))

from loop import ParseError, parse_action, run_loop  # noqa: E402
from planner import PlanError, _parse, command_problems, load_plan, make_plan, revise_plan, validate_plan  # noqa: E402
from replies import NormalizingModel, normalize_reply  # noqa: E402
from runtime import load_json, prompt_text  # noqa: E402
from tools.registry import ToolRegistry, ToolSpec  # noqa: E402
from workspace_server import ToolError, Workspace  # noqa: E402

PROMPTS = {k: prompt_text(load_json(ROOT / "prompts.json"), k) for k in load_json(ROOT / "prompts.json")}


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


def task(tid, done_when, status="pending", deps=()):
    return {"id": tid, "title": f"task {tid}", "description": "d", "files": [], "depends_on": list(deps),
            "done_when": done_when, "status": status}


# ---------------------------------------------------------------- parsing

class ParserTests(unittest.TestCase):
    def test_real_line_breaks_inside_strings(self):
        a = parse_action('{"thought": "w", "action": "workspace.write_file", '
                         '"args": {"path": "a.py", "content": "import sys\nprint(1)\n"}}')
        self.assertEqual(a["args"]["content"], "import sys\nprint(1)\n")

    def test_error_says_where(self):
        with self.assertRaises(ParseError) as ctx:
            parse_action('{"thought": "t",\n "action": "x",\n "args": {"python -c 1" 2}}')
        msg = str(ctx.exception)
        self.assertIn("line 3 column", msg)
        self.assertIn("⟨here⟩", msg)
        self.assertTrue(msg.startswith("no JSON object found"))

    def test_cut_off_reply(self):
        with self.assertRaisesRegex(ParseError, "cut off"):
            parse_action('{"thought": "w", "action": "workspace.write_file", "args": {"path": "a.py", "content": "imp')

    def test_first_object_with_the_right_keys(self):
        a = parse_action('I will create {"path": "a.py"} now: {"thought": "w", "action": "workspace.list_dir"}')
        self.assertEqual(a["action"], "workspace.list_dir")

    def test_python_style_dict(self):
        a = parse_action("{'thought': 'w', 'action': 'workspace.list_dir', 'args': {'recursive': True}}")
        self.assertEqual((a["action"], a["args"]), ("workspace.list_dir", {"recursive": True}))


# ---------------------------------------------------------------- finishing

FINAL_AS_TOOL = json.dumps({"thought": "done", "action": "final", "args": {
    "final": "I have completed Task R6-1 by implementing the `uncomplete_task(self, task_number: int)` method."}})


class FinishTests(unittest.TestCase):
    def test_action_final_becomes_a_final_answer(self):
        out, how = normalize_reply(FINAL_AS_TOOL)
        self.assertEqual(how, "final-action")
        self.assertTrue(parse_action(out)["final"].startswith("I have completed Task R6-1"))
        out, _ = normalize_reply('{"action": "finish", "args": {"summary": "ok"}}')
        self.assertEqual(parse_action(out)["final"], "ok")

    def test_loop_ends_instead_of_calling_a_tool_named_final(self):
        reg = ToolRegistry()
        r = run_loop(NormalizingModel(FakeModel([FINAL_AS_TOOL])), "{tools}", "do R6-1", reg, max_iterations=3)
        self.assertEqual((r.status, len(r.steps)), ("final", 1))

    def test_unknown_tool_named_final_explains_how_to_finish(self):
        reg = ToolRegistry()
        reg.add_source(type("S", (), {"list_tools": lambda self: [ToolSpec("calc", "d", {"properties": {}})],
                                      "call": lambda self, n, a: "x"})())
        self.assertIn('reply {"thought": "...", "final": "your answer"}', reg.call("final", {}))
        self.assertNotIn("To finish", reg.call("search_web", {}))


# ---------------------------------------------------------------- edit_file

class EditFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name), {"python"})
        (Path(self.tmp.name) / "todo_cli.py").write_text(
            'if args.command == "add":\n    todo.add_task(args.text)\nelif args.command == "done":\n'
            '    result = todo.complete_task(args.task_number)\n    print(result)\n', encoding="utf-8")

    def test_not_found_shows_the_closest_lines(self):
        with self.assertRaises(ToolError) as ctx:
            self.ws.edit_file("todo_cli.py", 'elif args.command == "done":\n    result = todo.complete_task(args.number)',
                              "x")
        msg = str(ctx.exception)
        self.assertIn("Closest match (lines 3–4", msg)
        self.assertIn('   3| elif args.command == "done":', msg)

    def test_whitespace_only_difference_is_named(self):
        with self.assertRaisesRegex(ToolError, "spaces, tabs or line breaks differ"):
            self.ws.edit_file("todo_cli.py", 'elif args.command == "done":\n  result = todo.complete_task(args.task_number)',
                              "x")


# ---------------------------------------------------------------- plans and revisions

# revision 6 from a real run: done_when without a key, and shell operators in a check
REVISION_6 = json.dumps({"changes": "undo", "retry": [], "tasks": [
    {"id": "R6-1", "title": "undo in TodoList", "description": "d", "files": ["todo_manager.py"], "depends_on": ["T1"],
     "done_when": "__R61__"},
    {"id": "R6-2", "title": "un-done command", "description": "d", "files": ["todo_cli.py"], "depends_on": ["R6-1"],
     "done_when": "__R62__"}]}, indent=2) \
    .replace('"__R61__"', '{\n        "python -c \\"import todo_manager; todo_manager.TodoList.undo_task\\""\n      }') \
    .replace('"__R62__"', '{\n        "python todo_cli.py list && python todo_cli.py un-done 1 > /dev/null || exit 1"\n      }')


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.plan, _ = validate_plan({"title": "Todo", "summary": "s", "tasks": [
            task("T1", {"command": 'python -c "import todo_manager"'}, "done"),
            task("T2", {"command": "python -m pytest -q"}, "done", ["T1"]),
            task("T3", {"file": "README.md"}, "failed", ["T1"])]})

    def revise(self, *replies):
        model = FakeModel(replies)
        verdict = {"feedback": "add undo", "problems": []}
        try:
            new, record = revise_plan(model, PROMPTS, request="todo", plan=self.plan, spec="", plan_status="",
                                      verdict=verdict, files="", round_no=6, max_attempts=len(replies))
        except PlanError as e:
            return model, None, e
        return model, new, record

    def test_keyless_done_when_is_repaired(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(REVISION_6)                                        # really invalid JSON
        obj = _parse(REVISION_6, ("changes", "retry", "tasks"))
        self.assertEqual(obj["tasks"][0]["done_when"],
                         {"command": 'python -c "import todo_manager; todo_manager.TodoList.undo_task"'})

    def test_shell_operators_are_sent_back_with_the_reason(self):
        fixed = REVISION_6.replace("python todo_cli.py list && python todo_cli.py un-done 1 > /dev/null || exit 1",
                                   "python -m pytest -q")
        model, new, record = self.revise(REVISION_6, fixed)
        self.assertIsNotNone(new, record)
        feedback = model.calls[1][-1]["content"]
        self.assertIn("R6-2: done_when.command uses && > ||", feedback)
        self.assertIn("revision", feedback)                               # the reviser's own retry wording

    def test_copied_check_is_rejected_but_a_test_suite_is_not(self):
        copy = json.dumps({"changes": "c", "tasks": [task("R6-1", {"command": 'python -c "import todo_manager"'},
                                                          deps=["T1"])]})
        tests = json.dumps({"changes": "c", "tasks": [task("R6-1", {"command": "python -m pytest -q"}, deps=["T2"])]})
        model, new, _ = self.revise(copy, tests)
        self.assertIn("same check as T1, which already passes", model.calls[1][-1]["content"])
        self.assertEqual([t["id"] for t in new["tasks"]][-1], "R6-1")

    def test_new_ids_under_retry_are_dropped(self):
        reply = json.dumps({"changes": "c", "retry": ["R6-1", "T3"],
                            "tasks": [task("R6-1", {"file": "UNDO.md"}, deps=["T1"])]})
        _, new, record = self.revise(reply)
        self.assertEqual(record["retry"], ["T3"])
        self.assertEqual({t["id"]: t["status"] for t in new["tasks"]}["T3"], "pending")
        self.assertEqual({t["id"]: t["status"] for t in new["tasks"]}["R6-1"], "pending")

    def test_reviser_is_told_what_it_can_retry(self):
        model, _, _ = self.revise(json.dumps({"changes": "c", "retry": ["T3"]}))
        self.assertIn("Tasks you can retry (failed or blocked): T3", model.calls[0][0]["content"])


class PlanTests(unittest.TestCase):
    def test_new_plans_reject_shell_operators(self):
        bad = json.dumps({"title": "t", "summary": "s", "tasks": [task("T1", {"command": "python a.py > out.txt"})]})
        good = bad.replace(" > out.txt", "")
        model = FakeModel([bad, good])
        plan = make_plan(model, PROMPTS, "x", max_attempts=2)
        self.assertEqual(plan["tasks"][0]["done_when"], {"command": "python a.py"})
        self.assertIn("uses >", model.calls[1][-1]["content"])

    def test_old_saved_plans_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = {"title": "t", "summary": "s", "tasks": [task("T1", {"command": "python a.py && echo ok"})]}
            (Path(d) / "plan.json").write_text(json.dumps(p))
            self.assertEqual(load_plan(Path(d))[0]["tasks"][0]["done_when"]["command"], "python a.py && echo ok")

    def test_wrong_done_when_key_gets_an_example(self):
        _, errors = validate_plan({"title": "t", "summary": "s", "tasks": [task("T1", {"python": "python -c 1"})]})
        self.assertIn("it uses 'python'; write it as {\"command\"", errors[0])

    def test_command_problems_quotes(self):
        self.assertIn("quotes", command_problems([task("T1", {"command": "python -c \"print(1)"})])[0])
        self.assertEqual(command_problems([task("T1", {"command": 'python -c "print(1 > 0)"'})]), [])


if __name__ == "__main__":
    unittest.main()
