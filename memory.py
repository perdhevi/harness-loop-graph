"""Chapter B — memory.

Project memory: a map of the workspace built by the harness (files, and the
functions and classes in Python files), so every task sees the real names
earlier tasks created.

Long-term memory: lessons shared by all builds.
  fix     error signature → what fixed it       (written when a fixed task passes)
  review  request → what the judge found missing (written on a revise verdict)

Recall score = relevance × time decay × track record. Plain TF-IDF cosine; an
embedding model could replace relevance() later without touching the rest.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from build import SKIP_DIRS

# ---------------------------------------------------------------- project map

def _py_outline(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"(syntax error line {e.lineno})"]
    items = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append(f"def {node.name}({ast.unparse(node.args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            items.append(f"class {node.name}" + (f" [{', '.join(methods)}]" if methods else ""))
    return items


def project_map(workspace: Path, *, max_chars: int = 4000) -> str:
    lines: list[str] = []
    used = 0
    names_only: list[str] = []
    for p in sorted(workspace.rglob("*")):
        rel = p.relative_to(workspace)
        if not p.is_file() or any(x in SKIP_DIRS for x in rel.parts):
            continue
        size = p.stat().st_size
        head = f"- {rel.as_posix()} ({size} bytes)"
        detail: list[str] = []
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = None
        if text is not None and p.suffix == ".py":
            detail = ["    " + x for x in _py_outline(text)]
        elif text is not None:
            first = next((x.strip() for x in text.splitlines() if x.strip()), "")
            if first:
                head += f' — "{first[:80]}"'
        block = "\n".join([head] + detail)
        if used + len(block) > max_chars:
            names_only.append(rel.as_posix())
            continue
        lines.append(block)
        used += len(block) + 1
    if names_only:
        lines.append("- (not detailed, map limit reached): " + ", ".join(names_only))
    return "\n".join(lines) or "(empty)"


# ---------------------------------------------------------------- text similarity

_STOP = set("""the a an and or of to in on for with is are was were be been it this that as at by from not no
into out if then else def return import self none true false error line file exit code stdout stderr""".split())
_ERROR_LINE = re.compile(r"(error|exception|failed|failure|assert|traceback|not found|no module|refused|timed out)",
                         re.IGNORECASE)


def tokens(text: str) -> list[str]:
    text = re.sub(r"(/[\w.\-]+)+", " ", text)          # paths
    text = re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: " " + m.group(0)[1:-1] + " ", text)   # keep quoted words
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    return [w for w in words if w not in _STOP]


def error_signature(output: str, max_lines: int = 6) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    hits = [ln for ln in lines if _ERROR_LINE.search(ln)]
    chosen = hits[-max_lines:] if hits else lines[-max_lines:]
    return "\n".join(chosen)


def cosine(a: Counter, b: Counter, idf: dict[str, float]) -> float:
    dot = sum(a[t] * b[t] * idf.get(t, 1.0) ** 2 for t in a if t in b)
    na = math.sqrt(sum((a[t] * idf.get(t, 1.0)) ** 2 for t in a))
    nb = math.sqrt(sum((b[t] * idf.get(t, 1.0)) ** 2 for t in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- lessons

class LessonStore:
    def __init__(self, path: str | Path, *, half_life_days: float = 30, relative_cutoff: float = 0.5,
                 min_relevance: float = 0.2, recall_chars: int = 1500, clock=time.time):
        self.path = Path(path)
        self.half_life_days = half_life_days
        self.relative_cutoff = relative_cutoff
        self.min_relevance = min_relevance
        self.recall_chars = recall_chars
        self.clock = clock

    # ------------------------------------------------------------ storage

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, lessons: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(lessons, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, kind: str, key: str, **content) -> dict:
        lesson = {"id": uuid.uuid4().hex[:8], "kind": kind, "key": key, "ts": self.clock(),
                  "shown": 0, "helped": 0, **content}
        lessons = self.load()
        lessons.append(lesson)
        self._save(lessons)
        return lesson

    def add_fix(self, *, failure_output: str, check: str, fix_note: str, task: str, request: str,
                run_id: str) -> dict:
        sig = error_signature(failure_output)
        return self.add("fix", sig, check=check, error=sig[:600], fix=fix_note[:800], task=task,
                        request=request[:300], run_id=run_id)

    def add_review(self, *, request: str, feedback: str, problems: list[str], run_id: str) -> dict:
        return self.add("review", request, feedback=feedback[:800], problems=[p[:200] for p in problems][:6],
                        run_id=run_id)

    def mark(self, ids: list[str], *, shown: bool = False, helped: bool = False) -> None:
        if not ids:
            return
        lessons = self.load()
        for lesson in lessons:
            if lesson["id"] in ids:
                lesson["shown"] += int(shown)
                lesson["helped"] += int(helped)
        self._save(lessons)

    # ------------------------------------------------------------ scoring

    def score_all(self, kind: str, query: str) -> list[tuple[float, float, dict]]:
        """(score, relevance, lesson) for every lesson of this kind, best first."""
        lessons = [x for x in self.load() if x["kind"] == kind]
        if not lessons:
            return []
        docs = [Counter(tokens(x["key"])) for x in lessons]
        q = Counter(tokens(query))
        n = len(docs) + 1
        df = Counter(t for d in docs + [q] for t in set(d))
        idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        now = self.clock()
        out = []
        for lesson, d in zip(lessons, docs):
            rel = cosine(q, d, idf)
            age_days = max(0.0, (now - lesson["ts"]) / 86400)
            decay = 0.5 ** (age_days / self.half_life_days)
            record = (lesson["helped"] + 1) / (lesson["shown"] + 2)
            out.append((rel * decay * record, rel, lesson))
        return sorted(out, key=lambda x: -x[0])

    def recall(self, kind: str, query: str) -> list[dict]:
        scored = self.score_all(kind, query)
        if not scored or scored[0][1] < self.min_relevance:
            return []
        best = scored[0][0]
        chosen, used = [], 0
        for score, rel, lesson in scored:
            if score < self.relative_cutoff * best or rel <= 0:
                break
            size = len(json.dumps(lesson))
            if chosen and used + size > self.recall_chars:
                break
            chosen.append({**lesson, "_score": round(score, 3), "_relevance": round(rel, 3)})
            used += size
        return chosen


def render_fix_lessons(lessons: list[dict]) -> str:
    out = []
    for i, x in enumerate(lessons, 1):
        out.append(f"{i}. In a past build ({x.get('task', '?')}), the check `{x.get('check', '?')}` failed with:\n"
                   f"   {x['error'].replace(chr(10), chr(10) + '   ')}\n"
                   f"   What fixed it: {x['fix']}")
    return "\n".join(out)


def render_review_lessons(lessons: list[dict]) -> str:
    out = []
    for i, x in enumerate(lessons, 1):
        probs = "; ".join(x.get("problems", [])) or "—"
        out.append(f"{i}. A similar request (\"{x['key'][:120]}\") was sent back by the reviewer.\n"
                   f"   Problems: {probs}\n   Feedback: {x['feedback']}")
    return "\n".join(out)
