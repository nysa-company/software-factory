#!/usr/bin/env python3
"""Bind one unmatched successful Reviewer run to canonical ticket evidence."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess


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


def review_head_matches(
    ticket_file: Path, ticket: str, reviewed: str, current: str
) -> bool:
    if reviewed == current:
        return True
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed):
        return False
    try:
        worktree = ticket_file.parents[2]
        ancestor = subprocess.run(
            ["git", "-C", str(worktree), "merge-base", "--is-ancestor", reviewed, current],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ancestor.returncode != 0:
            return False
        changed = subprocess.check_output(
            ["git", "-C", str(worktree), "diff", "--name-only", "-z", reviewed, current],
            stderr=subprocess.DEVNULL,
        ).decode().rstrip("\0").split("\0")
    except (IndexError, OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return False
    return set(filter(None, changed)) <= {f"factory/tickets/{ticket}.md"}


def canonicalize_reviews(
    text: str,
    successful: list[tuple[str, str, dict[str, str], Path]],
    imported: int,
    verdicts: list[tuple[int, str]],
    owners: dict[int, str],
    parser_path: Path,
    contract_version: str,
) -> str:
    parse_verdict, review_text = load_parser(parser_path)
    for offset, (_, _, value, output) in enumerate(successful, start=1):
        round_number = imported + offset
        raw = output.read_text(encoding="utf-8", errors="replace")
        try:
            verdict, owner = parse_verdict(
                raw, value.get("adapter", ""), contract_version
            )
            detail = quote_review(
                review_text(raw, value.get("adapter", ""), contract_version),
                round_number,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if (
            round_number > len(verdicts)
            or verdicts[round_number - 1] != (round_number, verdict)
            or owners.get(round_number, "") != owner
        ):
            raise SystemExit("reviewer detail contradicts canonical verdict history")
        pattern = re.compile(
            rf"^Reviewer round {round_number} signed detail:\n\n.*?"
            rf"(?=^reviewer round {round_number}:)",
            re.M | re.S,
        )
        text, count = pattern.subn(detail, text, count=1)
        if count != 1:
            raise SystemExit("reviewer signed detail is missing or ambiguous")
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--ticket-file", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.contract_version not in {"1.7.0", "1.8.0"}:
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

    imported = 0
    if args.checkpoint is not None:
        if not regular(args.checkpoint):
            raise SystemExit("reviewer checkpoint is unsafe")
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        records = [
            item for item in checkpoint.get("tickets", [])
            if item.get("ticket") == args.ticket
        ]
        if (
            checkpoint.get("schema") != "factory-dev-product-checkpoint-import/v2"
            or len(records) != 1
            or not isinstance(records[0].get("roles"), list)
        ):
            raise SystemExit("reviewer checkpoint is malformed")
        imported = records[0]["roles"].count("reviewer")
        if imported > len(verdicts):
            raise SystemExit("reviewer checkpoint exceeds canonical verdict history")

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
            value.get("contract_version") not in {"1.7.0", "1.8.0"}
            or value.get("role_exit") != "ok"
            or not regular(output)
        ):
            raise SystemExit("successful reviewer evidence is not trusted Contract 1.7 output")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        if value.get("output_sha256") != digest:
            raise SystemExit("successful reviewer output digest does not match its manifest")
        successful.append((value.get("started_at", ""), manifest.name, value, output))
    successful.sort(key=lambda item: (item[0], item[1]))

    matched = len(verdicts) - imported
    if imported + len(successful) == len(verdicts):
        text = canonicalize_reviews(
            text,
            successful,
            imported,
            verdicts,
            owners,
            Path(__file__).with_name("reviewer-verdict.py"),
            args.contract_version,
        )
        args.output.write_text(text, encoding="utf-8")
        return
    if imported + len(successful) != len(verdicts) + 1:
        raise SystemExit("reviewer runs and canonical verdicts are ambiguous")
    if state[0].lower() != "review":
        raise SystemExit("unmatched reviewer evidence requires Review state")

    text = canonicalize_reviews(
        text,
        successful[:matched],
        imported,
        verdicts,
        owners,
        Path(__file__).with_name("reviewer-verdict.py"),
        args.contract_version,
    )
    _, _, value, output = successful[-1]
    reviewed_head = value.get("role_head_before", "")
    if (
        reviewed_head != value.get("role_remote_before")
        or not review_head_matches(args.ticket_file, args.ticket, reviewed_head, args.head)
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
