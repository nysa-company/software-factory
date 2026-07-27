#!/usr/bin/env python3
"""Reduce Contract 1.8 qualification evidence against protected GitHub truth."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


SCHEMA = "nysa.software-factory.qualification-report/v1"
MANIFEST_SCHEMA = "nysa.software-factory.qualification/v2"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^T-[0-9]+$")
ROLES = {"planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"}


class QualificationError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def regular(path: Path, mode: int | None = None, limit: int = 5_000_000) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or info.st_size > limit
        ):
            raise QualificationError(f"unsafe qualification evidence: {path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise QualificationError(
            result.stderr.strip() or result.stdout.strip() or "evidence query failed"
        )
    return result.stdout


def project_value(product: Path, name: str) -> str:
    values = re.findall(
        rf"^(?:export\s+)?{re.escape(name)}\s*=\s*['\"]?([^'\"\s]+)['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise QualificationError(f"{name} is missing or ambiguous")
    return values[0]


def event_records(path: Path, factory_sha: str) -> list[dict[str, Any]]:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise QualificationError("controller event directory is unsafe")
    records, selected = [], []
    for item in sorted(path.glob("*.json")):
        value = json.loads(regular(item, 0o600))
        digest = value.pop("event_sha256", "")
        if (
            value.get("schema") != EVENT_SCHEMA
            or digest != hashlib.sha256(canonical(value).encode()).hexdigest()
        ):
            raise QualificationError("controller event evidence is invalid")
        value["event_sha256"] = digest
        records.append(value)
        if value.get("factory_sha") == factory_sha:
            selected.append(value)
    starts = [
        item["observed_at_epoch_ns"]
        for item in selected if item.get("event") == "restart_boundary"
    ]
    if starts and any(
        item["observed_at_epoch_ns"] >= starts[0]
        and item.get("factory_sha") != factory_sha
        for item in records
    ):
        raise QualificationError("Factory candidate changed during qualification")
    return sorted(selected, key=lambda value: value["observed_at_epoch_ns"])


def iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise QualificationError("GitHub timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise QualificationError("GitHub timestamp lacks a timezone")
    return parsed


def verify(
    manifest: dict[str, Any],
    passports: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    terminals: dict[str, dict[str, Any]],
    pull_requests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tickets = manifest.get("tickets")
    factory_sha = manifest.get("factory_sha")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("contract_version") != "1.8.0"
        or manifest.get("capacity") != 4
        or manifest.get("target_done") != 4
        or manifest.get("budget_usd") != "100.000000"
        or manifest.get("per_ticket_budget_usd") != "25.000000"
        or manifest.get("per_run_budget_usd") != "2.000000"
        or not SHA.fullmatch(factory_sha or "")
        or not isinstance(tickets, list)
        or len(tickets) != 4
        or len(set(tickets)) != 4
        or any(not TICKET.fullmatch(ticket) for ticket in tickets)
        or set(passports) != set(tickets)
        or set(terminals) != set(tickets)
        or set(pull_requests) != set(tickets)
    ):
        raise QualificationError("qualification inputs are incomplete")

    run_ids: set[str] = set()
    manifest_digests: set[str] = set()
    ticket_reports = []
    total = 0
    for ticket in tickets:
        passport = passports[ticket]
        charges = passport.get("charge_records")
        completed = passport.get("completed_role_evidence")
        if (
            passport.get("ticket") != ticket
            or passport.get("factory_sha") != factory_sha
            or passport.get("contract_version") != "1.8.0"
            or passport.get("publication_state") != "merged"
            or not isinstance(charges, list)
            or not isinstance(completed, list)
        ):
            raise QualificationError(f"{ticket} passport is not terminal")
        if (
            any(
                not isinstance(item.get("charge_micro_usd"), int)
                or item["charge_micro_usd"] < 0
                or item["charge_micro_usd"] > 2_000_000
                or item.get("factory_sha") != factory_sha
                or item.get("contract_version") != "1.8.0"
                or not DIGEST.fullmatch(item.get("manifest_sha256", ""))
                for item in charges
            )
        ):
            raise QualificationError(f"{ticket} charges do not match the envelope")
        charge = sum(item["charge_micro_usd"] for item in charges)
        if (
            charge != passport.get("cumulative_charges_micro_usd")
            or charge > 25_000_000
        ):
            raise QualificationError(f"{ticket} charges do not match the envelope")
        for item in charges:
            if (
                not isinstance(item.get("run_id"), str)
                or not item["run_id"]
                or item["run_id"] in run_ids
                or item["manifest_sha256"] in manifest_digests
            ):
                raise QualificationError("run or charge evidence was duplicated")
            run_ids.add(item["run_id"])
            manifest_digests.add(item["manifest_sha256"])
        role_heads = [(item.get("role"), item.get("head_before")) for item in completed]
        if (
            not ROLES.issubset({role for role, _ in role_heads})
            or len(role_heads) != len(set(role_heads))
            or any(
                item.get("factory_sha") != factory_sha
                or item.get("contract_version") != "1.8.0"
                or not SHA.fullmatch(item.get("head_before", ""))
                or not DIGEST.fullmatch(item.get("transition_receipt_sha256", ""))
                for item in completed
            )
        ):
            raise QualificationError(f"{ticket} role evidence was replayed or is incomplete")
        done = terminals[ticket]
        pr = pull_requests[ticket]
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if (
            done.get("schema") != "nysa.software-factory.ticket-done/v1"
            or done.get("ticket") != ticket
            or done.get("kit_sha") != factory_sha
            or done.get("required_checks") != done.get("successful_checks")
            or not done.get("required_checks")
            or pr.get("number") != done.get("pr_number")
            or pr.get("headRefName") != passport.get("branch")
            or pr.get("headRefOid") != done.get("approved_pr_head")
            or pr.get("baseRefName") != "main"
            or pr.get("state") != "MERGED"
            or merge != done.get("merge_commit")
            or not SHA.fullmatch(merge or "")
            or passport.get("head_sha") != done.get("approved_pr_head")
        ):
            raise QualificationError(f"{ticket} protected merge truth does not match")
        total += charge
        ticket_reports.append({
            "charge_micro_usd": charge,
            "merge_commit": merge,
            "pr_head": pr["headRefOid"],
            "pr_number": pr["number"],
            "roles": len(completed),
            "ticket": ticket,
        })
    if total > 100_000_000:
        raise QualificationError("qualification exceeded its total budget")

    def matching(name: str) -> list[dict[str, Any]]:
        return [item for item in events if item.get("event") == name]

    boundaries = matching("restart_boundary")
    recoveries = matching("controller_recovered")
    relocations = matching("cell_relocated")
    if (
        len(boundaries) != 1
        or sorted(boundaries[0].get("tickets", [])) != sorted(tickets)
        or len(recoveries) != 1
        or sorted(recoveries[0].get("tickets", [])) != sorted(tickets)
        or len(relocations) != 1
        or relocations[0].get("ticket") not in tickets
        or {item.get("ticket") for item in matching("ticket_complete")} != set(tickets)
    ):
        raise QualificationError("restart, relocation, or completion proof is missing")
    holder = None
    acquired: set[str] = set()
    released: set[str] = set()
    for item in events:
        if item.get("event") == "publication_acquired":
            if holder is not None:
                raise QualificationError("publication leases overlapped")
            holder = item.get("ticket")
            acquired.add(holder)
        elif item.get("event") == "publication_released":
            if item.get("ticket") != holder:
                raise QualificationError("publication lease release is out of order")
            released.add(holder)
            holder = None
    if holder is not None or acquired != set(tickets) or released != set(tickets):
        raise QualificationError("publication serialization proof is incomplete")
    created = [iso(pull_requests[ticket]["createdAt"]) for ticket in tickets]
    merged = [iso(pull_requests[ticket]["mergedAt"]) for ticket in tickets]
    if max(created) > min(merged):
        raise QualificationError("four PRs did not validate concurrently")
    return {
        "factory_sha": factory_sha,
        "schema": SCHEMA,
        "status": "green",
        "tickets": ticket_reports,
        "total_charge_micro_usd": total,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--kit-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        product = args.product_root.resolve(strict=True)
        state = args.state_dir.resolve(strict=True)
        manifest = json.loads(
            regular(product / "factory/QUALIFICATION.json").decode("utf-8")
        )
        spec = importlib.util.spec_from_file_location(
            "ticket_passport", args.kit_dir / "scripts/ticket-passport.py"
        )
        if not spec or not spec.loader:
            raise QualificationError("passport verifier is unavailable")
        passport_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(passport_module)
        secret = passport_module.key(state)
        passports = {
            ticket: passport_module.load_passport(
                state / "passports" / f"{ticket}.json", secret
            )[0]
            for ticket in manifest["tickets"]
        }
        command(
            "git", "-C", str(product), "fetch", "--quiet", "origin",
            "+main:refs/remotes/origin/main",
        )
        protected = command(
            "git", "-C", str(product), "rev-parse", "origin/main"
        ).strip()
        repo = project_value(product, "GH_REPO")
        terminals, pull_requests = {}, {}
        for ticket in manifest["tickets"]:
            terminals[ticket] = json.loads(command(
                "git", "-C", str(product), "show",
                f"origin/main:factory/attestations/{ticket}/done.json",
            ))
            pr_number = terminals[ticket]["pr_number"]
            pull_requests[ticket] = json.loads(command(
                "gh", "pr", "view", str(pr_number), "--repo", repo, "--json",
                "number,headRefName,headRefOid,baseRefName,state,createdAt,mergedAt,mergeCommit",
            ))
            merge = (pull_requests[ticket].get("mergeCommit") or {}).get("oid", "")
            command(
                "git", "-C", str(product), "merge-base", "--is-ancestor",
                merge, "origin/main",
            )
            checks = json.loads(command(
                "gh", "api", f"repos/{repo}/commits/{merge}/check-runs",
                "--method", "GET", "-f", "per_page=100",
            )).get("check_runs", [])
            successes = {
                item.get("name") for item in checks
                if item.get("status") == "completed"
                and item.get("conclusion") in {"success", "neutral", "skipped"}
            }
            if not set(terminals[ticket]["required_checks"]).issubset(successes):
                raise QualificationError(f"{ticket} protected checks are not green")
        report = verify(
            manifest, passports,
            event_records(state / "events", manifest["factory_sha"]),
            terminals, pull_requests,
        )
        report["protected_main_sha"] = protected
        report["report_sha256"] = hashlib.sha256(canonical(report).encode()).hexdigest()
        destination = state / f"qualification-report-{manifest['factory_sha']}.json"
        raw = (canonical(report) + "\n").encode()
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if regular(destination, 0o600) != raw:
                raise QualificationError("immutable qualification report changed")
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        print(canonical(report))
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, QualificationError,
        subprocess.SubprocessError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
