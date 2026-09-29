"""Acceptance: csv-report."""
import subprocess
import sys
from pathlib import Path

Path("_sales.csv").write_text(
    "region,amount\nNorth,10.5\nSouth,20\nNorth,9.5\nEast,\nWest,abc\nEast,20\nSouth,0.004\n")
r = subprocess.run([sys.executable, "report.py", "_sales.csv"], capture_output=True, text=True, timeout=20)
assert r.returncode == 0, f"report.py exited {r.returncode}: {r.stderr[-300:]}"
lines = r.stdout.strip().splitlines()
assert lines == ["East: 20.00", "North: 20.00", "South: 20.00"], f"got {lines!r}"
print("csv-report: ok")
