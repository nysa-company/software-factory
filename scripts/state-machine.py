#!/usr/bin/env python3
"""Contract 1.8 deterministic transition resolver and one-use receipts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Any


SCHEMA = "nysa.software-factory.state-machine/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ROLE = re.compile(r"^(planner|spec-linter|test-author|builder|reviewer|narrator)$")
TARGET_STATE = {
    "planner": "Planning",
    "spec-linter": "Planning",
    "test-author": "Building",
    "builder": "Building",
    "reviewer": "Review",
    "narrator": "Review",
}


class StateError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise StateError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def safe_state_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("controller state directory is unsafe")
    return path


def safe_receipt(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
    ):
        raise StateError("transition receipt is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise StateError("transition receipt is malformed")
    immutable = {key: item for key, item in value.items() if key not in {
        "consumed", "consumed_at_epoch", "receipt_sha256",
    }}
    digest = hashlib.sha256(canonical(immutable)).hexdigest()
    if value.get("receipt_sha256") != digest:
        raise StateError("transition receipt digest is invalid")
    if not isinstance(value.get("consumed"), bool):
        raise StateError("transition receipt consumption state is invalid")
    return value


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def ticket_evidence_digest(factory: Path, ticket: str) -> str:
    selected: list[tuple[str, bytes]] = []
    runs = factory / "runs"
    if runs.exists():
        info = runs.lstat()
        if not stat.S_ISDIR(info.st_mode) or runs.is_symlink():
            raise StateError("run manifest directory is unsafe")
        for path in sorted(runs.glob("*.meta")):
            info = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise StateError("run manifest is unsafe")
            raw = path.read_bytes()
            values: dict[str, str] = {}
            for line in raw.decode("utf-8").splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in values:
                    raise StateError("run manifest is malformed")
                values[key] = value
            if (
                values.get("ticket") == ticket
                and values.get("accounting_state") not in {None, "", "reserved"}
            ):
                selected.append((path.name, raw))
                output = path.with_suffix(".out")
                if output.is_file() and not output.is_symlink():
                    selected.append((output.name, output.read_bytes()))
    digest = hashlib.sha256()
    for name, raw in selected:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def stage_role(stage: str) -> str | None:
    action, separator, detail = stage.partition(" ")
    if action in {"RUN", "FIX"} and separator and ROLE.fullmatch(detail):
        return detail
    if action in {
        "AWAIT-OPERATOR", "AWAIT-MERGE", "AWAIT_BUDGET", "COMPLETE", "REFUSE",
    }:
        return None
    raise StateError("state resolver returned an unsupported transition")


def current_state(workdir: Path, ticket: str) -> str:
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text(
        encoding="utf-8"
    )
    values = re.findall(r"^State:\s*(.*?)\s*$", text, re.IGNORECASE | re.MULTILINE)
    if len(values) != 1:
        raise StateError("ticket state is ambiguous")
    return values[0]


def core(
    args: argparse.Namespace, stage: str, role: str | None
) -> dict[str, Any]:
    workdir = args.workdir.resolve(strict=True)
    factory = args.factory_root.resolve(strict=True) / "factory"
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
    route = workdir / "factory" / "route-plans" / f"{args.ticket}.json"
    passport = args.state_dir / "passports" / f"{args.ticket}.json"
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not origin or any(character in origin for character in "\n\r\t"):
        raise StateError("certified product origin is unavailable")
    return {
        "branch": branch,
        "contract_version": args.contract_version,
        "evidence_sha256": ticket_evidence_digest(factory, args.ticket),
        "factory_sha": args.factory_sha,
        "head_sha": git(workdir, "rev-parse", "HEAD"),
        "head_tree": git(workdir, "rev-parse", "HEAD^{tree}"),
        "lease_sha256": (
            hashlib.sha256(args.lease.encode()).hexdigest()
            if args.lease else None
        ),
        "passport_sha256": (
            hashlib.sha256(passport.read_bytes()).hexdigest()
            if passport.is_file() and not passport.is_symlink()
            else None
        ),
        "product_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "project": args.project,
        "role": role,
        "route_plan_sha256": (
            hashlib.sha256(route.read_bytes()).hexdigest()
            if route.is_file() and not route.is_symlink()
            else None
        ),
        "schema": RECEIPT_SCHEMA,
        "stage": stage,
        "ticket": args.ticket,
        "ticket_blob": git(
            workdir, "rev-parse", f"HEAD:factory/tickets/{args.ticket}.md"
        ),
    }


def issue(args: argparse.Namespace, stage: str) -> dict[str, Any]:
    role = stage_role(stage)
    value = core(args, stage, role)
    path = args.state_dir / f"{args.ticket}.json"
    prior: dict[str, Any] | None = None
    if path.exists() or path.is_symlink():
        prior = safe_receipt(path)
        prior_core = {
            key: item for key, item in prior.items()
            if key not in {
                "consumed", "consumed_at_epoch", "nonce", "parent_digest",
                "receipt_sha256",
            }
        }
        if prior_core == value and not prior["consumed"]:
            return prior
        value["parent_digest"] = prior["receipt_sha256"]
    value["nonce"] = secrets.token_hex(16)
    immutable = dict(value)
    value["receipt_sha256"] = hashlib.sha256(canonical(immutable)).hexdigest()
    value["consumed"] = False
    write_atomic(path, value)
    return value


def verify(args: argparse.Namespace, *, consume: bool) -> dict[str, Any]:
    path = args.state_dir / f"{args.ticket}.json"
    lock_path = args.state_dir / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            descriptor = -1
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = safe_receipt(path)
            if value.get("receipt_sha256") != args.receipt:
                raise StateError("transition receipt does not match")
            expected = core(args, value["stage"], value.get("role"))
            actual = {
                key: item for key, item in value.items()
                if key not in {
                    "consumed", "consumed_at_epoch", "nonce", "parent_digest",
                    "receipt_sha256",
                }
            }
            if actual != expected:
                raise StateError("transition receipt inputs drifted")
            if args.role and value.get("role") != args.role:
                raise StateError("transition receipt does not authorize the role")
            if consume:
                if value["consumed"]:
                    raise StateError("transition receipt was already consumed")
                value["consumed"] = True
                value["consumed_at_epoch"] = int(__import__("time").time())
                write_atomic(path, value)
            elif args.require_used and not value["consumed"]:
                raise StateError("transition receipt has not been consumed")
            elif not args.require_used and value["consumed"]:
                raise StateError("transition receipt was already consumed")
            return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run_helper(args: argparse.Namespace, script: str, *arguments: str) -> str:
    result = subprocess.run(
        ["/bin/bash", str(args.kit_dir / "scripts" / script), *arguments],
        cwd=args.workdir,
        env={
            **os.environ,
            "FACTORY_ROOT": str(args.factory_root),
            "FACTORY_TRANSITION_STATE_DIR": str(args.state_dir),
            "FACTORY_RELEASE_SHA": args.factory_sha,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode:
        raise StateError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def resolve(args: argparse.Namespace) -> str:
    command = ["--ticket", args.ticket]
    if args.lease:
        command.extend(["--lease", args.lease])
    command.extend(["--workdir", str(args.workdir)])
    output = run_helper(args, "next-stage.sh", *command)
    return next((line.strip() for line in output.splitlines() if line.strip()), "")


def transition(args: argparse.Namespace, state: str) -> None:
    run_helper(
        args, "ticket-state.sh", "--ticket", args.ticket,
        "--workdir", str(args.workdir), "--action", "transition", "--state", state,
    )


def migrate_passport(args: argparse.Namespace) -> None:
    passport = args.state_dir / "passports" / f"{args.ticket}.json"
    if not passport.exists() and not passport.is_symlink():
        return
    result = subprocess.run(
        [
            __import__("sys").executable,
            "-B",
            str(args.kit_dir / "scripts" / "ticket-passport.py"),
            "migrate",
            "--factory-root", str(args.factory_root),
            "--workdir", str(args.workdir),
            "--state-dir", str(args.state_dir),
            "--ticket", args.ticket,
            "--contract-version", args.contract_version,
            "--factory-sha", args.factory_sha,
            "--project", args.project,
            "--publication-state", "preserve",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise StateError("authenticated passport migration failed")


def next_transition(args: argparse.Namespace) -> dict[str, Any]:
    current = current_state(args.workdir, args.ticket)
    if current.casefold() in {"backlog", "blocked-escalated"}:
        run_helper(
            args, "ticket-state.sh", "--ticket", args.ticket,
            "--workdir", str(args.workdir), "--action", "materialize",
        )
    stage = resolve(args)
    role = stage_role(stage)
    if role:
        current = current_state(args.workdir, args.ticket)
        target = TARGET_STATE[role]
        while current != target:
            if current == "Ready":
                transition(args, "Planning")
            elif current == "Planning" and target in {"Building", "Review"}:
                transition(args, "Building")
            elif current == "Building" and target == "Review":
                transition(args, "Review")
            elif current == "Review" and target == "Building":
                transition(args, "Building")
            else:
                raise StateError(f"state machine cannot enter {target} from {current}")
            current = current_state(args.workdir, args.ticket)
        if resolve(args) != stage:
            raise StateError("transition changed the resolved stage")
    migrate_passport(args)
    receipt = issue(args, stage)
    return {
        "action": stage.partition(" ")[0],
        "detail": stage.partition(" ")[2] or None,
        "receipt": receipt["receipt_sha256"],
        "role": role,
        "schema": SCHEMA,
        "stage": stage,
        "status": "ok",
        "ticket": args.ticket,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("next", "verify", "consume"))
    value.add_argument("--factory-root", required=True, type=Path)
    value.add_argument("--workdir", required=True, type=Path)
    value.add_argument("--kit-dir", required=True, type=Path)
    value.add_argument("--state-dir", required=True, type=Path)
    value.add_argument("--ticket", required=True)
    value.add_argument("--contract-version", required=True)
    value.add_argument("--factory-sha", required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--lease", default="")
    value.add_argument("--receipt", default="")
    value.add_argument("--role", default="")
    value.add_argument("--require-used", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or args.contract_version != "1.8.0"
            or not SHA.fullmatch(args.factory_sha)
            or (args.receipt and not DIGEST.fullmatch(args.receipt))
            or (args.role and not ROLE.fullmatch(args.role))
        ):
            raise StateError("invalid state-machine arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.kit_dir = args.kit_dir.resolve(strict=True)
        args.state_dir = safe_state_dir(args.state_dir)
        if args.action == "next":
            result = next_transition(args)
        else:
            if not args.receipt:
                raise StateError("receipt is required")
            receipt = verify(args, consume=args.action == "consume")
            result = {
                "receipt": receipt["receipt_sha256"],
                "schema": SCHEMA,
                "stage": receipt["stage"],
                "status": "ok",
                "ticket": args.ticket,
            }
        print(json.dumps(result, sort_keys=True))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        StateError,
        subprocess.SubprocessError,
        UnicodeError,
    ) as error:
        print(json.dumps({
            "error": str(error), "schema": SCHEMA, "status": "error",
            "ticket": args.ticket,
        }, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
