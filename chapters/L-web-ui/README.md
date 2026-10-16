# Chapter L — A local web UI

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with scripted models and a real server on localhost — try it with your own model)

[← Chapter K: Remote MCP servers](../../chapters/K-remote-mcp/README.md) · [Index](../../README.md)

## Read this when

You'd rather start, watch and look back at runs in a browser than in a terminal: follow the steps live, answer the planner's questions in a form, read SPEC/REPORT/REVIEW and the patch side by side, and apply a review's changes with a button.

## Needs

Stage 07 (graph & state) for resumable runs, Chapter A (tracing) for the trace view, Chapter J (review & apply) for the apply buttons.

## Goal

A browser front end for what `main.py build`, `review`, `apply`, `runs` and `trace` already do, served by the standard library on `127.0.0.1`, that drives the **same** `run_build()` the CLI uses. Nothing in the loop, the graph or the nodes changes; the UI only plugs into the callbacks they already have (`ask`, `on_step`, `on_enter`, `say`).

## What gets built

- `gui.py`
  - `Job` — one run on a background thread. Every callback becomes a numbered event (`say`, `node`, `step`, `questions`, `done`, `stopped`, `error`) kept in memory, so a page that reconnects gets everything it missed
  - `JobRunner` — at most **one** job at a time (one GPU, one model); `start()`, `answer()`, `stop()`
  - `make_server(root, …)` — `ThreadingHTTPServer` with a small JSON API and a Server-Sent Events stream:

    | Method | Path | What |
    |---|---|---|
    | GET | `/` | the page (`web/index.html`) |
    | GET | `/api/status` | the models per role, and the current job (if any) |
    | GET | `/api/runs` | every run folder, newest first, merged with `runs/index.jsonl` metrics |
    | GET | `/api/runs/<id>` | state, plan tasks, summary, which documents exist, what `apply` would do |
    | GET | `/api/runs/<id>/doc/<name>` | `SPEC.md`, `REPORT.md`, `REVIEW.md`, `CHANGES.patch` or `plan.json` as text |
    | GET | `/api/runs/<id>/trace` | the `trace --steps` view as text |
    | GET | `/api/runs/<id>/export?what=project\|run` | a `.zip` download: the generated code (`workspace/`), or the whole run folder; caches and virtualenvs left out |
    | GET | `/api/jobs/<id>/events` | SSE stream of the job's events (honours `Last-Event-ID`) |
    | POST | `/api/jobs` | start `build`, `plan` (plan only), `review`, `resume` (also builds a reviewed plan), `fix`, `rerun` or `replan` (tasks of a finished run, Chapter G) |
    | POST | `/api/jobs/<id>/answers` | answers to the planner's questions |
    | POST | `/api/jobs/<id>/stop` | stop after the current model or tool call; the run stays resumable |
    | POST | `/api/runs/<id>/apply` | `apply_run` / `undo_apply`, with `dry_run` and `force` |

- `web/index.html` — one page, no build step, no external scripts or fonts: a form for new runs, the live step feed with the current graph node and task, the planner's questions, the run list, and a run view with tabs for the plan, documents, patch (coloured diff) and trace, and buttons to download the project or the whole run as a `.zip`. Light and dark follow the system.
- `main.py gui [--port 8765] [--host 127.0.0.1] [--no-browser]` — starts the server and opens the browser
- `main.run_build()` takes optional `say`, `on_step` and `on_enter` callbacks; without them it prints, as before
- `tests/test_gui.py`

## Rules

- **Same code path as the CLI.** Jobs call `main.run_build()`; a run started in the browser is an ordinary run folder that `trace`, `runs`, `build --resume` and `apply` all understand, and the other way round.
- **One job at a time**; a second start gets HTTP 409 with a readable message.
- **Stop is cooperative**: the stop flag is checked at every step, node and question, then the usual `KeyboardInterrupt` path saves `state.json` as `interrupted`. A model call already under way finishes first.
- **Local only by default.** Binds `127.0.0.1`. Requests whose `Host` isn't the server's own name are refused (DNS rebinding), and POSTs must be JSON with no foreign `Origin` (a web page elsewhere can't start a build).
- **No path tricks**: run ids must name a folder directly inside `runs/`, and documents come from a fixed list.
- Every failure is a JSON `{"error": "…"}` the page shows as a sentence, never a stack trace.
- Standard library only.

## Untouched

`loop.py`, `graph.py`, the nodes in `pipeline.py`, the prompts and every file format.

## Done when

- [x] A build, a plan-only run, a review, a resume, a fix, a task re-run and a task re-plan can be started from the page and followed live
- [x] The planner's questions show up in the page, and its answers reach the planner
- [x] Stop leaves an `interrupted` run that resumes from the page or from `build --resume`
- [x] The run list and run view read old run folders (no `summary.json`, no trace, no index row)
- [x] Apply, dry run and undo work for review runs, with conflicts shown as a message
- [x] A run's generated project, or the whole run, downloads as a `.zip` with one top folder, byte for byte
- [x] Foreign `Host`/`Origin`, path-traversal ids and a second job are refused

## How to run

```bash
python main.py gui                        # http://127.0.0.1:8765 opens in your browser
python main.py gui --port 9000 --no-browser
python -m unittest tests.test_gui -v           # 14 tests
python -m unittest discover -s tests -v   # 307 tests, no model needed
```

## Known limits

- **Stop waits for the current model call.** With a slow local model that can be a minute or more; the page says "stopping…" until then.
- **Events live in memory.** Restarting the server loses the live feed of a finished job, not its run folder: the run view, trace and documents are still there.
- **No authentication.** Anyone who can reach the port can start builds that run commands. Keep the default `127.0.0.1`; `--host 0.0.0.0` prints a warning.
- `doctor`, `bench` and `memory` stay CLI-only.

## Commit

```
chapter L: local web UI — stdlib HTTP server + SSE, one background job, live steps, questions, stop/resume, apply
```

---

[← Chapter K: Remote MCP servers](../../chapters/K-remote-mcp/README.md) · [Index](../../README.md)
