"""harness-loop-graph — command line entry point.

Stage 3: the loop finds its tools in a registry; local tools are defined in tools.json.

Usage:
    python main.py "What is (17 * 23) + 4, and is it prime?"
    python main.py "What time is it in Jakarta and how many hours until midnight?"
    python main.py --once "Write a palindrome check in Python"   # single call (Stage 1)
    python main.py --list-tools
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from loop import Step, run_loop
from model_adapter import ModelError, make_adapter
from tools.local import LocalToolSource
from tools.registry import ToolRegistry

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
PROMPTS_PATH = ROOT / "prompts.json"


# ---------------------------------------------------------------- setup

def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def prompt_text(prompts: dict, key: str) -> str:
    value = prompts[key]
    return "\n".join(value) if isinstance(value, list) else value


def build_registry(config: dict) -> ToolRegistry:
    """Every tool source in one registry. Today: the local tools from tools.json."""
    registry = ToolRegistry()
    tools_cfg = config.get("tools", {})
    if tools_cfg.get("local"):
        registry.add_source(LocalToolSource(ROOT / tools_cfg["local"]))
    return registry


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


# ---------------------------------------------------------------- Stages 2–3: the ReAct loop

def run_react(request: str, max_iterations: int | None = None):
    config = load_json(CONFIG_PATH)
    prompts = load_json(PROMPTS_PATH)
    model = make_adapter(config)
    if config.get("replies", {}).get("normalize", True):
        from replies import NormalizingModel
        model = NormalizingModel(model)
    return run_loop(
        model,
        prompt_text(prompts, "react_system"),
        request,
        build_registry(config),
        max_iterations=max_iterations or config.get("loop", {}).get("max_iterations", 8),
        format_reminder=prompt_text(prompts, "format_reminder"),
        on_step=print_step,
    )


def _safe_console() -> None:
    """Windows: printing ✓ or — to a cp1252 pipe/file raises UnicodeEncodeError; replace instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _safe_console()

    parser = argparse.ArgumentParser(description="harness-loop-graph")
    parser.add_argument("request", nargs="*", help="a question or small task")
    parser.add_argument("--once", action="store_true", help="single model call (Stage 1)")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--list-tools", action="store_true", help="show available tools and exit")
    args = parser.parse_args(argv)

    config = load_json(CONFIG_PATH)
    if args.list_tools:
        print(build_registry(config).describe())
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
        result = run_react(request, args.max_iterations)
    except ModelError as e:
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
