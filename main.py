"""harness-loop-graph — command line entry point.

Stage 6: `build` plans first — request → SPEC.md + plan.json — then builds from the plan.

Usage:
    python main.py build "Build a Python CLI to-do app with add/list/done and pytest tests"
    python main.py build --yes "..."              # never ask questions; record assumptions
    python main.py build --review "..."           # plan only, then stop for a human to review
    python main.py build --from-run runs/<id>     # build from a reviewed (maybe edited) plan
    python main.py build --no-plan "..."          # Stage 5 behaviour
    python main.py build --max-iterations 60 "..."
    python main.py doctor                          # check settings, workspace server, model and reply format

    python main.py "What is (17 * 23) + 4, and is it prime?"     # question mode (Stages 2–4)
    python main.py --workspace ./scratch "Write hello.py and run it"
    python main.py --once "Write a palindrome check in Python"   # single call (Stage 1)
    python main.py --list-tools
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from loop import Step, run_loop
from model_adapter import ModelError, make_adapter
from build import execute_build, open_run, start_run
from planner import PlanError, load_plan, make_plan, render_tasks, save_plan
from replies import NormalizingModel
from runtime import CONFIG_PATH, PROMPTS_PATH, ROOT, build_registry, load_json, prompt_text
from tools.mcp_client import McpError


# ---------------------------------------------------------------- output

def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def print_step(step: Step) -> None:
    print(f"── step {step.n} " + "─" * 40)
    if step.error:
        print(f"  parse error : {step.error}")
        print(f"  raw reply   : {_short(step.raw)}")
    if step.thought:
        print(f"  thought     : {_short(step.thought)}")
    if step.action:
        print(f"  action      : {step.action} {_short(json.dumps(step.args, ensure_ascii=False), 200)}")
    if step.observation is not None:
        print(f"  observation : {_short(step.observation)}")
    if step.final is not None:
        print("  final       : (answer below)")


# ---------------------------------------------------------------- Stage 1

def run_once(prompt: str) -> str:
    config = load_json(CONFIG_PATH)
    model = make_adapter(config)
    return model.complete(config["system_prompt"], [{"role": "user", "content": prompt}])


# ---------------------------------------------------------------- Stages 2–4: question mode

def run_react(request: str, workspace: Path, max_iterations: int | None = None):
    config = load_json(CONFIG_PATH)
    prompts = load_json(PROMPTS_PATH)
    model = make_adapter(config)
    if config.get("replies", {}).get("normalize", True):
        from replies import NormalizingModel
        model = NormalizingModel(model)
    with contextlib.ExitStack() as stack:
        registry = build_registry(config, workspace, stack)
        return run_loop(
            model,
            prompt_text(prompts, "react_system"),
            request,
            registry,
            max_iterations=max_iterations or config.get("loop", {}).get("max_iterations", 8),
            format_reminder=prompt_text(prompts, "format_reminder"),
            on_step=print_step,
        )


# ---------------------------------------------------------------- Stages 5–6: build mode

def ask_in_terminal(questions: list[str]) -> list[str] | None:
    print("\nThe planner has questions (press Enter to skip one and let it assume):")
    answers = []
    for i, q in enumerate(questions, 1):
        answers.append(input(f"  {i}. {q}\n     > ").strip() or "(no answer — assume something reasonable)")
    return answers


def check_context_settings(config: dict, say=print) -> list[str]:
    """Warn when the harness plans for a bigger window than Ollama will really give the model."""
    warnings = []
    if config.get("provider") == "ollama":
        num_ctx = config["providers"]["ollama"].get("num_ctx")
        window = config.get("context", {}).get("window_tokens", 16000)
        if not num_ctx:
            warnings.append("providers.ollama.num_ctx is not set: Ollama may use 4,096 tokens and silently "
                            "drop the start of long prompts (system prompt, tools, task)")
        elif window > num_ctx:
            warnings.append(f"context.window_tokens ({window}) is larger than providers.ollama.num_ctx ({num_ctx}): "
                            "prompts will be cut by Ollama, silently. Make them match.")
    for w in warnings:
        say(f"[warning] {w}")
    return warnings


def _append_jsonl(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_build(request: str | None, max_iterations: int | None = None, *, model=None,
              ask=None, review: bool = False, from_run: str | None = None, no_plan: bool = False) -> dict:
    """Plan, then build. `review` stops after planning; `from_run` builds a reviewed plan."""
    config = load_json(CONFIG_PATH)
    check_context_settings(config)
    prompts = load_json(PROMPTS_PATH)
    P = {k: prompt_text(prompts, k) for k in prompts}
    provider = config["provider"]
    model = model or make_adapter(config)
    loop_model = NormalizingModel(model) if config.get("replies", {}).get("normalize", True) else model
    cfg_plan = config.get("plan", {})
    max_tasks = cfg_plan.get("max_tasks", 12)
    plan = None

    if from_run:
        run = open_run(Path(from_run))
        if run.summary_file.exists():
            raise PlanError(f"{run.dir} has already been built; start a new run instead")
        plan, spec = load_plan(run.dir, max_tasks)                   # picks up a reviewer's edits
        request = json.loads(run.request_file.read_text(encoding="utf-8"))["request"]
        print(f"[run] {run.dir}  (building the reviewed plan)")
    else:
        run = start_run(ROOT / config.get("build", {}).get("runs_dir", "runs"), request,
                        meta={"provider": provider, "model": config["providers"][provider].get("model"),
                              "planned": not no_plan})
        print(f"[run] {run.dir}")
        if not no_plan:
            print("[plan] asking the planner …")
            try:
                plan = make_plan(model, P, request, ask=ask,
                                 max_attempts=cfg_plan.get("max_attempts", 3), max_tasks=max_tasks,
                                 max_questions=cfg_plan.get("max_questions", 3),
                                 log=lambda e: _append_jsonl(run.dir / "planner.jsonl", e))
            except PlanError as e:
                summary = {"id": run.id, "status": "plan_failed", "error": str(e), "plan": None}
                run.summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
                return {**summary, "run_dir": str(run.dir)}
            save_plan(run.dir, plan, request)
            spec = (run.dir / "SPEC.md").read_text(encoding="utf-8")
            print(f"[plan] {plan['title']} — {len(plan['tasks'])} tasks")
            print("\n".join("       " + line for line in render_tasks(plan).splitlines() if line[:1] != " "))
            if review:
                return {"id": run.id, "status": "planned", "run_dir": str(run.dir),
                        "plan": {"title": plan["title"], "tasks": len(plan["tasks"])}}

    if plan is None:
        first = P["build_request"].replace("{request}", request)
        plan_info = None
    else:
        first = (P["build_with_plan"].replace("{request}", request)
                 .replace("{spec}", spec.strip()).replace("{tasks}", render_tasks(plan)))
        plan_info = {"title": plan["title"], "tasks": len(plan["tasks"])}
    with contextlib.ExitStack() as stack:
        summary = execute_build(
            run, loop_model, build_registry(config, run.workspace, stack),
            P["react_system"] + "\n\n" + P["build_rules"], first,
            max_iterations=max_iterations or config.get("build", {}).get("max_iterations", 40),
            format_reminder=P["format_reminder"], on_step=print_step, plan_info=plan_info)
    summary["run_dir"] = str(run.dir)
    return summary


def print_summary(s: dict) -> None:
    print("═" * 50)
    if s["status"] == "planned":
        print(f"Planned: {s['plan']['title']} ({s['plan']['tasks']} tasks). Nothing built yet.")
        print(f"Review  : {s['run_dir']}/SPEC.md and plan.json (edit either if you like)")
        print(f"Build   : python main.py build --from-run {s['run_dir']}")
        return
    if s["status"] == "plan_failed":
        print(f"Planning failed: {s['error']}")
        print(f"Details : {s['run_dir']}/planner.jsonl")
        return
    if s["answer"]:
        print(s["answer"])
        print("─" * 50)
    if s["status"] == "max_iterations":
        print(f"Stopped: hit the iteration limit ({s['steps']} steps) without a final answer.")
    elif s["status"] == "error":
        print(f"Stopped with an error: {s['error']}")
    elif s["status"] == "interrupted":
        print("Interrupted (Ctrl-C). The run folder keeps what was done so far.")
    if s.get("plan"):
        print(f"plan     : {s['plan']['title']} ({s['plan']['tasks']} tasks)")
    print(f"status   : {s['status']}  (NOT verified: \"done\" means the model said so)")
    print(f"steps    : {s['steps']}  ·  malformed: {s['malformed']}  ·  model calls: {s['model_calls']}")
    tools = ", ".join(f"{k}×{v}" for k, v in sorted(s["tool_calls"].items())) or "none"
    print(f"tools    : {tools}")
    print(f"files    : {len(s['files'])}  " + ", ".join(f["path"] for f in s["files"][:12])
          + (" …" if len(s["files"]) > 12 else ""))
    print(f"size     : ~{s['approx_tokens_in']:,} tokens in / ~{s['approx_tokens_out']:,} out (chars ÷ 4)")
    print(f"time     : {s['duration_s']}s")
    print(f"run      : {s['run_dir']}")


# ---------------------------------------------------------------- CLI

def main_build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="main.py build", description="Build a project from a request")
    parser.add_argument("request", nargs="*")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--yes", action="store_true", help="never ask questions; the planner records assumptions")
    parser.add_argument("--review", action="store_true", help="plan only, then stop")
    parser.add_argument("--from-run", metavar="RUN_DIR", help="build from a reviewed plan in this run folder")
    parser.add_argument("--no-plan", action="store_true", help="skip planning (Stage 5 behaviour)")
    args = parser.parse_args(argv)
    if args.review and args.no_plan:
        parser.error("--review needs a plan; drop --no-plan")

    request = None
    if not args.from_run:
        request = " ".join(args.request).strip() or input("build request> ").strip()
        if not request:
            print("Empty request.")
            return 1
    interactive = sys.stdin.isatty() and not args.yes
    try:
        summary = run_build(request, args.max_iterations,
                            ask=ask_in_terminal if interactive else None,
                            review=args.review, from_run=args.from_run, no_plan=args.no_plan)
    except (ModelError, McpError, PlanError, FileNotFoundError) as e:
        print(f"[error] {e}")
        return 1
    print_summary(summary)
    return {"finished": 0, "planned": 0, "max_iterations": 2}.get(summary["status"], 1)


def _safe_console() -> None:
    """Windows: printing ✓ or — to a cp1252 pipe/file raises UnicodeEncodeError; replace instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main_doctor(argv: list[str]) -> int:
    """Check the setup a build depends on: settings, the workspace server, the model and its reply format."""
    import tempfile
    import urllib.request
    from loop import ParseError, parse_action
    parser = argparse.ArgumentParser(prog="main.py doctor", description="Check the setup before a real build")
    parser.add_argument("--skip-model", action="store_true", help="don't call the model")
    args = parser.parse_args(argv)
    config = load_json(CONFIG_PATH)
    provider = config["provider"]
    pcfg = config["providers"][provider]
    ok = True

    def report(good: bool, what: str, hint: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {what}" + (f"\n      → {hint}" if hint and not good else ""))

    print(f"python   {sys.version.split()[0]} on {sys.platform} · encoding stdout={sys.stdout.encoding}")
    print(f"provider {provider} · model {pcfg.get('model')}")
    print("settings")
    warnings = check_context_settings(config, say=lambda m: None)
    report(not warnings, "context window matches what the model is given", "; ".join(warnings))

    print("workspace server")
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        try:
            tools = build_registry(config, Path(tmp), stack)
            report(True, "starts")
            wrote = tools.call("workspace.write_file", {"path": "check.py", "content": "print('ok ✓ — Ёлка')\n"})
            report(not wrote.startswith("Error"), "writes a file with non-ASCII text", wrote[:200])
            back = (Path(tmp) / "check.py").read_text(encoding="utf-8")
            report("✓ — Ёлка" in back, "file content is intact on disk", repr(back[:80]))
            ran = tools.call("workspace.run_command", {"command": "python check.py"})
            report("ok ✓ — Ёлка" in ran and "exit code: 0" in ran, "runs `python` and reads its output",
                   ran[:300] + "  (is `python` on PATH? On Windows, the Microsoft Store alias doesn't count)")
        except Exception as e:
            report(False, "starts", f"{type(e).__name__}: {e}")

    if provider == "ollama":
        print("ollama")
        base = pcfg["base_url"].rstrip("/")
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as r:
                names = [m["name"] for m in json.loads(r.read())["models"]]
            report(True, f"reachable at {base}")
            want = pcfg["model"]
            report(want in names or f"{want}:latest" in names, f"model {want} is pulled",
                   f"run: ollama pull {want}   (found: {', '.join(names[:8]) or 'none'})")
        except Exception as e:
            report(False, f"reachable at {base}", f"{e} — is `ollama serve` running?")

    if not args.skip_model and ok:
        print("model reply format")
        prompts = load_json(PROMPTS_PATH)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            tools = build_registry(config, Path(tmp), stack)
            system = prompt_text(prompts, "react_system").replace("{tools}", tools.describe())
            t0 = time.perf_counter()
            try:
                reply = make_adapter(config).complete(system, [{"role": "user", "content":
                        "Create a file hello.txt containing the word hi. Use the right tool."}])
            except ModelError as e:
                report(False, "model answers", str(e))
                reply = None
            if reply is not None:
                report(True, f"model answers ({time.perf_counter() - t0:.1f}s)")
                print("      raw reply: " + " ".join(reply.split())[:300])
                from replies import normalize_reply
                converted, how = normalize_reply(reply)
                if how:
                    print(f"      (not the harness format: converted from {how} — builds will still work)")
                    reply = converted
                try:
                    act = parse_action(reply)
                    good = tools.resolve(act.get("action", "")) == "workspace.write_file" if act.get("action") else False
                    report(good, "reply is a valid JSON action that calls write_file",
                           f"got {act.get('action') or 'a final answer'} — the model answered instead of using a tool")
                except ParseError as e:
                    report(False, "reply is a valid JSON action", f"{e}; reply starts: {reply[:200]!r}")
    print("result:", "OK" if ok else "problems found (see ✗ above)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _safe_console()
    if argv[:1] == ["doctor"]:
        return main_doctor(argv[1:])
    if argv[:1] == ["build"]:
        return main_build(argv[1:])

    parser = argparse.ArgumentParser(description="harness-loop-graph",
                                     epilog="For projects, use: main.py build \"<request>\"")
    parser.add_argument("request", nargs="*", help="a question or small task")
    parser.add_argument("--once", action="store_true", help="single model call (Stage 1)")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--list-tools", action="store_true", help="show available tools and exit")
    parser.add_argument("--workspace", default=None, help="project folder the tools work in")
    args = parser.parse_args(argv)

    config = load_json(CONFIG_PATH)
    workspace = Path(args.workspace or config.get("workspace", "workspace")).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    if args.list_tools:
        try:
            with contextlib.ExitStack() as stack:
                print(build_registry(config, workspace, stack).describe())
        except McpError as e:
            print(f"[error] {e}")
            return 1
        return 0

    request = " ".join(args.request).strip() or input("request> ").strip()
    if not request:
        print("Empty request.")
        return 1

    start = time.perf_counter()
    try:
        if args.once:
            print(run_once(request))
            print(f"\n[{time.perf_counter() - start:.1f}s]")
            return 0
        print(f"[workspace] {workspace}")
        result = run_react(request, workspace, args.max_iterations)
    except (ModelError, McpError) as e:
        print(f"[error] {e}")
        return 1
    elapsed = time.perf_counter() - start

    print("═" * 50)
    if result.status == "final":
        print(result.answer)
    else:
        print(f"Stopped: hit the iteration limit ({len(result.steps)} steps) without a final answer.")
    tool_calls = sum(1 for s in result.steps if s.action)
    malformed = sum(1 for s in result.steps if s.error)
    print(f"\n[{result.status} · {len(result.steps)} steps · {tool_calls} tool calls · "
          f"{malformed} malformed · {elapsed:.1f}s]")
    return 0 if result.status == "final" else 2


if __name__ == "__main__":
    sys.exit(main())
