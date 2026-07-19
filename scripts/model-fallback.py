#!/usr/bin/env python3
"""Preview or apply one operator-approved mid-ticket model fallback."""

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
from failed_attempt_handoff import (  # noqa: E402
    HandoffError,
    RoleBoundaryPolicy,
    build_handoff_commit,
    preview_handoff,
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANAGER = load_module("model_manager", ROOT / "scripts/model-manager.py")
ROUTER = load_module("model_router_fallback", ROOT / "scripts/model-router.py")
ROLE_ORDER = ("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator")
PRODUCER_BOUNDARY = {"planner": "P", "test-author": "T", "builder": "B"}
REASONS = frozenset((
    "budget_exhausted", "credits_exhausted", "operator_requested",
    "provider_unavailable",
))


class FallbackError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def git(repo, *args, input_bytes=None, extra_env=None):
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra_env:
        environment.update(extra_env)
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "diff.external=",
            *args,
        ],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode:
        raise FallbackError(result.stderr.decode("utf-8", "replace").strip() or "Git failed")
    return result.stdout


def atomic_replace(path, raw):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_meta(path):
    values = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            raise FallbackError("failed manifest is malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise FallbackError("failed manifest contains duplicate fields")
        values[key] = value
    return values


def load_evidence(factory_root, ticket, failed_run):
    runs = factory_root / "factory/runs"
    failed_path = runs / f"{failed_run}.meta"
    if failed_path.is_symlink() or not failed_path.is_file() or failed_path.stat().st_nlink != 1:
        raise FallbackError("failed run manifest is missing or unsafe")
    failed_raw = failed_path.read_bytes()
    failed = read_meta(failed_path)
    terminal_accounting = (
        failed.get("phase"),
        failed.get("accounting_state"),
    )
    if (
        failed.get("ticket") != ticket
        or failed.get("go_issued") != "1"
        or failed.get("task_submitted") != "1"
        or not re.fullmatch(r"[1-9][0-9]{0,2}", failed.get("exit_status", ""))
        or terminal_accounting not in {
            ("completed", "completed"),
            ("completed", "abandoned_conservative"),
            ("abandoned", "abandoned_conservative"),
            ("cancelled_conservative", "cancelled_conservative"),
        }
        or failed.get("role_exit") not in ("provider_failed", "cancelled")
        or failed.get("role") not in ROLE_ORDER
        or not failed.get("route_id")
        or not failed.get("provider_family")
        or not re.fullmatch(r"[0-9a-f]{40}", failed.get("role_head_before", ""))
    ):
        raise FallbackError("run is not an eligible terminal provider failure")
    if (
        failed.get("role_exit") == "cancelled"
        and failed.get("cancellation_reason") not in (
            "budget_exhausted", "operator_requested",
        )
    ):
        raise FallbackError("cancelled run lacks an eligible fallback reason")
    if (runs / f"{failed_run}.pid").exists():
        raise FallbackError("failed run still has a process record")
    ledger_path = factory_root / "factory/runtime-ledger.csv"
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise FallbackError("effective runtime ledger is missing or unsafe")
    ledger_raw = ledger_path.read_bytes()
    with ledger_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    ticket_rows = [row for row in rows if row.get("ticket") == ticket and row.get("run_id")]
    matching = [row for row in ticket_rows if row["run_id"] == failed_run]
    if len(matching) != 1 or not ticket_rows or ticket_rows[-1]["run_id"] != failed_run:
        raise FallbackError("failed run is not the latest unique ticket attempt")
    manifests = []
    for path in sorted(runs.glob("*.meta")):
        if path.is_file() and not path.is_symlink():
            value = read_meta(path)
            if value.get("ticket") == ticket and value.get("go_issued") == "1":
                manifests.append(value)
    return failed, failed_raw, ledger_raw, manifests


def policy_for(path, ticket):
    value = json.loads(path.read_text())
    encoded = canonical(value).replace("TICKET", ticket)
    value = json.loads(encoded)
    value["journal_path"] = f"factory/route-plans/{ticket}.json"
    return RoleBoundaryPolicy.from_dict(value)


def contributors_from(journal, manifests):
    active = MANAGER.active_resolution(journal)
    if active.get("schema") == "model-fallback-resolution/v2":
        contributors = {
            name: list(values)
            for name, values in active["contributor_families"].items()
        }
    else:
        contributors = {"P": [], "T": [], "B": []}
    for value in manifests:
        if value.get("exit_status") != "0":
            continue
        boundary = PRODUCER_BOUNDARY.get(value.get("role"))
        family = value.get("provider_family")
        if boundary and family and family not in contributors[boundary]:
            contributors[boundary].append(family)
    return contributors


def load_policy_files(catalog_path, profiles_path):
    return ROUTER.load_policy(catalog_path, profiles_path)


def calculate(args, nonce):
    repo = Path(args.workdir).resolve()
    factory_root = Path(args.factory_root).resolve()
    plan_path = repo / f"factory/route-plans/{args.ticket}.json"
    raw_journal = plan_path.read_bytes()
    journal = json.loads(raw_journal)
    catalog, routes, _profiles, profile_map = load_policy_files(
        args.catalog, args.profiles
    )
    MANAGER.validate_journal(journal, catalog, routes, profile_map)
    failed, failed_raw, ledger_raw, manifests = load_evidence(
        factory_root, args.ticket, args.failed_run
    )
    if (
        failed.get("role_exit") == "cancelled"
        and failed.get("cancellation_reason") != args.reason
    ):
        raise FallbackError("fallback reason does not match the cancellation receipt")
    role = failed["role"]
    role_head_before = failed["role_head_before"]
    expected_head = git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    git(repo, "merge-base", "--is-ancestor", role_head_before, expected_head)
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
    remote_head = git(
        repo, "ls-remote", "--heads", "--", args.remote,
        f"refs/heads/{branch}",
    ).decode().split()[0]
    if (
        failed.get("role_remote_before") != remote_head
        or failed.get("role_branch_before") != branch
        or failed.get("kit_sha") != journal["kit_sha"]
        or failed.get("policy_hash") != MANAGER.active_resolution(journal)["policy_hash"]
    ):
        raise FallbackError("failed attempt provenance does not match the journal or Git state")
    handoff = preview_handoff(
        repo,
        role=role,
        policy=policy_for(Path(args.boundaries), args.ticket),
        expected_head=expected_head,
        expected_branch=branch,
        remote="origin",
        remote_branch=branch,
        expected_remote_head=remote_head,
        remote_destination=args.remote,
        provider_scan_base=role_head_before,
    )
    readiness = json.loads(Path(args.readiness).read_text())
    contributors = contributors_from(journal, manifests)
    future_roles = list(ROLE_ORDER[ROLE_ORDER.index(role):])
    prior = MANAGER.active_resolution(journal)
    if prior["profile_id"] == "project-policy":
        profile = ROUTER.model_policy_profile(prior["model_policy"], routes)
    else:
        profile = profile_map[prior["profile_id"]]
    resolution = ROUTER.resolve_fallback_policy(
        catalog,
        routes,
        profile,
        readiness,
        prior,
        role,
        failed["route_id"],
        future_roles,
        contributors,
        (
            {"reviewer": args.allow_reviewer_family}
            if args.allow_reviewer_family else None
        ),
    )
    payload = {
        "catalog_hash": resolution["catalog_hash"],
        "failed_manifest_digest": digest(failed_raw),
        "failed_run_id": args.failed_run,
        "journal_digest": digest(raw_journal),
        "journal_revision_hash": journal["revisions"][-1]["revision_hash"],
        "kit_sha": journal["kit_sha"],
        "ledger_digest": digest(ledger_raw),
        "nonce": nonce,
        "project": args.project,
        "reason": args.reason,
        "remote_head": remote_head,
        "remote_url": handoff.remote_url,
        "resolution": resolution,
        "snapshot_digest": handoff.snapshot_digest,
        "snapshot_preview_digest": handoff.preview_digest,
        "ticket": args.ticket,
    }
    return {
        "approval_hash": digest(canonical(payload).encode()),
        "failed": failed,
        "failed_manifest_digest": digest(failed_raw),
        "handoff": handoff,
        "journal": journal,
        "journal_path": plan_path,
        "nonce": nonce,
        "payload": payload,
        "policy": policy_for(Path(args.boundaries), args.ticket),
        "resolution": resolution,
        "catalog": catalog,
        "routes": routes,
        "profile_map": profile_map,
    }


def preview(args):
    result = calculate(args, secrets.token_hex(16))
    return {
        "approval_hash": result["approval_hash"],
        "failed_run_id": args.failed_run,
        "nonce": result["nonce"],
        "reason": args.reason,
        "resolution": result["resolution"],
        "schema": "ticket-model-fallback-preview/v1",
        "snapshot_digest": result["handoff"].snapshot_digest,
        "linear_comment": (
            f"FACTORY MODEL FALLBACK APPROVAL: {result['approval_hash']} "
            f"RUN: {args.failed_run} REASON: {args.reason} "
            f"NONCE: {result['nonce']}"
        ),
    }


def recover_applied(args, approval):
    repo = Path(args.workdir)
    relative = f"factory/route-plans/{args.ticket}.json"
    path = repo / relative
    try:
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        committed = git(repo, "show", f"HEAD:{relative}")
        journal = json.loads(committed)
        revision = journal["revisions"][-1]
        body = revision["body"]
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        return None
    if (
        body.get("kind") != "fallback"
        or body.get("approval_receipt") != approval
        or body.get("reason") != args.reason
    ):
        return None
    _failed, failed_raw, _ledger, _manifests = load_evidence(
        Path(args.factory_root), args.ticket, args.failed_run
    )
    if body.get("failed_manifest_digest") != digest(failed_raw):
        raise FallbackError("existing fallback revision references different failed evidence")
    message = git(repo, "show", "-s", "--format=%B", "HEAD").decode()
    if f"Model-Route-Revision: {revision['revision_hash']}" not in message:
        raise FallbackError("existing fallback journal is not committed by its handoff")
    descriptor, temporary_index = tempfile.mkstemp(prefix=".fallback-index.")
    os.close(descriptor)
    os.unlink(temporary_index)
    try:
        index_environment = {"GIT_INDEX_FILE": temporary_index}
        git(repo, "read-tree", "HEAD", extra_env=index_environment)
        status = git(
            repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            extra_env=index_environment,
        )
    finally:
        try:
            os.unlink(temporary_index)
        except FileNotFoundError:
            pass
    allowed_status = {
        b"",
        f" M {relative}\n".encode(),
    }
    if status not in allowed_status:
        raise FallbackError("worktree drifted after the fallback handoff commit")
    git(repo, "read-tree", "HEAD")
    if path.read_bytes() != committed:
        atomic_replace(path, committed)
    return {
        "approval_hash": approval["approval_hash"],
        "commit_sha": head,
        "failed_run_id": args.failed_run,
        "recovered": True,
        "revision_hash": revision["revision_hash"],
        "schema": "ticket-model-fallback-result/v1",
        "snapshot_digest": body["approved_snapshot_digest"],
    }


def recover(args):
    repo = Path(args.workdir)
    relative = f"factory/route-plans/{args.ticket}.json"
    try:
        committed = git(repo, "show", f"HEAD:{relative}")
        journal = json.loads(committed)
        approval = journal["revisions"][-1]["body"]["approval_receipt"]
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        return {
            "recovered": False,
            "schema": "ticket-model-fallback-recovery/v1",
        }
    if not isinstance(approval, dict):
        raise FallbackError("committed fallback approval receipt is malformed")
    result = recover_applied(args, approval)
    if result is None:
        return {
            "recovered": False,
            "schema": "ticket-model-fallback-recovery/v1",
        }
    result["approval_receipt"] = approval
    return result


def apply(args):
    approval = json.loads(Path(args.approval).read_text())
    recovered = recover_applied(args, approval)
    if recovered is not None:
        return recovered
    result = calculate(args, approval["nonce"])
    if approval.get("approval_hash") != result["approval_hash"]:
        raise FallbackError("Linear approval does not match the current fallback preview")
    created_at = (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        .replace("+00:00", "Z")
    )
    journal = MANAGER.append_fallback_revision(
        result["journal"],
        result["resolution"],
        result["failed_manifest_digest"],
        result["handoff"].snapshot_digest,
        args.reason,
        approval,
        created_at,
        result["catalog"],
        result["routes"],
        result["profile_map"],
    )
    journal_raw = (canonical(journal) + "\n").encode()
    revision_hash = journal["revisions"][-1]["revision_hash"]
    commit = build_handoff_commit(
        result["handoff"],
        result["policy"],
        revision_hash=revision_hash,
        commit_timestamp=str(int(dt.datetime.now().timestamp())) + " +0000",
        journal_content=journal_raw,
        subject=f"{args.ticket}: preserve failed attempt and revise model route",
    )
    repo = Path(args.workdir)
    ref = "refs/heads/" + result["handoff"].branch
    git(repo, "update-ref", ref, commit.commit, commit.parent)
    git(repo, "read-tree", commit.commit)
    atomic_replace(result["journal_path"], journal_raw)
    return {
        "approval_hash": result["approval_hash"],
        "commit_sha": commit.commit,
        "failed_run_id": args.failed_run,
        "revision_hash": revision_hash,
        "schema": "ticket-model-fallback-result/v1",
        "snapshot_digest": result["handoff"].snapshot_digest,
    }


def parser():
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("preview", "apply", "recover"))
    value.add_argument("--workdir", required=True)
    value.add_argument("--factory-root", required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--ticket", required=True)
    value.add_argument("--failed-run", required=True)
    value.add_argument("--reason", required=True, choices=sorted(REASONS))
    value.add_argument("--allow-reviewer-family")
    value.add_argument("--readiness", required=True)
    value.add_argument("--remote", required=True)
    value.add_argument("--approval")
    value.add_argument(
        "--catalog", default=str(ROOT / "scripts/model-routing/catalog-v1.json")
    )
    value.add_argument(
        "--profiles", default=str(ROOT / "scripts/model-routing/profiles-v1.json")
    )
    value.add_argument(
        "--boundaries",
        default=str(ROOT / "scripts/model-routing/handoff-boundaries-v1.json"),
    )
    return value


def main():
    args = parser().parse_args()
    if args.action == "apply" and not args.approval:
        raise FallbackError("apply requires a Linear approval")
    if args.action == "preview":
        value = preview(args)
    elif args.action == "recover":
        value = recover(args)
    else:
        value = apply(args)
    print(canonical(value))


if __name__ == "__main__":
    try:
        main()
    except (FallbackError, HandoffError, MANAGER.ManagerError, ROUTER.RouterError) as error:
        raise SystemExit(f"model-fallback: {error}")
