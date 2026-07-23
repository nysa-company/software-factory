#!/usr/bin/env python3
"""Extract one strict reviewer verdict from an adapter result."""

import argparse
import json
import pathlib
import re


parser = argparse.ArgumentParser()
parser.add_argument("--adapter", required=True)
parser.add_argument("--input", required=True, type=pathlib.Path)
parser.add_argument("--contract-version", default="1.6.0")
parser.add_argument("--format", choices=("verdict", "fields"), default="verdict")
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

signals = []
for line in raw.splitlines():
    stripped = line.strip()
    if re.fullmatch(r"APPROVE|REQUEST CHANGES", stripped, re.I):
        signals.append(stripped.upper())
    signals.extend(match.upper() for match in re.findall(
        r"\*\*(APPROVE|REQUEST CHANGES)(?:\.)?\*\*", stripped, re.I
    ))
if not signals or len(set(signals)) != 1:
    raise SystemExit("reviewer result must contain one unambiguous verdict")
verdict = signals[0]
owners = [
    match.group(1).lower()
    for line in raw.splitlines()
    if (match := re.fullmatch(
        r"\s*FIX-OWNER:\s*(builder|test-author|both)\s*", line, re.I
    ))
]
owner = ""
if args.contract_version == "1.7.0":
    if verdict == "REQUEST CHANGES" and len(owners) != 1:
        raise SystemExit("contract 1.7 request changes requires exactly one FIX-OWNER")
    if verdict == "APPROVE" and owners:
        raise SystemExit("contract 1.7 approval must not include FIX-OWNER")
    owner = owners[0] if owners else ""
elif owners:
    raise SystemExit("FIX-OWNER requires contract 1.7")

if args.format == "fields":
    print(f"{verdict}\t{owner}")
else:
    print(verdict)
