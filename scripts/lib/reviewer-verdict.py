#!/usr/bin/env python3
"""Extract one strict reviewer verdict from an adapter result."""

import argparse
import json
import pathlib
import re


def verdict_signals(raw: str) -> list[str]:
    signals = []
    for line in raw.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"APPROVE|REQUEST CHANGES", stripped, re.I):
            signals.append(stripped.upper())
        signals.extend(match.upper() for match in re.findall(
            r"\*\*(APPROVE|REQUEST CHANGES)(?:\.)?\*\*", stripped, re.I
        ))
    return signals


def parse_review(raw: str, contract_version: str) -> tuple[str, str]:
    signals = verdict_signals(raw)
    if not signals or len(set(signals)) != 1:
        raise ValueError("reviewer result must contain one unambiguous verdict")
    verdict = signals[0]
    owners = [
        match.group(1).lower()
        for line in raw.splitlines()
        if (match := re.fullmatch(
            r"\s*FIX-OWNER:\s*(builder|test-author|both)\s*", line, re.I
        ))
    ]
    owner = ""
    if contract_version == "1.7.0":
        if verdict == "REQUEST CHANGES" and len(owners) != 1:
            raise ValueError("contract 1.7 request changes requires exactly one FIX-OWNER")
        if verdict == "APPROVE" and owners:
            raise ValueError("contract 1.7 approval must not include FIX-OWNER")
        owner = owners[0] if owners else ""
    elif owners:
        raise ValueError("FIX-OWNER requires contract 1.7")
    return verdict, owner


def cursor_review(raw: str, contract_version: str) -> str:
    results = []
    assistants = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and event.get("subtype") == "success":
            result = event.get("result")
            if isinstance(result, str):
                results.append(result)
        if event.get("type") == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and verdict_signals(content):
                assistants.append(content)
    if len(results) != 1:
        raise ValueError("reviewer stream must contain exactly one successful result")
    if len(assistants) > 1:
        raise ValueError("reviewer stream contains multiple verdict-bearing assistants")
    if not assistants:
        return results[0]

    assistant = assistants[0]
    if assistant not in results[0]:
        raise ValueError("reviewer assistant is not bound to the successful result")
    verdict, owner = parse_review(assistant, contract_version)
    terminal_signals = verdict_signals(results[0])
    if set(terminal_signals) != {verdict}:
        raise ValueError("reviewer assistant contradicts the successful result")
    terminal_owners = {
        match.group(1).lower()
        for line in results[0].splitlines()
        if (match := re.fullmatch(
            r"\s*FIX-OWNER:\s*(builder|test-author|both)\s*", line, re.I
        ))
    }
    if terminal_owners != ({owner} if owner else set()):
        raise ValueError("reviewer assistant owner contradicts the successful result")
    return assistant


def parse_verdict(raw: str, adapter: str, contract_version: str) -> tuple[str, str]:
    """Return the canonical verdict and Contract 1.7 repair owner."""
    if adapter.startswith("cursor-"):
        raw = cursor_review(raw, contract_version)
    return parse_review(raw, contract_version)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--contract-version", default="1.6.0")
    parser.add_argument("--format", choices=("verdict", "fields"), default="verdict")
    args = parser.parse_args()

    try:
        verdict, owner = parse_verdict(
            args.input.read_text(encoding="utf-8", errors="replace"),
            args.adapter,
            args.contract_version,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.format == "fields":
        print(f"{verdict}\t{owner}")
    else:
        print(verdict)


if __name__ == "__main__":
    main()
