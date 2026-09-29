"""Reply normalizer: other tool-call formats → the harness's JSON action.

Models are trained on their own tool-call syntax and fall back to it even when
the prompt asks for {"action": …, "args": …}. The loop is locked, so this runs
as a model wrapper: the loop only ever sees the canonical format (and so does
the history, which nudges the model toward it on later steps).

Recognised:
  Gemma 4     <|tool_call>call:NAME{key:<|"|>value<|"|>,…}<tool_call|>   (also without the special tokens)
  Qwen 3.5    <tool_call><function=NAME><parameter=key>value</parameter>…</function></tool_call>
  Hermes      <tool_call>{"name": …, "arguments": {…}}</tool_call>          (via the JSON shapes below)
  JSON        {"name"|"tool"|"tool_name"|"function": …, "arguments"|"args"|"parameters"|"input": …}
              {"function": {"name": …, "arguments": "<json string>"}}     (OpenAI)
              {"type": "tool_use", "name": …, "input": …}                 (Anthropic)
              {"tool_calls": [ … ]}                                        (first call)
              {"final_answer"|"answer": …}                                 → final
              {"action": "final"|"finish"|"answer"|…, "args": {"final": …}}  → final   (Chapter I)
Anything else is passed through unchanged, so the loop's own error handling still applies.
"""

from __future__ import annotations

import json
import re
from typing import Callable

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_GEMMA_START = re.compile(r"(?:<\|tool_call>\s*)?call\s*:\s*([A-Za-z_][\w.\-/:]*)\s*\{")
_Q = '<|"|>'
_DECODER = json.JSONDecoder(strict=False)                # real line breaks inside strings are fine (Chapter I)
FINAL_ACTIONS = {"final", "finish", "final_answer", "answer", "done"}
FINAL_ARG_KEYS = ("final", "answer", "final_answer", "text", "message", "result", "response", "summary", "content")
NAME_KEYS = ("action", "tool", "tool_name", "name", "function", "function_name")
ARG_KEYS = ("args", "arguments", "parameters", "input", "params", "tool_input")


class _Parse(ValueError):
    pass


# ---------------------------------------------------------------- Gemma call syntax

def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _value(s: str, i: int, stop: str):
    i = _skip_ws(s, i)
    if s.startswith(_Q, i):
        end = s.find(_Q, i + len(_Q))
        if end < 0:
            raise _Parse("unterminated <|\"|> string")
        return s[i + len(_Q):end], end + len(_Q)
    if i < len(s) and s[i] == '"':
        try:
            return _DECODER.raw_decode(s, i)
        except json.JSONDecodeError as e:
            raise _Parse(str(e)) from e
    if i < len(s) and s[i] == "{":
        return _object(s, i)
    if i < len(s) and s[i] == "[":
        items, i = [], i + 1
        while True:
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == "]":
                return items, i + 1
            v, i = _value(s, i, ",]")
            items.append(v)
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
    # bare word / number: read to the next stop character at this level
    j = i
    while j < len(s) and s[j] not in stop:
        j += 1
    raw = s[i:j].strip()
    if raw in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[raw], j
    if re.fullmatch(r"-?\d+", raw):
        return int(raw), j
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw), j
    return raw, j


def _object(s: str, i: int) -> tuple[dict, int]:
    if s[i] != "{":
        raise _Parse("expected {")
    out, i = {}, i + 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise _Parse("unterminated {")
        if s[i] == "}":
            return out, i + 1
        if s[i] == '"':
            key, i = _DECODER.raw_decode(s, i)
        elif s.startswith(_Q, i):
            key, i = _value(s, i, ":")
        else:
            m = re.match(r"[A-Za-z_][\w\-]*", s[i:])
            if not m:
                raise _Parse(f"bad key at {i}")
            key, i = m.group(0), i + m.end()
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise _Parse("expected :")
        val, i = _value(s, i + 1, ",}")
        out[key] = val
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1


def _gemma(text: str) -> dict | None:
    m = _GEMMA_START.search(text)
    if not m:
        return None
    try:
        args, _ = _object(text, m.end() - 1)
    except _Parse:
        return None
    thought = text[:m.start()].strip()
    thought = re.sub(r"<\|?[a-z_]+\|?>", "", thought).strip()
    return {"thought": thought[:300], "action": m.group(1), "args": args}


# ---------------------------------------------------------------- Qwen XML-style calls

_QWEN_FN = re.compile(r"<function=([^>\s]+)\s*>(.*?)(?:</function>|$)", re.DOTALL)
_QWEN_PARAM = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL)


def _qwen_xml(text: str) -> dict | None:
    m = _QWEN_FN.search(text)
    if not m:
        return None
    args = {}
    for key, raw in _QWEN_PARAM.findall(m.group(2)):
        value = raw[1:] if raw.startswith("\n") else raw          # the format puts values on their own line
        value = value[:-1] if value.endswith("\n") else value
        stripped = value.strip()
        if stripped in ("true", "false"):
            args[key] = stripped == "true"
        elif re.fullmatch(r"-?\d+", stripped):
            args[key] = int(stripped)
        elif stripped[:1] in "[{":
            try:
                args[key] = json.loads(stripped)
            except json.JSONDecodeError:
                args[key] = value
        else:
            args[key] = value
    thought = text[:m.start()]
    thought = re.sub(r"</?tool_call>", "", thought).strip()
    return {"thought": thought[:300], "action": m.group(1), "args": args}


# ---------------------------------------------------------------- JSON shapes

def _json_objects(text: str):
    start = text.find("{")
    while start != -1:
        try:
            obj, end = _DECODER.raw_decode(text, start)
            if isinstance(obj, dict):
                yield obj
                start = text.find("{", end)
                continue
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)


def _from_json(obj: dict) -> dict | None:
    if "action" in obj and isinstance(obj.get("args", {}), dict) and isinstance(obj["action"], str):
        return None                                          # already canonical
    if "final" in obj:
        return None
    for k in ("final_answer", "answer"):
        if k in obj and isinstance(obj[k], str):
            return {"thought": str(obj.get("thought", "")), "final": obj[k]}
    if isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
        first = obj["tool_calls"][0]
        return _from_json(first) if isinstance(first, dict) else None
    fn = obj.get("function")
    if isinstance(fn, dict) and "name" in fn:                # OpenAI: {"function": {"name", "arguments"}}
        return _call(obj.get("thought", ""), fn["name"], fn.get("arguments", {}))
    name = next((obj[k] for k in NAME_KEYS if isinstance(obj.get(k), str)), None)
    if name is None:
        return None
    args = next((obj[k] for k in ARG_KEYS if k in obj), {})
    return _call(obj.get("thought", obj.get("reasoning", "")), name, args)


def _final_action(obj: dict) -> dict | None:
    """{"action": "final", "args": {"final": "…"}}: the model tried to finish by calling a tool named final."""
    action = obj.get("action")
    if not isinstance(action, str) or action.strip().lower() not in FINAL_ACTIONS:
        return None
    args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    text = next((args[k] for k in FINAL_ARG_KEYS if isinstance(args.get(k), str)), None)
    if text is None:
        strings = [v for v in args.values() if isinstance(v, str)]
        text = strings[0] if len(strings) == 1 else (json.dumps(args, ensure_ascii=False) if args else "")
    if not text and isinstance(obj.get("final"), str):
        text = obj["final"]
    return {"thought": str(obj.get("thought", "")), "final": text}


def _call(thought, name: str, args) -> dict | None:
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return {"thought": str(thought or ""), "action": name, "args": args}


# ---------------------------------------------------------------- entry points

def normalize_reply(text: str) -> tuple[str, str | None]:
    """Return (reply for the loop, name of the conversion or None if unchanged)."""
    cleaned = _THINK_RE.sub("", text or "")
    for obj in _json_objects(cleaned):
        final = _final_action(obj)
        if final is not None:
            return json.dumps(final, ensure_ascii=False), "final-action"
        if ("action" in obj and isinstance(obj.get("action"), str)) or "final" in obj:
            return text, None                                # canonical: leave it exactly as it was
        converted = _from_json(obj)
        if converted:
            return _finish(converted, "json-alias")
        break
    q = _qwen_xml(cleaned)
    if q:
        return _finish(q, "qwen-xml")
    g = _gemma(cleaned)
    if g:
        return _finish(g, "gemma-call")
    return text, None


def _finish(converted: dict, how: str) -> tuple[str, str]:
    """A call to "final" in any format (e.g. Gemma's call:final{…}) is a final answer too."""
    final = _final_action(converted)
    if final is not None:
        return json.dumps(final, ensure_ascii=False), f"{how}+final-action"
    return json.dumps(converted, ensure_ascii=False), how


class NormalizingModel:
    """Wraps a model so the loop always receives the canonical JSON action when one can be recovered."""

    def __init__(self, model, on_normalize: Callable[[str, str], None] | None = None):
        self.model = model
        self.on_normalize = on_normalize
        self.count = 0

    def complete(self, system: str, messages: list[dict]) -> str:
        raw = self.model.complete(system, messages)
        reply, how = normalize_reply(raw)
        if how:
            self.count += 1
            if self.on_normalize:
                self.on_normalize(how, raw)
        return reply
