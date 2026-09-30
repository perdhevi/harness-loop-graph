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

    def test_final_called_in_other_formats(self):
        q = '<|"|>'
        cases = {
            "gemma-call+final-action": f"<|tool_call>call:final{{final:{q}Task R6-1 is complete.{q}}}<tool_call|>",
            "qwen-xml+final-action": "<tool_call><function=final><parameter=final>Task R6-1 is complete."
                                     "</parameter></function></tool_call>",
            "json-alias+final-action": '{"name": "finish", "arguments": {"answer": "Task R6-1 is complete."}}',
        }
        for how, text in cases.items():
            with self.subTest(how=how):
                out, got = normalize_reply(text)
                self.assertEqual((got, parse_action(out)["final"]), (how, "Task R6-1 is complete."))
        out, how = normalize_reply(f"call:write_file{{path:{q}a.py{q},content:{q}x{q}}}")
        self.assertEqual((how, parse_action(out)["action"]), ("gemma-call", "write_file"))   # other calls unchanged

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


# ---------------------------------------------------------------- follow-up: allowed programs, dropping tasks

ALLOWED = {"python", "python3", "pip", "pytest", "node", "npm"}


class AllowedProgramTests(unittest.TestCase):
    def test_check_must_start_with_an_allowed_program(self):
        # R9-5 from a real run: a check that can't even start (and tests nothing)
        r95 = task("R9-5", {"command": "echo 'README.md updated and final verification complete.'"})
        problems = command_problems([r95], ALLOWED)
        self.assertIn("starts with 'echo', which checks can't run; use one of: node, npm, pip, pytest, python, python3",
                      problems[0])
        ok = [task("A", {"command": "python.exe -m pytest -q"}), task("B", {"command": "C:/Python312/python.exe t.py"}),
              task("C", {"command": "pytest"}), task("D", {"file": "README.md"})]
        self.assertEqual(command_problems(ok, ALLOWED), [])
        self.assertEqual(command_problems([r95]), [])                     # no list known: not checked

    def test_allowed_programs_come_from_mcp_json(self):
        from runtime import allowed_programs
        self.assertEqual(allowed_programs(load_json(ROOT / "config.json")), ALLOWED)
        self.assertIsNone(allowed_programs({"tools": {}}))


def todo_plan():
    """The shape of the real run: R6-1 failed, the chain after it blocked, R9-x rebuilt beside it."""
    tasks = [task("T1", {"command": "python -m pytest -q"}, "done"),
             task("R6-1", {"command": "python -m pytest -q"}, "failed", ["T1"]),
             task("R6-2", {"file": "cli.md"}, "blocked", ["R6-1"]),
             task("R6-5", {"file": "docs.md"}, "blocked", ["R6-2"]),
             task("R7-1", {"file": "README.md"}, "blocked", ["R6-5"]),
             task("R9-1", {"command": "python -m pytest -q"}, "done", ["T1"])]
    plan, errors = validate_plan({"title": "Todo", "summary": "s", "tasks": tasks})
    assert not errors, errors
    return plan


class DropTests(unittest.TestCase):
    def revise(self, reply, plan=None, max_tasks=12):
        model = FakeModel([reply])
        return revise_plan(model, PROMPTS, request="todo", plan=plan or todo_plan(), spec="", plan_status="",
                           verdict={"feedback": "f", "problems": []}, files="", round_no=10,
                           max_attempts=1, max_tasks=max_tasks, allowed=ALLOWED), model

    def test_drop_cascades_to_unfinished_dependents(self):
        (new, record), model = self.revise(json.dumps({"changes": "R9 replaced R6", "drop": ["R6-1"]}))
        status = {t["id"]: t["status"] for t in new["tasks"]}
        self.assertEqual(status, {"T1": "done", "R6-1": "dropped", "R6-2": "dropped", "R6-5": "dropped",
                                  "R7-1": "dropped", "R9-1": "done"})
        self.assertEqual(record["dropped"], ["R6-1", "R6-2", "R6-5", "R7-1"])
        self.assertEqual({t["id"]: t.get("dropped_in") for t in new["tasks"]}["R7-1"], 10)
        self.assertIn("Tasks you can retry (failed or blocked): R6-1, R6-2, R6-5, R7-1", model.calls[0][0]["content"])
        from planner import revision_section
        self.assertIn("**Dropped (replaced or no longer needed):** R6-1, R6-2, R6-5, R7-1",
                      revision_section(record, {"feedback": "f"}, new))

    def test_done_tasks_cannot_be_dropped(self):
        with self.assertRaisesRegex(PlanError, "drop: task 'T1' is done"):
            self.revise(json.dumps({"changes": "c", "drop": ["T1"]}))

    def test_judge_rule_and_finish_ignore_dropped_tasks(self):
        from judge import apply_rules
        (new, _), _ = self.revise(json.dumps({"changes": "c", "drop": ["R6-1"]}))
        v = {"verdict": "accept", "requirements": [{"requirement": "undo", "met": True, "evidence": "R9-1"}]}
        self.assertEqual(apply_rules(v, new, [], revisions_used=0, max_revisions=2)["verdict"], "accept")

    def test_dropped_tasks_free_their_slots(self):
        plan = todo_plan()
        extra = [task(f"R10-{i}", {"file": f"f{i}.md"}, deps=["T1"]) for i in range(1, 5)]   # 6 + 4 = 10 > 2 × 4
        with self.assertRaisesRegex(PlanError, "too many tasks"):
            self.revise(json.dumps({"changes": "c", "tasks": extra}), plan, max_tasks=4)
        (new, _), _ = self.revise(json.dumps({"changes": "c", "drop": ["R6-1"], "tasks": extra[:2]}), plan, max_tasks=3)
        self.assertEqual(sum(1 for t in new["tasks"] if t["status"] != "dropped"), 4)


class DropFlowTests(unittest.TestCase):
    """A failed task used to make every later verdict 'revise'; dropping it lets the run be accepted."""

    def test_accepted_after_dropping_the_replaced_task(self):
        import contextlib
        import io
        from unittest import mock
        import main
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        config = json.loads((ROOT / "config.json").read_text())
        config["build"]["task_max_iterations"] = 2
        (tmp / "config.json").write_text(json.dumps(config))

        def j(**kw):
            return json.dumps(kw)
        write = j(thought="w", action="workspace.write_file", args={"path": "a.py", "content": "x = 1\n"})
        look = j(thought="l", action="workspace.list_dir", args={})
        accept = j(verdict="accept", requirements=[{"requirement": "a", "met": True, "evidence": "a.py"}],
                   problems=[], feedback="", summary="ok")
        model = FakeModel([
            j(title="A", summary="s", tasks=[task("T1", {"file": "a.py"}), task("T2", {"file": "b.py"}, deps=["T1"])]),
            write, j(thought="d", final="a"),                  # T1 done
            look, look,                                        # T2 runs out of steps → failed
            accept,                                            # overridden to revise: T2 failed
            j(changes="b.py isn't needed", drop=["T2"]),       # the reviser drops it
            accept,                                            # nothing left to do → judged again
        ])
        with mock.patch.object(main, "ROOT", tmp), mock.patch.object(main, "CONFIG_PATH", tmp / "config.json"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            s = main.run_build("a", model=model, verbose_graph=False)
            main.print_summary(s)
        self.assertEqual(s["status"], "accepted")
        self.assertEqual({t["id"]: t["status"] for t in s["tasks"]}, {"T1": "done", "T2": "dropped"})
        self.assertIn("~ T2", out.getvalue())
        self.assertIn("(dropped in round 1)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
