#!/usr/bin/env python3
"""Validate Claude/Kimi JSON telemetry and redact the bearer token."""

import json
import os
from pathlib import Path
import sys


EXPECTED_MODEL = "moonshotai/kimi-k2.6"
MAX_TOKEN_COUNT = 1_000_000_000
IDENTITY_KEYS = {"model", "model_id", "modelId", "model_name", "modelName"}
TOKEN_KEYS = {
    "input_tokens": "input_tokens",
    "inputTokens": "input_tokens",
    "output_tokens": "output_tokens",
    "outputTokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cacheReadInputTokens": "cache_read_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cacheReadTokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    "cacheCreationInputTokens": "cache_write_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "cacheWriteTokens": "cache_write_tokens",
}


def bounded_integer(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("telemetry is not an integer")
    if value < 0 or value > maximum:
        raise ValueError("telemetry is outside its bound")
    return value


def redact(value: object, token: str) -> object:
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]").replace("cost_usd", "cost_[removed]")
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {
            key.replace(token, "[REDACTED]"): redact(item, token)
            for key, item in value.items()
            if "cost_usd" not in key.lower()
        }
    return value


def identities(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(identities(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "modelUsage":
                if not isinstance(item, dict) or not item:
                    raise ValueError("modelUsage is malformed")
                found.update(str(model) for model in item)
            elif key in IDENTITY_KEYS and isinstance(item, str):
                found.add(item)
            found.update(identities(item))
    return found


def find_turns(value: object, maximum: int) -> int:
    found: list[int] = []

    def visit(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in ("num_turns", "numTurns", "turns"):
                    found.append(bounded_integer(child, maximum))
                else:
                    visit(child)

    visit(value)
    if not found:
        return 0
    if len(set(found)) != 1:
        raise ValueError("multiple turn counts")
    return found[0]


def find_tokens(value: object) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    def validate_all(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                validate_all(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in TOKEN_KEYS:
                    bounded_integer(child, MAX_TOKEN_COUNT)
                else:
                    validate_all(child)

    validate_all(value)
    usage = value.get("modelUsage") if isinstance(value, dict) else None
    if isinstance(usage, dict):
        usage = usage.get(EXPECTED_MODEL)
    if usage is None and isinstance(value, dict):
        usage = value.get("usage")
    if not isinstance(usage, dict):
        return totals
    for key, result_key in TOKEN_KEYS.items():
        if key in usage:
            totals[result_key] = bounded_integer(usage[key], MAX_TOKEN_COUNT)
    return totals


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    stdout_path, stderr_path, metrics_path, max_turns_text = sys.argv[1:]
    token = os.environ.get("FACTORY_KIMI_REDACTION_TOKEN", "")
    if not token:
        return 2
    try:
        max_turns = bounded_integer(int(max_turns_text), 1000)
        raw_stdout = Path(stdout_path).read_text(encoding="utf-8")
        raw_stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
        document = json.loads(raw_stdout)
        safe_document = redact(document, token)
        found = identities(document)
        valid_identity = found == {EXPECTED_MODEL}
        turns = find_turns(document, max_turns)
        tokens = find_tokens(document)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        print("Kimi output failed structured telemetry validation", file=sys.stderr)
        return 8

    # stderr is unstructured, so replace the exact secret before emitting it.
    safe_stderr = (
        raw_stderr.replace(token, "[REDACTED]").replace("cost_usd", "cost_[removed]")
    )
    if safe_stderr:
        print(safe_stderr, file=sys.stderr, end="" if safe_stderr.endswith("\n") else "\n")
    print(json.dumps(safe_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not valid_identity:
        print("Kimi output model identity was missing, mismatched, or multiple", file=sys.stderr)
        return 8
    metrics = (
        f"turns={turns} input_tokens={tokens['input_tokens']} "
        f"output_tokens={tokens['output_tokens']} "
        f"cache_read_tokens={tokens['cache_read_tokens']} "
        f"cache_write_tokens={tokens['cache_write_tokens']} "
        "token_basis=observational cost_basis=conservative_reservation\n"
    )
    Path(metrics_path).write_text(metrics, encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
