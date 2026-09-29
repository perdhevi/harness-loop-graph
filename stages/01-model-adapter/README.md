# Stage 01 — Model adapter

**Part 1 · Build pipeline** · **Status:** ✅ done

[Index](../../README.md) · [Stage 02: ReAct loop →](../../stages/02-react-loop/README.md)

## Goal

Send one prompt to a model and get one answer back. Establishes the boundary between the model client and the harness.

## Problem it solves

The model can only be reached through raw HTTP, with provider details scattered in code.

## What gets built

- `config.json` — provider, model, endpoint, system prompt
- `model_adapter.py` — `OllamaAdapter`, `AnthropicAdapter`, `make_adapter()`, `ModelError`
- `main.py` — `run_once(prompt)`: one request, one response
- `tests/test_platform.py` — adapter tests against a local fake server (no model needed)

## Rules

- Interface: `complete(system: str, messages: list[dict]) -> str`
- Network/HTTP failures raise `ModelError`
- Standard library only; no SDKs, no streaming

## Stays untouched

- Nothing yet — this is the first stage

## Done when

- [x] `python main.py "<request>"` prints an answer from Ollama or Anthropic
- [x] Switching provider is a one-word config change
- [x] A dead server prints a readable error, not a stack trace

## Leads to

The model can describe code but can't write or run anything. The ReAct loop (Stage 02) needs a stable way to call it.

## Ollama settings (found on a real Windows + Ollama run)

`OllamaAdapter` sends `options.num_ctx` and `think`, both from `config.json`. Without them, Ollama kept its small default context (4,096 tokens on GPUs under ~23 GB of VRAM) and **silently dropped the start of long prompts**. qwen3 also thought by default, which is slower and can leave `content` empty. See the troubleshooting section in Stage 05.

## Read more

- [Full write-up](ARTICLE.md)

## Commit

```
stage 1: model adapter + single prompt/response cycle
```

---

[Index](../../README.md) · [Stage 02: ReAct loop →](../../stages/02-react-loop/README.md)
