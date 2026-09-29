"""Stage 3 — local tools: definitions in JSON, handlers in Python."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Callable

from tools.registry import ToolSpec

ALLOWED_PACKAGE = "tools."


def _resolve(handler: str) -> Callable[..., Any]:
    module_name, sep, func_name = handler.partition(":")
    if not sep or not module_name.startswith(ALLOWED_PACKAGE):
        raise ValueError(f"handler '{handler}' must look like '{ALLOWED_PACKAGE}<module>:<function>'")
    fn = getattr(importlib.import_module(module_name), func_name, None)
    if not callable(fn):
        raise ValueError(f"handler '{handler}' is not a callable")
    return fn


class LocalToolSource:
    def __init__(self, path: str | Path):
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        self._specs: list[ToolSpec] = []
        self._fns: dict[str, Callable[..., Any]] = {}
        for e in entries:
            spec = ToolSpec(
                name=e["name"],
                description=e.get("description", ""),
                input_schema=e.get("inputSchema", {"type": "object", "properties": {}}),
            )
            self._specs.append(spec)
            self._fns[spec.name] = _resolve(e["handler"])

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, name: str, args: dict) -> Any:
        return self._fns[name](**args)
