"""Chapter J follow-up — put a review run's changes back into the original folder (and undo that)."""

import contextlib
import functools
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import main  # noqa: E402
from apply import ApplyError, apply_run, plan_apply, undo_apply  # noqa: E402
from test_review import ACCEPT, BUG, F1, FIXED, PLAN, TESTS, FakeModel, Harness, act, findings, j  # noqa: E402


def fake_run(tmp: Path, status="accepted") -> tuple[Path, Path]:
    """A finished review run by hand: keep.py unchanged, edit.py changed, new.py added, gone.py deleted."""
    source, run = tmp / "project", tmp / "runs" / "r1"
    files = {"keep.py": "k\n", "edit.py": "old\n", "gone.py": "bye\n", "pkg/deep.py": "d\n"}
    for root in (source, run / "original", run / "workspace"):
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    (run / "workspace" / "edit.py").write_text("new\n")
    (run / "workspace" / "pkg" / "new.py").write_text("added\n")
    (run / "workspace" / "gone.py").unlink()
    (run / "summary.json").write_text(json.dumps({"status": status, "source": str(source)}))
    return source, run


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.source, self.run = fake_run(self.tmp)
        (self.source / "my_notes.txt").write_text("mine")                 # not in the run: never touched

    def test_apply_then_undo(self):
        p = apply_run(self.run)
        self.assertEqual((p["changed"], p["added"], p["deleted"]), (["edit.py"], ["pkg/new.py"], ["gone.py"]))
        self.assertEqual((self.source / "edit.py").read_text(), "new\n")
        self.assertEqual((self.source / "pkg" / "new.py").read_text(), "added\n")
        self.assertFalse((self.source / "gone.py").exists())
        self.assertEqual((self.source / "my_notes.txt").read_text(), "mine")
        self.assertEqual((self.run / "backup" / "edit.py").read_text(), "old\n")
        applied = json.loads((self.run / "summary.json").read_text())["applied"]
        self.assertEqual(applied["changed"], ["edit.py"])
        with self.assertRaisesRegex(ApplyError, "already applied"):
            apply_run(self.run)

        undo_apply(self.run)
        self.assertEqual((self.source / "edit.py").read_text(), "old\n")
        self.assertEqual((self.source / "gone.py").read_text(), "bye\n")
        self.assertFalse((self.source / "pkg" / "new.py").exists())
        self.assertEqual((self.source / "my_notes.txt").read_text(), "mine")
        apply_run(self.run)                                                # can be applied again after undo
        self.assertEqual((self.source / "edit.py").read_text(), "new\n")

    def test_conflicts_write_nothing(self):
        (self.source / "edit.py").write_text("I edited this meanwhile\n")
        (self.source / "pkg" / "new.py").write_text("mine too\n")
        with self.assertRaises(ApplyError) as ctx:
            apply_run(self.run)
        msg = str(ctx.exception)
        self.assertIn("edit.py: changed in your folder since the review copied it", msg)
        self.assertIn("pkg/new.py: the run adds it, but your folder now has a different pkg/new.py", msg)
        self.assertTrue((self.source / "gone.py").exists())                 # nothing at all was written
        self.assertFalse((self.run / "backup").exists())

    def test_only_good_runs_unless_forced(self):
        _, run = fake_run(self.tmp / "x", status="escalated")
        with self.assertRaisesRegex(ApplyError, "ended 'escalated'"):
            apply_run(run)
        self.assertEqual(apply_run(run, force=True)["changed"], ["edit.py"])

    def test_dry_run_and_undo_conflict(self):
        p = apply_run(self.run, dry_run=True)
        self.assertEqual(p["changed"], ["edit.py"])
        self.assertEqual((self.source / "edit.py").read_text(), "old\n")   # dry run: nothing written
        apply_run(self.run)
        (self.source / "edit.py").write_text("edited after apply\n")
        with self.assertRaisesRegex(ApplyError, "edit.py: changed in your folder after apply"):
            undo_apply(self.run)
        undo_apply(self.run, force=True)
        self.assertEqual((self.source / "edit.py").read_text(), "old\n")

    def test_runs_without_a_source_are_refused(self):
        run = self.tmp / "runs" / "built"
        (run / "workspace").mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({"status": "accepted"}))
        with self.assertRaisesRegex(ApplyError, "didn't start from an existing folder"):
            plan_apply(run)

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main.main(["apply", str(self.run), "--dry-run"]), 0)
            self.assertEqual(main.main(["apply", str(self.run)]), 0)
            self.assertEqual(main.main(["apply", str(self.run)]), 1)
            self.assertEqual(main.main(["apply", "--undo", str(self.run)]), 0)
        text = out.getvalue()
        self.assertIn("would change 3 file(s)", text)
        self.assertIn("   M edit.py\n   A pkg/new.py\n   D gone.py", text)
        self.assertIn("[apply] already applied", text)
        self.assertIn("undone   : 2 file(s) restored, 1 added file(s) removed", text)


class ReviewApplyFlagTests(Harness):
    def review(self, *args, replies):
        real = main.run_build
        with mock.patch.object(main, "run_build", functools.partial(real, model=FakeModel(replies), verbose_graph=False)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main.main(["review", str(self.project), "--yes", "--test", TESTS, *args, "fix average"])
        return code, out.getvalue()

    def test_accepted_run_is_applied(self):
        code, out = self.review("--apply", replies=[
            findings(F1), PLAN, act("edit_file", path="stats.py", old=BUG, new=FIXED), j(thought="d", final="ok"), ACCEPT])
        self.assertEqual(code, 0, out)
        self.assertIn(FIXED, (self.project / "stats.py").read_text())
        self.assertIn("apply    : applied 1 file(s)", out)
        self.assertIn("   M stats.py", out)

    def test_escalated_run_is_not_applied(self):
        code, out = self.review("--apply", replies=[
            findings(F1), PLAN, act("edit_file", path="stats.py", old=BUG, new=FIXED), j(thought="d", final="ok"),
            "not json", "still not json"])                                  # the judge fails → escalated
        self.assertIn("apply    : skipped, the run ended 'escalated'", out)
        self.assertIn(BUG, (self.project / "stats.py").read_text())
