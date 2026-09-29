# Chapter H — A model per role

**Part 2 · Reference chapter** · **Status:** ✅ done (tested against a fake Ollama server — confirm with real models)

[← Chapter G: Fixing a finished run](../../chapters/G-fix-run/README.md) · [Index](../../README.md)

## Read this when

One model can't do every job well: a small, fast model is fine for writing code but makes invalid plans or lenient verdicts, or a strong model is too slow to use for every step.

## Needs

Stage 10 (judge) for all five roles. Chapter A (tracing) to see which model did what; Chapter F (benchmark) to compare mixes.

## Goal

Let each role — planner, task worker, judge, reviser, compactor — use its own model, provider and settings, without changing the loop, the nodes or the adapter.

## What gets built

- `models.py` — `role_settings()`, `make_role_models()`, `describe_roles()`, `ollama_models()`
- `config.json` — a `roles` section; each role can override `model`, `provider` or any provider setting (`num_ctx`, `think`, `base_url`, `max_tokens`, …)
- `pipeline.py` — `Context.models` and `ctx.model_for(role)`; each node asks for its role's model
- Trace events record the model name; `trace` shows it per role; `metrics` adds `models`
- `doctor` checks every model any role uses; the `num_ctx` warning is per model
- Each run's `request.json` records the role → model mapping

## Rules

- A missing or empty role means "the default provider and model", so an old `config.json` behaves exactly as before
- Roles with identical settings share one adapter
- `model_adapter.py`, `loop.py` and every node's logic are untouched; only *which* model a node receives changes
- `run_build(model=…)` (tests) still means one model for every role

## Done when

- [x] Each role sends its requests to its own model (checked against a fake Ollama server that answers by model name)
- [x] The trace and metrics show which model served which role
- [x] `doctor` reports a missing model and which roles need it
- [x] Without a `roles` section, nothing changes (all earlier tests pass unchanged)

## How to run

```json
"roles": {
  "planner":   {"model": "qwen3:8b"},
  "reviser":   {"model": "qwen3:8b"},
  "judge":     {"model": "mistral:7b"},
  "task":      {"model": "gemma4:e4b", "think": null},
  "compactor": {}
}
```

```bash
ollama pull qwen3:8b && ollama pull gemma4:e4b && ollama pull mistral:7b
python main.py doctor                     # every model pulled? num_ctx set for each?
python main.py build "…"
python main.py trace runs/<id>            # "by role" shows the model behind each role
python main.py bench benchmarks/core.json --label mix-a --save-baseline   # compare mixes
python -m unittest discover -s tests -v   # 217 tests, no model needed
```

`"think": null` leaves thinking to the model's default. Use it for models without a thinking switch, like Gemma or Mistral; the adapter would retry without it anyway, at the cost of one failed request.

`bench --provider/--model` still means *one* model for every role (roles are cleared), so a baseline measures that model alone. Without those flags, the benchmark uses your `roles` mix, and the default label becomes `mix-<models>`.

## With Ollama on a small GPU

One Ollama server serves all the models; each request names the model it wants. With 6 GB of VRAM, only one 7–9B model fits at a time, so **Ollama unloads one model and loads the other** whenever the role changes. Each switch takes a few seconds (longer from a slow disk).

A build switches roles in bursts, not per step: planner once, then many task steps, then judge once per round. So a planner/worker/judge mix costs a handful of reloads per build, not hundreds. What to avoid is a separate model for `compactor`: it's called in the middle of task loops and would reload twice each time. Leave it empty (default model) or set it to the task model.

## Leads to

Which mix works best is a measurement question: run the benchmark (Chapter F) once per mix and compare.

## Commit

```
chapter H: a model per role — roles section in config.json, per-role adapters, trace/metrics/doctor per model
```

---

[← Chapter G: Fixing a finished run](../../chapters/G-fix-run/README.md) · [Index](../../README.md)
