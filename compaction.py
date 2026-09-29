"""Chapter D — compaction.

CompactingModel wraps the model for one task loop. The loop keeps its full
message list; what is *sent* is a view: the task prompt (never compacted) with
a summary of older steps appended, followed by the most recent steps verbatim.
Compaction triggers on pressure (share of the call budget), compacts down toward
a lower target, and then waits for the same growth gap before compacting again
(hysteresis relative to where it landed), so it can't thrash when the target
is out of reach.
"""

from __future__ import annotations

import re
from typing import Callable

from context import estimate_tokens
from loop import ParseError, parse_action

_EXIT_RE = re.compile(r"^exit code: (-?\d+)", re.MULTILINE)


def _short(text: str, n: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _telling_line(obs: str) -> str:
    """The most informative line of an output: an error-looking line, else the last output line."""
    lines = [ln.strip() for ln in (obs or "").splitlines() if ln.strip()]
    for line in lines:
        if re.search(r"(Error|FAILED|failed|assert|Traceback|not allowed|timed out)", line):
            return _short(line, 160)
    body = [ln for ln in lines if not ln.startswith(("exit code:", "--- stdout", "--- stderr"))]
    return _short(body[-1], 160) if body else ""


def digest(steps: list[tuple[str, str]], first_n: int, max_tried: int = 12) -> str:
    """Extractive summary of (assistant reply, observation) pairs, numbered from first_n.

    Bounded: the lists keep their most recent entries (failures 8, thoughts 4, actions max_tried),
    so re-digesting a long prefix doesn't grow without limit.
    """
    tried, failed, thoughts = [], [], []
    files: dict[str, None] = {}
    last_exit: dict[str, str] = {}
    for i, (reply, obs) in enumerate(steps):
        n = first_n + i
        try:
            act = parse_action(reply)
        except ParseError:
            tried.append(f"(step {n}: reply was not a valid action)")
            continue
        if act.get("thought"):
            thoughts.append(_short(act["thought"], 90))
        if "final" in act:
            tried.append(f"(step {n}: gave a final answer)")
            continue
        tool, args = act["action"], act["args"]
        short_tool = tool.split(".", 1)[-1]
        target = args.get("path") or (f"`{args['command']}`" if "command" in args else "")
        if short_tool in ("write_file", "edit_file") and args.get("path"):
            files[args["path"]] = None
        m = _EXIT_RE.search(obs or "")
        if m and "command" in args:
            last_exit[args["command"]] = m.group(1)
        is_error = (obs or "").startswith("Error") or (m is not None and m.group(1) != "0")
        tried.append(f"{short_tool} {target}".strip() + (f" (exit {m.group(1)})" if m else "")
                     + (" ✗" if is_error else ""))
        if is_error:
            detail = _telling_line(obs)
            status = f"exit {m.group(1)}" + (": " if detail else "") if m else ""
            failed.append(f"{short_tool} {target}".strip() + f" → {status}{detail}")
    last = first_n + len(steps) - 1
    shown_tried = tried[-max_tried:]
    earlier = len(tried) - len(shown_tried)
    lines = [f"Steps {first_n}–{last} were compacted. What happened:",
             "- Tried: " + (f"({earlier} earlier actions not listed) " if earlier else "")
             + ("; ".join(shown_tried) or "(nothing)")]
    if failed:
        lines.append("- Failed:\n    " + "\n    ".join(failed[-8:]))
    if files:
        lines.append("- Files written or edited: " + ", ".join(list(files)[-20:]))
    if last_exit:
        recent = list(last_exit.items())[-10:]
        lines.append("- Last result of each command: " + "; ".join(f"`{_short(c, 80)}` → exit {code}"
                                                                    for c, code in recent))
    if thoughts:
        lines.append("- Thoughts, most recent last: " + "; ".join(f'"{t}"' for t in thoughts[-4:]))
    return "\n".join(lines)


class CompactingModel:
    def __init__(self, model, *, window_tokens: int, call_fraction: float = 0.85, compact_at: float = 0.75,
                 target: float = 0.5, ceiling: float = 0.95, keep_recent_steps: int = 2, note: str = "{summary}",
                 mode: str = "extractive", summarizer=None, summarizer_system: str = "",
                 on_compact: Callable[[dict], None] | None = None):
        self.model = model
        self.call_budget = int(window_tokens * call_fraction)
        self.compact_at = compact_at
        self.target = target
        self.ceiling = ceiling
        self.next_trigger = compact_at      # moves up if the target can't be reached (see _compact)
        self.keep_recent = max(1, keep_recent_steps)
        self.note = note
        self.mode = mode
        self.summarizer = summarizer or model
        self.summarizer_system = summarizer_system
        self.on_compact = on_compact
        self.upto = 1               # messages[1:upto] are covered by the summary
        self.summary = ""
        self.compactions = 0

    # ------------------------------------------------------------ view

    def _view(self, messages: list[dict]) -> list[dict]:
        if self.upto <= 1 or not self.summary:
            return messages
        first = dict(messages[0])
        first["content"] = first["content"] + "\n\n" + self.note.replace("{summary}", self.summary)
        return [first] + messages[self.upto:]

    @staticmethod
    def _size(system: str, msgs: list[dict]) -> int:
        return estimate_tokens(system) + sum(estimate_tokens(m.get("content", "")) for m in msgs)

    # ------------------------------------------------------------ compaction

    @staticmethod
    def _pairs(messages: list[dict], start: int, stop: int) -> list[tuple[str, str]]:
        """(assistant reply, observation) pairs; the loop's 'Observation: ' prefix is removed."""
        out = []
        for i in range(start, stop, 2):
            obs = messages[i + 1]["content"]
            out.append((messages[i]["content"], obs[len("Observation: "):] if obs.startswith("Observation: ") else obs))
        return out

    def _summarise(self, messages: list[dict], new_from: int, cut: int) -> tuple[str, str]:
        # extractive: one bounded digest over *all* compacted steps (cheap, deterministic)
        extract = digest(self._pairs(messages, 1, cut), 1)
        if self.mode != "model":
            return extract, "extractive"
        # model: incremental — previous summary + only the newly compacted steps
        pairs = self._pairs(messages, new_from, cut)
        first_n = (new_from + 1) // 2
        raw = "\n\n".join(f"[step {first_n + i}]\nassistant: {a}\nobservation: {_short(o, 1500)}"
                          for i, (a, o) in enumerate(pairs))
        prompt = (f"Earlier summary:\n{self.summary or '(none)'}\n\nExtracted facts:\n{extract}\n\n"
                  f"Raw steps:\n{raw}")
        try:
            text = self.summarizer.complete(self.summarizer_system, [{"role": "user", "content": prompt}])
        except Exception:
            return extract, "extractive (model summary failed)"
        text = (text or "").strip()
        return (text, "model") if text else (extract, "extractive (empty model summary)")

    def _compact(self, system: str, messages: list[dict]) -> None:
        # candidate cut points: the start of an assistant message (odd index), leaving
        # at least keep_recent steps (assistant + observation pairs) uncompacted
        last_allowed = len(messages) - 2 * self.keep_recent
        candidates = [k for k in range(self.upto + 2, last_allowed + 1) if k % 2 == 1]
        if not candidates:
            return
        view_before = self._view(messages)
        before = self._size(system, view_before)
        goal = self.target * self.call_budget
        cut = candidates[-1]                      # compact as much as allowed if we must
        for k in candidates:                      # …but no more than needed to reach the target
            tail = self._size(system, [messages[0]] + messages[k:])
            if tail + estimate_tokens(self.summary) + 200 <= goal:
                cut = k
                break
        self.summary, mode = self._summarise(messages, self.upto, cut)
        old_upto, self.upto = self.upto, cut
        self.compactions += 1
        view_after = self._view(messages)
        after = self._size(system, view_after)
        # Hysteresis relative to where we landed: if the fixed part (system, task prompt, summary,
        # recent steps) keeps us above the target, require the same growth gap before compacting
        # again, instead of compacting one step at a time. Capped so calls stay within budget.
        gap = self.compact_at - self.target
        self.next_trigger = min(self.ceiling, max(self.compact_at, after / self.call_budget + gap))
        if self.on_compact:
            self.on_compact({"from_step": (old_upto + 1) // 2, "to_step": (cut - 1) // 2,
                             "messages_before": len(view_before), "messages_after": len(view_after),
                             "tokens_before": before, "tokens_after": after, "mode": mode,
                             "pressure_before": round(before / self.call_budget, 3),
                             "pressure_after": round(after / self.call_budget, 3),
                             "target_reached": after <= self.target * self.call_budget,
                             "next_trigger": round(self.next_trigger, 3)})

    # ------------------------------------------------------------ the model interface

    def complete(self, system: str, messages: list[dict]) -> str:
        if self._size(system, self._view(messages)) > self.next_trigger * self.call_budget:
            self._compact(system, messages)
        return self.model.complete(system, self._view(messages))
