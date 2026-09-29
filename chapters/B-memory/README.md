# Chapter B — Memory

**Part 2 · Reference chapter** · **Status:** ✅ done (tested with a scripted fake model — confirm with a real model)

[← Chapter A: Tracing](../../chapters/A-tracing/README.md) · [Index](../../README.md) · Chapter C: Context management →

## Read this when

Later tasks ignore decisions made by earlier ones, or the same error and dead end come back build after build.

## Needs

Stage 07 (graph & state), Stage 08 (task execution).

## Goal

Add project memory (decisions and a file map for this build) and long-term memory (what worked and what broke in past builds).

## What gets built

- **Project memory:** a *project map* built by the harness from the workspace (files, with the functions and classes inside Python files), shown to every task alongside the hand-off notes
- **Long-term memory:** a lesson store shared by all builds, holding *fix lessons* (error → what fixed it) and *review lessons* (what the judge found missing)
- Recall scored by relevance × time decay × track record

## Rules

- Scores are smooth curves, not hard top-k cutoffs
- Memory is read and written around the loop, not inside it

## Stays untouched

- Loop shape
- State schema (only additive fields)

## Done when

- [x] Later tasks see the real names earlier tasks created (project map)
- [x] A known error in a new build retrieves the fix that worked before
- [x] Older memories fade in score rather than disappearing

## Spec

### Files

| File | Change | Purpose |
|---|---|---|
| `memory.py` | new | `project_map()`, `error_signature()`, `LessonStore` (add, recall, mark shown/helped), scoring |
| `pipeline.py` | changed | Hooks in `plan`, `run_task`, `verify` and `judge`; `Context` gets `memory` |
| `planner.py` | small change | `make_plan(..., notes=)`: optional extra text for the first planner message |
| `prompts.json` | + `lessons_fix`, `lessons_plan`; `task_request` uses the project map | |
| `config.json` | + `memory` section (see below) | |
| `main.py` | changed | Creates the store; new `memory` subcommand to list or query lessons |
| `loop.py`, `graph.py`, `build.py`, `checks.py`, `judge.py`, `tools/*`, `servers/*`, `model_adapter.py` | **untouched** | |

### Decision: no `NOTES.md` in the workspace

The outline had the model keep `workspace/NOTES.md`. Two things already cover that job **without asking the model to maintain a file**:
- the hand-off notes (Stage 08), which record *decisions*
- a **project map** the harness builds itself, which records *what exists*. It is always accurate, because it's read from the files instead of written from memory.

This also keeps harness files out of the requester's project, the same reasoning as moving `plan.json` in Stage 06.

### Project memory: the project map

This is what a task sees in place of the plain file list:

```
- todo.py (1116 bytes)
    def load(db=DB)            def save(items, db=DB)
    def add(items, text)       def done(items, item_id)
    def render(items)          def main(argv)
- test_todo.py (307 bytes)
    def test_add()  def test_empty()  def test_done()
- README.md (125 bytes) — "# To-do CLI"
```

- **Python files** are read with `ast`, listing top-level functions with their arguments and classes with their methods. A file that fails to parse is listed with `(syntax error line N)`, which is useful to know too.
- **Other text files** show their first non-empty line.
- The map is capped at `map_chars`. Anything past the cap is listed by name only.

This is how later tasks follow the conventions of earlier ones: they see the real names (`add(items, text)`, `render(items)`) and not just file names.

### Long-term memory: lessons (`memory/lessons.json`, shared by all builds)

| Kind | Written when | Key (what recall matches against) | Content |
|---|---|---|---|
| `fix` | `verify` passes on a task that had failed its check earlier | the **error signature** of the last failure | the check, an excerpt of the error, and the hand-off note of the fix that worked |
| `review` | the judge's verdict is `revise` (after the hard rules) | the **request** | the judge's feedback and problems |

The **error signature** is built from the lines of the check output that look like errors (`Error`, `FAILED`, `assert`, `Traceback`, `exit code`). If there are none, the last few lines are used. Paths, numbers and quoted values are removed, so `/tmp/run-1/todo.py:12` and `/tmp/run-7/todo.py:40` compare as equal.

### Recall and scoring

It's plain standard library: TF-IDF cosine over word tokens, with no embeddings (build to delete; an embedding model can replace `relevance()` later).

```
score = relevance(query, key)                 cosine similarity, 0…1
      × 0.5 ** (age_days / half_life_days)    decay: older lessons fade, they don't vanish
      × (helped + 1) / (shown + 2)            track record: lessons that helped rise, ones that didn't sink
```

**Which lessons are shown** uses fractions, not a fixed top-k:
- A lesson is kept if its score is at least `relative_cutoff` (0.5) × the best score.
- The best one must reach `min_relevance`, so that an unrelated store returns nothing.
- Lessons are added best-first until `recall_chars` is used up.

**Where lessons are recalled**
- **Fix mode** (`run_task` after a failed check): fix lessons are matched on the failure's error signature and added to the fix prompt with `lessons_fix`. Their ids are saved on the task as `lessons_shown`.
- **Planning:** review lessons are matched on the request and passed to the planner with `lessons_plan`, e.g. *"a similar request was sent back because 'done' was missing"*.

**Updating the track record**
- Every lesson shown increments `shown`.
- When the task it was shown for then passes its check, the lesson also gets `helped`.

### Config

```json
"memory": {"enabled": true, "path": "memory/lessons.json", "half_life_days": 30,
           "relative_cutoff": 0.5, "min_relevance": 0.2, "recall_chars": 1500, "map_chars": 4000}
```

With `enabled: false`, tasks get the Stage 08 file list, and nothing is read or written.

### Command line

```bash
python main.py memory                      # list lessons with age, shown, helped
python main.py memory --query "No module named pytest"   # what recall would return, with scores
```

### Out of scope

- Embeddings or a vector database: the scoring function is the only part that would change.
- Sharing lessons between machines.
- Automatically forgetting lessons that keep failing. They sink in score but stay listed; `main.py memory` shows them.

## How to run

```bash
python main.py build "…"                      # memory is on by default (config: memory.enabled)
python main.py memory                         # all lessons: kind, age, shown, helped
python main.py memory --query "FileNotFoundError: No such file" --kind fix   # scores, and what recall picks (→)
python -m unittest discover -s tests -v      # 158 tests, no model needed
```

`memory/` sits at the repo root and is in `.gitignore`. It holds your lessons, not project code.

## Verified

- **Two separate builds through the CLI:**
  - **Build 1 (notes app):** `store.load('notes.json')` crashed with `FileNotFoundError` because the file didn't exist yet. The fix (catch `FileNotFoundError`, return `[]`) passed, and **lesson `f06f4eec` was saved**.
  - **Build 2 (bookmarks app):** the same crash, in a different project with a different file name. The fix prompt got `[memory] 1 fix lesson(s) from past builds → T1`, containing build 1's error and what fixed it. The check passed, and the lesson now reads `shown 1 helped 1`.
  - **Track record:** querying afterwards gives both lessons the same relevance (0.44), but the one that helped ranks higher (0.296 vs 0.222).
- **Review lessons:** a to-do build that the judge sent back ("done is missing") left a review lesson.
  - A **similar** request later ("Python to-do list CLI: add, list, done commands") got it in the planner prompt. When that build was accepted, the lesson was marked `helped`.
  - A **different** request ("weather forecast for Jakarta") didn't get it.
- **Scoring:**
  - 30 days halves a score, and 300 days leaves 0.5¹⁰ of it (faded, not gone).
  - Lessons that helped outrank ones that didn't.
  - An unrelated store returns nothing, and the relative cutoff drops weak matches.
- **Project map:** T2's prompt showed `def add(a, b)`, which T1 wrote. It also shows classes with their methods, and `(syntax error line N)` for a file that doesn't parse.
- **Memory off:** plain file list, and nothing written.

### Known limits

- Relevance is word overlap (TF-IDF). It recognises *the same kind* of error in different projects, but misses paraphrases ("can't find the file" vs `FileNotFoundError`). Embeddings would fix that, and only `relevance()` would need to change.
- Lessons are only as good as the fixing task's hand-off note. A vague note ("fixed it") makes a vague lesson.
- The store never deletes anything, and similar lessons from different builds pile up. They crowd each other within `recall_chars` rather than cause errors. Merging near-duplicates is a possible follow-up.

## Commit

```
chapter B: memory — project map for tasks; lesson store (fix + review) with relevance × decay × track record
```

---

[← Chapter A: Tracing](../../chapters/A-tracing/README.md) · [Index](../../README.md) · Chapter C: Context management →
