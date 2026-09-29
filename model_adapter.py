"""Stage 1 — model adapter.

A thin HTTP wrapper around a model endpoint. It knows how to send a
system prompt + messages and return the reply text. Nothing else:
no loop, no tools, no memory. That all belongs to the harness.
"""

import json
import os
import urllib.request
import urllib.error


class ModelError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ModelError(f"HTTP {e.code} from {url}: {body}") from e
    except urllib.error.URLError as e:
        raise ModelError(f"Could not reach {url}: {e.reason}") from e


class OllamaAdapter:
    """Ollama's native /api/chat.

    num_ctx: Ollama's default context is small (4,096 tokens below ~23 GB of VRAM) and it truncates
    longer prompts *silently*, dropping the start of the conversation (system prompt, tool list, task).
    think: qwen3 and similar models "think" by default; false makes replies faster and shorter.
    """

    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.timeout_s = cfg.get("timeout_s", 120)
        self.num_ctx = cfg.get("num_ctx")
        self.think = cfg.get("think")            # None: leave to the model's default

    def complete(self, system: str, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
        }
        if self.num_ctx:
            payload["options"] = {"num_ctx": int(self.num_ctx)}
        if self.think is not None:
            payload["think"] = self.think
        try:
            out = _post_json(f"{self.base_url}/api/chat", payload, {}, self.timeout_s)
        except ModelError as e:
            if "think" in payload and "thinking" in str(e).lower():
                # this model has no thinking switch: drop the setting and remember that
                self.think = None
                payload.pop("think")
                out = _post_json(f"{self.base_url}/api/chat", payload, {}, self.timeout_s)
            else:
                raise
        msg = out.get("message") or {}
        content = msg.get("content") or ""
        if not content.strip() and msg.get("thinking"):
            # all the output went into the reasoning field: surface it rather than an empty reply
            content = msg["thinking"]
        return content


class AnthropicAdapter:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.max_tokens = cfg.get("max_tokens", 1024)
        self.timeout_s = cfg.get("timeout_s", 120)
        key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        self.api_key = os.environ.get(key_env)
        if not self.api_key:
            raise ModelError(f"Set the {key_env} environment variable.")

    def complete(self, system: str, messages: list[dict]) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        out = _post_json(f"{self.base_url}/v1/messages", payload, headers, self.timeout_s)
        return "".join(b.get("text", "") for b in out["content"] if b.get("type") == "text")


ADAPTERS = {
    "ollama": OllamaAdapter,
    "anthropic": AnthropicAdapter,
}


def make_adapter(config: dict):
    name = config["provider"]
    if name not in ADAPTERS:
        raise ModelError(f"Unknown provider '{name}'. Options: {list(ADAPTERS)}")
    return ADAPTERS[name](config["providers"][name])
