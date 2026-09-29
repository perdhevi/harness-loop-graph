"""Stage 2 — the ReAct loop.

reason → act → observe → repeat, until the model gives a final answer
or the iteration budget runs out.

The loop only knows three things:
  - a model with complete(system, messages) -> str   (Stage 1 adapter)
  - a tool box with describe() -> str and call(name, args) -> str
    (Stage 3's ToolRegistry; call() never raises)
  - the action format: one JSON object per reply

It never prints and never raises on bad model output or tool failures;
those become observations the model can react to.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# strict=False: accept real line breaks and tabs inside strings, which models often put in file content (Chapter I)
_DECODER = json.JSONDecoder(strict=False)


# ---------------------------------------------------------------- data types

class ToolBox(Protocol):
    def describe(self) -> str: ...
    def call(self, name: str, args: dict) -> str: ...


@dataclass
class Step:
    n: int
    raw: str
    thought: str = ""
    action: str | None = None
    args: dict = field(default_factory=dict)
    observation: str | None = None
    final: str | None = None
    error: str | None = None        # parse error, if the reply was malformed


@dataclass
class LoopResult:
    status: str                     # "final" | "max_iterations"
    answer: str | None
    steps: list[Step]
    messages: list[dict]


class ParseError(ValueError):
    pass


# ---------------------------------------------------------------- parsing

def _describe_json_error(text: str, e: json.JSONDecodeError) -> str:
    """Tell the model where its JSON broke (Chapter I), instead of only that it did."""
    if e.pos >= len(text.rstrip()) - 1 or e.msg.startswith("Unterminated string"):
        return ("no JSON object found: the reply ends before the JSON object is closed (was it cut off?); "
                "send a shorter reply")
    snippet = (text[max(0, e.pos - 60):e.pos] + "⟨here⟩" + text[e.pos:e.pos + 25]).replace("\n", "⏎")
    return f"no JSON object found: invalid JSON at line {e.lineno} column {e.colno}: {e.msg}, near: {snippet}"


def _balanced(text: str, start: int) -> str | None:
    """The {...} starting at `start`, by brace counting (quotes are not tracked; good enough for a fallback)."""
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
    return None


def _first_json_object(text: str, want: tuple[str, ...] = ()) -> dict:
    """The first JSON object in `text`; with `want`, the first one that has one of those keys.

    Falls back to a Python-style dict ({'action': ...}) and otherwise raises a ParseError
    that says where the JSON broke.
    """
    first_error: json.JSONDecodeError | None = None
    fallback: dict | None = None
    start = text.find("{")
    while start != -1:
        try:
            obj, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError as e:
            first_error = first_error or e
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            if not want or any(k in obj for k in want):
                return obj
            fallback = fallback if fallback is not None else obj
        start = text.find("{", end)
    start = text.find("{")
    while start != -1:                      # Python-style dicts: single quotes, True/False/None
        chunk = _balanced(text, start)
        if chunk:
            try:
                obj = ast.literal_eval(chunk)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                obj = None
            if isinstance(obj, dict) and (not want or any(k in obj for k in want)):
                return obj
        start = text.find("{", start + 1)
    if fallback is not None:
        return fallback
    if first_error is not None:
        raise ParseError(_describe_json_error(text, first_error))
    raise ParseError("no JSON object found")


def parse_action(text: str) -> dict:
    """Return {"thought", "final"} or {"thought", "action", "args"}."""
    cleaned = _THINK_RE.sub("", text or "").strip()
    obj = _first_json_object(cleaned, want=("action", "final"))
    thought = str(obj.get("thought", ""))

    if "final" in obj:
        final = obj["final"]
        if not isinstance(final, str):
            final = json.dumps(final, ensure_ascii=False)
        return {"thought": thought, "final": final}

    if "action" in obj:
        action = obj["action"]
        if not isinstance(action, str) or not action.strip():
            raise ParseError("'action' must be a non-empty string")
        args = obj.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ParseError("'args' must be a JSON object")
        return {"thought": thought, "action": action.strip(), "args": args}

    raise ParseError("JSON object has neither 'action' nor 'final'")


# ---------------------------------------------------------------- the loop

def build_system(template: str, tools: ToolBox) -> str:
    # str.replace, not str.format: the template contains literal JSON braces.
    return template.replace("{tools}", tools.describe())


def run_loop(
    model,
    system_template: str,
    request: str,
    tools: ToolBox,
    *,
    max_iterations: int = 8,
    format_reminder: str = "Reply with exactly one JSON action object ({error}).",
    on_step: Callable[[Step], None] | None = None,
) -> LoopResult:
    system = build_system(system_template, tools)
    messages: list[dict] = [{"role": "user", "content": request}]
    steps: list[Step] = []

    for n in range(1, max_iterations + 1):
        # reason
        raw = model.complete(system, messages)
        messages.append({"role": "assistant", "content": raw if raw.strip() else "(empty reply)"})
        step = Step(n=n, raw=raw)
        steps.append(step)

        try:
            act = parse_action(raw)
        except ParseError as e:
            step.error = str(e)
            observation = format_reminder.replace("{error}", str(e))
        else:
            step.thought = act["thought"]
            if "final" in act:
                step.final = act["final"]
                if on_step:
                    on_step(step)
                return LoopResult("final", step.final, steps, messages)
            # act
            step.action, step.args = act["action"], act["args"]
            observation = tools.call(step.action, step.args)

        # observe
        step.observation = observation
        messages.append({"role": "user", "content": f"Observation: {observation}"})
        if on_step:
            on_step(step)

    return LoopResult("max_iterations", None, steps, messages)
