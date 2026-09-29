"""Acceptance: greet (runs in a copy of the finished workspace)."""
import subprocess
import sys


def run(*args):
    return subprocess.run([sys.executable, "greet.py", *args], capture_output=True, text=True, timeout=20)


r = run("Ana")
assert r.returncode == 0, f"greet.py Ana exited {r.returncode}: {r.stderr[-300:]}"
assert r.stdout.strip() == "Hello, Ana!", f"expected 'Hello, Ana!', got {r.stdout.strip()!r}"
r = run()
assert r.stdout.strip() == "Hello, world!", f"expected 'Hello, world!', got {r.stdout.strip()!r}"
print("greet: ok")
