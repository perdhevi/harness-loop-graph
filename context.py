"""Chapter C — context management.

Budgeted sections: a task's first message is split into named sections that
share a token budget by fraction (water-filling: what one section doesn't
need is shared out again). Relevant files: current file contents, ranked by
score. Output limiter: long tool results are cut to head + tail, the full text
is saved, and harness.read_output reads the rest.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from build import SKIP_DIRS
from memory import cosine, tokens
from tools.registry import ToolSpec

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# ---------------------------------------------------------------- sections

@dataclass
class Section:
    name: str
    text: str
    share: float = 0.0            # fraction of the budget; ignored when required
    strategy: str = "head"        # head | tail | middle
    required: bool = False        # never cut


@dataclass
class Allocation:
    budget_chars: int
    given: dict[str, int] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)

    def stats(self, sections: list[Section]) -> dict:
        wanted = {s.name: len(s.text) for s in sections}
        return {"budget_chars": self.budget_chars,
                "used_chars": sum(len(t) for t in self.texts.values()),
                "sections": {n: {"wanted": wanted[n], "given": len(self.texts[n]),
                                 "cut": max(0, wanted[n] - len(self.texts[n]))} for n in wanted}}


def shrink(text: str, limit: int, strategy: str = "head", label: str = "") -> str:
    """Cut text to at most `limit` chars (marker included), keeping head, tail or both."""
    if len(text) <= limit:
        return text
    where = f" from {label}" if label else ""
    longest_marker = f"\n[… {len(text):,} chars cut{where}]\n"     # the marker can only get shorter
    room = limit - len(longest_marker)
    if room <= 0:
        return longest_marker.strip()[:max(0, limit)]
    marker = f"\n[… {len(text) - room:,} chars cut{where}]\n"
    if strategy == "tail":
        return marker.lstrip("\n") + text[len(text) - room:]
    if strategy == "middle":
        head = room // 2
        return text[:head] + marker + text[len(text) - (room - head):]
    return text[:room] + marker.rstrip("\n")


def allocate(sections: list[Section], budget_chars: int) -> Allocation:
    """Water-filling: shares of what's left; unused shares flow to sections that need more."""
    alloc = Allocation(budget_chars)
    left = budget_chars
    for s in sections:
        if s.required:
            alloc.given[s.name] = len(s.text)
            left -= len(s.text)
    active = [s for s in sections if not s.required]
    left = max(0, left)
    while active:
        total_share = sum(s.share for s in active) or 1.0
        offers = {s.name: left * s.share / total_share for s in active}
        satisfied = [s for s in active if len(s.text) <= offers[s.name]]
        if not satisfied:
            for s in active:
                alloc.given[s.name] = int(offers[s.name])
            break
        for s in satisfied:
            alloc.given[s.name] = len(s.text)
            left -= len(s.text)
        active = [s for s in active if s not in satisfied]
    for s in sections:
        alloc.texts[s.name] = s.text if s.required else shrink(s.text, alloc.given[s.name], s.strategy, s.name)
    return alloc


# ---------------------------------------------------------------- relevant files

def _text_files(workspace: Path) -> list[tuple[str, str]]:
    out = []
    for p in sorted(workspace.rglob("*")):
        rel = p.relative_to(workspace)
        if not p.is_file() or any(x in SKIP_DIRS for x in rel.parts):
            continue
        try:
            out.append((rel.as_posix(), p.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def rank_files(workspace: Path, *, listed: list[str], query: str, mentions: str = "") -> list[tuple[float, str, str]]:
    files = _text_files(workspace)
    if not files:
        return []
    docs = [Counter(tokens(text)) for _, text in files]
    q = Counter(tokens(query))
    n = len(docs) + 1
    df = Counter(t for d in docs + [q] for t in set(d))
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    listed_set = {x.replace("\\", "/").lstrip("./") for x in listed}
    ranked = []
    for (path, text), d in zip(files, docs):
        score = 0.0
        if path in listed_set:
            score += 1.0
        if mentions and (path in mentions or Path(path).name in mentions):
            score += 0.8
        score += 0.5 * cosine(q, d, idf)
        if score > 0:
            ranked.append((round(score, 4), path, text))
    return sorted(ranked, key=lambda x: (-x[0], x[1]))


def relevant_files(ranked: list[tuple[float, str, str]], budget_chars: int) -> tuple[str, list[str]]:
    """Render ranked files into at most budget_chars; returns (text, paths shown)."""
    parts, shown, skipped, used = [], [], [], 0
    # one file may take at most half the section, so a huge file can't crowd out the rest;
    # a single file may use all of it
    per_file = budget_chars if len(ranked) == 1 else max(200, budget_chars // 2)
    reserve = 0 if len(ranked) == 1 else 80           # room for the "not shown" line
    for score, path, text in ranked:
        block_head = f"----- {path} -----\n"
        room = min(per_file, budget_chars - reserve - used - len(block_head) - 1)
        if room < 200:
            skipped.append(path)
            continue
        body = shrink(text, room, "middle", path)
        parts.append(block_head + body + "\n")
        shown.append(path)
        used += len(block_head) + len(body) + 1
    if skipped:
        note = "(not shown, budget reached: " + ", ".join(skipped) + ")\n"
        if used + len(note) > budget_chars:
            note = f"({len(skipped)} more relevant file(s) not shown)\n"
        parts.append(note)
    return "".join(parts) or "(none)", shown


# ---------------------------------------------------------------- output limiter

READ_OUTPUT = "harness.read_output"
_READ_SPEC = ToolSpec(
    READ_OUTPUT,
    "Read part of a long tool output that was cut. Use the id from the [output … cut] note.",
    {"type": "object", "properties": {"id": {"type": "string"}, "offset": {"type": "integer"},
                                      "limit": {"type": "integer"}}, "required": ["id"]},
)


class OutputLimiter:
    """Wraps a tool box: long results are saved in full and cut to head + tail with a pointer."""

    def __init__(self, tools, out_dir: Path, max_chars: int = 4000):
        self._tools = tools
        self.out_dir = Path(out_dir)
        self.max_chars = max_chars
        self._n = len(list(self.out_dir.glob("*.txt"))) if self.out_dir.exists() else 0

    def __getattr__(self, name):
        return getattr(self._tools, name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.list_tools()) + [_READ_SPEC]

    def describe(self) -> str:
        base = self._tools.describe()
        return base + f"\n- {READ_OUTPUT}(id: string, offset?: integer, limit?: integer) — {_READ_SPEC.description}"

    def call(self, name: str, args: dict) -> str:
        if name == READ_OUTPUT:
            return self._read(args)
        result = self._tools.call(name, args)
        if len(result) <= self.max_chars:
            return result
        self._n += 1
        oid = f"{self._n:04d}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"{oid}.txt").write_text(result, encoding="utf-8")
        half = max(1, (self.max_chars - 300) // 2)
        return (result[:half]
                + f"\n[output {oid} cut: showing the first {half} and last {half} of {len(result):,} chars. "
                  f'Call {READ_OUTPUT} with {{"id": "{oid}", "offset": {half}}} to read more.]\n'
                + result[-half:])

    def _read(self, args: dict) -> str:
        oid = str(args.get("id", "")).strip()
        extra = set(args) - {"id", "offset", "limit"}
        if extra:
            return f"Error: bad arguments for '{READ_OUTPUT}': unknown argument(s) {sorted(extra)}"
        path = self.out_dir / f"{oid}.txt"
        if not oid.isdigit() or not path.exists():
            return f"Error: {READ_OUTPUT}: no saved output with id '{oid}'"
        text = path.read_text(encoding="utf-8")
        try:
            offset = max(0, int(args.get("offset", 0)))
            limit = max(1, min(int(args.get("limit", self.max_chars - 200)), self.max_chars - 200))
        except (TypeError, ValueError):
            return f"Error: bad arguments for '{READ_OUTPUT}': offset and limit must be integers"
        if offset >= len(text):
            return f"Error: {READ_OUTPUT}: offset {offset} is past the end (output {oid} has {len(text):,} chars)"
        chunk = text[offset:offset + limit]
        end = offset + len(chunk)
        more = f" Next: offset {end}." if end < len(text) else " (end of output)"
        return f"[output {oid}: chars {offset}–{end} of {len(text):,}.{more}]\n{chunk}"
