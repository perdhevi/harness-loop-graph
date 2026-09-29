"""Stage 1 — one cycle: prompt in, result out.

Usage:
    python main.py "Write a Python function that checks whether a string is a palindrome"
    python main.py            # prompts for input
"""

import json
import sys
import time
from pathlib import Path

from model_adapter import make_adapter, ModelError

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def run_once(prompt: str) -> str:
    config = load_config()
    model = make_adapter(config)
    messages = [{"role": "user", "content": prompt}]
    return model.complete(config["system_prompt"], messages)


def _safe_console() -> None:
    """Windows: printing ✓ or — to a cp1252 pipe/file raises UnicodeEncodeError; replace instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    _safe_console()
    prompt = " ".join(sys.argv[1:]).strip() or input("prompt> ").strip()
    if not prompt:
        print("Empty prompt.")
        return 1

    start = time.perf_counter()
    try:
        result = run_once(prompt)
    except ModelError as e:
        print(f"[error] {e}")
        return 1
    elapsed = time.perf_counter() - start

    print(result)
    print(f"\n[{elapsed:.1f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
