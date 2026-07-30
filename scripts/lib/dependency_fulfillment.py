#!/usr/bin/env python3
"""Validate dependency-only adoption of already-merged protected work."""

from functools import lru_cache
import re
from pathlib import Path

from legacy_closeout import (
    ValidationError,
    blob_at,
    exact,
    json_at,
    oid,
    one_field,
    repository_from_project,
    run,
    text_at,
    timestamp,
)
from protected_merge_reconciliation import (
    CHECK_IDENTITY_KEYS,
    CHECK_KEYS,
    _check_identities,
    _project_required_checks,
)


MIGRATION_DIR = "factory/migrations/dependency-fulfillment"
AUTH_SCHEMA = "nysa.software-factory.dependency-fulfillment-authorization/v1"
RECEIPT_SCHEMA = "nysa.software-factory.dependency-fulfillment/v1"
AUTH_KEYS = {
    "schema", "repository", "target_kit_sha", "candidate_contract", "cutoff",
    "protected_main_basis", "required_checks", "authorization", "tickets",
}
AUTH_TICKET_KEYS = {"ticket", "pr_number", "receipt"}
AUTHORIZATION_KEYS = {
    "method", "operator", "authorized_at", "statement", "auto_merge", "bypass",
}
BASIS_KEYS = {"commit", "tree"}
RECEIPT_KEYS = {
    "schema", "ticket", "repository", "target_kit_sha", "candidate_contract",
    "source_state", "source_ticket_blob", "pr", "checks",
    "authorization_blob", "cutoff", "protected_main_basis",
}
PR_KEYS = {
    "number", "head_ref", "base_ref", "head", "merge_commit", "merged_at",
    "merged_by",
}
TICKET_ID = re.compile(r"T-[0-9]+")


def _migration_commit(repo, ref, basis, expected_paths):
    additions = run(
        repo, "log", "--format=%H", "--diff-filter=A", ref, "--",
        f"{MIGRATION_DIR}/authorization.json",
    ).stdout.splitlines()
    if len(additions) != 1:
        raise ValidationError(
            "dependency fulfillment authorization was introduced more than once"
        )
    migration = additions[0]
    parents = run(repo, "show", "-s", "--format=%P", migration).stdout.split()
    paths = set(
        run(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", migration
        ).stdout.splitlines()
    )
    if parents != [basis] or paths != expected_paths:
        raise ValidationError(
            "dependency fulfillment must have one atomic protected introduction"
        )
    return migration


def _validate_documents(repo, ref, authorization, receipts):
    exact(authorization, AUTH_KEYS, "dependency fulfillment authorization")
    if (
        authorization["schema"] != AUTH_SCHEMA
        or authorization["candidate_contract"] != "1.8.0"
        or repository_from_project(repo, ref) != authorization["repository"]
    ):
        raise ValidationError("dependency fulfillment authorization identity is invalid")
    oid(authorization["target_kit_sha"], "dependency fulfillment target kit")
    cutoff = timestamp(authorization["cutoff"], "dependency fulfillment cutoff")
    if cutoff.microsecond:
        raise ValidationError(
            "dependency fulfillment cutoff must use whole-second precision"
        )
    basis = exact(
        authorization["protected_main_basis"],
        BASIS_KEYS,
        "dependency fulfillment protected basis",
    )
    oid(basis["commit"], "dependency fulfillment basis commit")
    oid(basis["tree"], "dependency fulfillment basis tree")
    if (
        run(repo, "rev-parse", f"{basis['commit']}^{{tree}}").stdout.strip()
        != basis["tree"]
        or run(
            repo, "merge-base", "--is-ancestor", basis["commit"], ref, check=False
        ).returncode
    ):
        raise ValidationError("dependency fulfillment protected basis is invalid")
    identities = _check_identities(
        authorization["required_checks"],
        _project_required_checks(repo, basis["commit"]),
    )
    approval = exact(
        authorization["authorization"],
        AUTHORIZATION_KEYS,
        "dependency fulfillment authorization payload",
    )
    authorized_at = timestamp(
        approval["authorized_at"], "dependency fulfillment authorized_at"
    )
    basis_time = timestamp(
        run(repo, "show", "-s", "--format=%cI", basis["commit"]).stdout.strip(),
        "dependency fulfillment basis time",
    )
    if (
        approval["method"] != "manual-protected-main-merge"
        or approval["auto_merge"] is not False
        or approval["bypass"] is not False
        or not isinstance(approval["operator"], str)
        or not approval["operator"].strip()
        or not isinstance(approval["statement"], str)
        or approval["statement"].strip() != approval["statement"]
        or len(approval["statement"]) < 20
        or authorized_at < basis_time
        or authorized_at != cutoff
    ):
        raise ValidationError(
            "dependency fulfillment requires fresh manual operator authorization"
        )
    entries = authorization["tickets"]
    if not isinstance(entries, list) or not entries:
        raise ValidationError("dependency fulfillment ticket batch is empty")
    expected = {}
    for entry in entries:
        exact(entry, AUTH_TICKET_KEYS, "dependency fulfillment ticket authorization")
        ticket = entry["ticket"]
        if (
            not isinstance(ticket, str)
            or not TICKET_ID.fullmatch(ticket)
            or ticket in expected
            or not isinstance(entry["pr_number"], int)
            or entry["pr_number"] <= 0
            or entry["receipt"] != f"{MIGRATION_DIR}/{ticket}.json"
        ):
            raise ValidationError(
                "dependency fulfillment ticket authorization is invalid"
            )
        expected[ticket] = entry
    if list(expected) != sorted(expected) or set(receipts) != set(expected):
        raise ValidationError(
            "dependency fulfillment receipts are unsorted, partial, or extra"
        )
    expected_files = {
        f"{MIGRATION_DIR}/authorization.json",
        *(entry["receipt"] for entry in expected.values()),
    }
    actual_files = set(
        run(
            repo, "ls-tree", "-r", "--name-only", ref, "--", MIGRATION_DIR
        ).stdout.splitlines()
    )
    if actual_files != expected_files:
        raise ValidationError(
            "dependency fulfillment migration contains partial or extra files"
        )
    authorization_blob = blob_at(
        repo, ref, f"{MIGRATION_DIR}/authorization.json"
    )
    migration = _migration_commit(
        repo,
        ref,
        basis["commit"],
        {*expected_files, "factory/KIT_PIN"},
    )
    if (
        text_at(repo, migration, "factory/KIT_PIN")
        != authorization["target_kit_sha"] + "\n"
    ):
        raise ValidationError(
            "dependency fulfillment migration does not install its target kit"
        )
    for path in expected_files:
        if run(
            repo, "log", "--format=%H", ref, "--", path
        ).stdout.splitlines() != [migration]:
            raise ValidationError(
                "dependency fulfillment evidence changed after introduction"
            )
    result = {}
    for ticket, entry in expected.items():
        receipt = exact(
            receipts[ticket],
            RECEIPT_KEYS,
            f"{ticket} dependency fulfillment receipt",
        )
        receipt_basis = exact(
            receipt["protected_main_basis"],
            BASIS_KEYS,
            f"{ticket} dependency fulfillment basis",
        )
        pr = exact(receipt["pr"], PR_KEYS, f"{ticket} dependency fulfillment PR")
        source_ticket = text_at(
            repo, basis["commit"], f"factory/tickets/{ticket}.md"
        )
        if (
            receipt["schema"] != RECEIPT_SCHEMA
            or receipt["ticket"] != ticket
            or receipt["repository"] != authorization["repository"]
            or receipt["target_kit_sha"] != authorization["target_kit_sha"]
            or receipt["candidate_contract"] != "1.8.0"
            or receipt["source_state"] != "Backlog"
            or receipt["authorization_blob"] != authorization_blob
            or receipt["cutoff"] != authorization["cutoff"]
            or receipt_basis != basis
            or source_ticket is None
            or one_field(source_ticket, "State") != "Backlog"
            or blob_at(
                repo, basis["commit"], f"factory/tickets/{ticket}.md"
            )
            != receipt["source_ticket_blob"]
        ):
            raise ValidationError(
                f"{ticket} dependency fulfillment identity is invalid"
            )
        if (
            pr["number"] != entry["pr_number"]
            or pr["base_ref"] != "main"
            or not isinstance(pr["head_ref"], str)
            or not pr["head_ref"]
            or not isinstance(pr["merged_by"], str)
            or not pr["merged_by"]
        ):
            raise ValidationError(
                f"{ticket} dependency fulfillment PR identity is invalid"
            )
        oid(pr["head"], f"{ticket} dependency fulfillment PR head")
        oid(pr["merge_commit"], f"{ticket} dependency fulfillment merge commit")
        if (
            timestamp(
                pr["merged_at"], f"{ticket} dependency fulfillment merged_at"
            )
            > cutoff
            or run(
                repo,
                "merge-base",
                "--is-ancestor",
                pr["merge_commit"],
                basis["commit"],
                check=False,
            ).returncode
        ):
            raise ValidationError(
                f"{ticket} dependency fulfillment merge is outside the basis"
            )
        checks = receipt["checks"]
        if not isinstance(checks, list) or len(checks) != len(identities):
            raise ValidationError(
                f"{ticket} dependency fulfillment checks are incomplete"
            )
        observed = {}
        for check in checks:
            exact(check, CHECK_KEYS, f"{ticket} dependency fulfillment check")
            identity = identities.get(check["name"])
            if (
                identity is None
                or check["name"] in observed
                or any(check[name] != identity[name] for name in CHECK_IDENTITY_KEYS)
                or check["status"] != "completed"
                or check["conclusion"] != "success"
                or check["skipped"] is not False
            ):
                raise ValidationError(
                    f"{ticket} dependency fulfillment check is invalid"
                )
            observed[check["name"]] = check
        if set(observed) != set(identities):
            raise ValidationError(
                f"{ticket} dependency fulfillment checks are incomplete"
            )
        result[ticket] = {
            "basis": "validated-protected-dependency-fulfillment",
            "ticket": ticket,
            "receipt": entry["receipt"],
            "pr_number": pr["number"],
            "merge_commit": pr["merge_commit"],
            "target_kit_sha": receipt["target_kit_sha"],
        }
    return result


def validate_generated_dependency_batch(repo, authorization, receipts, ref):
    return _validate_documents(Path(repo), ref, authorization, receipts)


@lru_cache(maxsize=64)
def _dependency_batch_at(repo, commit):
    repo = Path(repo)
    authorization = json_at(
        repo,
        commit,
        f"{MIGRATION_DIR}/authorization.json",
        "dependency fulfillment authorization",
    )
    files = run(
        repo, "ls-tree", "-r", "--name-only", commit, "--", MIGRATION_DIR
    ).stdout.splitlines()
    if authorization is None:
        if files:
            raise ValidationError("dependency fulfillment batch is partial")
        return {}
    entries = authorization.get("tickets") if isinstance(authorization, dict) else None
    receipts = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("ticket"), str
            ):
                raise ValidationError(
                    "dependency fulfillment ticket authorization is invalid"
                )
            ticket = entry["ticket"]
            value = json_at(
                repo,
                commit,
                f"{MIGRATION_DIR}/{ticket}.json",
                f"{ticket} dependency fulfillment receipt",
            )
            if value is not None:
                receipts[ticket] = value
    return _validate_documents(repo, commit, authorization, receipts)


def dependency_fulfillment(repo, ticket, ref="refs/remotes/origin/main"):
    if not isinstance(ticket, str) or not TICKET_ID.fullmatch(ticket):
        raise ValidationError("invalid ticket identifier")
    repo = Path(repo).resolve(strict=True)
    commit = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    oid(commit, "protected-main commit")
    result = _dependency_batch_at(str(repo), commit)
    if ticket not in result:
        raise ValidationError("protected main lacks dependency fulfillment evidence")
    return dict(result[ticket])
