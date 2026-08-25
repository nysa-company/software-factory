#!/usr/bin/env python3
"""Classify fallback refusal text without exposing it."""

import pathlib
import re
import sys


RULES = (
    ("readiness", r"readiness|direct cli|version|credential|authentication"),
    ("manifest", r"manifest|qualification (?:fallback )?authority|sealed qualification|product changed"),
    ("attempt_count", r"process record|first role attempt|latest unique ticket attempt"),
    ("handoff", r"handoff|historical qualification fallback|journal|worktree|commit|revision|repository|filesystem|tracked|index|symlink|hardlink|file|path|branch|snapshot|binary|secret|provider-authored|tree|submodule|migration|content|utf-?8|forbidden|exception"),
    ("route_policy", r"route|resolution|profile|policy|provider (?:famil|identit)"),
    ("provenance", r"provenance|eligible terminal|cancelled run|failed (?:run|attempt)|receipt|git state|remote|cancellation|approval|account|ledger|evidence|drift|reason|git failed"),
)


def classify(text: str) -> str:
    lowered = text[:65536].lower()
    return next((code for code, pattern in RULES if re.search(pattern, lowered)), "unknown")


if __name__ == "__main__":
    path = pathlib.Path(sys.argv[1])
    print(classify(path.read_text(encoding="utf-8", errors="replace")))
