"""Chapter E — sensors.

Watchers that observe a task's loop steps and checks and turn them into
smooth scores (0 → 1). They only count and score: they never stop a loop,
skip a task or change a status. Consumers (console warning, judge evidence,
fix-prompt note) read the signals and decide for themselves.

Raw counters live in task["sensor_state"] so they add up across fix attempts
and resumes; the scores go to task["signals"].
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from memory import error_signature, tokens

_EXIT_RE = re.compile(r"^exit code: (-?\d+)", re.MULTILINE)
WEIGHTS = {"same_failure": 1.0, "no_progress": 0.9, "repeated_action": 0.8, "repeated_edit": 0.6}


# ---------------------------------------------------------------- score curves

def _grow(x: float, start: float, half: float) -> float:
    """0 until x reaches `start`, then approaches 1; +`half` beyond start gives 0.5."""
    return 0.0 if x <= start else round(1 - 0.5 ** ((x - start) / half), 4)


def repeated_edit(max_writes: int) -> float:
    return _grow(max_writes, 2, 2)


def same_failure(max_repeats: int) -> float:
    return _grow(max_repeats, 1, 1)


def no_progress(steps_since: int) -> float:
    return _grow(steps_since, 3, 4)


def repeated_action(max_identical: int) -> float:
    return _grow(max_identical, 1, 1.5)


def scope_drift(outside: int, written: int) -> float:
    if written == 0 or outside == 0:
        return 0.0
    return round((outside / written) * (1 - 0.5 ** outside), 4)


def combine(scores: dict[str, float]) -> float:
    rest = 1.0
    for name, w in WEIGHTS.items():
        rest *= 1 - w * scores.get(name, 0.0)
    return round(1 - rest, 4)


def _signature_key(output: str) -> str:
    return " ".join(sorted(set(tokens(error_signature(output)))))[:300] or "(empty)"


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------- per task

class TaskSensors:
    def __init__(self, task: dict, workspace: Path):
        self.task = task
        self.workspace = Path(workspace)
        st = task.get("sensor_state") or {}
        self.writes: dict[str, int] = dict(st.get("writes", {}))
        self.failures: dict[str, int] = dict(st.get("failures", {}))
        self.actions: dict[str, int] = dict(st.get("actions", {}))
        self.seen_hashes: dict[str, list[str]] = {k: list(v) for k, v in st.get("seen_hashes", {}).items()}
        self.failed_commands: list[str] = list(st.get("failed_commands", []))
        self.since_progress: int = st.get("since_progress", 0)
        self.outside: int = st.get("outside", 0)
        self.written: int = st.get("written", 0)
        self.peak: float = (task.get("signals") or {}).get("peak_stuck", 0.0)
        listed = task.get("files") or []
        self.listed = {x.replace("\\", "/").lstrip("./") for x in listed}

    # ------------------------------------------------------------ observations

    def _progress(self) -> None:
        self.since_progress = 0

    def observe_step(self, step) -> None:
        if not step.action:
            if step.final is None:
                self.since_progress += 1         # malformed reply: a step without progress
            return
        self.since_progress += 1
        key = json.dumps([step.action, step.args], sort_keys=True, default=str)[:500]
        self.actions[key] = self.actions.get(key, 0) + 1
        short = step.action.split(".", 1)[-1]
        obs = step.observation or ""
        path = step.args.get("path") if isinstance(step.args, dict) else None
        if short in ("write_file", "edit_file") and path and not obs.startswith("Error"):
            rel = path.replace("\\", "/").lstrip("./")
            self.writes[rel] = self.writes.get(rel, 0) + 1
            self.written += 1
            if self.listed and rel not in self.listed:
                self.outside += 1
            h = _hash(self.workspace / rel)
            if h and h not in self.seen_hashes.setdefault(rel, []):
                self.seen_hashes[rel].append(h)
                self._progress()
        if short == "run_command":
            cmd = str(step.args.get("command", ""))
            m = _EXIT_RE.search(obs)
            failed = obs.startswith("Error") or (m is not None and m.group(1) != "0")
            if failed:
                sig = _signature_key(obs)
                self.failures[sig] = self.failures.get(sig, 0) + 1
                if cmd not in self.failed_commands:
                    self.failed_commands.append(cmd)
            elif m and cmd in self.failed_commands:
                self.failed_commands.remove(cmd)       # it failed before and passes now
                self._progress()

    def observe_check(self, ok: bool, output: str) -> None:
        if ok:
            self._progress()
        else:
            sig = _signature_key(output)
            self.failures[sig] = self.failures.get(sig, 0) + 1

    # ------------------------------------------------------------ scores

    def scores(self) -> dict:
        s = {
            "repeated_edit": repeated_edit(max(self.writes.values(), default=0)),
            "same_failure": same_failure(max(self.failures.values(), default=0)),
            "no_progress": no_progress(self.since_progress),
            "repeated_action": repeated_action(max(self.actions.values(), default=0)),
            "scope_drift": scope_drift(self.outside, self.written),
        }
        s["stuck"] = combine(s)
        s["drift"] = s["scope_drift"]
        self.peak = max(self.peak, s["stuck"])
        s["peak_stuck"] = round(self.peak, 4)
        s["reasons"] = self.reasons()
        return s

    def reasons(self) -> list[str]:
        out = []
        if self.failures and max(self.failures.values()) > 1:
            out.append(f"same failure ×{max(self.failures.values())}")
        if self.since_progress > 3:
            out.append(f"{self.since_progress} steps without progress")
        if self.actions and max(self.actions.values()) > 1:
            out.append(f"same tool call ×{max(self.actions.values())}")
        if self.writes and max(self.writes.values()) > 2:
            f, n = max(self.writes.items(), key=lambda kv: kv[1])
            out.append(f"{f} written ×{n}")
        if self.outside:
            out.append(f"{self.outside} write(s) outside the task's files")
        return out

    def max_same_failure(self) -> int:
        return max(self.failures.values(), default=0)

    def save(self) -> dict:
        """Write counters and scores back onto the task; returns the scores."""
        self.task["sensor_state"] = {
            "writes": self.writes, "failures": self.failures, "actions": self.actions,
            "seen_hashes": self.seen_hashes, "failed_commands": self.failed_commands,
            "since_progress": self.since_progress, "outside": self.outside, "written": self.written,
        }
        self.task["signals"] = self.scores()
        return self.task["signals"]


def describe(signals: dict) -> str:
    parts = [f"stuck {signals.get('stuck', 0):.2f} (peak {signals.get('peak_stuck', 0):.2f})"]
    if signals.get("drift"):
        parts.append(f"drift {signals['drift']:.2f}")
    if signals.get("reasons"):
        parts.append("; ".join(signals["reasons"]))
    return " · ".join(parts)
