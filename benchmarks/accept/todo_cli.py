"""Acceptance: todo-cli."""
import subprocess
import sys
from pathlib import Path

Path("todo.json").unlink(missing_ok=True)


def todo(*args):
    r = subprocess.run([sys.executable, "todo.py", *args], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, f"todo.py {' '.join(args)} exited {r.returncode}: {r.stderr[-300:]}"
    return r.stdout.strip()


assert todo("list") == "(empty)", "list on an empty store should print (empty)"
assert todo("add", "buy", "milk") == "added 1"
assert todo("add", "call", "mom") == "added 2"
assert todo("done", "1") == "done 1"
assert todo("list").splitlines() == ["1. [x] buy milk", "2. [ ] call mom"], f"list: {todo('list')!r}"
assert Path("todo.json").exists(), "tasks must be stored in todo.json"
print("todo-cli: ok")
