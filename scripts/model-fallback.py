#!/usr/bin/env python3
"""Preview or apply one operator-approved mid-ticket model fallback."""

import argparse
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
LEDGER = load_module("ledger_view_fallback", ROOT / "scripts/ledger-view.py")
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
    try:
        rows = LEDGER.effective_rows(factory_root)
        ledger_raw = LEDGER.csv_bytes(rows)
    except (OSError, ValueError) as error:
        raise FallbackError(
            "effective accounting evidence is missing or unsafe"
        ) from error
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


def calculate(args, nonce, migrate_legacy=False, failed_role_only=False):
    repo = Path(args.workdir).resolve()
    factory_root = Path(args.factory_root).resolve()
    plan_path = repo / f"factory/route-plans/{args.ticket}.json"
    raw_journal = plan_path.read_bytes()
    journal = json.loads(raw_journal)
    catalog, routes, _profiles, profile_map = load_policy_files(
        args.catalog, args.profiles
    )
    if (
        migrate_legacy
        and journal.get("schema") == "ticket-model-route-plan/v1"
    ):
        pin_commit = git(
            repo, "log", "-1", "--format=%H", "--",
            f"factory/route-plans/{args.ticket}.json",
        ).decode().strip()
        commit_epoch = int(
            git(repo, "show", "-s", "--format=%ct", pin_commit).decode()
        )
        migrated_at = (
            dt.datetime.fromtimestamp(commit_epoch, dt.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        journal = MANAGER.migrate_v1_plan(
            raw_journal, pin_commit, journal["kit_sha"], migrated_at,
            catalog, routes, profile_map,
        )
        raw_journal = (canonical(journal) + "\n").encode()
    else:
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
    future_roles = (
        [role] if failed_role_only
        else list(ROLE_ORDER[ROLE_ORDER.index(role):])
    )
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
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        return None
    catalog, routes, _profiles, profile_map = load_policy_files(
        args.catalog, args.profiles
    )
    MANAGER.validate_journal(
        journal, catalog, routes, profile_map, allow_historical_active=True
    )
    matches = [
        (index, revision)
        for index, revision in enumerate(journal["revisions"])
        if revision["body"].get("kind") == "fallback"
        and revision["body"].get("approval_receipt") == approval
        and revision["body"].get("reason") == args.reason
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise FallbackError("existing fallback approval is ambiguous")
    index, revision = matches[0]
    body = revision["body"]
    suffix = journal["revisions"][index + 1:]
    if any(item["body"].get("kind") != "release-migration" for item in suffix):
        raise FallbackError("existing fallback has a non-migration suffix")
    _failed, failed_raw, _ledger, _manifests = load_evidence(
        Path(args.factory_root), args.ticket, args.failed_run
    )
    if body.get("failed_manifest_digest") != digest(failed_raw):
        if approval.get("schema") == "ticket-model-fallback-qualification/v1":
            return None
        raise FallbackError("existing fallback revision references different failed evidence")
    marker = f"Model-Route-Revision: {revision['revision_hash']}"
    fallback_kit = (
        suffix[0]["body"]["old_kit_sha"] if suffix else journal["kit_sha"]
    )
    handoff_commits = []
    for commit in git(
        repo, "log", "--format=%H", "HEAD", "--", relative
    ).decode().splitlines():
        if marker not in git(repo, "show", "-s", "--format=%B", commit).decode():
            continue
        try:
            candidate = json.loads(git(repo, "show", f"{commit}:{relative}"))
        except (FallbackError, json.JSONDecodeError):
            continue
        if (
            candidate.get("schema") == journal["schema"]
            and candidate.get("ticket") == journal["ticket"]
            and candidate.get("kit_sha") == fallback_kit
            and candidate.get("revisions") == journal["revisions"][:index + 1]
        ):
            handoff_commits.append(commit)
    if len(handoff_commits) != 1:
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
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        return {
            "recovered": False,
            "schema": "ticket-model-fallback-recovery/v1",
        }
    for revision in reversed(journal.get("revisions", [])):
        approval = revision.get("body", {}).get("approval_receipt")
        if isinstance(approval, dict):
            result = recover_applied(args, approval)
            if result is not None:
                result["approval_receipt"] = approval
                return result
    return {
        "recovered": False,
        "schema": "ticket-model-fallback-recovery/v1",
    }


def apply_result(args, approval, result):
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


def apply(args):
    approval = json.loads(Path(args.approval).read_text())
    recovered = recover_applied(args, approval)
    if recovered is not None:
        return recovered
    result = calculate(args, approval["nonce"])
    if approval.get("approval_hash") != result["approval_hash"]:
        raise FallbackError("Linear approval does not match the current fallback preview")
    return apply_result(args, approval, result)


def qualification_apply(args):
    try:
        journal = json.loads(
            git(
                Path(args.workdir), "show",
                f"HEAD:factory/route-plans/{args.ticket}.json",
            )
        )
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        journal = {}
    for revision in reversed(journal.get("revisions", [])):
        approval = revision.get("body", {}).get("approval_receipt")
        if isinstance(approval, dict):
            recovered = recover_applied(args, approval)
            if recovered is not None:
                return recovered
    result = calculate(
        args, secrets.token_hex(16),
        migrate_legacy=True,
        failed_role_only=True,
    )
    failed = result["failed"]
    if not failed.get("route_id", "").startswith("cursor-"):
        raise FallbackError("qualification fallback requires a failed Cursor route")
    expected_adapter = (
        "claude-code"
        if failed["role"] in {"spec-linter", "test-author", "reviewer"}
        else "codex"
    )
    if result["resolution"]["selections"][failed["role"]]["adapter"] != expected_adapter:
        raise FallbackError("qualification fallback did not resolve the approved direct CLI")
    attempts = 0
    for path in (Path(args.factory_root) / "factory/runs").glob("*.meta"):
        if path.is_file() and not path.is_symlink():
            value = read_meta(path)
            attempts += (
                value.get("ticket") == args.ticket
                and value.get("role") == failed["role"]
                and value.get("kit_sha") == failed["kit_sha"]
                and value.get("go_issued") == "1"
                and value.get("task_submitted") == "1"
            )
    if attempts != 1:
        raise FallbackError("qualification fallback is allowed only after the first role attempt")
    manifest_path = os.environ.get("FACTORY_QUALIFICATION_MANIFEST")
    if manifest_path:
        product = Path(os.environ.get("FACTORY_ROOT", ""))
        expected = product / "factory/QUALIFICATION.json"
        try:
            if (
                not product.is_absolute()
                or Path(manifest_path).resolve(strict=True)
                != expected.resolve(strict=True)
                or expected.is_symlink()
            ):
                raise FallbackError("sealed qualification manifest path is invalid")
        except OSError as error:
            raise FallbackError("sealed qualification manifest path is invalid") from error
        raw = git(product, "show", "HEAD:factory/QUALIFICATION.json")
    else:
        raw = git(
            Path(args.workdir), "show",
            "refs/remotes/origin/main:factory/QUALIFICATION.json",
        )
    try:
        qualification = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FallbackError("protected qualification manifest is malformed") from error
    release_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    authorized_factory_sha = (
        release_sha if manifest_path else result["journal"]["kit_sha"]
    )
    if (
        qualification.get("schema") not in {
            "nysa.software-factory.qualification/v1",
            "nysa.software-factory.qualification/v2",
        }
        or qualification.get("factory_sha") != authorized_factory_sha
        or args.ticket not in qualification.get("tickets", [])
        or not isinstance(qualification.get("generation"), int)
        or qualification["generation"] < 1
    ):
        raise FallbackError("protected qualification manifest does not authorize fallback")
    if manifest_path and (
        not re.fullmatch(r"[0-9a-f]{40}", release_sha)
        or qualification.get("schema") != "nysa.software-factory.qualification/v2"
        or qualification.get("mode") != "successor"
        or not re.fullmatch(r"[0-9a-f]{40}", qualification.get("source_factory_sha", ""))
    ):
        raise FallbackError("sealed successor manifest does not authorize fallback")
    approval = {
        "approval_hash": result["approval_hash"],
        "failed_run_id": args.failed_run,
        "generation": qualification["generation"],
        "manifest_digest": digest(raw),
        "nonce": result["nonce"],
        "schema": "ticket-model-fallback-qualification/v1",
    }
    return apply_result(args, approval, result)


def parser():
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("preview", "apply", "qualification-apply", "recover"))
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
    elif args.action == "qualification-apply":
        value = qualification_apply(args)
    else:
        value = apply(args)
    print(canonical(value))


if __name__ == "__main__":
    try:
        main()
    except (FallbackError, HandoffError, MANAGER.ManagerError, ROUTER.RouterError) as error:
        raise SystemExit(f"model-fallback: {error}")
