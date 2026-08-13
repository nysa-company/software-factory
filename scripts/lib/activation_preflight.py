#!/usr/bin/env python3
"""Authoritative ticket-inventory validation for Factory activation."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.dont_write_bytecode = True
LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

from effective_ticket import ticket_branch_prefix  # noqa: E402
from historical_pr_objects import HistoricalObjectError, hydrate  # noqa: E402
from inflight_release import (  # noqa: E402
    AuthorizationError,
    authorize_ticket,
    parse_authorization,
    unique_object,
)
from legacy_closeout import (  # noqa: E402
    ValidationError as TerminalError,
    certified_legacy_terminal,
    protected_terminal,
)

SHA = re.compile(r"[0-9a-f]{40}\Z")


class ActivationError(ValueError):
    def __init__(self, reason_code: str, scope: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.scope = scope

    def blocker(self) -> dict[str, str]:
        return {"reason_code": self.reason_code, "scope": self.scope}


class Validator:
    def __init__(
        self,
        product: Path,
        candidate: str,
        candidate_scripts: Path,
        origin: str,
        certified_previous_tree: str,
    ) -> None:
        self.product = product
        self.factory = product / "factory"
        self.candidate = candidate
        self.candidate_scripts = candidate_scripts
        self.origin = origin
        self.certified_previous_tree = certified_previous_tree
        self.prefix = ticket_branch_prefix(self.factory)
        self.authorization: dict[str, Any] | None = None
        self.authorized: dict[str, dict[str, Any]] = {}
        self.authorization_loaded = False
        self.authorization_error: ActivationError | None = None
        self.used_authorizations: set[str] = set()
        self.migration_policy: tuple[Any, Any, Any, Any] | None = None

    @staticmethod
    def fail(reason: str, scope: str, detail: str) -> None:
        raise ActivationError(reason, scope, detail)

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.product), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def checked(self, *arguments: str) -> str:
        result = self.git(*arguments)
        if result.returncode:
            self.fail(
                "activation_inventory_invalid",
                "activation",
                "activation Git inventory is unavailable",
            )
        return result.stdout.strip()

    def load_migration_policy(self) -> tuple[Any, Any, Any, Any]:
        if self.migration_policy is not None:
            return self.migration_policy
        spec = importlib.util.spec_from_file_location(
            "factory_inflight_model_manager",
            self.candidate_scripts / "model-manager.py",
        )
        if spec is None or spec.loader is None:
            self.fail(
                "activation_route_migration_invalid",
                "activation",
                "candidate model migration validator is unavailable",
            )
        manager = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(manager)
            catalog, routes, _, profiles = manager.ROUTER.load_policy(
                self.candidate_scripts / "model-routing/catalog-v1.json",
                self.candidate_scripts / "model-routing/profiles-v1.json",
            )
        except Exception as error:
            raise ActivationError(
                "activation_route_migration_invalid",
                "activation",
                "candidate model migration policy is invalid",
            ) from error
        self.migration_policy = manager, catalog, routes, profiles
        return self.migration_policy

    def load_inflight_authorization(self) -> None:
        if self.authorization_loaded:
            if self.authorization_error is not None:
                raise self.authorization_error
            return
        self.authorization_loaded = True
        try:
            relative = f"factory/migrations/inflight-release/{self.candidate}.json"
            result = self.git("show", "HEAD:" + relative)
            if result.returncode:
                self.fail(
                    "activation_authorization_missing",
                    "activation",
                    "nonterminal ticket uses another kit without an exact in-flight release authorization",
                )
            head = self.checked("rev-parse", "HEAD")
            remote = self.checked(
                "ls-remote", "--heads", "--", self.origin, "refs/heads/main",
            ).split()
            if not remote or remote[0] != head:
                self.fail(
                    "activation_authorization_invalid",
                    "activation",
                    "in-flight release authorization is not on protected main",
                )
            project = self.factory / "PROJECT.env"
            if not project.is_file() or project.is_symlink():
                self.fail(
                    "activation_authorization_invalid",
                    "activation",
                    "product project descriptor is unsafe",
                )
            self.authorization, self.authorized = parse_authorization(
                result.stdout, project.read_text(encoding="utf-8"), self.candidate,
            )
        except (AuthorizationError, OSError, UnicodeError) as error:
            self.authorization_error = ActivationError(
                "activation_authorization_invalid", "activation", str(error),
            )
            raise self.authorization_error from error
        except ActivationError as error:
            self.authorization_error = error
            raise

    def authorize_inflight(
        self,
        ticket: str,
        branch: str,
        remote_tip: str,
        source_ref: str,
        state: str,
        lease: str,
    ) -> None:
        self.load_inflight_authorization()
        assert self.authorization is not None
        try:
            if not remote_tip or source_ref == "HEAD":
                raise AuthorizationError("remote ticket ref is unavailable")
            authorize_ticket(
                self.authorization,
                self.authorized,
                ticket=ticket,
                branch=branch,
                head=remote_tip,
                state=state,
                source_kit_sha=lease,
            )
        except AuthorizationError as error:
            expected = self.authorized.get(ticket) or {
                "branch": branch, "head": remote_tip, "state": state,
            }
            raise ActivationError(
                "activation_authorization_mismatch",
                ticket,
                (
                    f"{ticket} does not match its exact in-flight release authorization; "
                    f"expected branch={expected.get('branch', '')} "
                    f"head={expected.get('head', '')} "
                    f"state={expected.get('state', '')} "
                    f"source_kit_sha={self.authorization['source_kit_sha']}"
                ),
            ) from error
        plan_path = f"factory/route-plans/{ticket}.json"
        result = self.git("show", f"{remote_tip}:{plan_path}")
        if result.returncode or len(result.stdout.encode("utf-8")) > 1024 * 1024:
            self.fail(
                "activation_route_migration_invalid",
                ticket,
                "authorized in-flight ticket lacks a safe migratable route document",
            )
        try:
            plan = json.loads(result.stdout, object_pairs_hook=unique_object)
            manager, catalog, routes, profiles = self.load_migration_policy()
            if (
                plan.get("ticket") != ticket
                or plan.get("kit_sha") != self.authorization["source_kit_sha"]
            ):
                raise ValueError("route plan identity mismatch")
            if plan.get("schema") == "ticket-model-route-plan/v1":
                if set(plan) != {
                    "schema", "ticket", "kit_sha", "created_at", "resolution",
                }:
                    raise ValueError("route plan shape mismatch")
                manager._validate_pin(
                    plan, catalog, routes, profiles, allow_historical_catalog=True,
                )
            elif plan.get("schema") == "ticket-model-route-journal/v2":
                manager.validate_journal(
                    plan, catalog, routes, profiles, allow_historical_active=True,
                )
                migrated = manager.migrate_v2_journal(
                    plan,
                    remote_tip,
                    self.candidate,
                    "1970-01-01T00:00:00Z",
                    catalog,
                    routes,
                    profiles,
                )
                if migrated["revisions"][:-1] != plan["revisions"]:
                    raise ValueError("route journal history changed")
            else:
                raise ValueError("unsupported route document schema")
        except ActivationError:
            raise
        except Exception as error:
            raise ActivationError(
                "activation_route_migration_invalid",
                ticket,
                "authorized in-flight ticket route document is not migratable by the candidate",
            ) from error
        self.used_authorizations.add(ticket)

    def protected_legacy_approval(
        self, ticket: str, lease: str, source_ref: str, text: str,
    ) -> bool:
        if source_ref != "HEAD":
            return False
        if re.findall(r"(?mi)^Operator-Approval:\s*(.*?)\s*$", text) != ["Linear"]:
            return False
        head = self.checked("rev-parse", "HEAD")
        remote = self.checked(
            "ls-remote", "--heads", "--", self.origin, "refs/heads/main",
        ).split()
        if not remote or remote[0] != head:
            return False
        root = f"factory/attestations/{ticket}"
        values = []
        for name in ("bundle.json", "approval.json"):
            result = self.git("show", f"HEAD:{root}/{name}")
            if result.returncode:
                return False
            try:
                values.append(json.loads(result.stdout))
            except json.JSONDecodeError:
                return False
        bundle, approval = values
        branch = self.prefix + ticket
        bundle_blob = self.checked("rev-parse", f"HEAD:{root}/bundle.json")
        return (
            bundle.get("schema") == "nysa.software-factory.ticket-bundle/v1"
            and approval.get("schema") == "nysa.software-factory.ticket-approval/v1"
            and bundle.get("ticket") == approval.get("ticket") == ticket
            and bundle.get("branch") == approval.get("branch") == branch
            and bundle.get("kit_sha") == approval.get("kit_sha") == lease
            and bundle.get("repository") == approval.get("repository")
            and bundle.get("pr_number") == approval.get("pr_number")
            and bundle.get("reviewed_sha") == approval.get("reviewed_sha")
            and bundle.get("bundle_blob") == approval.get("bundle_blob")
            and approval.get("bundle_attestation_blob") == bundle_blob
            and SHA.fullmatch(bundle.get("reviewed_sha", "")) is not None
            and SHA.fullmatch(bundle.get("bundle_blob", "")) is not None
        )

    def ticket_ids(self) -> tuple[set[str], dict[str, str]]:
        tickets = self.factory / "tickets"
        ticket_ids: set[str] = set()
        if tickets.is_dir():
            for path in tickets.glob("T-*.md"):
                if re.fullmatch(r"T-[0-9]+\.md", path.name):
                    if path.is_symlink():
                        self.fail(
                            "activation_ticket_path_unsafe",
                            path.stem,
                            f"ticket path is a symlink: {path}",
                        )
                    ticket_ids.add(path.stem)
        refs = self.checked(
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/origin/" + self.prefix,
            "refs/heads/" + self.prefix,
        ).splitlines()
        remote_tips: dict[str, str] = {}
        remote_lines = self.checked(
            "ls-remote", "--heads", "--", self.origin,
            "refs/heads/" + self.prefix + "T-*",
        ).splitlines()
        for line in remote_lines:
            fields = line.split()
            if len(fields) != 2:
                self.fail(
                    "activation_remote_ref_invalid", "activation",
                    "remote ticket ref is malformed",
                )
            tip, ref = fields
            match = re.fullmatch(
                r"refs/heads/" + re.escape(self.prefix) + r"(T-[0-9]+)", ref,
            )
            if not match:
                continue
            if not SHA.fullmatch(tip):
                self.fail(
                    "activation_remote_ref_invalid", "activation",
                    "remote ticket ref is malformed",
                )
            ticket = match.group(1)
            if ticket in remote_tips:
                self.fail(
                    "activation_remote_ref_invalid", ticket,
                    f"remote ticket ref is duplicated: {ticket}",
                )
            remote_tips[ticket] = tip
            ticket_ids.add(ticket)
        for ref in refs:
            branch = re.sub(r"^refs/(?:remotes/origin|heads)/", "", ref)
            match = re.fullmatch(re.escape(self.prefix) + r"(T-[0-9]+)", branch)
            if match:
                ticket_ids.add(match.group(1))
        return ticket_ids, remote_tips

    def validate_ticket(self, ticket: str, remote_tip: str) -> None:
        branch = self.prefix + ticket
        remote_ref = "refs/remotes/origin/" + branch
        local_ref = "refs/heads/" + branch
        relative = f"factory/tickets/{ticket}.md"
        protected = self.git("show", "HEAD:" + relative)
        protected_states = (
            re.findall(r"(?mi)^State:\s*(.*?)\s*$", protected.stdout)
            if protected.returncode == 0 else []
        )
        protected_terminal_state = (
            protected_states[0].strip().lower()
            if len(protected_states) == 1
            and protected_states[0].strip().lower() in {"done", "canceled"}
            else ""
        )
        tracking = self.git("rev-parse", "--verify", remote_ref)
        tracking_tip = tracking.stdout.strip() if tracking.returncode == 0 else ""
        if not protected_terminal_state and tracking_tip and remote_tip != tracking_tip:
            self.fail(
                "activation_remote_ref_stale", ticket,
                f"{ticket} remote ticket ref is stale or unverified",
            )
        audit_ref = ""
        if protected_terminal_state:
            source_ref = "HEAD"
        elif remote_tip:
            if tracking_tip:
                source_ref = remote_ref
            else:
                audit_ref = "refs/factory/lease-audit/" + ticket
                fetched = self.git(
                    "fetch", "--quiet", "--no-tags", self.origin,
                    "refs/heads/" + branch + ":" + audit_ref,
                )
                fetched_tip = self.git("rev-parse", "--verify", audit_ref)
                if fetched.returncode or fetched_tip.stdout.strip() != remote_tip:
                    self.git("update-ref", "-d", audit_ref)
                    self.fail(
                        "activation_remote_ref_unverified", ticket,
                        f"{ticket} remote ticket ref could not be verified",
                    )
                source_ref = audit_ref
        elif self.git("show-ref", "--verify", "--quiet", local_ref).returncode == 0:
            self.fail(
                "activation_local_branch_unverified", ticket,
                f"{ticket} has an unverified local-only ticket branch",
            )
        else:
            source_ref = "HEAD"
        try:
            content = self.git("show", source_ref + ":" + relative)
        finally:
            if audit_ref:
                self.git("update-ref", "-d", audit_ref)
        if content.returncode:
            self.fail(
                "activation_ticket_source_missing", ticket,
                f"{ticket} is missing from its committed ticket source",
            )
        text = content.stdout
        states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", text)
        leases = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", text)
        if len(states) != 1:
            self.fail(
                "activation_ticket_state_invalid", ticket,
                f"{ticket} must contain exactly one State field",
            )
        if len(leases) > 1:
            self.fail(
                "activation_ticket_lease_invalid", ticket,
                f"{ticket} contains duplicate Kit-SHA fields",
            )
        state = states[0].strip()
        lease = leases[0].strip() if leases else ""
        if lease and not SHA.fullmatch(lease):
            self.fail(
                "activation_ticket_lease_invalid", ticket,
                f"{ticket} has a noncanonical Kit-SHA",
            )
        if state.lower() == "done":
            try:
                terminal = protected_terminal(self.product, ticket)
            except TerminalError as error:
                terminal = (
                    certified_legacy_terminal(
                        self.product, ticket, "HEAD", self.certified_previous_tree,
                    )
                    if source_ref == "HEAD" else None
                )
                if terminal is None:
                    raise ActivationError(
                        "activation_terminal_invalid", ticket,
                        f"{ticket} claims Done without valid protected-main terminal evidence: {error}",
                    ) from error
            if terminal.get("basis") not in {
                "attested-done",
                "attested-emergency-closeout",
                "validated-legacy-closeout",
                "validated-terminal-backfill",
                "validated-protected-merge-reconciliation",
                "certified-legacy-done",
            }:
                self.fail(
                    "activation_terminal_invalid", ticket,
                    f"{ticket} has an unknown terminal basis",
                )
            return
        if state.lower() == "canceled":
            if lease:
                self.fail(
                    "activation_canceled_lease", ticket,
                    f"{ticket} is canceled but still carries a Kit-SHA lease",
                )
            return
        if lease:
            if lease != self.candidate:
                if state.lower() == "approved" and self.protected_legacy_approval(
                    ticket, lease, source_ref, text,
                ):
                    return
                self.authorize_inflight(
                    ticket, branch, remote_tip, source_ref, state, lease,
                )
        elif state.lower() not in {"ready", "backlog", "blocked-escalated"}:
            self.fail(
                "activation_ticket_lease_missing", ticket,
                f"{ticket} from {source_ref} is in progress without a Kit-SHA lease",
            )

    def run(self) -> tuple[list[dict[str, str]], list[str], int]:
        details: list[str] = []
        blockers: list[dict[str, str]] = []
        historical_objects = 0
        try:
            historical_objects = hydrate(self.product)
            head = self.checked("rev-parse", "HEAD")
            remote = self.checked(
                "ls-remote", "--heads", "--", self.origin, "refs/heads/main",
            ).split()
            if not remote or remote[0] != head:
                self.fail(
                    "activation_product_not_main",
                    "activation",
                    "activation product HEAD is not current protected main",
                )
            ticket_ids, remote_tips = self.ticket_ids()
        except (HistoricalObjectError, OSError, subprocess.SubprocessError) as error:
            blockers.append({
                "reason_code": "historical_objects_invalid", "scope": "activation",
            })
            details.append(str(error))
            return blockers, details, historical_objects
        except ActivationError as error:
            return [error.blocker()], [str(error)], historical_objects
        for ticket in sorted(ticket_ids):
            try:
                self.validate_ticket(ticket, remote_tips.get(ticket, ""))
            except ActivationError as error:
                blocker = error.blocker()
                if blocker not in blockers:
                    blockers.append(blocker)
                    details.append(str(error))
        if self.authorization_loaded and self.used_authorizations != set(self.authorized):
            blockers.append({
                "reason_code": "activation_authorization_unused",
                "scope": "activation",
            })
            details.append("in-flight release authorization contains an unused ticket")
        return blockers, details, historical_objects


def validate(
    product: Path,
    candidate: str,
    candidate_scripts: Path,
    origin: str,
    certified_previous_tree: str = "",
) -> tuple[list[dict[str, str]], list[str], int]:
    if not SHA.fullmatch(candidate):
        return ([{
            "reason_code": "activation_candidate_invalid", "scope": "activation",
        }], ["activation candidate SHA is invalid"], 0)
    return Validator(
        product.resolve(strict=True), candidate, candidate_scripts,
        origin, certified_previous_tree,
    ).run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--candidate-scripts", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--certified-previous-tree", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    blockers, details, count = validate(
        args.product,
        args.candidate,
        args.candidate_scripts,
        args.origin,
        args.certified_previous_tree,
    )
    if args.json:
        print(json.dumps({
            "blockers": blockers,
            "historical_pr_objects": count,
            "schema": "nysa.software-factory.activation-preflight/v1",
            "status": "blocked" if blockers else "pass",
        }, sort_keys=True, separators=(",", ":")))
    else:
        for detail in details:
            print(detail, file=sys.stderr)
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
