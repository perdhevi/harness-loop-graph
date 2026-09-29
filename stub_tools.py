"""Stage 2 — one stub tool, just enough to prove the loop.

Stage 3 replaces this file with a tool registry and tools defined in JSON.
"""

import ast
import operator

from loop import Tool

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


TOOLS = {
    "calculate": Tool(
        name="calculate",
        description="Evaluate an arithmetic expression, e.g. '(17 * 23) + 4'.",
        params={"expression": "string"},
        fn=calculate,
    ),
}
