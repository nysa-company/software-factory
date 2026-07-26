#!/usr/bin/env python3
"""Bind one unmatched successful Reviewer run to canonical ticket evidence."""

import argparse
import hashlib
import importlib.util
from pathlib import Path
import re
import stat


def regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def fields(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not key or key in values:
            raise ValueError(f"malformed reviewer manifest: {path.name}")
        values[key] = value
    return values


def load_parser(path: Path):
    spec = importlib.util.spec_from_file_location("reviewer_verdict", path)
    if spec is None or spec.loader is None:
        raise ValueError("reviewer verdict parser is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_verdict, module.review_text


def quote_review(review: str, round_number: int) -> str:
    review = review.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not review or len(review.encode("utf-8")) > 131_072 or "\0" in review:
        raise ValueError("reviewer detail is empty or exceeds the safe bound")
    lines = "\n".join(f"> {line}" if line else ">" for line in review.splitlines())
    return f"Reviewer round {round_number} signed detail:\n\n{lines}\n\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--ticket-file", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.contract_version != "1.7.0":
        raise SystemExit("reviewer reconciliation requires contract 1.7.0")
    if not re.fullmatch(r"T-[0-9]+", args.ticket):
        raise SystemExit("invalid ticket")
    if not re.fullmatch(r"[0-9a-f]{40}", args.head):
        raise SystemExit("invalid reviewer evidence head")
    if not regular(args.ticket_file) or not args.runs_dir.is_dir() or args.runs_dir.is_symlink():
        raise SystemExit("reviewer reconciliation paths are unsafe")

    text = args.ticket_file.read_text(encoding="utf-8")
    state = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
    if len(state) != 1 or state[0].lower() not in {"review", "building"}:
        raise SystemExit("reviewer reconciliation requires Review or its idempotent Building result")

    verdict_pattern = re.compile(
        r"^\s*reviewer round\s+(\d+):\s*(APPROVE|REQUEST CHANGES)\s*$", re.I | re.M
    )
    owner_pattern = re.compile(
        r"^\s*reviewer round\s+(\d+)\s+FIX-OWNER:\s*(builder|test-author|both)\s*$",
        re.I | re.M,
    )
    verdicts = [(int(number), verdict.upper()) for number, verdict in verdict_pattern.findall(text)]
    owners = {int(number): owner.lower() for number, owner in owner_pattern.findall(text)}
    if [number for number, _ in verdicts] != list(range(1, len(verdicts) + 1)):
        raise SystemExit("reviewer verdict rounds are not canonical")
    if len(owners) != len(owner_pattern.findall(text)):
        raise SystemExit("reviewer repair ownership is ambiguous")
    for number, verdict in verdicts:
        if (verdict == "REQUEST CHANGES") != (number in owners):
            raise SystemExit("reviewer verdict ownership is incomplete")
    if set(owners) - {number for number, _ in verdicts}:
        raise SystemExit("reviewer repair ownership has no verdict")

    successful = []
    for manifest in args.runs_dir.glob("*.meta"):
        if not regular(manifest):
            raise SystemExit(f"reviewer manifest is unsafe: {manifest.name}")
        try:
            value = fields(manifest)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if value.get("ticket") != args.ticket or value.get("role") != "reviewer":
            continue
        if value.get("exit_status") != "0":
            continue
        if value.get("accounting_state") not in {"completed", "abandoned_conservative"}:
            continue
        output = manifest.with_suffix(".out")
        if (
            value.get("contract_version") != "1.7.0"
            or value.get("role_exit") != "ok"
            or not regular(output)
        ):
            raise SystemExit("successful reviewer evidence is not trusted Contract 1.7 output")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        if value.get("output_sha256") != digest:
            raise SystemExit("successful reviewer output digest does not match its manifest")
        successful.append((value.get("started_at", ""), manifest.name, value, output))
    successful.sort(key=lambda item: (item[0], item[1]))

    if len(successful) == len(verdicts):
        args.output.write_text(text, encoding="utf-8")
        return
    if len(successful) != len(verdicts) + 1:
        raise SystemExit("reviewer runs and canonical verdicts are ambiguous")
    if state[0].lower() != "review":
        raise SystemExit("unmatched reviewer evidence requires Review state")

    _, _, value, output = successful[-1]
    if (
        value.get("role_head_before") != args.head
        or value.get("role_remote_before") != args.head
    ):
        raise SystemExit("unmatched reviewer evidence is not bound to the current ticket head")
    parse_verdict, review_text = load_parser(Path(__file__).with_name("reviewer-verdict.py"))
    raw_output = output.read_text(encoding="utf-8", errors="replace")
    try:
        verdict, owner = parse_verdict(
            raw_output,
            value.get("adapter", ""),
            args.contract_version,
        )
        detail = quote_review(
            review_text(raw_output, value.get("adapter", ""), args.contract_version),
            len(verdicts) + 1,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    round_number = len(verdicts) + 1
    addition = detail + f"reviewer round {round_number}: {verdict}\n"
    if owner:
        addition += f"reviewer round {round_number} FIX-OWNER: {owner}\n"
    if verdict == "REQUEST CHANGES":
        text, count = re.subn(
            r"^State:\s*.*$", "State: Building", text, count=1, flags=re.I | re.M
        )
        if count != 1:
            raise SystemExit("ticket State field is ambiguous")
    if text and not text.endswith("\n"):
        text += "\n"
    args.output.write_text(text + addition, encoding="utf-8")


if __name__ == "__main__":
    main()
