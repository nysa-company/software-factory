#!/usr/bin/env python3
"""Reuse protected attestation checks for pre-passport route recovery."""

from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


class RouteEvidenceError(ValueError):
    pass


def _attest():
    path = Path(__file__).resolve().parent.parent / "ticket-attest.py"
    spec = importlib.util.spec_from_file_location("factory_ticket_attest", path)
    if spec is None or spec.loader is None:
        raise RouteEvidenceError("ticket route attestation is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manager():
    path = Path(__file__).resolve().parent.parent / "model-manager.py"
    spec = importlib.util.spec_from_file_location("factory_model_manager", path)
    if spec is None or spec.loader is None:
        raise RouteEvidenceError("ticket route manager is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_route(product: Path, worktree: Path, ticket: str, kit_sha: str) -> None:
    try:
        _attest().route_plan_evidence(worktree, product, ticket, kit_sha, [])
    except Exception as error:
        raise RouteEvidenceError("ticket route evidence is invalid") from error


def exact_kit_sha_change(before: bytes, after: bytes) -> bool:
    pattern = re.compile(rb"^Kit-SHA:\s*([0-9a-f]{40})\s*$", re.M)
    old = pattern.findall(before)
    new = pattern.findall(after)
    return (
        len(old) == len(new) == 1
        and old != new
        and pattern.sub(b"Kit-SHA: <bound>", before)
        == pattern.sub(b"Kit-SHA: <bound>", after)
    )


def journal_extends(before: bytes, after: bytes) -> bool:
    try:
        prior = json.loads(before)
        current = json.loads(after)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if current.get("schema") != "ticket-model-route-journal/v2":
        return False
    revisions = current.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        return False
    if prior.get("schema") == "ticket-model-route-plan/v1":
        try:
            body = revisions[0]["body"]
            embedded = base64.b64decode(body["legacy_plan_b64"], validate=True)
        except (KeyError, TypeError, ValueError):
            return False
        return (
            body.get("kind") == "migration"
            and embedded == before
            and body.get("legacy_plan_sha256") == hashlib.sha256(before).hexdigest()
            and body.get("old_kit_sha") == prior.get("kit_sha")
            and body.get("new_kit_sha") == current.get("kit_sha")
            and len(revisions) == 1
        )
    prior_revisions = prior.get("revisions")
    appended = revisions[-1].get("body", {}) if revisions else {}
    return (
        prior.get("schema") == "ticket-model-route-journal/v2"
        and isinstance(prior_revisions, list)
        and revisions[:-1] == prior_revisions
        and appended.get("kind") == "release-migration"
        and appended.get("old_kit_sha") == prior.get("kit_sha")
        and appended.get("new_kit_sha") == current.get("kit_sha")
    )


def authenticated_fallback_head(
    product: Path, worktree: Path, ticket: str, branch: str,
    commit: str, route_raw: bytes,
) -> None:
    """Authenticate the exact qualification fallback sealed by a receipt head."""
    try:
        journal = json.loads(route_raw)
        manager = _manager()
        catalog, routes, _profiles, profiles = manager.ROUTER.load_policy()
        manager.validate_journal(
            journal, catalog, routes, profiles, allow_historical_active=True,
        )
        fallbacks = [
            revision for revision in journal["revisions"]
            if revision["body"].get("kind") == "fallback"
        ]
        if len(fallbacks) != 1 or fallbacks[0] != journal["revisions"][-1]:
            raise RouteEvidenceError("receipt route is not an exact fallback head")
        revision = fallbacks[0]
        body = revision["body"]
        approval = body["approval_receipt"]
        base_keys = {
            "approval_hash", "failed_run_id", "generation", "manifest_digest",
            "nonce", "schema",
        }
        if (
            not isinstance(approval, dict)
            or set(approval) not in (base_keys, base_keys | {"product_sha", "product_tree"})
            or approval.get("schema") != "ticket-model-fallback-qualification/v1"
            or not re.fullmatch(r"[0-9a-f]{64}", approval.get("approval_hash", ""))
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", approval.get("failed_run_id", ""))
            or not isinstance(approval.get("generation"), int)
            or approval["generation"] < 1
            or not re.fullmatch(r"[0-9a-f]{64}", approval.get("manifest_digest", ""))
            or not re.fullmatch(r"[0-9a-f]{32}", approval.get("nonce", ""))
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", approval.get(key, ""))
                for key in set(approval) & {"product_sha", "product_tree"}
            )
        ):
            raise RouteEvidenceError("fallback approval receipt is invalid")
        manifest = product / "factory/runs" / f"{approval['failed_run_id']}.meta"
        info = manifest.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_size > 1_048_576
        ):
            raise RouteEvidenceError("fallback terminal evidence is unsafe")
        failed_raw = manifest.read_bytes()
        failed = {}
        for line in failed_raw.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in failed:
                raise RouteEvidenceError("fallback terminal evidence is malformed")
            failed[key] = value
        prior = body["prior_resolution"]
        selection = prior["selections"]["planner"]
        if any((
            body.get("failed_manifest_digest") != hashlib.sha256(failed_raw).hexdigest(),
            failed.get("run_id") != approval["failed_run_id"],
            failed.get("ticket") != ticket,
            failed.get("role") != "planner",
            failed.get("kit_sha") != journal.get("kit_sha"),
            failed.get("role_branch_before") != branch,
            failed.get("phase") not in {"completed", "abandoned", "cancelled_conservative"},
            failed.get("go_issued") != "1",
            failed.get("task_submitted") != "1",
            failed.get("role_exit") not in {"provider_failed", "cancelled"},
            failed.get("adapter") != selection.get("adapter"),
            failed.get("provider_family") != selection.get("provider_family"),
            failed.get("model_id") != selection.get("selection_id"),
            failed.get("route_id") != selection.get("route_id"),
            failed.get("policy_hash") != prior.get("policy_hash"),
            not re.fullmatch(r"[0-9a-f]{40}", failed.get("role_head_before", "")),
        )):
            raise RouteEvidenceError("fallback terminal provenance is invalid")
        ancestor = subprocess.run(
            ["git", "-C", str(worktree), "merge-base", "--is-ancestor",
             failed["role_head_before"], commit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
        message = subprocess.run(
            ["git", "-C", str(worktree), "show", "-s", "--format=%B", commit],
            text=True, capture_output=True, check=True, timeout=120,
        ).stdout.splitlines()
        if (
            ancestor.returncode
            or message.count("Model-Route-Revision: " + revision["revision_hash"]) != 1
        ):
            raise RouteEvidenceError("fallback handoff commit is invalid")
    except RouteEvidenceError:
        raise
    except (
        FileNotFoundError, KeyError, OSError, TypeError, UnicodeError,
        ValueError, json.JSONDecodeError, subprocess.SubprocessError,
    ) as error:
        raise RouteEvidenceError("fallback route evidence is invalid") from error
