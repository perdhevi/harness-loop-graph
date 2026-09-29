"""Chapter F — benchmark: replay a fixed suite of build requests and compare with a baseline.

Every case is built with the normal harness, then checked by a human-written
acceptance script that runs in a *copy* of the finished workspace, through the
same workspace MCP server as everything else. Comparison uses pass rates and
relative changes, never exact text.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from checks import run_check
from runtime import build_registry

ACCEPT_NAME = "_accept.py"
METRICS = ("steps", "tokens_in", "tokens_out", "duration_s", "model_calls")
WATCHED = {"steps": "costlier", "tokens_in": "costlier", "duration_s": "slower"}


class SuiteError(ValueError):
    pass


def load_suite(path: str | Path) -> dict:
    path = Path(path)
    suite = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not suite.get("name"):
        problems.append("suite needs a 'name'")
    ids = set()
    for i, c in enumerate(suite.get("cases", []), 1):
        cid = c.get("id")
        if not cid or cid in ids:
            problems.append(f"case #{i}: missing or duplicate id")
        ids.add(cid)
        if not c.get("request"):
            problems.append(f"case {cid}: missing 'request'")
        script = (c.get("acceptance") or {}).get("script")
        if not script or not (path.parent / script).is_file():
            problems.append(f"case {cid}: acceptance script not found: {script}")
    if not suite.get("cases"):
        problems.append("suite has no cases")
    if problems:
        raise SuiteError("; ".join(problems))
    suite["_dir"] = str(path.parent)
    return suite


def run_acceptance(case: dict, run_dir: Path, config: dict, suite_dir: Path) -> dict:
    """Copy the finished workspace, add the acceptance script, run it through the workspace server."""
    src = Path(run_dir) / "workspace"
    dst = Path(run_dir) / "acceptance"
    if dst.exists():
        shutil.rmtree(dst)
    if src.exists():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    else:
        dst.mkdir(parents=True)
    shutil.copy(Path(suite_dir) / case["acceptance"]["script"], dst / ACCEPT_NAME)
    with contextlib.ExitStack() as stack:
        tools = build_registry(config, dst, stack)
        res = run_check({"command": f"python {ACCEPT_NAME}"}, dst, tools,
                        timeout_s=case["acceptance"].get("timeout_s", 120), output_chars=1500)
    return {"ok": res.ok, "exit_code": res.exit_code, "output": res.output}


def _mean(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else None


def run_suite(suite: dict, *, build_fn: Callable[..., dict], config: dict, label: str, repeat: int = 1,
              only: list[str] | None = None, judge: bool | None = None, out_dir: Path | None = None,
              say: Callable[[str], None] = print) -> dict:
    """Build every case `repeat` times, run its acceptance script, write and return the results."""
    suite_dir = Path(suite["_dir"])
    cases = [c for c in suite["cases"] if not only or c["id"] in only]
    if only and len(cases) != len(set(only)):
        raise SuiteError(f"unknown case(s): {sorted(set(only) - {c['id'] for c in cases})}")
    use_judge = suite.get("judge", True) if judge is None else judge
    rows = []
    for case in cases:
        for attempt in range(1, repeat + 1):
            say(f"[bench] {case['id']} (difficulty {case.get('difficulty', '?')}) — run {attempt}/{repeat}")
            t0 = time.perf_counter()
            row = {"case": case["id"], "attempt": attempt}
            try:
                summary = build_fn(case["request"], judge=use_judge,
                                   tags={"suite": suite["name"], "case": case["id"], "label": label})
            except Exception as e:                   # a build that crashes is a failed case, not a crashed bench
                summary = {"status": "error", "error": f"{type(e).__name__}: {e}",
                           "run_dir": getattr(e, "run_dir", None)}
            m = summary.get("metrics") or {}
            row.update({
                "run_id": Path(summary["run_dir"]).name if summary.get("run_dir") else None,
                "status": summary.get("status"),
                "verdict": (summary.get("verdict") or {}).get("verdict"),
                "steps": summary.get("steps"),
                "tokens_in": m.get("tokens_in"), "tokens_out": m.get("tokens_out"),
                "model_calls": sum((m.get("model_calls") or {}).values()) if m else None,
                "duration_s": round(time.perf_counter() - t0, 2),
                "revisions": summary.get("revisions"),
                "compactions": m.get("compactions"), "sensor_warnings": m.get("sensor_warnings"),
                "error": summary.get("error"),
            })
            if summary.get("run_dir"):
                acc = run_acceptance(case, Path(summary["run_dir"]), config, suite_dir)
            else:
                acc = {"ok": False, "exit_code": None, "output": "no run folder (build failed to start)"}
            row["passed"] = acc["ok"]
            row["acceptance"] = {"exit_code": acc["exit_code"], "output": acc["output"][-600:]}
            say(f"[bench] {case['id']}: {'PASS' if acc['ok'] else 'FAIL'} · {row['status']}"
                f" · {row['steps']} steps · {row['duration_s']}s")
            rows.append(row)

    per_case = {}
    for case in cases:
        rs = [r for r in rows if r["case"] == case["id"]]
        per_case[case["id"]] = {
            "runs": len(rs),
            "pass_rate": round(sum(r["passed"] for r in rs) / len(rs), 4),
            "accepted_rate": round(sum(r["verdict"] == "accept" for r in rs) / len(rs), 4),
            **{k: _mean([r[k] for r in rs]) for k in METRICS},
        }
    results = {
        "suite": suite["name"], "label": label, "repeat": repeat,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": config.get("provider"), "model": config["providers"][config["provider"]].get("model"),
        "judge": use_judge,
        "overall": {"pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 4) if rows else 0,
                    "accepted_rate": round(sum(r["verdict"] == "accept" for r in rows) / len(rows), 4) if rows else 0,
                    "cases": len(cases), "runs": len(rows)},
        "cases": per_case,
        "rows": rows,
    }
    out_dir = Path(out_dir or suite_dir / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{suite['name']}-{label}-{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"{suite['name']}-{label}-{stamp}-{n}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    results["_path"] = str(path)
    return results


def baseline_path(suite: dict, label: str) -> Path:
    return Path(suite["_dir"]) / "baselines" / f"{suite['name']}-{label}.json"


def save_baseline(results: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in results.items() if not k.startswith("_")}
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _n(v) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def compare(now: dict, base: dict, *, tolerance: float = 0.0, metric_tolerance: float = 0.25) -> dict:
    """Regressions fail (pass rate drops); cost/speed changes are warnings or improvements."""
    report = {"regressions": [], "warnings": [], "improvements": [], "new": [], "missing": []}
    for cid, c in now["cases"].items():
        b = base["cases"].get(cid)
        if b is None:
            report["new"].append(cid)
            continue
        drop = b["pass_rate"] - c["pass_rate"]
        if drop > tolerance + 1e-9:
            report["regressions"].append(f"{cid}: pass rate {b['pass_rate']:.0%} → {c['pass_rate']:.0%}")
        elif c["pass_rate"] > b["pass_rate"]:
            report["improvements"].append(f"{cid}: pass rate {b['pass_rate']:.0%} → {c['pass_rate']:.0%}")
        for k, word in WATCHED.items():
            if not b.get(k) or c.get(k) is None:
                continue
            change = (c[k] - b[k]) / b[k]
            if change > metric_tolerance:
                report["warnings"].append(f"{cid}: {word} — {k} {_n(b[k])} → {_n(c[k])} ({change:+.0%})")
            elif change < -metric_tolerance:
                report["improvements"].append(f"{cid}: {k} {_n(b[k])} → {_n(c[k])} ({change:+.0%})")
    report["missing"] = [cid for cid in base["cases"] if cid not in now["cases"]]
    ob, on = base["overall"]["pass_rate"], now["overall"]["pass_rate"]
    if not report["missing"] and ob - on > tolerance + 1e-9:
        report["regressions"].append(f"overall: pass rate {ob:.0%} → {on:.0%}")
    report["ok"] = not report["regressions"]
    return report


def render_results(r: dict) -> str:
    lines = [f"suite {r['suite']} · label {r['label']} · {r['provider']}/{r['model']} · judge {'on' if r['judge'] else 'off'}"
             f" · repeat {r['repeat']}",
             f"{'case':<14}{'pass':>7}{'accepted':>10}{'steps':>8}{'tok in':>9}{'tok out':>9}{'time s':>9}"]
    for cid, c in r["cases"].items():
        lines.append(f"{cid:<14}{c['pass_rate']:>7.0%}{c['accepted_rate']:>10.0%}{_fmt(c['steps']):>8}"
                     f"{_fmt(c['tokens_in']):>9}{_fmt(c['tokens_out']):>9}{_fmt(c['duration_s']):>9}")
    o = r["overall"]
    lines.append(f"{'overall':<14}{o['pass_rate']:>7.0%}{o['accepted_rate']:>10.0%}   ({o['runs']} runs)")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and (v >= 100 or v.is_integer()):
        return f"{v:.0f}"
    return str(v)


def render_compare(report: dict, base_label: str) -> str:
    lines = [f"compared with baseline {base_label}:"]
    marks = {"regressions": "✗", "warnings": "!", "improvements": "✓"}
    for key, mark in marks.items():
        lines += [f"  {mark} {item}" for item in report[key]]
    lines += [f"  + new case (no baseline yet): {cid}" for cid in report["new"]]
    lines += [f"  ? not run this time: {cid}" for cid in report["missing"]]
    if len(lines) == 1:
        lines.append("  no changes beyond tolerance")
    lines.append("result: " + ("OK" if report["ok"] else "REGRESSION"))
    return "\n".join(lines)


def safe_label(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "default"
