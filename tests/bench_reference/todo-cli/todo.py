import json
import sys
from pathlib import Path

DB = Path("todo.json")
items = json.loads(DB.read_text()) if DB.exists() else []
cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else "list"), sys.argv[2:]
if cmd == "add":
    items.append({"id": len(items) + 1, "text": " ".join(rest), "done": False})
    print(f"added {items[-1]['id']}")
elif cmd == "done":
    for i in items:
        if i["id"] == int(rest[0]):
            i["done"] = True
    print(f"done {rest[0]}")
else:
    print("\n".join(f"{i['id']}. [{'x' if i['done'] else ' '}] {i['text']}" for i in items) or "(empty)")
DB.write_text(json.dumps(items))
