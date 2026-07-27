#!/usr/bin/env python3
"""Extract one strict reviewer verdict from an adapter result."""

import argparse
import json
import pathlib
import re

CALLBACK_OWNER = re.compile(
    r"^(\s*)FIX-OWNER:\s*(builder|test-author|both)"
    r"((?:The|That) background `[^`\r\n]+`[^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
CALLBACK_APPROVE = re.compile(
    r"^(\s*)APPROVE((?:The|That) background `[^`\r\n]+`[^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
CALLBACK_SUMMARY = re.compile(
    r"^(?:No follow-up action needed — my review above already stands as "
    r"\*\*REQUEST CHANGES / FIX-OWNER: "
    r"(?P<slash>builder|test-author|both)\*\*\.|"
    r"\*\*REQUEST CHANGES — FIX-OWNER: "
    r"(?P<bold>builder|test-author|both)\*\*[^\r\n]*|"
    r"\*\*REQUEST CHANGES\*\*\s+—\s+`FIX-OWNER:\s*"
    r"(?P<inline>builder|test-author|both)`[^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)


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


def normalize_cursor_callback(raw: str) -> str:
    raw = CALLBACK_APPROVE.sub(
        lambda match: f"{match.group(1)}APPROVE\n{match.group(1)}{match.group(2)}",
        raw,
    )
    corrupted = list(CALLBACK_OWNER.finditer(raw))
    if corrupted:
        raw = CALLBACK_OWNER.sub(
            lambda match: (
                f"{match.group(1)}FIX-OWNER: {match.group(2).lower()}\n"
                f"{match.group(1)}{match.group(3)}"
            ),
            raw,
        )
    summaries = list(CALLBACK_SUMMARY.finditer(raw))
    summary_owners = [
        (match.group("slash") or match.group("bold") or match.group("inline")).lower()
        for match in summaries
    ]
    if corrupted:
        if len(corrupted) != len(summaries) or any(
            owner.start() >= summary.start()
            for owner, summary in zip(corrupted, summaries)
        ):
            raise ValueError("reviewer background callback lacks a later summary")
        if {match.group(2).lower() for match in corrupted} != set(summary_owners):
            raise ValueError(
                "reviewer background callback owner contradicts its summary"
            )
    elif summaries:
        if len(summaries) != 1:
            raise ValueError("reviewer background callback is ambiguous")
        prefix = raw[:summaries[0].start()]
        primary_verdicts = [
            line.strip().upper()
            for line in prefix.splitlines()
            if re.fullmatch(r"\s*(?:APPROVE|REQUEST CHANGES)\s*", line, re.I)
        ]
        primary_owners = [
            match.group(1).lower()
            for line in prefix.splitlines()
            if (match := re.fullmatch(
                r"\s*FIX-OWNER:\s*(builder|test-author|both)\s*", line, re.I
            ))
        ]
        if primary_verdicts != ["REQUEST CHANGES"] or \
           primary_owners != summary_owners:
            raise ValueError(
                "reviewer background callback contradicts its primary verdict"
            )
    return CALLBACK_SUMMARY.sub("REQUEST CHANGES", raw)


def canonical_review_detail(raw: str, verdict: str, owner: str) -> str:
    lines = raw.rstrip().splitlines()
    verdict_rows = [
        index for index, line in enumerate(lines)
        if re.fullmatch(rf"\s*{re.escape(verdict)}\s*", line, re.I)
    ]
    if not verdict_rows:
        return raw
    end = verdict_rows[-1]
    if owner:
        owner_rows = [
            index for index, line in enumerate(lines)
            if index > end and re.fullmatch(
                rf"\s*FIX-OWNER:\s*{re.escape(owner)}\s*", line, re.I
            )
        ]
        if owner_rows:
            end = owner_rows[-1]
    return "\n".join(lines[: end + 1]).strip()


def assistant_text(event: dict):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("reviewer assistant contains malformed content")
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError("reviewer assistant contains malformed text")
        texts.append(text)
    return "\n".join(texts)


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
            content = assistant_text(event)
            if isinstance(content, str):
                normalized = normalize_cursor_callback(content)
                if verdict_signals(normalized):
                    assistants.append((content, normalized))
    if len(results) != 1:
        raise ValueError("reviewer stream must contain exactly one successful result")
    if len(assistants) > 1:
        raise ValueError("reviewer stream contains multiple verdict-bearing assistants")
    if not assistants:
        return results[0]

    assistant, normalized_assistant = assistants[0]
    if assistant not in results[0]:
        raise ValueError("reviewer assistant is not bound to the successful result")
    verdict, owner = parse_review(normalized_assistant, contract_version)
    normalized_terminal = normalize_cursor_callback(results[0])
    terminal_signals = verdict_signals(normalized_terminal)
    if set(terminal_signals) != {verdict}:
        raise ValueError("reviewer assistant contradicts the successful result")
    terminal_owners = {
        match.group(1).lower()
        for line in normalized_terminal.splitlines()
        if (match := re.fullmatch(
            r"\s*FIX-OWNER:\s*(builder|test-author|both)\s*", line, re.I
        ))
    }
    if terminal_owners != ({owner} if owner else set()):
        raise ValueError("reviewer assistant owner contradicts the successful result")
    return canonical_review_detail(normalized_assistant, verdict, owner)


def claude_review(raw: str) -> str:
    results = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "result"
            and event.get("subtype") == "success"
            and isinstance(event.get("result"), str)
        ):
            results.append(event["result"])
    if len(results) != 1:
        raise ValueError("Claude reviewer output must contain exactly one successful result")
    return results[0]


def review_text(raw: str, adapter: str, contract_version: str) -> str:
    """Return the adapter-normalized, verdict-bearing review text."""
    if adapter.startswith("cursor-"):
        return cursor_review(raw, contract_version)
    if adapter == "claude-code":
        return claude_review(raw)
    return raw


def parse_verdict(raw: str, adapter: str, contract_version: str) -> tuple[str, str]:
    """Return the canonical verdict and Contract 1.7 repair owner."""
    return parse_review(review_text(raw, adapter, contract_version), contract_version)


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
