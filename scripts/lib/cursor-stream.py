#!/usr/bin/env python3
"""Validate, measure, and redact Cursor stream-json without persisting raw data."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

SENSITIVE_KEY = re.compile(
    r"(key|token|secret|password|url|dsn|conn|auth)", re.IGNORECASE
)
USAGE_KEYS = {
    "input_tokens",
    "inputTokens",
    "output_tokens",
    "outputTokens",
    "cache_tokens",
    "cacheReadTokens",
    "cacheWriteTokens",
}
URL_CREDENTIALS = re.compile(
    r"([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@", re.IGNORECASE
)
TEXT_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)"
    r"[A-Za-z0-9_.-]*)\s*([:=])\s*([^\s,;]+)"
)
AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+\S+"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def dictionaries(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def numeric(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def redact_text(value: str) -> str:
    value = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = AUTH_HEADER.sub(r"\1: [REDACTED]", value)
    value = URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    return TEXT_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                redact_value(child)
                if key in USAGE_KEYS and numeric(child) is not None
                else "[REDACTED]"
                if SENSITIVE_KEY.search(str(key))
                else redact_value(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_value(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def write_metrics(path: Path, values: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    os.replace(tmp, path)


def completed_tool_error(event: dict[str, Any]) -> tuple[str, str] | None:
    if event.get("type") != "tool_call" or event.get("subtype") != "completed":
        return None
    tool_call = event.get("tool_call")
    if not isinstance(tool_call, dict):
        return None
    for kind, value in tool_call.items():
        if not str(kind).endswith("ToolCall") or not isinstance(value, dict):
            continue
        result = value.get("result")
        if not isinstance(result, dict):
            continue
        error = result.get("error")
        if not isinstance(error, dict):
            continue
        message = error.get("error", error.get("modelVisibleError"))
        if isinstance(message, str) and message.strip():
            return str(kind), " ".join(redact_text(message).split())
    return None


def main() -> int:
    if len(sys.argv) not in {6, 7, 8}:
        print(
            "usage: cursor-stream.py METRICS_FILE EXPECTED_MODEL "
            "EXPECTED_REPORTED_MODEL EXPECTED_CWD MAX_TURNS "
            "[REPEATED_TOOL_ERROR_LIMIT] [PROGRESS_JOURNAL]",
            file=sys.stderr,
        )
        return 2

    metrics_path = Path(sys.argv[1])
    expected_model = sys.argv[2]
    expected_reported_model = sys.argv[3]
    expected_cwd = os.path.realpath(sys.argv[4])
    max_turns = int(sys.argv[5])
    repeated_tool_error_limit = int(sys.argv[6]) if len(sys.argv) >= 7 else 0
    progress_path = Path(sys.argv[7]) if len(sys.argv) == 8 else None
    if repeated_tool_error_limit < 0:
        print("REPEATED_TOOL_ERROR_LIMIT must be nonnegative", file=sys.stderr)
        return 2
    terminal_success = False
    malformed_json = False
    reported_model = ""
    reported_cwd = ""
    turns = 0
    usage_sources = {
        "input_tokens": 0,
        "inputTokens": 0,
        "output_tokens": 0,
        "outputTokens": 0,
        "cache_tokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
    }
    in_private_key = False
    turn_limit_exceeded = False
    internal_retries = 0
    tool_errors: Counter[tuple[str, str]] = Counter()
    repeated_tool_error_count = 0
    repeated_tool_error_limit_exceeded = False
    progress_events = 0
    progress_digest = hashlib.sha256()
    progress_stream = None
    if progress_path is not None:
        descriptor = os.open(
            progress_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        progress_stream = os.fdopen(descriptor, "wb")

    try:
        for raw_line in sys.stdin:
            line = raw_line.rstrip("\n")
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                if line.lstrip().startswith(("{", "[")):
                    malformed_json = True
                if "-----BEGIN" in line and "PRIVATE KEY-----" in line:
                    in_private_key = True
                    print("[REDACTED PRIVATE KEY]", flush=True)
                elif in_private_key:
                    if "-----END" in line and "PRIVATE KEY-----" in line:
                        in_private_key = False
                else:
                    print(redact_text(line), flush=True)
                continue

            if not isinstance(event, dict):
                print(json.dumps(redact_value(event), separators=(",", ":")), flush=True)
                continue
            event_type = str(event.get("type", ""))
            subtype = str(event.get("subtype", ""))
            if event_type == "retry" and subtype == "starting":
                internal_retries += 1
            tool_error = completed_tool_error(event)
            if tool_error is not None:
                tool_errors[tool_error] += 1
                repeated_tool_error_count = max(
                    repeated_tool_error_count, tool_errors[tool_error]
                )
            if event_type == "result" and subtype == "success":
                terminal_success = True
            if event_type == "assistant":
                turns += 1
            if event_type == "system" and subtype in {"init", "initialize"}:
                if isinstance(event.get("model"), str):
                    reported_model = event["model"]
                if isinstance(event.get("cwd"), str):
                    reported_cwd = event["cwd"]
            if progress_stream is not None and (
                event_type == "assistant"
                or event_type == "result" and subtype == "success"
                or event_type == "system" and subtype in {"init", "initialize"}
                or event_type == "tool_call" and subtype in {"started", "completed"}
            ):
                progress_events += 1
                event_raw = (
                    json.dumps(
                        event, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                    )
                    + "\n"
                ).encode()
                record = (
                    json.dumps({
                        "event_sha256": hashlib.sha256(event_raw).hexdigest(),
                        "observed_monotonic_ns": time.monotonic_ns(),
                        "sequence": progress_events,
                        "subtype": subtype,
                        "type": event_type,
                    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
                progress_stream.write(record)
                progress_stream.flush()
                os.fsync(progress_stream.fileno())
                progress_digest.update(record)
            for item in dictionaries(event):
                for key in usage_sources:
                    amount = numeric(item.get(key))
                    if amount is not None:
                        usage_sources[key] = max(usage_sources[key], amount)
            print(json.dumps(redact_value(event), separators=(",", ":")), flush=True)
            if (
                repeated_tool_error_limit > 0
                and repeated_tool_error_count >= repeated_tool_error_limit
            ):
                repeated_tool_error_limit_exceeded = True
                break
            if turns > max_turns:
                turn_limit_exceeded = True
                break
    finally:
        if progress_stream is not None:
            progress_stream.close()

    usage = {
        "input_tokens": max(
            usage_sources["input_tokens"], usage_sources["inputTokens"]
        ),
        "output_tokens": max(
            usage_sources["output_tokens"], usage_sources["outputTokens"]
        ),
        "cache_tokens": max(
            usage_sources["cache_tokens"],
            usage_sources["cacheReadTokens"] + usage_sources["cacheWriteTokens"],
        ),
    }
    write_metrics(
        metrics_path,
        {
            "turns": turns,
            **usage,
            "requested_model": expected_model,
            "reported_model": reported_model,
            "reported_cwd": reported_cwd,
            "internal_retries": internal_retries,
            "progress_events": progress_events,
            "progress_sha256": progress_digest.hexdigest(),
            "repeated_tool_error_count": repeated_tool_error_count,
        },
    )
    if repeated_tool_error_limit_exceeded:
        print(
            "cursor repeated identical tool failure limit reached: "
            f"{repeated_tool_error_count} >= {repeated_tool_error_limit}",
            file=sys.stderr,
        )
        return 15
    if turn_limit_exceeded:
        print(
            f"cursor stream exceeded turn limit: {turns} > {max_turns}",
            file=sys.stderr,
        )
        return 14
    if not terminal_success:
        print("cursor stream has no terminal success result", file=sys.stderr)
        return 10
    if malformed_json:
        print("cursor stream contains malformed JSON events", file=sys.stderr)
        return 13
    if reported_model and reported_model not in {
        expected_model,
        expected_reported_model,
    }:
        print(f"cursor reported unapproved model: {reported_model}", file=sys.stderr)
        return 11
    if reported_cwd and os.path.realpath(reported_cwd) != expected_cwd:
        print("cursor reported an unexpected workspace", file=sys.stderr)
        return 12
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
