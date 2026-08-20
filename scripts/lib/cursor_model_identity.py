"""Finite, route-bound Cursor presentation aliases."""

from __future__ import annotations

from pathlib import Path
import re
import sys

from role_output import RoleOutputError, _stable_bytes

REPORTED_MODEL_ALIASES = {
    ("gpt-5.6-sol-high", "GPT-5.6 Sol 1M High"): frozenset({
        "GPT-5.6 Sol 272K High",
    }),
    (
        "claude-fable-5-thinking-medium",
        "Claude Fable 5 1M Medium Thinking (NO ZDR)",
    ): frozenset({
        "Claude Fable 5 1M Medium Thinking",
        "Claude Fable 5 300K Medium",
        "Fable 5 1M Medium Thinking",
        "Fable 5 300K Medium",
    }),
    (
        "claude-opus-5-thinking-medium",
        "Claude Opus 5 1M Medium Thinking",
    ): frozenset({
        "Claude Opus 5 300K Medium",
        "Opus 5 300K Medium",
    }),
    (
        "claude-sonnet-5-thinking-high",
        "Claude Sonnet 5 1M Thinking",
    ): frozenset({
        "Claude Sonnet 5 300K High",
        "Sonnet 5 300K High",
    }),
}

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DISPLAY_SUFFIXES = (" (current, default)", " (current)", " (default)")


def approved_reported_models(selection: str, canonical: str) -> frozenset[str]:
    vendor_aliases = (
        frozenset({f"Claude {canonical}"})
        if selection.startswith("claude-")
        and canonical
        and not canonical.startswith("Claude ")
        else frozenset()
    )
    return frozenset((selection, canonical)) | vendor_aliases | REPORTED_MODEL_ALIASES.get(
        (selection, canonical), frozenset()
    )


def listed_reported_model(raw: str, selection: str) -> str:
    prefix = f"{selection} - "
    matches = []
    for line in raw.splitlines():
        line = ANSI.sub("", line)
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):]
        for suffix in DISPLAY_SUFFIXES:
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                break
        matches.append(value)
    return matches[0] if len(matches) == 1 else ""


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    selection, canonical, source = sys.argv[1:]
    try:
        raw = _stable_bytes(Path(source))
        if len(raw) > 1_000_000:
            return 2
        text = raw.decode("utf-8", errors="strict")
    except (OSError, RoleOutputError, UnicodeError):
        return 2
    return int(
        listed_reported_model(text, selection)
        not in approved_reported_models(selection, canonical)
    )


if __name__ == "__main__":
    raise SystemExit(main())
