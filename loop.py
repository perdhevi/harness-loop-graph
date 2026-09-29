"""Stage 2 — the ReAct loop.

reason → act → observe → repeat, until the model gives a final answer
or the iteration budget runs out.

The loop only knows three things:
  - a model with complete(system, messages) -> str   (Stage 1 adapter)
  - a dict of tools: name -> Tool(name, description, params, fn)
  - the action format: one JSON object per reply

It never prints and never raises on bad model output or tool failures;
those become observations the model can react to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_DECODER = json.JSONDecoder()


# ---------------------------------------------------------------- data types

@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]          # argument name -> type, shown to the model
    fn: Callable[..., Any]


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

def _first_json_object(text: str) -> dict:
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = _DECODER.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    raise ParseError("no JSON object found")


def parse_action(text: str) -> dict:
    """Return {"thought", "final"} or {"thought", "action", "args"}."""
    cleaned = _THINK_RE.sub("", text or "").strip()
    obj = _first_json_object(cleaned)
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

def render_tools(tools: dict[str, Tool]) -> str:
    if not tools:
        return "(none)"
    return "\n".join(f"- {t.name}({', '.join(f'{k}: {v}' for k, v in t.params.items())}) — {t.description}"
                     for t in tools.values())


def dispatch(tools: dict[str, Tool], name: str, args: dict) -> str:
    """Run a tool. Never raises: every failure becomes an observation."""
    tool = tools.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'. Available tools: {', '.join(tools) or '(none)'}"
    try:
        result = tool.fn(**args)
    except TypeError as e:
        return f"Error: bad arguments for '{name}': {e}"
    except Exception as e:
        return f"Error: {name} failed: {type(e).__name__}: {e}"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def build_system(template: str, tools: dict[str, Tool]) -> str:
    # str.replace, not str.format: the template contains literal JSON braces.
    return template.replace("{tools}", render_tools(tools))


def run_loop(
    model,
    system_template: str,
    request: str,
    tools: dict[str, Tool],
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
            observation = dispatch(tools, step.action, step.args)

        # observe
        step.observation = observation
        messages.append({"role": "user", "content": f"Observation: {observation}"})
        if on_step:
            on_step(step)

    return LoopResult("max_iterations", None, steps, messages)
