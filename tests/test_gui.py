"""Chapter L — the local web UI: a real server on an ephemeral port, jobs driven by scripted models."""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import gui  # noqa: E402
import main  # noqa: E402
import test_rerun  # noqa: E402
import test_review as tr  # noqa: E402  (the review sample, its replies and FakeModel)
from state import load_state  # noqa: E402

REVIEW_FIX = [
    tr.act("read_file", path="stats.py"), tr.findings(tr.F1), tr.PLAN,
    tr.act("edit_file", path="stats.py", old=tr.BUG, new=tr.FIXED), tr.j(thought="d", final="fixed average"),
    tr.ACCEPT,
]


class HookModel(tr.FakeModel):
    """A FakeModel that runs `hook()` just before answering call number `at` (1-based)."""

    def __init__(self, replies, at=0, hook=None):
        super().__init__(replies)
        self.at, self.hook = at, hook

    def complete(self, system, messages):
        if len(self.calls) + 1 == self.at and self.hook:
            self.hook()
        return super().complete(system, messages)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = tr.copy_sample(self.tmp / "project")
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        config["build"]["task_max_iterations"] = 4
        config["review"]["max_iterations"] = 5
        config["memory"]["enabled"] = False
        (self.tmp / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.models: list = []
        real_run_build = self.real_run_build = main.run_build

        def run_build(request, **kw):                     # what the page starts, with a scripted model
            return real_run_build(request, model=self.models.pop(0), **kw)
        for target, value in [("ROOT", self.tmp), ("CONFIG_PATH", self.tmp / "config.json"),
                              ("run_build", run_build)]:
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.server = gui.make_server(self.runs, "127.0.0.1", 0)
        self.app = self.server.app
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.app.closing.set)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    # -- HTTP helpers
    def call(self, path, body=None, headers=None, raw=False):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8")
                return r.status, (text if raw or "json" not in r.headers.get("Content-Type", "") else json.loads(text))
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode("utf-8"))

    def events(self, job_id, since=0):
        """Read the SSE stream to its end; the list of events."""
        req = urllib.request.Request(f"{self.base}/api/jobs/{job_id}/events?since={since}")
        out, data, kind = [], [], None
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                line = line.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    kind = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line:
                    if kind == "end":
                        return out
                    if data:
                        out.append(json.loads("\n".join(data)))
                    data, kind = [], None
        return out

    def start(self, body):
        status, job = self.call("/api/jobs", body)
        self.assertEqual(status, 201, job)
        return job


class JobTests(Server):
    def test_review_job_end_to_end_then_apply_and_undo(self):
        self.models.append(tr.FakeModel(list(REVIEW_FIX)))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            job = self.start({"kind": "review", "path": str(self.project), "request": "average() is wrong",
                              "test_command": tr.TESTS})
            evs = self.events(job["id"])
        self.assertEqual(out.getvalue(), "", "nothing goes to the terminal: say/on_step/on_enter are the page's")
        types = [e["type"] for e in evs]
        self.assertEqual(types[-1], "done", evs[-1])
        self.assertEqual(evs[-1]["status"], "accepted")
        self.assertEqual([e["seq"] for e in evs], list(range(1, len(evs) + 1)))
        nodes = [e["node"] for e in evs if e["type"] == "node"]
        self.assertEqual(nodes[:4], ["intake", "survey", "review", "plan"])
        steps = [e for e in evs if e["type"] == "step"]
        self.assertTrue(any(s["action"] == "workspace.edit_file" for s in steps))
        self.assertTrue(any(e["type"] == "say" and "[run]" in e["text"] for e in evs))
        run_id = evs[-1]["run_id"]

        # a reconnecting page gets only what it missed
        self.assertEqual([e["seq"] for e in self.events(job["id"], since=len(evs) - 2)], [len(evs) - 1, len(evs)])

        status, runs = self.call("/api/runs")
        row = next(r for r in runs["runs"] if r["id"] == run_id)
        self.assertEqual((row["status"], row["kind"], row["tasks_done"], row["tasks"]), ("accepted", "review", 1, 1))
        self.assertTrue(row["actions"]["apply"] and row["actions"]["fix"] and not row["actions"]["resume"])

        status, d = self.call(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(d["apply"]["changed"], ["stats.py"])
        self.assertIn("CHANGES.patch", d["docs"])
        self.assertEqual(d["plan"]["tasks"][0]["status"], "done")
        status, patch = self.call(f"/api/runs/{run_id}/doc/CHANGES.patch", raw=True)
        self.assertIn(f"+    {tr.FIXED}", patch)
        status, trace = self.call(f"/api/runs/{run_id}/trace", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("review", trace)

        status, r = self.call(f"/api/runs/{run_id}/apply", {"dry_run": True})
        self.assertEqual((status, r["dry_run"]), (200, True))
        self.assertIn(tr.BUG, (self.project / "stats.py").read_text(encoding="utf-8"))
        status, r = self.call(f"/api/runs/{run_id}/apply", {})
        self.assertEqual(status, 200, r)
        self.assertIn(tr.FIXED, (self.project / "stats.py").read_text(encoding="utf-8"))
        status, r = self.call(f"/api/runs/{run_id}/apply", {})
        self.assertEqual(status, 400)
        self.assertIn("already applied", r["error"])
        status, r = self.call(f"/api/runs/{run_id}/apply", {"undo": True})
        self.assertEqual(status, 200, r)
        self.assertIn(tr.BUG, (self.project / "stats.py").read_text(encoding="utf-8"))

    def test_stop_leaves_a_resumable_run_that_resumes_from_the_page(self):
        stop = lambda: self.app.runner.current().stop()            # noqa: E731 — the user presses Stop mid-call
        self.models += [HookModel(list(REVIEW_FIX), at=2, hook=stop), tr.FakeModel(list(REVIEW_FIX))]
        with contextlib.redirect_stdout(io.StringIO()):
            job = self.start({"kind": "review", "path": str(self.project), "test_command": tr.TESTS})
            evs = self.events(job["id"])
        last = evs[-1]
        self.assertEqual((last["type"], last["resumable"]), ("stopped", True), last)
        self.assertIn("stopping", [e.get("state") for e in evs if e["type"] == "state"])
        self.assertEqual(len([e for e in evs if e["type"] == "step"]), 2, "stops after the call under way")
        run_id = last["run_id"]
        self.assertEqual(load_state(self.runs / run_id).status, "interrupted")
        status, d = self.call(f"/api/runs/{run_id}")
        self.assertTrue(d["actions"]["resume"])

        with contextlib.redirect_stdout(io.StringIO()):
            job = self.start({"kind": "resume", "run": run_id})
            evs = self.events(job["id"])
        self.assertEqual((evs[-1]["type"], evs[-1].get("status")), ("done", "accepted"), evs[-1])
        self.assertEqual(evs[-1]["run_id"], run_id)

    def test_planner_questions_are_answered_from_the_page(self):
        seen = {}

        def run_build(request, *, ask, say, on_enter, on_step, **kw):
            seen["answers"] = ask(["Which database?", "Need a CLI?"])
            return {"status": "planned", "run_dir": None}
        with mock.patch.object(main, "run_build", run_build):
            job = self.start({"kind": "plan", "request": "a notes app"})
            for _ in range(200):
                if self.app.runner.get(job["id"]).state == "waiting":
                    break
                threading.Event().wait(0.02)
            status, r = self.call(f"/api/jobs/{job['id']}/answers", {"answers": ["sqlite", ""]})
            self.assertEqual(status, 200, r)
            evs = self.events(job["id"])
        self.assertEqual(seen["answers"], ["sqlite", "(no answer — assume something reasonable)"])
        q = next(e for e in evs if e["type"] == "questions")
        self.assertEqual(q["questions"], ["Which database?", "Need a CLI?"])
        self.assertEqual(evs[-1]["type"], "done")
        status, r = self.call(f"/api/jobs/{job['id']}/answers", {"answers": []})
        self.assertEqual(status, 409)

    def test_one_job_at_a_time(self):
        release = threading.Event()

        def run_build(request, **kw):
            release.wait(10)
            return {"status": "finished", "run_dir": None}
        with mock.patch.object(main, "run_build", run_build):
            first = self.start({"kind": "build", "request": "one"})
            status, r = self.call("/api/jobs", {"kind": "build", "request": "two"})
            self.assertEqual(status, 409)
            self.assertIn("already running", r["error"])
            status, s = self.call("/api/status")
            self.assertEqual(s["job"]["id"], first["id"])
            release.set()
            self.assertEqual(self.events(first["id"])[-1]["type"], "done")
            self.start({"kind": "build", "request": "two"})

    def test_failures_are_sentences(self):
        def run_build(request, **kw):
            raise main.ModelError("Ollama isn't running at http://localhost:11434")
        with mock.patch.object(main, "run_build", run_build):
            job = self.start({"kind": "build", "request": "x"})
            last = self.events(job["id"])[-1]
        self.assertEqual((last["type"], last["message"]), ("error", "Ollama isn't running at http://localhost:11434"))
        for body, msg in [({"kind": "build", "request": "  "}, "Say what to build"),
                          ({"kind": "review", "path": str(self.tmp / "nope")}, "not a folder"),
                          ({"kind": "fix", "run": "nope", "request": "x"}, "no run"),
                          ({"kind": "build", "request": "x", "max_iterations": "lots"}, "whole number"),
                          ({"kind": "deploy"}, "kind must be")]:
            status, r = self.call("/api/jobs", body)
            self.assertIn(status, (400, 404), body)
            self.assertIn(msg, r["error"])


class RerunTests(Server):
    def test_rerun_preview_and_job(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run = test_rerun.escalated_run(lambda *a, **kw: self.real_run_build(*a, verbose_graph=False, **kw))
        status, d = self.call(f"/api/runs/{run.name}")
        self.assertTrue(d["actions"]["rerun"])
        tasks = {t["id"]: t for t in d["plan"]["tasks"]}
        self.assertNotIn("rerun", tasks["T1"])
        self.assertEqual(tasks["T2"]["rerun"], {"ok": True, "tasks": ["T2", "T3"]})
        self.assertFalse(tasks["T3"]["rerun"]["ok"])
        self.assertIn("T3 needs T2 (dropped)", tasks["T3"]["rerun"]["error"])
        self.assertEqual(json.loads((run / "plan.json").read_text(encoding="utf-8"))["tasks"][1]["status"],
                         "dropped", "the preview changes nothing")

        status, r = self.call("/api/jobs", {"kind": "rerun", "run": run.name})
        self.assertEqual(status, 400)
        self.assertIn("Say which task", r["error"])

        self.models.append(test_rerun.FakeModel(list(test_rerun.RERUN_T2)))
        with contextlib.redirect_stdout(io.StringIO()):
            job = self.start({"kind": "rerun", "run": run.name, "tasks": ["T2"]})
            evs = self.events(job["id"])
        self.assertEqual(job["request"], "re-run T2")
        self.assertEqual((evs[-1]["type"], evs[-1].get("status"), evs[-1]["run_id"]), ("done", "accepted", run.name))
        self.assertTrue(any(e["type"] == "say" and "re-running: T2, T3" in e["text"] for e in evs))
        status, d = self.call(f"/api/runs/{run.name}")
        self.assertEqual([t["status"] for t in d["plan"]["tasks"]], ["done", "done", "done"])
        self.assertFalse(d["actions"]["rerun"])


    def test_replan_job_with_a_comment(self):
        import test_replan
        with contextlib.redirect_stdout(io.StringIO()):
            run = test_rerun.escalated_run(lambda *a, **kw: self.real_run_build(*a, verbose_graph=False, **kw))
        status, d = self.call(f"/api/runs/{run.name}")
        self.assertEqual({t["id"]: t["replan"] for t in d["plan"]["tasks"]}, {"T1": False, "T2": True, "T3": True})
        status, r = self.call("/api/jobs", {"kind": "replan", "run": run.name, "request": "x"})
        self.assertEqual((status, r["error"]), (400, "Say which task to re-plan."))

        model = test_rerun.FakeModel([
            test_replan.revision(test_rerun.task("R2-1", {"file": "cli.py"}, ["T1"])),
            test_rerun.write("cli.py"), test_rerun.final(), test_rerun.verdict("accept")])
        self.models.append(model)
        with contextlib.redirect_stdout(io.StringIO()):
            job = self.start({"kind": "replan", "run": run.name, "task": "T2", "request": "make it one small CLI task"})
            evs = self.events(job["id"])
        self.assertEqual(job["request"], "re-plan T2: make it one small CLI task")
        self.assertEqual((evs[-1]["type"], evs[-1].get("status")), ("done", "accepted"), evs[-1])
        self.assertIn("make it one small CLI task", model.calls[0]["messages"][0]["content"])
        status, d = self.call(f"/api/runs/{run.name}")
        self.assertEqual({t["id"]: t["status"] for t in d["plan"]["tasks"]},       # T3 now comes after R2-1
                         {"T1": "done", "T2": "dropped", "T3": "dropped", "R2-1": "done"})


class ReadingTests(Server):
    def test_old_and_partial_run_folders(self):
        (self.runs / "20250101-old-run").mkdir()
        (self.runs / "20250101-old-run" / "REPORT.md").write_text("# Old\n", encoding="utf-8")
        (self.runs / "index.jsonl").write_text('{"id": "20250101-old-run", "status": "finished", "tasks": 2}\nnot json\n',
                                               encoding="utf-8")
        (self.runs / "not a run!").mkdir()
        status, r = self.call("/api/runs")
        self.assertEqual([x["id"] for x in r["runs"]], ["20250101-old-run"])
        self.assertEqual(r["runs"][0]["status"], "finished")
        status, d = self.call("/api/runs/20250101-old-run")
        self.assertEqual((status, d["docs"], d["trace"], d["plan"]), (200, ["REPORT.md"], False, None))
        status, e = self.call("/api/runs/20250101-old-run/trace")
        self.assertEqual(status, 404)
        self.assertIn("trace.jsonl", e["error"])

    def test_page_and_status(self):
        status, page = self.call("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("<title>Harness Runs</title>", page)
        status, s = self.call("/api/status")
        self.assertIn("task", s["roles"])
        self.assertIsNone(s["job"])


class GuardTests(Server):
    def test_foreign_host_origin_and_content_type(self):
        status, r = self.call("/api/status", headers={"Host": "evil.example:80"})
        self.assertEqual((status, r["error"]), (403, "unexpected Host header"))
        status, r = self.call("/api/jobs", {"kind": "build", "request": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, r = self.call("/api/jobs", {"kind": "build", "request": "x"}, headers={"Origin": "null"})
        self.assertEqual(status, 403)
        status, r = self.call("/api/jobs", b'{"kind": "build", "request": "x"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(self.app.runner.jobs, {})

    def test_paths_stay_inside_runs(self):
        (self.tmp / "secret").mkdir()
        (self.tmp / "secret" / "SPEC.md").write_text("secret", encoding="utf-8")
        (self.runs / "r1").mkdir()
        (self.runs / "r1" / "state.json").write_text("{}", encoding="utf-8")
        for path in ["/api/runs/..%2Fsecret/doc/SPEC.md", "/api/runs/%2E%2E/doc/SPEC.md", "/api/runs/r1/doc/state.json",
                     "/api/runs/r1/doc/..%2F..%2Fconfig.json"]:
            status, r = self.call(path)
            self.assertEqual(status, 404, path)
        with self.assertRaises(gui.GuiError):
            gui.run_path(self.runs, "../secret")
        self.assertEqual(gui.run_path(self.runs, "r1"), (self.runs / "r1").resolve())


class ExportTests(Server):
    def setUp(self):
        super().setUp()
        run = self.runs / "r1"
        for rel, text in [("state.json", '{"status": "accepted"}'), ("REPORT.md", "# Report\n"),
                          ("workspace/hangman.py", "print('héllo')\n"), ("workspace/sub/words.txt", "python\n"),
                          ("workspace/__pycache__/hangman.cpython-314.pyc", "x"), ("workspace/.pytest_cache/v", "x"),
                          ("workspace/old.pyc", "x")]:
            (run / rel).parent.mkdir(parents=True, exist_ok=True)
            (run / rel).write_bytes(text.encode("utf-8"))
        (self.runs / "empty").mkdir()
        (self.runs / "empty" / "state.json").write_text("{}", encoding="utf-8")

    def download(self, path):
        import zipfile
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            data, headers = r.read(), r.headers
        z = zipfile.ZipFile(io.BytesIO(data))
        return headers, {n: z.read(n) for n in z.namelist()}

    def test_project_and_whole_run(self):
        headers, files = self.download("/api/runs/r1/export?what=project")
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn('filename="r1-project.zip"', headers["Content-Disposition"])
        self.assertEqual(files, {"r1-project/hangman.py": "print('héllo')\n".encode("utf-8"),
                                 "r1-project/sub/words.txt": b"python\n"})
        headers, files = self.download("/api/runs/r1/export?what=run")
        self.assertIn('filename="r1.zip"', headers["Content-Disposition"])
        self.assertEqual(sorted(files), ["r1/REPORT.md", "r1/state.json", "r1/workspace/hangman.py",
                                         "r1/workspace/sub/words.txt"])
        _, files = self.download("/api/runs/r1/export")                    # project is the default
        self.assertIn("r1-project/hangman.py", files)

    def test_what_can_be_exported_and_refusals(self):
        self.assertTrue(self.call("/api/runs/r1")[1]["export"]["project"])
        self.assertFalse(self.call("/api/runs/empty")[1]["export"]["project"])
        for path, code, msg in [("/api/runs/empty/export", 404, "no generated files"),
                                ("/api/runs/r1/export?what=secrets", 400, "what=project"),
                                ("/api/runs/..%2Fr1/export", 404, "")]:
            status, r = self.call(path)
            self.assertEqual(status, code, path)
            self.assertIn(msg, r["error"])
        job = gui.Job("j1", "build", {"request": "x"})                      # a run still going
        job.run_dir = str(self.runs / "r1")
        self.app.runner.jobs[job.id] = job
        status, r = self.call("/api/runs/r1/export")
        self.assertEqual(status, 409)
        self.assertIn("still going", r["error"])


class RunBuildCallbackTests(unittest.TestCase):
    def test_callbacks_replace_terminal_output(self):
        said = []
        with mock.patch.object(main, "run_pipeline", return_value={"status": "x"}) as rp, \
                mock.patch.object(main, "make_role_models", return_value={"task": object()}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            enter = lambda n, s: None                             # noqa: E731
            main.run_build("r", say=said.append, on_step=print, on_enter=enter, config={"provider": "ollama",
                           "providers": {"ollama": {"model": "m"}}, "memory": {"enabled": False}})
        self.assertIs(rp.call_args.kwargs["on_enter"], enter)
        self.assertEqual(rp.call_args.args[0].say, said.append)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
