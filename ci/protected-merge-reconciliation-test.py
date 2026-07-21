#!/usr/bin/env python3
"""Focused adversarial coverage for protected-merge reconciliation."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from legacy_closeout import ValidationError, protected_terminal  # noqa: E402
from protected_merge_reconciliation import (  # noqa: E402
    MIGRATION_DIR,
    reconciliation_batch,
    terminal_projection,
)


BASIS_KIT = "1" * 40
TARGET_KIT = "2" * 40
OTHER_KIT = "3" * 40
CHECKS = ("app-tests", "ci", "deploy", "test-immutability")
TICKETS = ("T-024", "T-030", "T-031")


def command(*args, cwd=None, check=True):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


class ProtectedMergeReconciliationTests(unittest.TestCase):
    """Exercise the one-time batch through real Git and a deterministic gh stub."""

    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="protected-merge-reconciliation-test.",
        )
        root = Path(self.temp.name)
        self.repo = root / "product"
        self.remote = root / "product.git"
        command("git", "init", "-q", "-b", "main", self.repo)
        command("git", "init", "--bare", "-q", self.remote)
        command("git", "-C", self.repo, "remote", "add", "origin", self.remote)
        self.cutoff = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat().replace("+00:00", "Z")
        self._build_evidence()

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, message):
        command("git", "-C", self.repo, "add", ".")
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "commit", "-qm", message,
        )
        return self.rev("HEAD")

    def rev(self, value):
        return command(
            "git", "-C", self.repo, "rev-parse", value,
        ).stdout.strip()

    def push_main(self, *, force=False):
        args = ["git", "-C", str(self.repo), "push", "-q"]
        if force:
            args.append("--force")
        args.extend(("origin", "main"))
        command(*args)
        command("git", "-C", self.repo, "fetch", "-q", "origin")

    def blob(self, ref, path):
        return self.rev(f"{ref}:{path}")

    def hash_object(self, path):
        return command(
            "git", "-C", self.repo, "hash-object", path,
        ).stdout.strip()

    def hash_file(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _ticket_text(self, ticket, state, kit):
        return (
            f"# {ticket} — migration fixture\n\n"
            f"State: {state}\n"
            "Initiative: I-005\n"
            "Priority: high\n"
            f"Branch: ticket/{ticket}\n"
            "Risk class: low\n"
            "External: no\n\n"
            "## Frozen contract\n\nExact migration fixture.\n\n"
            f"Kit-SHA: {kit}\n"
        )

    def _bundle_attestation(self, ticket, reviewed, source_kit, pr):
        route = self.repo / f"factory/route-plans/{ticket}.json"
        bundle = self.repo / f"factory/tickets/{ticket}-bundle.md"
        return {
            "schema": "nysa.software-factory.ticket-bundle/v1",
            "ticket": ticket,
            "repository": "acme/widget",
            "branch": f"ticket/{ticket}",
            "branch_head": reviewed,
            "reviewed_sha": reviewed,
            "bundle_path": f"factory/tickets/{ticket}-bundle.md",
            "bundle_blob": self.hash_object(bundle),
            "pr_number": pr,
            "pr_url": f"https://example.invalid/acme/widget/pull/{pr}",
            "reviewer_run_id": f"review-{ticket}",
            "narrator_run_id": f"narrate-{ticket}",
            "kit_sha": source_kit,
            "policy_hash": "a" * 64,
            "route_plan_path": f"factory/route-plans/{ticket}.json",
            "route_plan_blob": self.hash_object(route),
            "route_plan_sha256": self.hash_file(route),
            "attested_at": self.cutoff,
        }

    def _approval_attestation(self, ticket, reviewed, bundle_head, source_kit, pr):
        bundle = self.repo / f"factory/tickets/{ticket}-bundle.md"
        attestation = self.repo / f"factory/attestations/{ticket}/bundle.json"
        return {
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": ticket,
            "repository": "acme/widget",
            "branch": f"ticket/{ticket}",
            "parent_head": bundle_head,
            "reviewed_sha": reviewed,
            "bundle_blob": self.hash_object(bundle),
            "bundle_attestation_blob": self.hash_object(attestation),
            "pr_number": pr,
            "operator_version": "1",
            "linear_updated_at": self.cutoff,
            "observed_at": self.cutoff,
            "kit_sha": source_kit,
            "auto_merge_method": "squash",
            "attested_at": self.cutoff,
        }

    def _build_evidence(self):
        (self.repo / "factory/tickets").mkdir(parents=True)
        (self.repo / "factory/route-plans").mkdir(parents=True)
        (self.repo / "apps/api/src").mkdir(parents=True)
        (self.repo / "apps/api/tests").mkdir(parents=True)
        (self.repo / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\n"
            f"DONE_REQUIRED_CHECKS={','.join(CHECKS)}\n"
        )
        (self.repo / "factory/KIT_PIN").write_text(BASIS_KIT + "\n")
        ledger = (
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id\n"
        )
        for ticket in TICKETS:
            ledger += (
                f"2026-07-20,00:00:00,{ticket},reviewer,test,1,1,1,0,review-{ticket}\n"
                f"2026-07-20,00:01:00,{ticket},narrator,test,1,1,1,0,narrate-{ticket}\n"
            )
        (self.repo / "factory/ledger.csv").write_text(ledger)
        self.evidence = {}
        self.reviewed = {}
        self.bundle_heads = {}
        self.source_kits = {
            "T-024": BASIS_KIT,
            "T-030": OTHER_KIT,
            "T-031": BASIS_KIT,
        }
        self.prs = {
            "T-024": (145, 150),
            "T-030": (143, 143),
            "T-031": (144, 144),
        }
        for ticket in TICKETS:
            source_kit = self.source_kits[ticket]
            (self.repo / f"factory/tickets/{ticket}.md").write_text(
                self._ticket_text(
                    ticket, "Ready" if ticket == "T-024" else "Review", source_kit,
                )
            )
            (self.repo / f"factory/tickets/{ticket}-bundle.md").write_text(
                f"# {ticket} evidence bundle\n\nAll criteria pass.\n"
            )
            write_json(
                self.repo / f"factory/route-plans/{ticket}.json",
                {"schema": "route-plan/v1", "ticket": ticket},
            )
            (self.repo / f"apps/api/src/{ticket.lower()}.ts").write_text(
                f"export const value = '{ticket}';\n"
            )
            (self.repo / f"apps/api/tests/{ticket.lower()}.test.ts").write_text(
                f"// deterministic proof for {ticket}\n"
            )
            reviewed = self.commit(f"{ticket} reviewed source")
            self.reviewed[ticket] = reviewed
            if ticket == "T-024":
                ticket_path = self.repo / f"factory/tickets/{ticket}.md"
                ticket_path.write_text(
                    ticket_path.read_text() + "\nReviewer round 1: APPROVE\n"
                )
                verdict = self.commit(f"{ticket} reviewer verdict")
                bundle_path = self.repo / f"factory/tickets/{ticket}-bundle.md"
                bundle_path.write_text(bundle_path.read_text() + "\nFinal evidence.\n")
                self.bundle_heads[ticket] = self.commit(f"{ticket} legacy bundle evidence")
                self.evidence[ticket] = self.bundle_heads[ticket]
                continue
            state = "Approved" if ticket == "T-031" else "Awaiting Approval"
            (self.repo / f"factory/tickets/{ticket}.md").write_text(
                self._ticket_text(ticket, state, source_kit)
            )
            write_json(
                self.repo / f"factory/attestations/{ticket}/bundle.json",
                self._bundle_attestation(
                    ticket, reviewed, source_kit, self.prs[ticket][0],
                ),
            )
            bundle_head = self.commit(f"{ticket} bundle evidence")
            self.bundle_heads[ticket] = bundle_head
            if ticket == "T-031":
                write_json(
                    self.repo / f"factory/attestations/{ticket}/approval.json",
                    self._approval_attestation(
                        ticket, reviewed, bundle_head, source_kit,
                        self.prs[ticket][0],
                    ),
                )
                self.evidence[ticket] = self.commit(f"{ticket} approval evidence")
            else:
                self.evidence[ticket] = bundle_head
        self.basis = self.rev("HEAD")
        self.tree = self.rev("HEAD^{tree}")
        self.push_main()
        self.cutoff = datetime.now(timezone.utc).replace(
            microsecond=0,
        ).isoformat().replace("+00:00", "Z")
        checks = [
            {"name": name, "app_id": 7, "app_slug": "github-actions"}
            for name in CHECKS
        ]
        self.authorization = {
            "schema": "nysa.software-factory.protected-merge-reconciliation-authorization/v1",
            "repository": "acme/widget",
            "basis_kit_sha": BASIS_KIT,
            "target_kit_sha": TARGET_KIT,
            "candidate_contract": "1.6.0",
            "cutoff": self.cutoff,
            "protected_main_basis": {"commit": self.basis, "tree": self.tree},
            "required_checks": checks,
            "authorization": {
                "method": "manual-protected-main-merge",
                "operator": "operator@example.com",
                "authorized_at": self.cutoff,
                "statement": "Operator approves this exact one-time protected batch.",
                "auto_merge": False,
                "bypass": False,
            },
            "companions": [],
            "tickets": [],
        }
        self.receipts = {}
        ledger_digest = self.hash_file(self.repo / "factory/ledger.csv")
        for ticket in TICKETS:
            original_number, adoption_number = self.prs[ticket]
            classification = (
                "reviewed-clean-history-adoption"
                if ticket == "T-024" else "merged-adoption"
            )
            source_state = (
                "Ready" if ticket == "T-024"
                else "Approved" if ticket == "T-031"
                else "Awaiting Approval"
            )
            paths = [
                f"apps/api/src/{ticket.lower()}.ts",
                f"apps/api/tests/{ticket.lower()}.test.ts",
            ]
            self.authorization["tickets"].append({
                "ticket": ticket,
                "source_state": source_state,
                "source_kit_sha": self.source_kits[ticket],
                "classification": classification,
                "evidence_head": self.evidence[ticket],
                "original_pr_number": original_number,
                "adoption_pr_number": adoption_number,
                "paths": paths,
                "receipt": f"{MIGRATION_DIR}/{ticket}.json",
            })
            original = {
                "number": original_number,
                "head_ref": f"ticket/{ticket}",
                "base_ref": "main",
                "head": self.evidence[ticket],
                "merged": classification == "merged-adoption",
                "merge_commit": self.evidence[ticket] if classification == "merged-adoption" else None,
                "merged_at": self.cutoff if classification == "merged-adoption" else None,
                "merged_by": "operator" if classification == "merged-adoption" else None,
            }
            adoption = copy.deepcopy(original)
            if classification == "reviewed-clean-history-adoption":
                adoption.update({
                    "number": adoption_number,
                    "head_ref": f"ticket/{ticket}-adoption",
                    "merged": True,
                    "merge_commit": self.basis,
                    "merged_at": self.cutoff,
                    "merged_by": "operator",
                })
            route = self.repo / f"factory/route-plans/{ticket}.json"
            approval = self.repo / f"factory/attestations/{ticket}/approval.json"
            self.receipts[ticket] = {
                "schema": "nysa.software-factory.protected-merge-reconciliation/v1",
                "ticket": ticket,
                "repository": "acme/widget",
                "classification": classification,
                "source_state": source_state,
                "source_kit_sha": self.source_kits[ticket],
                "basis_kit_sha": BASIS_KIT,
                "target_kit_sha": TARGET_KIT,
                "candidate_contract": "1.6.0",
                "evidence_head": self.evidence[ticket],
                "source_ticket_blob": self.blob(
                    self.evidence[ticket], f"factory/tickets/{ticket}.md",
                ),
                "source_bundle_blob": self.blob(
                    self.evidence[ticket], f"factory/tickets/{ticket}-bundle.md",
                ),
                "route_plan_blob": self.blob(
                    self.evidence[ticket], f"factory/route-plans/{ticket}.json",
                ),
                "route_plan_sha256": self.hash_file(route),
                "bundle_attestation_blob": self.blob(
                    self.evidence[ticket], f"factory/attestations/{ticket}/bundle.json",
                ) if ticket != "T-024" else None,
                "approval_attestation_blob": (
                    self.hash_object(approval) if ticket == "T-031" else None
                ),
                "legacy_review": ({
                    "reviewed_sha": self.reviewed[ticket],
                    "verdict_commit": self.rev(f"{self.evidence[ticket]}^"),
                } if ticket == "T-024" else None),
                "original_pr": original,
                "adoption_pr": adoption,
                "paths": [
                    {"path": path, "blob": self.blob(self.evidence[ticket], path)}
                    for path in paths
                ],
                "checks": [dict(item, status="completed", conclusion="success", skipped=False)
                           for item in checks],
                "ledger": {
                    "sha256": ledger_digest,
                    "run_ids": [f"review-{ticket}", f"narrate-{ticket}"],
                    "reviewer_run_id": f"review-{ticket}",
                    "narrator_run_id": f"narrate-{ticket}",
                },
                "authorization_blob": "0" * 40,
                "cutoff": self.cutoff,
                "protected_main_basis": {"commit": self.basis, "tree": self.tree},
            }

    def publish(self, mutate=None, *, companions=None, unbound=None):
        authorization = copy.deepcopy(self.authorization)
        receipts = copy.deepcopy(self.receipts)
        for path, content in (companions or {}).items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            authorization["companions"].append({
                "path": path,
                "blob": self.hash_object(target),
            })
        for path, content in (unbound or {}).items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        terminals = {
            ticket: terminal_projection(
                command(
                    "git", "-C", self.repo, "show",
                    f"{self.basis}:factory/tickets/{ticket}.md",
                ).stdout,
                f"{MIGRATION_DIR}/{ticket}.json",
            )
            for ticket in TICKETS
        }
        if mutate:
            mutate(authorization, receipts, terminals)
        write_json(self.repo / MIGRATION_DIR / "authorization.json", authorization)
        auth_blob = self.hash_object(self.repo / MIGRATION_DIR / "authorization.json")
        for receipt in receipts.values():
            receipt["authorization_blob"] = auth_blob
        for ticket, receipt in receipts.items():
            write_json(self.repo / MIGRATION_DIR / f"{ticket}.json", receipt)
        for ticket, text in terminals.items():
            (self.repo / f"factory/tickets/{ticket}.md").write_text(text)
        (self.repo / "factory/KIT_PIN").write_text(TARGET_KIT + "\n")
        migration = self.commit("manual protected reconciliation")
        self.push_main()
        return migration

    def reset_to_basis(self):
        command("git", "-C", self.repo, "reset", "--hard", "-q", self.basis)
        self.push_main(force=True)

    def assert_refused(self, mutate):
        self.publish(mutate)
        with self.assertRaises(ValidationError):
            reconciliation_batch(self.repo)
        self.reset_to_basis()

    def test_exact_batch_covers_clean_adoption_missing_approval_and_approved_source(self):
        self.publish()
        batch = reconciliation_batch(self.repo)
        self.assertEqual(list(batch), list(TICKETS))
        self.assertEqual(
            {value["basis"] for value in batch.values()},
            {"validated-protected-merge-reconciliation"},
        )
        for ticket in TICKETS:
            self.assertEqual(
                protected_terminal(self.repo, ticket)["basis"],
                "validated-protected-merge-reconciliation",
            )
        self.assertNotEqual(
            self.receipts["T-024"]["original_pr"],
            self.receipts["T-024"]["adoption_pr"],
        )
        self.assertIsNone(self.receipts["T-030"]["approval_attestation_blob"])
        self.assertIsNotNone(self.receipts["T-031"]["approval_attestation_blob"])

    def test_repository_kit_basis_authority_and_batch_boundaries_fail_closed(self):
        mutations = {
            "wrong repository": lambda auth, receipts, terminals: auth.update(
                repository="other/widget",
            ),
            "wrong basis kit": lambda auth, receipts, terminals: auth.update(
                basis_kit_sha=OTHER_KIT,
            ),
            "same target kit": lambda auth, receipts, terminals: auth.update(
                target_kit_sha=BASIS_KIT,
            ),
            "wrong basis tree": lambda auth, receipts, terminals: auth[
                "protected_main_basis"
            ].update(tree="f" * 40),
            "stale operator adoption": lambda auth, receipts, terminals: auth[
                "authorization"
            ].update(authorized_at="2020-01-01T00:00:00Z"),
            "missing operator": lambda auth, receipts, terminals: auth[
                "authorization"
            ].update(operator=""),
            "missing receipt": lambda auth, receipts, terminals: receipts.pop("T-030"),
            "duplicate ticket": lambda auth, receipts, terminals: auth[
                "tickets"
            ].append(copy.deepcopy(auth["tickets"][0])),
            "extra receipt": lambda auth, receipts, terminals: receipts.update(
                {"T-999": copy.deepcopy(receipts["T-031"])}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assert_refused(mutate)

    def test_source_ticket_bundle_route_paths_and_ledger_are_immutable(self):
        mutations = {
            "ticket blob": lambda auth, receipts, terminals: receipts["T-030"].update(
                source_ticket_blob="f" * 40,
            ),
            "bundle blob": lambda auth, receipts, terminals: receipts["T-030"].update(
                source_bundle_blob="f" * 40,
            ),
            "route blob": lambda auth, receipts, terminals: receipts["T-030"].update(
                route_plan_blob="f" * 40,
            ),
            "product blob": lambda auth, receipts, terminals: receipts["T-030"][
                "paths"
            ][0].update(blob="f" * 40),
            "test blob": lambda auth, receipts, terminals: receipts["T-030"][
                "paths"
            ][1].update(blob="f" * 40),
            "missing reviewer": lambda auth, receipts, terminals: receipts["T-030"][
                "ledger"
            ].update(reviewer_run_id="missing-review"),
            "ledger omission": lambda auth, receipts, terminals: receipts["T-030"][
                "ledger"
            ]["run_ids"].pop(),
            "altered terminal projection": lambda auth, receipts, terminals: terminals.update(
                {"T-030": terminals["T-030"] + "\nForged-Contract: yes\n"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assert_refused(mutate)

    def test_pr_topology_evidence_approval_and_required_checks_fail_closed(self):
        mutations = {
            "wrong PR number": lambda auth, receipts, terminals: receipts["T-024"][
                "original_pr"
            ].update(number=999),
            "wrong evidence head": lambda auth, receipts, terminals: receipts["T-031"].update(
                evidence_head=self.evidence["T-024"],
            ),
            "non-ancestor evidence": lambda auth, receipts, terminals: receipts["T-024"][
                "original_pr"
            ].update(head=self.reviewed["T-024"]),
            "wrong merge commit": lambda auth, receipts, terminals: receipts["T-030"][
                "adoption_pr"
            ].update(merge_commit=self.evidence["T-024"]),
            "wrong check app": lambda auth, receipts, terminals: receipts["T-030"][
                "checks"
            ][0].update(app_id=99),
            "failed check": lambda auth, receipts, terminals: receipts["T-030"][
                "checks"
            ][0].update(conclusion="failure"),
            "missing deploy": lambda auth, receipts, terminals: receipts["T-030"][
                "checks"
            ].pop(2),
            "missing approved evidence": lambda auth, receipts, terminals: receipts["T-031"].update(
                approval_attestation_blob=None,
            ),
            "fabricated approval": lambda auth, receipts, terminals: receipts["T-030"].update(
                approval_attestation_blob=self.receipts["T-031"]["approval_attestation_blob"],
            ),
            "legacy fabricates bundle attestation": lambda auth, receipts, terminals: receipts[
                "T-024"
            ].update(bundle_attestation_blob=self.receipts["T-030"]["bundle_attestation_blob"]),
            "legacy omits review topology": lambda auth, receipts, terminals: receipts[
                "T-024"
            ].update(legacy_review=None),
            "normal claims legacy review": lambda auth, receipts, terminals: receipts[
                "T-030"
            ].update(legacy_review=copy.deepcopy(self.receipts["T-024"]["legacy_review"])),
            "merged classification reuses clean adoption": lambda auth, receipts, terminals: (
                auth["tickets"][0].update(classification="merged-adoption"),
                receipts["T-024"].update(classification="merged-adoption"),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assert_refused(mutate)

    def test_revert_and_reintroduction_is_not_a_second_authorization(self):
        migration = self.publish()
        command(
            "git", "-C", self.repo, "-c", "user.name=test",
            "-c", "user.email=test@example.com", "revert", "--no-edit", migration,
        )
        command("git", "-C", self.repo, "cherry-pick", migration)
        self.push_main()
        with self.assertRaises(ValidationError):
            reconciliation_batch(self.repo)

    def test_post_merge_evidence_drift_fails_closed(self):
        self.publish()
        path = self.repo / MIGRATION_DIR / "T-030.json"
        receipt = json.loads(path.read_text())
        receipt["checks"][0]["conclusion"] = "failure"
        write_json(path, receipt)
        self.commit("tamper with protected reconciliation")
        self.push_main()
        with self.assertRaises(ValidationError):
            protected_terminal(self.repo, "T-030")

    def test_superseded_partial_evidence_cannot_change_after_migration(self):
        paths = {
            "bundle markdown": "factory/tickets/T-030-bundle.md",
            "route plan": "factory/route-plans/T-030.json",
            "bundle attestation": "factory/attestations/T-030/bundle.json",
            "new approval": "factory/attestations/T-030/approval.json",
            "new refresh": "factory/attestations/T-030/refresh.json",
        }
        for label, relative in paths.items():
            with self.subTest(label):
                self.publish()
                target = self.repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(target.read_text() + "\n" if target.exists() else "{}\n")
                self.commit(f"mutate superseded {label}")
                self.push_main()
                with self.assertRaises(ValidationError):
                    reconciliation_batch(self.repo)
                self.reset_to_basis()

    def test_companion_files_are_exactly_bound_and_cannot_hide_extra_changes(self):
        companion = {"factory/tickets/T-032.md": "# T-032\n\nState: Backlog\n"}
        self.publish(companions=companion)
        self.assertEqual(list(reconciliation_batch(self.repo)), list(TICKETS))
        self.reset_to_basis()

        def wrong_blob(auth, receipts, terminals):
            auth["companions"][0]["blob"] = "f" * 40

        self.publish(wrong_blob, companions=companion)
        with self.assertRaises(ValidationError):
            reconciliation_batch(self.repo)
        self.reset_to_basis()

        self.publish(unbound={"factory/rulings.md": "# Rulings\n"})
        with self.assertRaises(ValidationError):
            reconciliation_batch(self.repo)

if __name__ == "__main__":
    unittest.main()
