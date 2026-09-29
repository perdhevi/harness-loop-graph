"""Chapter A — tracing.

A side channel: wrappers around the model, the tool registry, the loop's
step callback and each graph node write events to runs/<id>/trace.jsonl.
They pass everything through unchanged, re-raise every exception, and never
let a tracing failure stop a build.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TRACE_FILE = "trace.jsonl"
BIG_FIELDS = {"plan", "spec", "summary", "history", "verdicts", "final_checks", "updated_at", "options"}
ROLE_BY_NODE = {"plan": "planner", "run_task": "task", "build": "task", "judge": "judge", "revise": "reviser"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _snapshot(state) -> dict:
    if not is_dataclass(state):
        return {}
    out = {}
    for f in fields(state):
        v = getattr(state, f.name)
        out[f.name] = ("<big>", len(json.dumps(v, default=str))) if f.name in BIG_FIELDS else v
    return out


def _diff(before: dict, after: dict) -> dict:
    d = {}
    for k in after:
        if before.get(k) != after.get(k):
            d[k] = "changed" if k in BIG_FIELDS else [before.get(k), after.get(k)]
    return d


class NullTracer:
    enabled = False
    node = task = None
    role_override = None        # Chapter D: e.g. "compactor" for summary calls made inside a task

    def bind(self, run_dir, **kw): ...
    def event(self, type_, **fields): ...
    def step(self, step): ...

    def wrap_model(self, model):
        return model

    def wrap_tools(self, tools):
        return tools

    def wrap_node(self, name, fn):
        return fn


class Tracer(NullTracer):
    enabled = True

    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._t0 = clock()
        self._path: Path | None = None
        self._buffer: list[dict] = []
        self.session = 1
        self.node: str | None = None
        self.task: str | None = None
        self.broken: str | None = None      # set if writing ever fails
        self.role_override: str | None = None

    # ------------------------------------------------------------ output

    def bind(self, run_dir: str | Path, *, run_id: str | None = None, resumed_at: str | None = None) -> None:
        """Start writing to runs/<id>/trace.jsonl. Earlier events were buffered."""
        if self._path is not None:
            return
        self._path = Path(run_dir) / TRACE_FILE
        if self._path.exists():
            last = 0
            try:
                for line in self._path.read_text(encoding="utf-8").splitlines():
                    last = max(last, json.loads(line).get("session", 0))
            except (OSError, ValueError):
                pass
            self.session = last + 1
        buffered, self._buffer = self._buffer, []
        self._write({"type": "session", "run_id": run_id, "resumed_at": resumed_at, "pid": os.getpid()})
        for e in buffered:
            e["session"] = self.session
            self._append(e)

    def event(self, type_: str, **fields: Any) -> None:
        if self.broken:
            return
        self._write({"type": type_, **fields})

    def _write(self, fields: dict) -> None:
        e = {"type": fields.pop("type"), "ts": _now_iso(), "ms": round((self._clock() - self._t0) * 1000, 1),
             "session": self.session, "node": self.node, "task": self.task, **fields}
        if self._path is None:
            self._buffer.append(e)
        else:
            self._append(e)

    def _append(self, e: dict) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        except Exception as ex:          # never let tracing stop a build
            self.broken = f"{type(ex).__name__}: {ex}"

    # ------------------------------------------------------------ hooks

    def step(self, step) -> None:
        self.event("step", n=step.n, action=step.action, parse_error=step.error, final=step.final is not None)

    def wrap_model(self, model):
        name = getattr(model, "model", None)             # adapters keep their model name here (Chapter H)
        return TracingModel(model, self, name if isinstance(name, str) else None)

    def wrap_tools(self, tools):
        return TracingTools(tools, self)

    def wrap_node(self, name: str, fn: Callable):
        tracer = self

        def traced(state):
            tracer.node = name
            tracer.task = getattr(state, "current_task", None) if name in ("run_task", "verify") else None
            before = _snapshot(state)
            tracer.event("node_start")
            start = tracer._clock()
            try:
                result = fn(state)
            except BaseException as ex:
                tracer.event("node_end", duration_ms=round((tracer._clock() - start) * 1000, 1),
                             diff=_diff(before, _snapshot(state)), error=f"{type(ex).__name__}: {ex}")
                raise
            after_state = result if result is not None else state
            tracer.event("node_end", duration_ms=round((tracer._clock() - start) * 1000, 1),
                         diff=_diff(before, _snapshot(after_state)), error=None)
            if name == "intake" and getattr(after_state, "run_dir", None):
                tracer.bind(after_state.run_dir, run_id=getattr(after_state, "run_id", None))
            return result

        traced.__name__ = f"traced_{name}"
        return traced


class TracingModel:
    def __init__(self, model, tracer: Tracer, name: str | None = None):
        self.model = model
        self.tracer = tracer
        self.name = name

    def complete(self, system: str, messages: list[dict]) -> str:
        chars_in = len(system) + sum(len(m.get("content", "")) for m in messages)
        start = self.tracer._clock()
        role = self.tracer.role_override or ROLE_BY_NODE.get(self.tracer.node or "", "other")
        try:
            reply = self.model.complete(system, messages)
        except BaseException as ex:
            self.tracer.event("model", role=role, model=self.name,
                              duration_ms=round((self.tracer._clock() - start) * 1000, 1),
                              chars_in=chars_in, chars_out=0, approx_tokens_in=chars_in // 4,
                              approx_tokens_out=0, error=f"{type(ex).__name__}: {ex}")
            raise
        chars_out = len(reply or "")
        self.tracer.event("model", role=role, model=self.name,
                          duration_ms=round((self.tracer._clock() - start) * 1000, 1),
                          chars_in=chars_in, chars_out=chars_out, approx_tokens_in=chars_in // 4,
                          approx_tokens_out=chars_out // 4, error=None)
        return reply


class TracingTools:
    """Wraps a ToolRegistry (or any tool box). Unknown attributes pass through."""

    def __init__(self, tools, tracer: Tracer):
        self._tools = tools
        self._tracer = tracer

    def __getattr__(self, name):
        return getattr(self._tools, name)

    def describe(self) -> str:
        return self._tools.describe()

    def call(self, name: str, args: dict) -> str:
        start = self._tracer._clock()
        try:
            result = self._tools.call(name, args)
        except BaseException as ex:
            self._tracer.event("tool", tool=name, server=name.split(".", 1)[0] if "." in name else "local",
                               duration_ms=round((self._tracer._clock() - start) * 1000, 1), ok=False,
                               args_chars=len(json.dumps(args, default=str)), result_chars=0,
                               error=f"{type(ex).__name__}: {ex}")
            raise
        self._tracer.event("tool", tool=name, server=name.split(".", 1)[0] if "." in name else "local",
                           duration_ms=round((self._tracer._clock() - start) * 1000, 1),
                           ok=not str(result).startswith("Error:"),
                           args_chars=len(json.dumps(args, default=str)), result_chars=len(str(result)),
                           command=args.get("command") if name.endswith("run_command") else None)
        return result


class RoleModel:
    """Tags calls made through it with a role in the trace (e.g. the compaction summariser)."""

    def __init__(self, model, tracer, role: str):
        self.model, self.tracer, self.role = model, tracer, role

    def complete(self, system: str, messages: list[dict]) -> str:
        previous, self.tracer.role_override = self.tracer.role_override, self.role
        try:
            return self.model.complete(system, messages)
        finally:
            self.tracer.role_override = previous
