"""Chapter J — put a review run's changes back into the original folder.

    apply_run(run_dir)   copy the changed and added files back, delete the deleted ones
    undo_apply(run_dir)  put the folder back the way it was before apply

Safety:
- only runs that ended well ("accepted", or "finished" without a judge), unless forced
- a file the user changed after the import is a conflict: nothing is written, the conflicts are listed
- every file that gets changed or deleted is backed up to runs/<id>/backup/ first
- summary.json records what was applied, so a run isn't applied twice by accident
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from review import _files

GOOD_STATUSES = {"accepted", "finished"}


class ApplyError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(run_dir: Path) -> tuple[dict, Path]:
    summary_file = run_dir / "summary.json"
    if not summary_file.exists():
        raise ApplyError(f"no summary.json in {run_dir}: the run hasn't finished")
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    if not summary.get("source") or not (run_dir / "original").is_dir():
        raise ApplyError(f"{run_dir} didn't start from an existing folder (python main.py review …); "
                         f"its project is in {run_dir / 'workspace'}")
    return summary, Path(summary["source"])


def _save(run_dir: Path, summary: dict) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read(p: Path) -> bytes | None:
    return p.read_bytes() if p.is_file() else None


def plan_apply(run_dir: str | Path) -> dict:
    """What apply would do: {source, changed, added, deleted, conflicts}. Changes nothing."""
    run_dir = Path(run_dir).resolve()
    summary, source = _load(run_dir)
    if not source.is_dir():
        raise ApplyError(f"the original folder is gone: {source}")
    before, after = _files(run_dir / "original"), _files(run_dir / "workspace")
    out = {"source": str(source), "status": summary.get("status"), "changed": [], "added": [], "deleted": [],
           "conflicts": [], "applied": summary.get("applied")}
    for rel in sorted(set(before) | set(after)):
        old = before[rel].read_bytes() if rel in before else None
        new = after[rel].read_bytes() if rel in after else None
        if old == new:
            continue
        now = _read(source / rel)                      # what the user's folder has today
        if old is None:                                # the run added this file
            if now is not None and now != new:
                out["conflicts"].append(f"{rel}: the run adds it, but your folder now has a different {rel}")
            elif now is None:
                out["added"].append(rel)
            continue
        if now != old:
            what = "deleted" if now is None else "changed"
            out["conflicts"].append(f"{rel}: {what} in your folder since the review copied it")
            continue
        out["deleted" if new is None else "changed"].append(rel)
    return out


def apply_run(run_dir: str | Path, *, force: bool = False, dry_run: bool = False) -> dict:
    """Write the run's changes into the original folder. Returns the plan that was applied."""
    run_dir = Path(run_dir).resolve()
    summary, source = _load(run_dir)
    if summary.get("status") not in GOOD_STATUSES and not force:
        raise ApplyError(f"the run ended '{summary.get('status')}', not accepted or finished; read REPORT.md, "
                         "and use --force if you still want its changes")
    applied = summary.get("applied")
    if applied and not applied.get("undone_at"):
        raise ApplyError(f"already applied at {applied['at']}; `python main.py apply --undo {run_dir}` first")
    p = plan_apply(run_dir)
    if p["conflicts"]:
        raise ApplyError("nothing was written; these files changed in your folder since the review started:\n"
                         + "\n".join(f"  - {c}" for c in p["conflicts"])
                         + f"\nCompare them with {run_dir / 'CHANGES.patch'}, or start a new review.")
    if dry_run or not (p["changed"] or p["added"] or p["deleted"]):
        return p
    backup = run_dir / "backup"
    if backup.exists():
        shutil.rmtree(backup)
    for rel in p["changed"] + p["deleted"]:               # back up first, so a half-written apply can be undone
        (backup / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, backup / rel)
    for rel in p["changed"] + p["added"]:
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_dir / "workspace" / rel, source / rel)
    for rel in p["deleted"]:
        (source / rel).unlink()
    summary["applied"] = {"at": _now(), "to": str(source), "changed": p["changed"], "added": p["added"],
                          "deleted": p["deleted"], "backup": str(backup)}
    _save(run_dir, summary)
    return p


def undo_apply(run_dir: str | Path, *, force: bool = False) -> dict:
    """Put back what apply changed. Files edited again after apply are conflicts unless forced."""
    run_dir = Path(run_dir).resolve()
    summary, source = _load(run_dir)
    applied = summary.get("applied")
    if not applied or applied.get("undone_at"):
        raise ApplyError("this run's changes aren't applied, so there is nothing to undo")
    backup = Path(applied["backup"])
    ws = run_dir / "workspace"
    conflicts = [f"{rel}: changed in your folder after apply"
                 for rel in applied["changed"] + applied["added"]
                 if _read(source / rel) is not None and _read(source / rel) != _read(ws / rel)]
    conflicts += [f"{rel}: apply deleted it, and your folder has a {rel} again"
                  for rel in applied["deleted"] if (source / rel).exists()]
    if conflicts and not force:
        raise ApplyError("nothing was undone; these files changed after apply:\n"
                         + "\n".join(f"  - {c}" for c in conflicts) + "\nUse --force to restore them anyway.")
    for rel in applied["changed"] + applied["deleted"]:
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / rel, source / rel)
    for rel in applied["added"]:
        if (source / rel).is_file():
            (source / rel).unlink()
    applied["undone_at"] = _now()
    _save(run_dir, summary)
    return applied
