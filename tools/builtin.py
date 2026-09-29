"""Stage 3 — Python handlers for local tools listed in tools.json."""

import ast
import operator
from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"unsupported expression: {ast.dump(node)[:60]}")


def calculate(expression: str) -> str:
    """Safe arithmetic: numbers, + - * / // % ** and parentheses only."""
    return str(_eval(ast.parse(expression, mode="eval")))


def current_time(timezone: str = "UTC") -> str:
    """Current date and time in ISO 8601 for an IANA timezone."""
    tz = _tz.utc if timezone.upper() == "UTC" else ZoneInfo(timezone)
    return datetime.now(tz).isoformat(timespec="seconds")
