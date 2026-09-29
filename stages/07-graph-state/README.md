# Stage 07 — Graph & state

**Part 1 · Build pipeline** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Stage 06: Planner: request → spec → tasks](../../stages/06-planner/README.md) · [Index](../../README.md) · Stage 08: Task-by-task execution →

## Goal

Define one typed state object and wire the steps as a graph: intake → plan → build → finish.

## Problem it solves

Data moves between steps as loose variables, and the flow is hardcoded in `main.py`.

## What gets built

- State type: request, spec, plan, current task, messages, files touched, check results, status
- Graph with nodes `intake`, `plan`, `build`, `finish` and explicit edges
- State can be saved and loaded, so a run can be resumed

## Rules

- State is the only thing passed between nodes
- Nodes are small and single-purpose
- Own graph runner with a dataclass state (decided in Stage 02); its API mirrors LangGraph's so a swap stays possible

## Stays untouched

- Loop shape
- Tools, MCP and planner output format

## Done when

- [x] The same build runs through the graph with the same result as Stage 06
- [x] State can be dumped at any node
- [x] A killed run can resume from its last saved state

## How to run

```bash
python main.py build "count the words in a file"      # shows [graph] → intake / plan / build / finish
# press Ctrl-C during the build, then:
python main.py build --resume runs/<id>
cat runs/<id>/state.json                               # state at the last node boundary
python -m unittest discover -s tests -v               # 108 tests, no model needed
```

## Verified

- **Same behaviour as Stage 06:** the existing tests for Stage 06 (review, then build from an edited plan; `--no-plan`; plan failures) pass unchanged, now running through the graph.
- **Kill and resume through the CLI:**
  - A real SIGINT arrived partway through a build, after `wc.py` was written and while the model was still thinking. The CLI exited with code 130, printed the resume command, and left no MCP server process running. The state was saved with `status: interrupted`, `next: build`.
  - `--resume` re-entered `build`. The model was told the workspace wasn't empty (`wc.py`), checked it, and finished T2. The history ended as `intake, plan, build, finish`, and resuming again was refused.
- **Crash while planning:** the model was unreachable during the planner call. The state was saved with `next: plan`, and a resume re-ran `plan` and finished the build.
- **Graph rules:** a router returning an undeclared target raises an error; validation catches missing or unknown edges; `max_steps` stops runaway cycles; `interrupt_before` pauses and a resume continues.

### Known limits

- A resumed build starts a **fresh loop**. The model sees the files but not the earlier conversation. Stage 08 makes this finer-grained: progress is tracked per task, so a resume skips finished tasks.
- `summary.json` after a resume counts only the resumed session's steps and time. The complete history is in `transcript.jsonl`, with a `resume` event marking the join.

## Commit

```
stage 7: graph runner + BuildState checkpoints; intake → plan → build → finish; --resume
```

## Leads to

The build node still tries to do every task in one conversation.

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `graph.py` | new | `Graph`: nodes, edges, conditional branches, `interrupt_before`, a checkpoint hook, a step guard |
| `state.py` | new | `BuildState` dataclass; `save_state()` / `load_state()` → `runs/<id>/state.json` |
| `pipeline.py` | new | The four nodes, `build_graph()`, and `run_pipeline()` (new / review / resume) |
| `main.py` | changed | `run_build()` becomes a thin wrapper around `run_pipeline()`; new `--resume` flag |
| `build.py` | small change | On Ctrl-C, write the summary and **re-raise** so the graph can keep the run resumable |
| `prompts.json` | + `build_resume_note` | Tells the model the workspace already has files from an earlier attempt |
| `loop.py`, `planner.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### The graph

```
intake ─▶ plan ─┬─(plan_failed)─────────────▶ finish ─▶ END
                └─(ok)─▶ [interrupt if --review] ─▶ build ─▶ finish ─▶ END
```

| Node | Reads | Writes |
|---|---|---|
| `intake` | request, options | creates `runs/<id>/`; sets `run_id`, `run_dir` |
| `plan` | request | `plan`, `spec`, `status = planned`, or `status = plan_failed` + `error`. Skipped with `--no-plan` |
| `build` | the plan and spec **read back from disk** (so review edits count) | `summary`, `status` |
| `finish` | everything | `summary.json` |

### Graph API

It is intentionally close to LangGraph's, so moving to LangGraph later would mean rewriting `graph.py` and leaving the nodes alone.

```python
g = Graph(entry="intake")
g.node("intake", fn)                   # fn(state) mutates and/or returns state
g.edge("intake", "plan")
g.branch("plan", router, {"build", "finish"})   # router(state) -> name
g.run(state, start=None, interrupt_before=set(), checkpoint=save, on_enter=print, max_steps=50)
```

- `validate()` checks that every node has exactly one way out (an edge or a branch) and that every target exists. It runs before each `run()`.
- A router that returns a name outside its declared targets raises `GraphError`.
- **Checkpoints:** before each node the state records `next = <node>` and is saved. After each node, the node name goes into `history`, and at `END`, `next` becomes `None`.
- If a node raises, the state has already been saved with `next` set to that node. The error propagates, and **resuming re-runs that node**.
- `interrupt_before={"build"}` stops right before `build`, with the state saved as `next = "build"`. This is how `--review` works now.
- `max_steps` guards against loops, since later stages add cycles such as revise → plan.

### State (`state.json`)

```json
{
  "request": "…", "run_id": "…", "run_dir": "…",
  "options": {"review": false, "no_plan": false, "max_iterations": 40},
  "plan": {…}, "spec": "…",
  "status": "new | planned | plan_failed | finished | max_iterations | error | interrupted",
  "next": "build", "history": ["intake", "plan"],
  "summary": {…}, "error": null, "updated_at": "…"
}
```

It's plain JSON, so `cat runs/<id>/state.json` shows the state at the last node boundary.

### Resume rules (`--resume` and `--from-run` both use them)

| State on disk | What happens |
|---|---|
| `next` is set (killed, crashed, or paused for review) | Continue from `next` |
| `next` is empty and status is `error` or `interrupted` | Re-enter `build` |
| `next` is empty and status is `finished`, `max_iterations` or `plan_failed` | Refused: "already been built" |

When `build` starts and the workspace already has files, `build_resume_note` is added to the first message along with the file list. A `{"event": "resume"}` line is also added to `transcript.jsonl`.

### Out of scope

Running each task separately and tracking task status (Stage 08). Resume in this stage works at **node** level: a build that's resumed starts its loop again, on top of whatever is already in the workspace.

---

[← Stage 06: Planner: request → spec → tasks](../../stages/06-planner/README.md) · [Index](../../README.md) · Stage 08: Task-by-task execution →
