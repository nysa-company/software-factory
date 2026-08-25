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
    GitHubHTTPSCredential,
    HandoffError,
    RoleBoundaryPolicy,
    build_handoff_commit,
    github_https_remote,
    preview_handoff,
    validate_committed_output,
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANAGER = load_module("model_manager", ROOT / "scripts/model-manager.py")
ROUTER = load_module("model_router_fallback", ROOT / "scripts/model-router.py")
LEDGER = load_module("ledger_view_fallback", ROOT / "scripts/ledger-view.py")
QUALIFICATION = load_module(
    "qualification_manifest_fallback",
    ROOT / "scripts/lib/qualification_manifest.py",
)
INFLIGHT = load_module(
    "inflight_release_fallback",
    ROOT / "scripts/lib/inflight_release.py",
)
ROLE_ORDER = ("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator")
PRODUCER_BOUNDARY = {"planner": "P", "test-author": "T", "builder": "B"}
REASONS = frozenset((
    "budget_exhausted", "credits_exhausted", "operator_requested",
    "provider_unavailable",
))
GITHUB_AUTH_ENVIRONMENT = (
    "FACTORY_GITHUB_TOKEN_FD",
    "GH_CONFIG_DIR",
    "GH_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
)


class FallbackError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def git(repo, *args, input_bytes=None, extra_env=None, git_auth=None):
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra_env:
        environment.update(extra_env)
    for name in GITHUB_AUTH_ENVIRONMENT:
        environment.pop(name, None)
    credential_args = []
    if git_auth is not None:
        home = Path(environment.get("HOME", ""))
        if not home.is_absolute():
            raise FallbackError("github_credential_unavailable")
        credential_args = [
            "-c",
            "credential.https://github.com.helper="
            f"!{git_auth.helper} auth git-credential",
        ]
        environment["GH_CONFIG_DIR"] = str(home / ".config" / "gh")
        environment["GH_PROMPT_DISABLED"] = "1"
    result = subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            *credential_args,
            "-c", "diff.external=",
            *args,
        ],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    if result.returncode:
        if git_auth is not None:
            raise FallbackError("github_https_authentication_failed")
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


def historical_qualification_handoff(args, failed, journal, authority, head):
    if (
        authority is None
        or authority["manifest"].get("mode") != "successor"
        or journal.get("kit_sha") != authority["release_sha"]
    ):
        raise FallbackError("failed attempt provenance does not match the journal or Git state")
    repo = Path(args.workdir).resolve()
    product = authority["product"]
    try:
        project = git(
            product, "show", f"{authority['product_sha']}:factory/PROJECT.env",
        ).decode()
        branch = git(
            repo, "symbolic-ref", "--quiet", "--short", "HEAD",
        ).decode().strip()
        if (
            failed.get("role_remote_before") != failed.get("role_head_before")
            or failed.get("role_branch_before") != branch
        ):
            raise FallbackError("historical qualification handoff is invalid")
        target_kit = authority["release_sha"]
        migration_head = head
        seen = set()
        for _ in range(64):
            if target_kit in seen:
                raise FallbackError("historical qualification migration loop")
            seen.add(target_kit)
            raw = git(
                product, "show",
                f"{authority['product_sha']}:factory/migrations/inflight-release/"
                f"{target_kit}.json",
            ).decode()
            authorization, entries = INFLIGHT.parse_authorization(
                raw, project, target_kit,
            )
            item = entries[args.ticket]
            source_kit = INFLIGHT.ticket_source_kit(authorization, item)
            if INFLIGHT.verify_migration(
                product, authority["product_sha"], target_kit,
                args.ticket, branch, migration_head, allow_legacy_pinless=True,
            ) != "replay":
                raise FallbackError("historical qualification migration is invalid")
            migration_head = item["head"]
            if source_kit == failed.get("kit_sha"):
                break
            target_kit = source_kit
        else:
            raise FallbackError("historical qualification migration chain is too long")
        source_raw = git(
            repo, "show",
            f"{failed['role_head_before']}:factory/route-plans/{args.ticket}.json",
        )
        source_journal = json.loads(source_raw)
        catalog, routes, _profiles, profile_map = load_policy_files(
            args.catalog, args.profiles,
        )
        MANAGER.validate_journal(
            source_journal, catalog, routes, profile_map,
            allow_historical_active=True,
        )
        source_policy = MANAGER.active_resolution(source_journal)["policy_hash"]
        current_policy = MANAGER.active_resolution(journal)["policy_hash"]
        if (
            source_journal.get("kit_sha") != source_kit
            or failed.get("policy_hash") != source_policy
            or current_policy != source_policy
        ):
            raise FallbackError("historical qualification route policy changed")
        snapshot = validate_committed_output(
            repo, baseline=failed["role_head_before"], head=item["head"],
            role=failed["role"],
            policy=policy_for(Path(args.boundaries), args.ticket),
        )
        commit_snapshot = validate_committed_output(
            repo, baseline=head, head=head, role=failed["role"],
            policy=policy_for(Path(args.boundaries), args.ticket),
        )
    except (
        FallbackError, HandoffError, INFLIGHT.AuthorizationError, KeyError,
        OSError, TypeError, ValueError, json.JSONDecodeError,
    ) as error:
        if isinstance(error, FallbackError):
            raise
        raise FallbackError("historical qualification handoff is invalid") from error
    return {
        "authorized_head": item["head"],
        "commit_snapshot_digest": commit_snapshot,
        "recovery_head": head,
        "snapshot_digest": snapshot,
        "source_factory_sha": source_kit,
        "source_head": failed["role_head_before"],
    }


def calculate(
    args, nonce, failed_role_only=False, qualification=None,
):
    repo = Path(args.workdir).resolve()
    factory_root = Path(args.factory_root).resolve()
    plan_path = repo / f"factory/route-plans/{args.ticket}.json"
    raw_journal = plan_path.read_bytes()
    journal = json.loads(raw_journal)
    catalog, routes, _profiles, profile_map = load_policy_files(
        args.catalog, args.profiles
    )
    if journal.get("schema") == "ticket-model-route-plan/v1":
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
        git_auth=args.git_auth,
    ).decode().split()[0]
    ordinary_provenance = (
        failed.get("role_remote_before") != remote_head
        or failed.get("role_branch_before") != branch
        or failed.get("kit_sha") != journal["kit_sha"]
        or failed.get("policy_hash") != MANAGER.active_resolution(journal)["policy_hash"]
    )
    historical = None
    if ordinary_provenance:
        historical = historical_qualification_handoff(
            args, failed, journal, qualification, expected_head,
        )
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
        provider_scan_base=(expected_head if historical else role_head_before),
        git_auth=args.git_auth,
    )
    approved_snapshot_digest = (
        historical["snapshot_digest"] if historical else handoff.snapshot_digest
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
        "project": args.project,
        "reason": args.reason,
        "remote_head": remote_head,
        "remote_url": handoff.remote_url,
        "resolution": resolution,
        "snapshot_digest": approved_snapshot_digest,
        "snapshot_preview_digest": handoff.preview_digest,
        "ticket": args.ticket,
    }
    if historical:
        if historical["commit_snapshot_digest"] != handoff.snapshot_digest:
            raise FallbackError("historical qualification handoff changed")
        payload["historical_handoff"] = historical
    return {
        "approval_hash": digest(canonical(payload).encode()),
        "failed": failed,
        "failed_manifest_digest": digest(failed_raw),
        "handoff": handoff,
        "historical_handoff": payload.get("historical_handoff"),
        "journal": journal,
        "journal_path": plan_path,
        "nonce": nonce,
        "payload": payload,
        "policy": policy_for(Path(args.boundaries), args.ticket),
        "approved_snapshot_digest": approved_snapshot_digest,
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
        "operator_comment": (
            f"FACTORY MODEL FALLBACK APPROVAL: {result['approval_hash']} "
            f"RUN: {args.failed_run} REASON: {args.reason} "
            f"NONCE: {result['nonce']}"
        ),
    }


def qualification_authority(journal_kit):
    if os.environ.get("FACTORY_KIT_TRUST_SCOPE") != "qualification-candidate":
        return None
    product = Path(os.environ.get("FACTORY_ROOT", ""))
    manifest_path = os.environ.get("FACTORY_QUALIFICATION_MANIFEST", "")
    product_sha = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_SHA", "")
    product_tree = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_TREE", "")
    release_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    expected = product / "factory/QUALIFICATION.json"
    if (
        not product.is_absolute()
        or product.is_symlink()
        or manifest_path != str(expected)
        or expected.is_symlink()
        or not re.fullmatch(r"[0-9a-f]{40}", product_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", product_tree)
    ):
        raise FallbackError("sealed qualification authority is invalid")
    if (
        git(product, "rev-parse", "HEAD").decode().strip() != product_sha
        or git(product, "rev-parse", "HEAD^{tree}").decode().strip() != product_tree
        or git(product, "status", "--porcelain", "--untracked-files=all")
    ):
        raise FallbackError("sealed qualification product changed")
    raw = git(product, "show", f"{product_sha}:factory/QUALIFICATION.json")
    try:
        manifest = QUALIFICATION.validate(json.loads(raw), release_sha)
    except (json.JSONDecodeError, QUALIFICATION.ManifestError) as error:
        raise FallbackError(
            "sealed qualification manifest does not authorize fallback"
        ) from error
    allowed_kits = {release_sha}
    if manifest.get("mode") == "successor":
        allowed_kits.add(manifest["source_factory_sha"])
    if journal_kit not in allowed_kits:
        raise FallbackError("sealed qualification manifest does not authorize fallback")
    return {
        "generation": manifest["generation"],
        "manifest": manifest,
        "manifest_digest": digest(raw),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "raw": raw,
        "release_sha": release_sha,
        "product": product,
    }


def recover_applied(args, approval):
    if approval.get("failed_run_id") != args.failed_run:
        return None
    repo = Path(args.workdir)
    relative = f"factory/route-plans/{args.ticket}.json"
    path = repo / relative
    try:
        head = git(repo, "rev-parse", "HEAD").decode().strip()
        committed = git(repo, "show", f"HEAD:{relative}")
        journal = json.loads(committed)
    except (FallbackError, KeyError, IndexError, json.JSONDecodeError):
        return None
    if journal.get("schema") == "ticket-model-route-plan/v1":
        return None
    catalog, routes, _profiles, profile_map = load_policy_files(
        args.catalog, args.profiles
    )
    MANAGER.validate_journal(
        journal, catalog, routes, profile_map, allow_historical_active=True
    )
    authority = None
    if (
        approval.get("schema") == "ticket-model-fallback-qualification/v1"
        and (
            "product_sha" in approval
            or os.environ.get("FACTORY_KIT_TRUST_SCOPE") == "qualification-candidate"
        )
    ):
        authority = qualification_authority(journal["kit_sha"])
        if authority is None or any(
            approval.get(key) != authority[key]
            for key in (
                "generation", "manifest_digest", "product_sha", "product_tree",
            )
        ):
            raise FallbackError("qualification fallback authority changed")
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
    handoff_commit = handoff_commits[0]
    if _failed["role"] == "spec-linter":
        ticket_relative = f"factory/tickets/{args.ticket}.md"
        baseline = git(
            repo, "show", f'{_failed["role_head_before"]}:{ticket_relative}',
        )
        handoff_ticket = git(repo, "show", f"{handoff_commit}:{ticket_relative}")
        if handoff_ticket != baseline:
            descendants = git(
                repo, "rev-list", "--reverse", "--ancestry-path",
                f"{handoff_commit}..{head}",
            ).decode().splitlines()
            if descendants:
                cleanup = descendants[0]
                message = git(repo, "show", "-s", "--format=%B", cleanup).decode()
                message_lines = message.rstrip("\n").splitlines()
                subject = f"{args.ticket}: retire failed spec-lint evidence before retry"
                expected = [
                    subject,
                    "",
                    f"Failed-Manifest-Digest: {digest(failed_raw)}",
                ]
                parents = git(
                    repo, "show", "-s", "--format=%P", cleanup,
                ).decode().split()
                changed = git(
                    repo, "diff", "--name-only", handoff_commit, cleanup,
                ).decode().splitlines()
                if (
                    message_lines != expected
                    or parents != [handoff_commit]
                    or changed != [ticket_relative]
                    or git(repo, "show", f"{cleanup}:{ticket_relative}") != baseline
                ):
                    raise FallbackError("failed spec-lint retirement is invalid")
                ticket_path = repo / ticket_relative
                if head == cleanup:
                    if ticket_path.is_symlink() or not ticket_path.is_file():
                        raise FallbackError("failed spec-lint retirement worktree is unsafe")
                    if ticket_path.read_bytes() == handoff_ticket:
                        atomic_replace(ticket_path, baseline)
    historical = approval.get("historical_handoff")
    if historical is not None:
        try:
            parent = git(
                repo, "show", "-s", "--format=%P", handoff_commits[0],
            ).decode().split()
            prior_journal = {**journal, "revisions": journal["revisions"][:index]}
            expected = historical_qualification_handoff(
                args, _failed, prior_journal, authority, historical["recovery_head"],
            )
        except (KeyError, TypeError) as error:
            raise FallbackError("historical qualification fallback is invalid") from error
        if (
            parent != [historical["recovery_head"]]
            or historical != {
                **expected,
            }
            or body.get("approved_snapshot_digest")
            != historical.get("snapshot_digest")
        ):
            raise FallbackError("historical qualification fallback is invalid")
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
        result["approved_snapshot_digest"],
        args.reason,
        approval,
        created_at,
        result["catalog"],
        result["routes"],
        result["profile_map"],
    )
    journal_raw = (canonical(journal) + "\n").encode()
    revision_hash = journal["revisions"][-1]["revision_hash"]
    commit_timestamp = str(int(dt.datetime.now().timestamp())) + " +0000"
    commit = build_handoff_commit(
        result["handoff"],
        result["policy"],
        revision_hash=revision_hash,
        commit_timestamp=commit_timestamp,
        journal_content=journal_raw,
        subject=f"{args.ticket}: preserve failed attempt and revise model route",
        git_auth=args.git_auth,
    )
    repo = Path(args.workdir)
    head = commit.commit
    ticket_relative = f"factory/tickets/{args.ticket}.md"
    if result["failed"]["role"] == "spec-linter":
        baseline = git(
            repo, "show",
            f'{result["failed"]["role_head_before"]}:{ticket_relative}',
        )
        current = git(repo, "show", f"{head}:{ticket_relative}")
        if current != baseline:
            with tempfile.TemporaryDirectory(prefix="nysa-fallback-retire-") as temporary:
                environment = {
                    "GIT_AUTHOR_NAME": "Nysa Failed Attempt Handoff",
                    "GIT_AUTHOR_EMAIL": "handoff@nysa.invalid",
                    "GIT_AUTHOR_DATE": commit_timestamp,
                    "GIT_COMMITTER_NAME": "Nysa Failed Attempt Handoff",
                    "GIT_COMMITTER_EMAIL": "handoff@nysa.invalid",
                    "GIT_COMMITTER_DATE": commit_timestamp,
                    "GIT_INDEX_FILE": str(Path(temporary) / "index"),
                }
                git(repo, "read-tree", head, extra_env=environment)
                oid = git(
                    repo, "hash-object", "-w", "--stdin",
                    input_bytes=baseline, extra_env=environment,
                ).decode().strip()
                git(
                    repo, "update-index", "--cacheinfo", "100644", oid,
                    ticket_relative, extra_env=environment,
                )
                tree = git(repo, "write-tree", extra_env=environment).decode().strip()
                message = (
                    f"{args.ticket}: retire failed spec-lint evidence before retry\n\n"
                    f"Failed-Manifest-Digest: {result['failed_manifest_digest']}\n"
                ).encode()
                head = git(
                    repo, "commit-tree", tree, "-p", head,
                    input_bytes=message, extra_env=environment,
                ).decode().strip()
    ref = "refs/heads/" + result["handoff"].branch
    git(repo, "update-ref", ref, head, commit.parent)
    git(repo, "read-tree", head)
    if head != commit.commit:
        atomic_replace(repo / ticket_relative, baseline)
    atomic_replace(result["journal_path"], journal_raw)
    return {
        "approval_hash": result["approval_hash"],
        "commit_sha": head,
        "failed_run_id": args.failed_run,
        "revision_hash": revision_hash,
        "schema": "ticket-model-fallback-result/v1",
        "snapshot_digest": result["approved_snapshot_digest"],
    }


def apply(args):
    approval = json.loads(Path(args.approval).read_text())
    recovered = recover_applied(args, approval)
    if recovered is not None:
        return recovered
    result = calculate(args, approval["nonce"])
    if approval.get("approval_hash") != result["approval_hash"]:
        raise FallbackError("operator approval receipt does not match the current fallback preview")
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
    authority = qualification_authority(journal.get("kit_sha"))
    for revision in reversed(journal.get("revisions", [])):
        approval = revision.get("body", {}).get("approval_receipt")
        if isinstance(approval, dict):
            recovered = recover_applied(args, approval)
            if recovered is not None:
                return recovered
    result = calculate(
        args, secrets.token_hex(16),
        failed_role_only=True,
        qualification=authority,
    )
    failed = result["failed"]
    transition_receipt = failed.get("transition_receipt_sha256", "")
    attempts = 0
    for path in (Path(args.factory_root) / "factory/runs").glob("*.meta"):
        if path.is_file() and not path.is_symlink():
            value = read_meta(path)
            if not (
                value.get("ticket") == args.ticket
                and value.get("role") == failed["role"]
                and value.get("kit_sha") == failed["kit_sha"]
                and value.get("go_issued") == "1"
                and value.get("task_submitted") == "1"
            ):
                continue
            receipt = value.get("transition_receipt_sha256", "")
            if receipt and not re.fullmatch(r"[0-9a-f]{64}", receipt):
                raise FallbackError("qualification fallback transition receipt is invalid")
            attempts += receipt == transition_receipt
    if attempts != 1:
        raise FallbackError("qualification fallback is allowed only after the first role attempt")
    if authority is not None:
        qualification = authority["manifest"]
        raw = authority["raw"]
    else:
        raw = git(
            Path(args.workdir), "show",
            "refs/remotes/origin/main:factory/QUALIFICATION.json",
        )
    try:
        qualification = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FallbackError("protected qualification manifest is malformed") from error
    authorized_factory_sha = (
        authority["release_sha"] if authority is not None
        else result["journal"]["kit_sha"]
    )
    if (
        authority is None
        and qualification.get("schema") not in {
             "nysa.software-factory.qualification/v1",
             "nysa.software-factory.qualification/v2",
         }
        or qualification.get("factory_sha") != authorized_factory_sha
        or args.ticket not in qualification.get("tickets", [])
        or not isinstance(qualification.get("generation"), int)
        or qualification["generation"] < 1
    ):
        raise FallbackError("protected qualification manifest does not authorize fallback")
    approval = {
        "approval_hash": result["approval_hash"],
        "failed_run_id": args.failed_run,
        "generation": qualification["generation"],
        "manifest_digest": digest(raw),
        "nonce": result["nonce"],
        **({
            "product_sha": authority["product_sha"],
            "product_tree": authority["product_tree"],
        } if authority is not None else {}),
        **({
            "historical_handoff": result["historical_handoff"],
        } if result["historical_handoff"] is not None else {}),
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
    value.add_argument("--github-helper")
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
    args.git_auth = None
    if github_https_remote(args.remote):
        if not args.github_helper:
            raise FallbackError("github_credential_unavailable")
        args.git_auth = GitHubHTTPSCredential(helper=args.github_helper)
    elif args.github_helper:
        raise FallbackError("github credential helper supplied for a non-GitHub remote")
    if args.action == "apply" and not args.approval:
        raise FallbackError("apply requires an operator approval receipt")
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
