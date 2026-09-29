import csv
import sys
from collections import defaultdict

totals = defaultdict(float)
with open(sys.argv[1], newline="") as f:
    for row in csv.DictReader(f):
        try:
            amount = float(row["amount"])
        except (TypeError, ValueError):
            continue          # parse first: `totals[k] += float(...)` would create the key before failing
        totals[row["region"]] += amount
for region, total in sorted(totals.items(), key=lambda kv: (-round(kv[1], 2), kv[0])):
    print(f"{region}: {total:.2f}")
