"""Chapter A — read runs/<id>/trace.jsonl and print a timeline with totals."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from tracing import TRACE_FILE


def load_events(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / TRACE_FILE
    if not path.exists():
        raise FileNotFoundError(f"no {TRACE_FILE} in {run_dir} (was tracing enabled?)")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue                       # a line cut short by a crash
    return events


def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def fmt_tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _buckets(events: list[dict]) -> list[dict]:
    """Group events into node runs: one bucket per node_start … node_end."""
    buckets, current = [], None
    for e in events:
        t = e["type"]
        if t == "node_start":
            current = {"node": e["node"], "task": e.get("task"), "session": e["session"], "start": e["ms"],
                       "duration": None, "error": None, "diff": {}, "items": []}
            buckets.append(current)
        elif t == "node_end" and current is not None:
            current.update(duration=e.get("duration_ms"), error=e.get("error"), diff=e.get("diff") or {})
            current = None
        elif current is not None and t in ("model", "tool", "step", "check", "verdict", "normalized"):
            current["items"].append(e)
    return buckets


def _bucket_line(b: dict) -> str:
    models = [i for i in b["items"] if i["type"] == "model"]
    tools = [i for i in b["items"] if i["type"] == "tool"]
    parts = []
    if models:
        tin = sum(m["approx_tokens_in"] for m in models)
        tout = sum(m["approx_tokens_out"] for m in models)
        parts.append(f"model×{len(models)} ~{fmt_tok(tin)}→{fmt_tok(tout)} tok")
    if tools:
        by_server = defaultdict(int)
        for t in tools:
            by_server[t["server"]] += 1
        bad = sum(1 for t in tools if not t["ok"])
        parts.append(f"tools×{len(tools)} (" + ", ".join(f"{k} {v}" for k, v in by_server.items()) + ")"
                     + (f" {bad} errors" if bad else ""))
    norm = [i for i in b["items"] if i["type"] == "normalized"]
    if norm:
        kinds = sorted({i["format"] for i in norm})
        parts.append(f"normalized×{len(norm)} ({', '.join(kinds)})")
    for c in (i for i in b["items"] if i["type"] == "check"):
        parts.append(f"check {'✓' if c['ok'] else '✗'} {c['target']}")
    for v in (i for i in b["items"] if i["type"] == "verdict"):
        parts.append(f"verdict {v['verdict']} {v['met']}/{v['total']}" + (f" ({', '.join(v['overrides'])})"
                                                                          if v["overrides"] else ""))
    if b["error"]:
        parts.append(f"ERROR {b['error']}")
    task = f" {b['task']}" if b["task"] else ""
    return f"{b['start'] / 1000:8.2f}s  {(b['node'] + task):<16} {fmt_ms(b['duration']):>9}   " + "   ".join(parts)


def render(events: list[dict], *, steps: bool = False) -> str:
    out: list[str] = []
    buckets = _buckets(events)
    sessions = [e for e in events if e["type"] == "session"]
    for sess in sessions or [{"session": 1, "run_id": None, "resumed_at": None}]:
        n = sess["session"]
        label = f"session {n}" + (f"  (resumed at {sess['resumed_at']})" if sess.get("resumed_at") else "")
        out.append(label + (f"  {sess['run_id']}" if sess.get("run_id") else ""))
        for b in (b for b in buckets if b["session"] == n):
            out.append("  " + _bucket_line(b))
            if steps:
                for i in b["items"]:
                    if i["type"] == "step":
                        what = i["action"] or ("final answer" if i["final"] else f"parse error: {i['parse_error']}")
                        out.append(f"               step {i['n']:<3} {what}")
                    elif i["type"] == "tool":
                        cmd = f"  `{i['command']}`" if i.get("command") else ""
                        out.append(f"                        ↳ {i['tool']} {fmt_ms(i['duration_ms'])}"
                                   f"{'' if i['ok'] else '  ERROR'}{cmd}")
        if any(b["session"] == n and b["duration"] is None for b in buckets):
            out.append("  (session ended inside a node: interrupted or crashed)")
        out.append("")

    models = [e for e in events if e["type"] == "model"]
    tools = [e for e in events if e["type"] == "tool"]

    # by task
    by_task: dict[str, dict] = {}
    for b in buckets:
        if not b["task"]:
            continue
        t = by_task.setdefault(b["task"], {"time": 0.0, "model": 0.0, "tools": 0.0, "tin": 0, "tout": 0, "steps": 0})
        t["time"] += b["duration"] or 0
        for i in b["items"]:
            if i["type"] == "model":
                t["model"] += i["duration_ms"]
                t["tin"] += i["approx_tokens_in"]
                t["tout"] += i["approx_tokens_out"]
            elif i["type"] == "tool":
                t["tools"] += i["duration_ms"]
            elif i["type"] == "step":
                t["steps"] += 1
    if by_task:
        out.append("by task")
        for tid, t in by_task.items():
            out.append(f"  {tid:<6} {fmt_ms(t['time']):>9}   model {fmt_ms(t['model']):>9}   tools {fmt_ms(t['tools']):>9}"
                       f"   ~{fmt_tok(t['tin'])}→{fmt_tok(t['tout'])} tok   {t['steps']} steps")
        out.append("")

    if tools:
        out.append("by tool")
        agg: dict[str, list] = defaultdict(list)
        for t in tools:
            agg[t["tool"]].append(t)
        for name, ts in sorted(agg.items()):
            errors = sum(1 for t in ts if not t["ok"])
            avg = sum(t["duration_ms"] for t in ts) / len(ts)
            out.append(f"  {name:<28} ×{len(ts):<3} avg {fmt_ms(avg):>8}   total {fmt_ms(sum(t['duration_ms'] for t in ts)):>9}"
                       + (f"   {errors} errors" if errors else ""))
        out.append("")

    if models:
        out.append("by role (model calls)")
        roles: dict[str, list] = defaultdict(list)
        for m in models:
            roles[m["role"]].append(m)
        for role, ms in roles.items():
            out.append(f"  {role:<8} ×{len(ms):<3} {fmt_ms(sum(m['duration_ms'] for m in ms)):>9}"
                       f"   ~{fmt_tok(sum(m['approx_tokens_in'] for m in ms))}→"
                       f"{fmt_tok(sum(m['approx_tokens_out'] for m in ms))} tok"
                       + (f"   {sum(1 for m in ms if m['error'])} errors" if any(m["error"] for m in ms) else ""))
        out.append("")

    slow = sorted(models, key=lambda m: -m["duration_ms"])[:3] + sorted(tools, key=lambda t: -t["duration_ms"])[:3]
    if slow:
        out.append("slowest")
        for e in slow:
            where = f"{e['node']}" + (f" {e['task']}" if e.get("task") else "")
            what = f"model ({e['role']})" if e["type"] == "model" else e["tool"] + (f" `{e['command']}`" if e.get("command") else "")
            out.append(f"  {fmt_ms(e['duration_ms']):>9}  {what}  — {where}")
    return "\n".join(out).rstrip() + "\n"

