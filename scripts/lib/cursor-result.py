#!/usr/bin/env python3
"""Print the single successful terminal result from a Cursor stream log."""

import json
import sys

results = []
for raw in sys.stdin:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if event.get("type") == "result" and event.get("subtype") == "success":
        value = event.get("result")
        if isinstance(value, str):
            results.append(value)

if len(results) != 1:
    raise SystemExit("Cursor log must contain exactly one successful terminal result")
print(results[0])
