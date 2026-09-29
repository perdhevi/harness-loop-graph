# Stage 1 — One Prompt, One Result

> **harness-loop-graph** is an agent harness that takes a request in plain language and builds the application it asks for. It is built from scratch, one stage at a time; each stage adds exactly one concern and ends in one commit. This is where it starts.

---

## Background

### Why build a harness at all?

When people say "an AI coding agent," they usually picture the model. But the model is only the part that turns text into more text. Turning "build me a to-do app" into a folder of working, tested code takes everything that sits around it:

- a **loop** that lets the model think, act, and look at the result
- **tools** it can call — write files, run commands, run tests
- **memory** of what already happened
- **context management** so the prompt fits the budget
- **compaction** when the conversation gets long
- **sensors** that notice when the loop gets stuck
- a **judge** that decides whether the finished app does what was asked

That surrounding runtime is the **harness**. The model client is just the HTTP wrapper inside it.

It's easy to blur the two together. Frameworks often ship both in one package, so it feels like "the agent" is one thing. This series keeps them apart on purpose, starting with the smallest possible piece.

### Why stage by stage?

Each stage follows the same rules:

| Rule | What it means |
|---|---|
| **One concern per stage** | A stage adds one capability, and only that. |
| **Spec before code** | Decide inputs, outputs, and what stays untouched before writing anything. |
| **One commit per stage** | The git history reads like the table of contents. |
| **Data in JSON, not Python** | Prompts, models, and settings live in config files. |
| **Build to delete** | Borrow patterns from frameworks, but write the thing yourself so you understand it. |

The goal isn't the fastest path to a working agent. The goal is to understand every layer, so that when something breaks later, you know which layer to look at.

---

## What we're building in this stage

The smallest useful thing: **send one prompt to a model, get one answer back, print it.** At this stage the "application" is just code printed to the screen — nothing is written to disk or run yet.

No loop, no tools, no memory. One request, one response, one cycle.

```
┌─────────────┐     ┌───────────────┐     ┌──────────────┐
│   main.py   │ ──▶ │ model_adapter │ ──▶ │ Model (HTTP) │
│ (one cycle) │ ◀── │  .complete()  │ ◀── │ Ollama / API │
└─────────────┘     └───────────────┘     └──────────────┘
        ▲
        │ reads
┌─────────────┐
│ config.json │
└─────────────┘
```

### The three files

#### 1. `config.json` — settings live in data

```json
{
  "provider": "ollama",
  "system_prompt": "You are a senior software engineer. Build what the requester asks for, and explain your choices briefly.",
  "providers": {
    "ollama":    { "base_url": "http://localhost:11434", "model": "qwen3:8b", "timeout_s": 120 },
    "anthropic": { "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5",
                   "api_key_env": "ANTHROPIC_API_KEY", "max_tokens": 1024, "timeout_s": 120 }
  }
}
```

Switching between a local model and a cloud model means changing one word. The API key stays in an environment variable, never in the file.

#### 2. `model_adapter.py` — the model client, and nothing more

Each provider gets a small class with a single method:

```python
def complete(self, system: str, messages: list[dict]) -> str:
    ...
```

Give it a system prompt and a message list, get text back. That's the whole contract.

- **`OllamaAdapter`** posts to `/api/chat` with the system prompt as the first message.
- **`AnthropicAdapter`** posts to `/v1/messages` with the system prompt in its own field and joins the text blocks of the reply.
- **`make_adapter(config)`** picks the right class based on `provider`.

Network and HTTP failures are turned into one error type, `ModelError`, so the caller has a single thing to catch.

It uses only the Python standard library (`urllib`, `json`). There are no SDKs, so every byte on the wire is visible.

#### 3. `main.py` — one cycle

```python
def run_once(prompt: str) -> str:
    config = load_config()
    model = make_adapter(config)
    messages = [{"role": "user", "content": prompt}]
    return model.complete(config["system_prompt"], messages)
```

It reads the prompt from the command line (or asks for one), calls `run_once`, then prints the answer and how long it took.

### Design decisions worth noting

- **The adapter's interface is the one thing that must stay stable.** Every later stage calls `complete(system, messages)`. If that signature holds, the adapter never has to change again.
- **Messages are already a list.** One message looks like overkill now, but it's the shape the loop will need in Stage 2.
- **Errors are values the harness can handle.** A dead model server prints `[error] Could not reach …` instead of a stack trace. Later, the loop can decide to retry or give up.
- **No streaming yet.** `"stream": false` keeps the first stage simple. Streaming can come later as its own concern.

---

## How to run it

```bash
# Local model (default)
ollama pull qwen3:8b
python main.py "Write a Python function that checks whether a string is a palindrome"

# Cloud model
export ANTHROPIC_API_KEY=sk-...
# set "provider": "anthropic" in config.json
python main.py "Write a Python function that checks whether a string is a palindrome"
```

Leave out the prompt and it asks for one:

```
$ python main.py
prompt> Write a bash one-liner that counts lines in all .py files
...
```

---

## The outcome

At the end of this stage we have:

- ✅ **A working prompt → response cycle** against a local or cloud model
- ✅ **A provider-agnostic model client** with one stable method, `complete(system, messages)`
- ✅ **Config outside the code**: model, endpoint, and system prompt live in JSON
- ✅ **Clean failure behaviour**: network and HTTP errors become readable messages, not crashes
- ✅ **Zero dependencies**: plain Python, easy to read end to end

And, just as important, what we **don't** have yet:

- ❌ The model can't act. It only answers once.
- ❌ No tools, no memory, no context budget.
- ❌ No record of what happened, beyond what's printed.

That gap is the point. Stage 1 draws the line between **model client** and **harness**. Everything from here on is harness, and the adapter shouldn't need to change again.

---

## Next: Stage 2 — the ReAct loop

Stage 2 turns this single call into a loop: **reason → act → observe → repeat**, with a maximum-iterations guard. The loop becomes the core graph (a `reason` node, a `tool` node, and a `finish` edge), and after that stage it stays locked while everything else is built around it.

```
git commit -m "stage 1: model adapter + single prompt/response cycle"
```
