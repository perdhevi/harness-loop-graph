"""Acceptance: word-stats."""
import subprocess
import sys
from pathlib import Path

Path("_sample.txt").write_text("one two three\nfour five\n\nsix\n")
r = subprocess.run([sys.executable, "stats.py", "_sample.txt"], capture_output=True, text=True, timeout=20)
assert r.returncode == 0, f"stats.py exited {r.returncode}: {r.stderr[-300:]}"
assert r.stdout.strip() == "6 words, 4 lines", f"expected '6 words, 4 lines', got {r.stdout.strip()!r}"
Path("_empty.txt").write_text("")
r = subprocess.run([sys.executable, "stats.py", "_empty.txt"], capture_output=True, text=True, timeout=20)
assert r.stdout.strip() == "0 words, 0 lines", f"empty file: got {r.stdout.strip()!r}"
print("word-stats: ok")
