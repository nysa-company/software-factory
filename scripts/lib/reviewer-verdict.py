#!/usr/bin/env python3
"""Extract one strict reviewer verdict from an adapter result."""

import argparse
import json
import pathlib
import re


parser = argparse.ArgumentParser()
parser.add_argument("--adapter", required=True)
parser.add_argument("--input", required=True, type=pathlib.Path)
args = parser.parse_args()

raw = args.input.read_text(encoding="utf-8", errors="replace")
if args.adapter.startswith("cursor-"):
    results = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result" and event.get("subtype") == "success":
            result = event.get("result")
            if isinstance(result, str):
                results.append(result)
    if len(results) != 1:
        raise SystemExit("reviewer stream must contain exactly one successful result")
    raw = results[0]

lines = [line.strip() for line in raw.splitlines() if line.strip()]
verdicts = [line.upper() for line in lines if re.fullmatch(r"APPROVE|REQUEST CHANGES", line, re.I)]
if len(verdicts) != 1 or not lines or lines[-1].upper() != verdicts[0]:
    raise SystemExit("reviewer result must end with exactly one standalone verdict")
print(verdicts[0])
