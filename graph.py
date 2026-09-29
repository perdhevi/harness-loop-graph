"""Stage 7 — a small graph runner.

Nodes are functions over a shared state object; edges say what runs next.
The API mirrors LangGraph's (nodes, edges, conditional branches,
interrupt_before, checkpoints), so swapping it in later would only mean
replacing this file.
"""

from __future__ import annotations

from typing import Any, Callable

END = "__end__"


class GraphError(RuntimeError):
    pass


class Graph:
    def __init__(self, entry: str):
        self.entry = entry
        self._nodes: dict[str, Callable[[Any], Any]] = {}
        self._edges: dict[str, str] = {}
        self._branches: dict[str, tuple[Callable[[Any], str], set[str]]] = {}

    # ------------------------------------------------------------ building

    def node(self, name: str, fn: Callable[[Any], Any]) -> "Graph":
        if name in self._nodes or name == END:
            raise GraphError(f"node '{name}' already exists or is reserved")
        self._nodes[name] = fn
        return self

    def edge(self, src: str, dst: str) -> "Graph":
        self._edges[src] = dst
        return self

    def branch(self, src: str, router: Callable[[Any], str], targets: set[str]) -> "Graph":
        self._branches[src] = (router, set(targets))
        return self

    def validate(self) -> None:
        problems = []
        if self.entry not in self._nodes:
            problems.append(f"entry '{self.entry}' is not a node")
        for name in self._nodes:
            outs = (name in self._edges) + (name in self._branches)
            if outs != 1:
                problems.append(f"node '{name}' needs exactly one edge or branch (has {outs})")
        targets = set(self._edges.values())
        for _, ts in self._branches.values():
            targets |= ts
        for src in list(self._edges) + list(self._branches):
            if src not in self._nodes:
                problems.append(f"edge from unknown node '{src}'")
        for t in targets - {END}:
            if t not in self._nodes:
                problems.append(f"edge to unknown node '{t}'")
        if problems:
            raise GraphError("; ".join(problems))

    # ------------------------------------------------------------ running

    def _next(self, name: str, state) -> str:
        if name in self._edges:
            return self._edges[name]
        router, targets = self._branches[name]
        choice = router(state)
        if choice not in targets:
            raise GraphError(f"router for '{name}' returned '{choice}', expected one of {sorted(targets)}")
        return choice

    def run(
        self,
        state,
        *,
        start: str | None = None,
        interrupt_before: set[str] | None = None,
        checkpoint: Callable[[Any], None] | None = None,
        on_enter: Callable[[str, Any], None] | None = None,
        max_steps: int = 50,
    ):
        """Run from `start` (default: entry) until END or an interrupt.

        The state must have `next` and `history` attributes. Before each node,
        `state.next` is set and the state is checkpointed, so a crash inside a
        node leaves a state that resumes by re-running that node.
        """
        self.validate()
        interrupt_before = interrupt_before or set()
        name = start or self.entry
        if name not in self._nodes:
            raise GraphError(f"cannot start at unknown node '{name}'")
        first = True
        for _ in range(max_steps):
            state.next = name
            if checkpoint:
                checkpoint(state)
            if name in interrupt_before and not first:
                return state                      # paused; resume with start=state.next
            first = False
            if on_enter:
                on_enter(name, state)
            result = self._nodes[name](state)
            if result is not None:
                state = result
            state.history.append(name)
            name = self._next(name, state)
            if name == END:
                state.next = None
                if checkpoint:
                    checkpoint(state)
                return state
        raise GraphError(f"graph did not reach END within {max_steps} steps")
