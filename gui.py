"""Chapter L — a local web UI around the same run_build() the CLI uses.

    python main.py gui [--port 8765] [--host 127.0.0.1] [--no-browser]

One job at a time runs on a background thread. Its callbacks (say, on_enter, on_step, ask)
become numbered events that the page reads over Server-Sent Events. Everything else is read
from the run folders, so runs started here and runs started from the CLI look the same.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import main
from apply import ApplyError, apply_run, plan_apply, undo_apply
from model_adapter import ModelError
from models import describe_roles
from pipeline import RESUMABLE_ENDED, RunStopped
from planner import REOPENABLE, PlanError, reopen_tasks
from tools.mcp_client import McpError
from trace_view import load_events, render

WEB_DIR = Path(__file__).parent / "web"
DOCS = ("SPEC.md", "REPORT.md", "REVIEW.md", "CHANGES.patch", "plan.json")
EXPORT_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules", ".git"}
EXPORT_SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp")
KINDS = ("build", "plan", "review", "resume", "fix", "rerun", "replan")
RUN_ID = re.compile(r"^[A-Za-z0-9][\w.-]*$")
MAX_BODY = 1_000_000
KEEPALIVE_S = 15.0
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class Stop(KeyboardInterrupt):
    """Raised inside the job's callbacks when the user presses Stop; the pipeline saves 'interrupted'."""


class GuiError(ValueError):
    """A request the UI can't do; the message is shown as is (HTTP 400 unless `status` says otherwise)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _short(text, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f" … (+{len(text) - limit} chars)"


def step_event(step) -> dict:
    """A loop Step as a small JSON event (long thoughts and observations are cut)."""
    d = asdict(step)
    return {"n": d["n"], "thought": _short(d["thought"], 800), "action": d["action"],
            "args": _short(json.dumps(d["args"], ensure_ascii=False), 600) if d["action"] else None,
            "observation": _short(d["observation"], 2000), "final": _short(d["final"], 2000),
            "error": _short(d["error"], 400), "raw": _short(d["raw"], 400) if d["error"] else None}


# ---------------------------------------------------------------- jobs

class Job:
    """One run on a background thread; every callback becomes an event in `events`."""

    def __init__(self, job_id: str, kind: str, params: dict):
        self.id, self.kind, self.params = job_id, kind, params
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.state = "running"          # running | waiting | stopping | done | stopped | error
        self.run_dir: str | None = params.get("run_dir")
        self.node: str | None = None
        self.task: str | None = None
        self.started = time.time()
        self._stop = threading.Event()
        self._answers: list[str] | None = None
        self._answered = threading.Event()
        self.thread: threading.Thread | None = None

    # -- events
    def emit(self, type_: str, **data) -> None:
        with self.cond:
            self.events.append({"seq": len(self.events) + 1, "type": type_, "ts": time.time(), **data})
            self.cond.notify_all()

    @property
    def finished(self) -> bool:
        return self.state in ("done", "stopped", "error")

    def info(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "run_dir": self.run_dir,
                "run_id": Path(self.run_dir).name if self.run_dir else None, "node": self.node, "task": self.task,
                "request": self.params.get("request"), "events": len(self.events), "started": self.started}

    # -- control
    def stop(self) -> None:
        self._stop.set()
        if not self.finished:
            self.state = "stopping"
            self.emit("state", state="stopping")
        self._answered.set()

    def answer(self, answers: list[str]) -> None:
        if self.state != "waiting":
            raise GuiError("the planner isn't waiting for answers", 409)
        self._answers = answers
        self._answered.set()

    def _check(self) -> None:
        if self._stop.is_set():
            raise Stop()

    # -- callbacks given to run_build
    def say(self, msg) -> None:
        self.emit("say", text=str(msg))

    def on_enter(self, name: str, state) -> None:
        self._check()
        self.node, self.task = name, getattr(state, "current_task", None)
        self.run_dir = getattr(state, "run_dir", None) or self.run_dir
        self.emit("node", node=name, task=self.task, run_id=Path(self.run_dir).name if self.run_dir else None,
                  status=getattr(state, "status", None))

    def on_step(self, step) -> None:
        self.emit("step", task=self.task, **step_event(step))
        self._check()

    def ask(self, questions: list[str]) -> list[str]:
        self._check()
        self._answers = None
        self._answered.clear()
        self.state = "waiting"
        self.emit("questions", questions=list(questions))
        self._answered.wait()
        self._check()
        self.state = "running"
        answers = list(self._answers or [])
        answers += [""] * (len(questions) - len(answers))
        answers = [a.strip() or "(no answer — assume something reasonable)" for a in answers[:len(questions)]]
        self.emit("answers", answers=answers)
        return answers

    # -- the run
    def kwargs(self) -> dict:
        p = self.params
        kw = {"ask": self.ask if p.get("ask", True) else None, "say": self.say, "on_step": self.on_step,
              "on_enter": self.on_enter, "judge": False if p.get("no_judge") else None}
        if p.get("max_iterations"):
            kw["max_iterations"] = int(p["max_iterations"])
        if self.kind == "plan":
            kw["review"] = True
        elif self.kind == "review":
            kw.update(source=p["path"], only_review=bool(p.get("only_review")),
                      test_command=p.get("test_command") or None)
        elif self.kind == "resume":
            kw["from_run"] = p["run_dir"]
        elif self.kind == "fix":
            kw.update(from_run=p["run_dir"], fix=p["request"])
        elif self.kind == "replan":
            kw.update(from_run=p["run_dir"], replan=p["task"], fix=p.get("comment") or "")
        elif self.kind == "rerun":
            kw.update(from_run=p["run_dir"], rerun=p["tasks"], rerun_dependents=p.get("dependents", True) is not False)
        return kw

    def run(self) -> None:
        request = None if self.kind in ("resume", "fix", "rerun", "replan") else self.params["request"]
        try:
            summary = main.run_build(request, **self.kwargs())
        except RunStopped as e:
            self.run_dir = e.run_dir or self.run_dir
            user = isinstance(e.cause, KeyboardInterrupt)
            self.state = "stopped" if user else "error"
            self.emit("stopped" if user else "error", run_id=self._run_id(), resumable=bool(e.run_dir),
                      message="Stopped. The run can be resumed." if user else f"{type(e.cause).__name__}: {e}")
        except (ModelError, McpError, PlanError, FileNotFoundError, GuiError) as e:
            self.state = "error"
            self.emit("error", run_id=self._run_id(), resumable=False, message=str(e))
        except Exception as e:  # noqa: BLE001 — the page shows a sentence, the server keeps running
            self.state = "error"
            self.emit("error", run_id=self._run_id(), resumable=False, message=f"{type(e).__name__}: {e}")
        else:
            self.run_dir = summary.get("run_dir") or self.run_dir
            self.state = "done"
            self.emit("done", run_id=self._run_id(), status=summary.get("status"),
                      summary=json.loads(json.dumps(summary, default=str)))

    def _run_id(self) -> str | None:
        return Path(self.run_dir).name if self.run_dir else None


class JobRunner:
    """Starts jobs, one at a time (one GPU, one model), and keeps finished ones for their events."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._n = 0

    def current(self) -> Job | None:
        return next((j for j in self.jobs.values() if not j.finished), None)

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise GuiError(f"no job {job_id!r} (jobs are forgotten when the server restarts)", 404)
        return job

    def validate(self, params: dict) -> tuple[str, dict]:
        kind = params.get("kind")
        if kind not in KINDS:
            raise GuiError(f"kind must be one of {', '.join(KINDS)}")
        p = dict(params)
        p["request"] = " ".join(str(p.get("request") or "").split())
        if kind in ("build", "plan") and not p["request"]:
            raise GuiError("Say what to build.")
        if kind == "fix" and not p["request"]:
            raise GuiError("Say what should change.")
        if kind == "review":
            path = Path(str(p.get("path") or "")).expanduser()
            if not str(p.get("path") or "").strip() or not path.is_dir():
                raise GuiError(f"not a folder: {p.get('path') or '(empty)'}")
            p["path"] = str(path.resolve())
            p["request"] = p["request"] or "Review the project for bugs and fix what you find."
        if kind in ("resume", "fix", "rerun", "replan"):
            p["run_dir"] = str(run_path(self.runs_dir, str(p.get("run") or "")))
        if kind == "replan":
            if not isinstance(p.get("task"), str) or not p["task"].strip():
                raise GuiError("Say which task to re-plan.")
            p["task"] = p["task"].strip()
            p["comment"] = p["request"]
            p["request"] = f"re-plan {p['task']}" + (f": {p['comment']}" if p["comment"] else "")
        if kind == "rerun":
            tasks = p.get("tasks")
            tasks = tasks.replace(",", " ").split() if isinstance(tasks, str) else tasks
            if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and t.strip() for t in tasks):
                raise GuiError("Say which task(s) to re-run, e.g. [\"T3\"].")
            p["tasks"] = [t.strip() for t in tasks]
            p["request"] = f"re-run {', '.join(p['tasks'])}"
        if p.get("max_iterations") not in (None, ""):
            try:
                if int(p["max_iterations"]) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                raise GuiError("max iterations must be a whole number above 0") from None
        return kind, p

    def start(self, params: dict, *, target=None) -> Job:
        kind, p = self.validate(params)
        with self.lock:
            busy = self.current()
            if busy:
                raise GuiError(f"A {busy.kind} is already running ({busy.info()['run_id'] or 'starting'}). "
                               "Wait for it or stop it first.", 409)
            self._n += 1
            job = Job(f"{time.strftime('%H%M%S')}-{self._n}", kind, p)
            self.jobs[job.id] = job
            job.thread = threading.Thread(target=target or job.run, name=f"job-{job.id}", daemon=True)
            job.emit("state", state="running", kind=kind)
            job.thread.start()
        return job

    def stop_all(self) -> None:
        for job in self.jobs.values():
            if not job.finished:
                job.stop()


# ---------------------------------------------------------------- reading run folders

def run_path(runs_dir: Path, run_id: str) -> Path:
    """The folder of a run, by id; only folders directly inside runs/."""
    run_id = Path(run_id.replace("\\", "/")).name if run_id.strip() else ""
    if not RUN_ID.match(run_id):
        raise GuiError(f"not a run id: {run_id!r}", 404)
    p = (runs_dir / run_id).resolve()
    if p.parent != runs_dir.resolve() or not p.is_dir():
        raise GuiError(f"no run {run_id!r} in {runs_dir}", 404)
    return p


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _index(runs_dir: Path) -> dict[str, dict]:
    rows = {}
    try:
        for line in (runs_dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                rows[row["id"]] = row
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return rows


def _actions(state: dict, run: Path) -> dict:
    status, nxt = state.get("status"), state.get("next")
    summary = _read_json(run / "summary.json") or {}
    finished = bool(state.get("plan")) and not nxt and status not in RESUMABLE_ENDED | {"planned", "plan_failed"}
    return {"resume": bool(nxt) or status in RESUMABLE_ENDED,
            "rerun": finished and any(t.get("status") in REOPENABLE for t in state["plan"].get("tasks") or []),
            "build_plan": status == "planned" and bool(nxt),
            "fix": bool(state.get("plan")) and not nxt and status not in RESUMABLE_ENDED | {"planned", "plan_failed"},
            "apply": bool(summary.get("source")) and (run / "original").is_dir()}


def list_runs(runs_dir: Path, limit: int = 200) -> list[dict]:
    if not runs_dir.is_dir():
        return []
    index = _index(runs_dir)
    out = []
    for run in sorted((p for p in runs_dir.iterdir() if p.is_dir() and RUN_ID.match(p.name)),
                      key=lambda p: p.name, reverse=True)[:limit]:
        state = _read_json(run / "state.json") or {}
        row = index.get(run.name, {})
        plan = state.get("plan") or {}
        tasks = plan.get("tasks") or []
        out.append({
            "id": run.name,
            "request": _short(state.get("request") or row.get("request"), 200),
            "status": state.get("status") or row.get("status") or "unknown",
            "kind": "review" if (state.get("options") or {}).get("source") else "build",
            "updated_at": state.get("updated_at") or row.get("ts"),
            "model": row.get("model"),
            "tasks": len(tasks) or row.get("tasks"),
            "tasks_done": sum(1 for t in tasks if t.get("status") == "done") if tasks else row.get("tasks_done"),
            "duration_s": row.get("duration_s"),
            "actions": _actions(state, run),
        })
    return out


def run_detail(runs_dir: Path, run_id: str) -> dict:
    run = run_path(runs_dir, run_id)
    state = _read_json(run / "state.json") or {}
    summary = _read_json(run / "summary.json")
    plan = state.get("plan") or _read_json(run / "plan.json")
    out = {"id": run.name, "path": str(run), "request": state.get("request"), "status": state.get("status", "unknown"),
           "next": state.get("next"), "current_task": state.get("current_task"), "error": state.get("error"),
           "updated_at": state.get("updated_at"), "revisions": state.get("revisions"),
           "source": (state.get("options") or {}).get("source"),
           "plan": None, "summary": summary, "docs": [d for d in DOCS if (run / d).is_file()],
           "trace": (run / "trace.jsonl").is_file(), "actions": _actions(state, run), "apply": None,
           "export": {"project": any(_export_files(run / "workspace"))}}
    if plan:
        out["plan"] = {"title": plan.get("title"), "summary": plan.get("summary"), "tasks": [
            {"id": t.get("id"), "title": t.get("title"), "status": t.get("status", "todo"), "steps": t.get("steps"),
             "files": t.get("files"), "done_when": t.get("done_when"), "verified": t.get("verified")}
            for t in plan.get("tasks") or []]}
        for t in out["plan"]["tasks"]:                  # Chapter G: --replan works on unfinished tasks
            t["replan"] = out["actions"]["fix"] and t["status"] != "done"
        if out["actions"]["rerun"]:                     # what a re-run of each task would reopen, or why not
            for t in out["plan"]["tasks"]:
                if t["status"] in REOPENABLE:
                    try:
                        t["rerun"] = {"ok": True, "tasks": reopen_tasks(json.loads(json.dumps(plan)), [t["id"]])}
                    except PlanError as e:
                        t["rerun"] = {"ok": False, "error": str(e)}
    if out["actions"]["apply"]:
        try:
            out["apply"] = plan_apply(run)
        except (ApplyError, OSError, ValueError) as e:
            out["apply"] = {"error": str(e)}
    return json.loads(json.dumps(out, default=str))


def run_doc(runs_dir: Path, run_id: str, name: str) -> str:
    if name not in DOCS:
        raise GuiError(f"unknown document {name!r}; one of {', '.join(DOCS)}", 404)
    path = run_path(runs_dir, run_id) / name
    if not path.is_file():
        raise GuiError(f"{run_id} has no {name}", 404)
    return path.read_text(encoding="utf-8", errors="replace")


def run_trace(runs_dir: Path, run_id: str) -> str:
    try:
        return render(load_events(run_path(runs_dir, run_id)), steps=True)
    except FileNotFoundError as e:
        raise GuiError(str(e), 404) from None


def do_apply(runs_dir: Path, run_id: str, body: dict) -> dict:
    run = run_path(runs_dir, run_id)
    try:
        if body.get("undo"):
            return {"undone": undo_apply(run, force=bool(body.get("force")))}
        return {"applied": apply_run(run, force=bool(body.get("force")), dry_run=bool(body.get("dry_run"))),
                "dry_run": bool(body.get("dry_run"))}
    except (ApplyError, FileNotFoundError, ValueError) as e:
        raise GuiError(str(e)) from None


def _export_files(root: Path):
    """(path, name in the zip) for every file under `root`, without caches, virtualenvs or links."""
    if not root.is_dir():
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXPORT_SKIP_DIRS and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if name.endswith(EXPORT_SKIP_SUFFIXES) or path.is_symlink() or not path.is_file():
                continue
            yield path, path.relative_to(root).as_posix()


def export_zip(runs_dir: Path, run_id: str, what: str = "project"):
    """A zip of the generated project (`project`: workspace/) or the whole run folder (`run`).

    Returns (file name, an open temporary file positioned at 0, size). Files go under one top folder,
    so unzipping never spills into the user's Downloads folder.
    """
    run = run_path(runs_dir, run_id)
    if what not in ("project", "run"):
        raise GuiError("export what=project (the generated code) or what=run (everything in the run folder)")
    root, top = (run / "workspace", f"{run.name}-project") if what == "project" else (run, run.name)
    out = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path, rel in _export_files(root):
            z.write(path, f"{top}/{rel}")
            n += 1
    if not n:
        out.close()
        raise GuiError(f"{run.name} has no generated files to export" if what == "project"
                       else f"{run.name} is empty", 404)
    size = out.tell()
    out.seek(0)
    return f"{top}.zip", out, size


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "harness-gui"
    app: "App"                          # set by make_server

    def log_message(self, fmt, *args):  # quiet; the page shows what matters
        pass

    # -- guards
    def _host_ok(self) -> bool:
        allowed = self.app.allowed_hosts
        return allowed is None or (self.headers.get("Host") or "").lower() in allowed

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:                  # same-origin fetches from our page may omit it; "null" is refused below
            return True
        return urlsplit(origin).netloc.lower() == (self.headers.get("Host") or "").lower()

    # -- replies
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _text(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _file(self, name: str, f, size: int) -> None:
        """Send an open file as a download, then close it."""
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile)

    def _query(self) -> dict:
        return dict(q.split("=", 1) for q in urlsplit(self.path).query.split("&") if "=" in q)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise GuiError("send JSON (Content-Type: application/json)", 415)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise GuiError("request too large", 413)
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise GuiError("the request body isn't valid JSON") from None
        if not isinstance(data, dict):
            raise GuiError("the request body must be a JSON object")
        return data

    # -- routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        if not self._host_ok():
            return self._error(403, "unexpected Host header")
        if method == "POST" and not self._origin_ok():
            return self._error(403, "requests from other sites are refused")
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        try:
            self._dispatch(method, parts)
        except GuiError as e:
            self._error(e.status, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            self._error(500, f"{type(e).__name__}: {e}")

    def _dispatch(self, method: str, parts: list[str]) -> None:
        app = self.app
        if method == "GET":
            if parts in ([], ["index.html"]):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                              "style-src 'self' 'unsafe-inline'; img-src 'self' data:"})
            if parts == ["api", "status"]:
                return self._json(app.status())
            if parts == ["api", "runs"]:
                return self._json({"runs": list_runs(app.runs_dir)})
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(run_detail(app.runs_dir, parts[2]))
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "doc":
                return self._text(run_doc(app.runs_dir, parts[2], parts[4]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "trace":
                return self._text(run_trace(app.runs_dir, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "export":
                busy = app.runner.current()
                if busy and busy.run_dir and Path(busy.run_dir).name == parts[2]:
                    raise GuiError("that run is still going; export it when it has finished or been stopped", 409)
                return self._file(*export_zip(app.runs_dir, parts[2], self._query().get("what", "project")))
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "events":
                return self._events(app.runner.get(parts[2]))
        if method == "POST":
            if parts == ["api", "jobs"]:
                return self._json(app.runner.start(self._body()).info(), 201)
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "answers":
                body = self._body()
                answers = body.get("answers")
                if not isinstance(answers, list):
                    raise GuiError("answers must be a list of strings")
                app.runner.get(parts[2]).answer([str(a) for a in answers])
                return self._json({"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop":
                self._body()
                job = app.runner.get(parts[2])
                job.stop()
                return self._json(job.info())
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "apply":
                body = self._body()
                busy = app.runner.current()
                if busy and busy.run_dir and Path(busy.run_dir).name == parts[2]:
                    raise GuiError("that run is still going", 409)
                return self._json(do_apply(app.runs_dir, parts[2], body))
        raise GuiError(f"no such endpoint: {method} {self.path}", 404)

    def _events(self, job: Job) -> None:
        """Server-Sent Events: every event after Last-Event-ID (or ?since=), then one 'end' once the job is over."""
        try:
            since = int(self.headers.get("Last-Event-ID") or self._query().get("since", 0))
        except ValueError:
            since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sent = max(0, since)
        while not self.app.closing.is_set():
            with job.cond:
                if len(job.events) <= sent and not job.finished:
                    job.cond.wait(KEEPALIVE_S)
                new = job.events[sent:]
                over = job.finished and sent + len(new) >= len(job.events)
            chunk = "".join(f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n" for e in new)
            sent += len(new)
            if over:
                chunk += "event: end\ndata: {}\n\n"
            self.wfile.write((chunk or ": keep-alive\n\n").encode("utf-8"))
            self.wfile.flush()
            if over:
                return


class App:
    def __init__(self, runs_dir: Path, *, host: str, port: int, config_loader=None):
        self.runs_dir = Path(runs_dir)
        self.runner = JobRunner(self.runs_dir)
        self.closing = threading.Event()
        self.config_loader = config_loader or (lambda: main.load_json(main.CONFIG_PATH))
        self.allowed_hosts = None if host in ("0.0.0.0", "::", "") else {
            h for name in {*LOCAL_HOSTS, host.lower()} for h in (name, f"{name}:{port}")}

    def status(self) -> dict:
        try:
            config = self.config_loader()
            roles, provider, problem = describe_roles(config), config.get("provider"), None
        except (OSError, ValueError, KeyError) as e:
            roles, provider, problem = {}, None, f"config.json: {e}"
        job = self.runner.current() or max(self.runner.jobs.values(), key=lambda j: j.started, default=None)
        return {"roles": roles, "provider": provider, "config_error": problem, "runs_dir": str(self.runs_dir),
                "job": job.info() if job else None}


def make_server(runs_dir: Path, host: str = "127.0.0.1", port: int = 8765, config_loader=None) -> ThreadingHTTPServer:
    """The HTTP server; `server.app` holds the runner. Port 0 picks a free port (tests)."""
    handler = type("BoundHandler", (Handler,), {})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    handler.app = server.app = App(runs_dir, host=host, port=server.server_address[1], config_loader=config_loader)
    return server


def main_gui(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="main.py gui", description="The local web UI (Chapter L)")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args(argv)
    config = main.load_json(main.CONFIG_PATH)
    runs_dir = main.ROOT / config.get("build", {}).get("runs_dir", "runs")
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        server = make_server(runs_dir, args.host, args.port)
    except OSError as e:
        print(f"[error] can't listen on {args.host}:{args.port}: {e.strerror or e} (try --port {args.port + 1})")
        return 1
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[warning] listening on {args.host}: anyone who can reach this port can start builds that run "
              "commands on this computer. There is no sign-in.")
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"harness web UI: {url}   (Ctrl-C to quit)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.app.closing.set()
        server.app.runner.stop_all()
        server.server_close()
        job = server.app.runner.current()
        if job and job.thread:
            job.thread.join(timeout=5)
            if job.run_dir:
                print(f"Resume with: python main.py build --resume {job.run_dir}")
    return 0
