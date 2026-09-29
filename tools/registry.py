"""Stage 3 — tool registry.

One place for the loop to find tools and call them, wherever they live.
Sources (local Python today, MCP servers in Stage 4) only list tools and
run them; the registry validates arguments and turns every outcome,
including failures, into an observation string.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict   # JSON Schema, same shape as MCP's inputSchema


class ToolSource(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...
    def call(self, name: str, args: dict) -> Any: ...   # may raise


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_args(schema: dict, args: dict) -> list[str]:
    """Return a list of problems; empty means the args are fine."""
    props = schema.get("properties", {}) or {}
    problems = []
    for name in schema.get("required", []) or []:
        if name not in args:
            problems.append(f"missing required '{name}'")
    for name, value in args.items():
        if name not in props:
            allowed = ", ".join(props) or "none"
            problems.append(f"unknown argument '{name}' (allowed: {allowed})")
            continue
        expected = props[name].get("type")
        check = _TYPES.get(expected)
        if check and not check(value):
            problems.append(f"'{name}' should be {expected}, got {type(value).__name__}")
    return problems


def _signature(spec: ToolSpec) -> str:
    props = spec.input_schema.get("properties", {}) or {}
    required = set(spec.input_schema.get("required", []) or [])
    parts = []
    for name, p in props.items():
        mark = "" if name in required else "?"
        parts.append(f"{name}{mark}: {p.get('type', 'any')}")
    return f"{spec.name}({', '.join(parts)})"


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._owner: dict[str, ToolSource] = {}

    def add_source(self, source: ToolSource) -> None:
        for spec in source.list_tools():
            if spec.name in self._specs:
                raise ValueError(f"tool name clash: '{spec.name}' is already registered")
            self._specs[spec.name] = spec
            self._owner[spec.name] = source

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def describe(self) -> str:
        if not self._specs:
            return "(none)"
        return "\n".join(f"- {_signature(s)} — {s.description}" for s in self._specs.values())

    def resolve(self, name: str) -> str | None:
        """Exact name, or one unambiguous match for how models often shorten or re-spell it.

        write_file / workspace_write_file / workspace/write_file / functions.write_file → workspace.write_file
        """
        if name in self._specs:
            return name
        norm = re.sub(r"[/:]|__", ".", name.strip()).lower()
        for prefix in ("functions.", "tools.", "tool."):
            if norm.startswith(prefix):
                norm = norm[len(prefix):]
        by_lower = {n.lower(): n for n in self._specs}
        if norm in by_lower:
            return by_lower[norm]
        underscored = {n.lower().replace(".", "_"): n for n in self._specs}
        if norm in underscored:
            return underscored[norm]
        short = norm.rsplit(".", 1)[-1]
        matches = [n for n in self._specs if n.lower().rsplit(".", 1)[-1] == short]
        return matches[0] if len(matches) == 1 else None

    def call(self, name: str, args: dict) -> str:
        resolved = self.resolve(name)
        if resolved is None:
            available = ", ".join(self._specs) or "(none)"
            return f"Error: unknown tool '{name}'. Available tools: {available}"
        if resolved != name:
            result = self.call(resolved, args)
            return f"(ran as {resolved}; use that exact name next time)\n{result}"
        spec = self._specs[name]
        problems = validate_args(spec.input_schema, args)
        if problems:
            return f"Error: bad arguments for '{name}': " + "; ".join(problems)
        try:
            result = self._owner[name].call(name, args)
        except Exception as e:   # tool failures are observations, not crashes
            return f"Error: {name} failed: {type(e).__name__}: {e}"
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
