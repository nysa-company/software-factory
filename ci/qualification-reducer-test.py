#!/usr/bin/env python3
"""Focused Contract 1.8 qualification reduction test."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_reducer", ROOT / "scripts/qualification-reducer.py"
)
assert SPEC and SPEC.loader
REDUCER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDUCER)


class QualificationReducerTest(unittest.TestCase):
    def test_immutable_report_recovers_exact_response_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            raw = b'{"status":"green"}\n'
            original = REDUCER.os.replace

            def lost(source, target):
                original(source, target)
                raise OSError("simulated response loss")

            with (
                patch.object(REDUCER.os, "replace", side_effect=lost),
                self.assertRaisesRegex(OSError, "response loss"),
            ):
                REDUCER.write_immutable(path, raw)
            REDUCER.write_immutable(path, raw)
            self.assertEqual(path.read_bytes(), raw)

    def test_remote_timeout_is_typed_but_local_timeout_is_not(self):
        timeout = subprocess.TimeoutExpired(["gh", "pr", "view"], 120)
        with patch.object(REDUCER.subprocess, "run", side_effect=timeout):
            with self.assertRaises(REDUCER.ExternalUnavailable):
                REDUCER.command("gh", "pr", "view", "1")
            with self.assertRaises(subprocess.TimeoutExpired):
                REDUCER.command("git", "status")

    def evidence(self):
        candidate = "a" * 40
        tickets = [f"T-{number}" for number in range(110, 114)]
        manifest = {
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": candidate,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": REDUCER.MANIFEST_SCHEMA,
            "target_done": 4,
            "tickets": tickets,
        }
        passports, terminals, prs = {}, {}, {}
        for ticket_number, ticket in enumerate(tickets, 110):
            completed, charges = [], []
            for role_number, role in enumerate(sorted(REDUCER.ROLES), 1):
                run_id = f"{ticket}-{role}"
                head = f"{ticket_number * 10 + role_number:040x}"
                digest = f"{ticket_number * 100 + role_number:064x}"
                receipt = f"{ticket_number * 1000 + role_number:064x}"
                completed.append({
                    "contract_version": "1.8.0",
                    "factory_sha": candidate,
                    "head_before": head,
                    "manifest_sha256": digest,
                    "role": role,
                    "run_id": run_id,
                    "transition_receipt_sha256": receipt,
                })
                charges.append({
                    "charge_micro_usd": 1_000_000,
                    "contract_version": "1.8.0",
                    "factory_sha": candidate,
                    "head_before": head,
                    "manifest_sha256": digest,
                    "role": role,
                    "run_id": run_id,
                    "transition_receipt_sha256": receipt,
                })
            pr_head = f"{ticket_number:040x}"
            merge = f"{ticket_number + 1000:040x}"
            passports[ticket] = {
                "branch": f"ticket/{ticket}",
                "charge_records": charges,
                "completed_role_evidence": completed,
                "contract_version": "1.8.0",
                "cumulative_charges_micro_usd": 6_000_000,
                "factory_release_history": [{
                    "contract_version": "1.8.0",
                    "factory_sha": candidate,
                }],
                "factory_sha": candidate,
                "head_sha": pr_head,
                "publication_state": "merged",
                "ticket": ticket,
            }
            terminals[ticket] = {
                "approved_pr_head": pr_head,
                "kit_sha": candidate,
                "merge_commit": merge,
                "pr_number": ticket_number,
                "required_checks": ["ci"],
                "schema": "nysa.software-factory.ticket-done/v1",
                "successful_checks": ["ci"],
                "ticket": ticket,
            }
            prs[ticket] = {
                "baseRefName": "main",
                "createdAt": f"2026-07-27T10:0{ticket_number - 110}:00Z",
                "headRefName": f"ticket/{ticket}",
                "headRefOid": pr_head,
                "mergeCommit": {"oid": merge},
                "mergedAt": f"2026-07-27T11:0{ticket_number - 110}:00Z",
                "number": ticket_number,
                "state": "MERGED",
            }
        events = [
            {"event": "restart_boundary", "factory_sha": candidate, "tickets": tickets},
            {"event": "controller_recovered", "factory_sha": candidate, "tickets": tickets},
            {"event": "cell_relocated", "factory_sha": candidate, "ticket": tickets[0]},
        ]
        for ticket in tickets:
            events.extend([
                {"event": "publication_acquired", "factory_sha": candidate, "ticket": ticket},
                {"event": "publication_released", "factory_sha": candidate, "ticket": ticket},
                {"event": "ticket_complete", "factory_sha": candidate, "ticket": ticket},
            ])
        for epoch, event in enumerate(events, 1):
            event["observed_at_epoch_ns"] = epoch
        caps = {ticket: 25_000_000 for ticket in tickets}
        return manifest, passports, events, terminals, prs, caps

    def add_latency_evidence(self, evidence):
        manifest, passports, events, _terminals, _prs, _caps = evidence
        base = 1_000_000_000_000
        boundary = next(item for item in events if item["event"] == "restart_boundary")
        boundary.update(
            event_sha256="b" * 64, observed_at_epoch_ns=base + 60_000_000_000,
        )
        for index, event in enumerate(events):
            if event is not boundary:
                event["observed_at_epoch_ns"] = base + (70 + index) * 1_000_000_000
        manifest_sha256 = hashlib.sha256(REDUCER.canonical(manifest).encode()).hexdigest()
        receipt = {
            "activation_started_epoch_ns": base,
            "kit_sha": manifest["factory_sha"], "kit_tree": "b" * 40,
            "product_sha": "c" * 40, "product_tree": "d" * 40,
            "status": "pass",
        }
        receipt["receipt_id"] = hashlib.sha256(
            (REDUCER.canonical(receipt) + "\n").encode()
        ).hexdigest()
        activation = {
            "activation_receipt_id": receipt["receipt_id"],
            "activation_started_epoch_ns": base,
            "event": "activation_complete",
            "factory_sha": manifest["factory_sha"],
            "factory_tree": "b" * 40,
            "observed_at_epoch_ns": base + 61_000_000_000,
            "product_sha": "c" * 40,
            "product_tree": "d" * 40,
            "qualification_generation": manifest["generation"],
            "qualification_manifest_sha256": manifest_sha256,
            "restart_boundary_event_sha256": boundary["event_sha256"],
            "schema": REDUCER.EVENT_SCHEMA,
            "ticket": None,
        }
        activation["event_sha256"] = hashlib.sha256(
            REDUCER.canonical(activation).encode()
        ).hexdigest()
        events.append(activation)
        narrator_run_ids = {}
        for index, ticket in enumerate(manifest["tickets"]):
            planner = next(
                item for item in passports[ticket]["completed_role_evidence"]
                if item["role"] == "planner"
            )
            completed = next(
                item for item in passports[ticket]["completed_role_evidence"]
                if item["role"] == "narrator"
            )
            narrator_run_ids[ticket] = completed["run_id"]
            narrator_at = base + (300 + index * 10) * 1_000_000_000
            if index == 0:
                planner_charge = next(
                    item for item in passports[ticket]["charge_records"]
                    if item["role"] == "planner"
                )
                failed_run = f"{ticket}-planner-failed"
                failed_receipt = "e" * 64
                passports[ticket]["charge_records"].append({
                    **planner_charge, "manifest_sha256": "d" * 64,
                    "run_id": failed_run,
                    "transition_receipt_sha256": failed_receipt,
                })
                passports[ticket]["cumulative_charges_micro_usd"] += 1_000_000
                events.append({
                    "event": "attempt_terminal", "factory_sha": manifest["factory_sha"],
                    "observed_at_epoch_ns": base + 71_000_000_000,
                    "role": "planner", "run_id": failed_run,
                    "submitted_at_epoch_ns": base + 70_000_000_000,
                    "task_submitted": "1", "ticket": ticket,
                    "transition_receipt_sha256": failed_receipt,
                })
            events.extend((
                {
                    "event": "attempt_terminal", "factory_sha": manifest["factory_sha"],
                    "observed_at_epoch_ns": base + (80 + index) * 1_000_000_000,
                    "role": "planner", "run_id": planner["run_id"],
                    "submitted_at_epoch_ns": base + (80 + index) * 1_000_000_000,
                    "task_submitted": "1",
                    "ticket": ticket,
                    "transition_receipt_sha256": planner["transition_receipt_sha256"],
                },
                {
                    "accounting_state": "completed", "event": "attempt_terminal",
                    "exit_status": "0", "factory_sha": manifest["factory_sha"],
                    "observed_at_epoch_ns": narrator_at, "role": "narrator",
                    "role_exit": "ok", "run_id": completed["run_id"],
                    "ticket": ticket,
                    "transition_receipt_sha256": completed["transition_receipt_sha256"],
                },
            ))
            if index == 0:
                narrator_charge = next(
                    item for item in passports[ticket]["charge_records"]
                    if item["role"] == "narrator"
                )
                narrator_evidence = next(
                    item for item in passports[ticket]["completed_role_evidence"]
                    if item["role"] == "narrator"
                )
                unused_run = f"{ticket}-narrator-unused"
                unused_receipt = "f" * 64
                passports[ticket]["charge_records"].append({
                    **narrator_charge, "head_before": "e" * 40,
                    "manifest_sha256": "c" * 64, "run_id": unused_run,
                    "transition_receipt_sha256": unused_receipt,
                })
                passports[ticket]["completed_role_evidence"].append({
                    **narrator_evidence, "head_before": "e" * 40,
                    "manifest_sha256": "c" * 64, "run_id": unused_run,
                    "transition_receipt_sha256": unused_receipt,
                })
                passports[ticket]["cumulative_charges_micro_usd"] += 1_000_000
                events.append({
                    "accounting_state": "completed", "event": "attempt_terminal",
                    "exit_status": "0", "factory_sha": manifest["factory_sha"],
                    "observed_at_epoch_ns": narrator_at + 100_000_000_000,
                    "role": "narrator", "role_exit": "ok", "run_id": unused_run,
                    "ticket": ticket, "transition_receipt_sha256": unused_receipt,
                })
            next(
                item for item in events
                if item["event"] == "ticket_complete" and item["ticket"] == ticket
            )["observed_at_epoch_ns"] = narrator_at + 240_000_000_000
        return receipt, narrator_run_ids

    def test_exact_green_evidence_passes_and_replayed_role_refuses(self):
        evidence = self.evidence()
        report = REDUCER.verify(*evidence)
        self.assertEqual(report["status"], "green")
        self.assertEqual(report["total_charge_micro_usd"], 24_000_000)
        evidence[1]["T-110"]["completed_role_evidence"].append(
            evidence[1]["T-110"]["completed_role_evidence"][0]
        )
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "replayed or is incomplete"
        ):
            REDUCER.verify(*evidence)

    def test_high_budget_fresh_evidence_passes(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"].pop()
        manifest.update({
            "budget_usd": "300.000000",
            "capacity": 3,
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "target_done": 3,
        })
        for values in (passports, terminals, prs, caps):
            del values[removed]
        events[:] = [item for item in events if item.get("ticket") != removed]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
        caps.update({ticket: 100_000_000 for ticket in caps})
        receipt, narrators = self.add_latency_evidence(evidence)
        report = REDUCER.verify(*evidence, receipt, narrators)
        self.assertEqual(report["status"], "green")
        self.assertEqual(report["latency"]["cold_activation_ms"], 61_000)
        self.assertEqual(
            report["latency"]["final_narrator_to_done_ms"],
            {ticket: 240_000 for ticket in manifest["tickets"]},
        )
        self.assertEqual(
            REDUCER.canonical(report),
            REDUCER.canonical(REDUCER.verify(*evidence, receipt, narrators)),
        )
        slow = copy.deepcopy(evidence)
        activation = next(item for item in slow[2] if item["event"] == "activation_complete")
        activation["observed_at_epoch_ns"] = (
            activation["activation_started_epoch_ns"] + 181_000_000_000
        )
        for index, event in enumerate(
            item for item in slow[2]
            if item.get("event") == "attempt_terminal"
            and item.get("role") == "planner"
        ):
            event["submitted_at_epoch_ns"] = (
                activation["observed_at_epoch_ns"] + (index + 1) * 1_000_000_000
            )
        with self.assertRaisesRegex(REDUCER.QualificationError, "target exceeded"):
            REDUCER.verify(*slow, receipt, narrators)
        out_of_order = copy.deepcopy(evidence)
        ticket = manifest["tickets"][0]
        narrator = next(
            item for item in out_of_order[2]
            if item.get("event") == "attempt_terminal" and item.get("ticket") == ticket
        )
        complete = next(
            item for item in out_of_order[2]
            if item.get("event") == "ticket_complete" and item.get("ticket") == ticket
        )
        complete["observed_at_epoch_ns"] = narrator["observed_at_epoch_ns"] - 1
        with self.assertRaisesRegex(REDUCER.QualificationError, "out of order"):
            REDUCER.verify(*out_of_order, receipt, narrators)

        changed_receipt = dict(receipt)
        changed_receipt["activation_started_epoch_ns"] += 1
        changed_receipt.pop("receipt_id")
        changed_receipt["receipt_id"] = hashlib.sha256(
            (REDUCER.canonical(changed_receipt) + "\n").encode()
        ).hexdigest()
        with self.assertRaisesRegex(REDUCER.QualificationError, "activation timing"):
            REDUCER.verify(*evidence, changed_receipt, narrators)

        wrong_narrator = {**narrators, ticket: "unprotected-narrator"}
        with self.assertRaisesRegex(REDUCER.QualificationError, "role timing"):
            REDUCER.verify(*evidence, receipt, wrong_narrator)

        duplicate_narrator = copy.deepcopy(evidence)
        selected = next(
            item for item in duplicate_narrator[2]
            if item.get("event") == "attempt_terminal"
            and item.get("run_id") == narrators[ticket]
        )
        duplicate_narrator[2].append({
            **selected, "observed_at_epoch_ns": selected["observed_at_epoch_ns"] + 1,
        })
        with self.assertRaisesRegex(REDUCER.QualificationError, "role timing"):
            REDUCER.verify(*duplicate_narrator, receipt, narrators)

        missing_submission = copy.deepcopy(evidence)
        next(
            item for item in missing_submission[2]
            if item.get("event") == "attempt_terminal"
            and item.get("role") == "planner"
        ).pop("submitted_at_epoch_ns")
        with self.assertRaisesRegex(REDUCER.QualificationError, "timing proof is invalid"):
            REDUCER.verify(*missing_submission, receipt, narrators)

        invalid = list(self.evidence())
        invalid[0].update({
            "budget_usd": "300.000000",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
        })
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "qualification inputs are incomplete",
        ):
            REDUCER.verify(*invalid)

    def test_high_budget_fresh_ticket_caps_ignore_runtime_overrides(self):
        manifest = self.evidence()[0]
        manifest.update({
            "budget_usd": "300.000000",
            "capacity": 3,
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "target_done": 3,
            "tickets": manifest["tickets"][:3],
        })
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                REDUCER.effective_ticket_caps(Path(temporary), ROOT, manifest),
                {ticket: 100_000_000 for ticket in manifest["tickets"]},
            )

    def test_protected_terminal_reconciliation_is_zero_cost_and_fail_closed(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, _caps = evidence
        ticket = manifest["tickets"][0]
        original_passport = passports.pop(ticket)
        normal = terminals[ticket]
        terminals[ticket] = {
            "merge_commit": normal["merge_commit"],
            "pr_head": normal["approved_pr_head"],
            "pr_number": normal["pr_number"],
            "required_checks": normal["required_checks"],
            "schema": "nysa.software-factory.ticket-emergency-done/v2",
            "successful_checks": normal["successful_checks"],
            "ticket": ticket,
        }
        prs[ticket]["headRefName"] = f"ticket/{ticket}-reviewed"
        events[:] = [
            item for item in events
            if not (
                item.get("ticket") == ticket
                and item.get("event") in {
                    "publication_acquired", "publication_released",
                }
            )
        ]
        reconciliation = {
            "done_sha256": hashlib.sha256(
                REDUCER.canonical(terminals[ticket]).encode()
            ).hexdigest(),
            "event": "protected_terminal_reconciled",
            "factory_sha": manifest["factory_sha"],
            "observed_at_epoch_ns": len(events) + 1,
            "protected_main_sha": "b" * 40,
            "protected_main_tree": "c" * 40,
            "protected_ticket_blob": "d" * 40,
            "qualification_charge_micro_usd": 0,
            "qualification_generation": manifest["generation"],
            "qualification_manifest_sha256": hashlib.sha256(
                REDUCER.canonical(manifest).encode()
            ).hexdigest(),
            "reconciliation_schema": (
                REDUCER.PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
            ),
            "terminal_basis": "attested-emergency-closeout",
            "ticket": ticket,
        }
        events.append(reconciliation)

        report = REDUCER.verify(*evidence)
        reconciled = next(item for item in report["tickets"] if item["ticket"] == ticket)
        self.assertEqual(reconciled["roles"], 0)
        self.assertEqual(reconciled["charge_micro_usd"], 0)
        self.assertEqual(report["total_charge_micro_usd"], 18_000_000)

        incomplete_boundary = copy.deepcopy(evidence)
        incomplete_boundary[2][-1].pop("qualification_manifest_sha256")
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is invalid"
        ):
            REDUCER.verify(*incomplete_boundary)

        unknown_boundary = copy.deepcopy(evidence)
        unknown_boundary[2][-1]["qualification_unknown"] = "refuse"
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is invalid"
        ):
            REDUCER.verify(*unknown_boundary)

        duplicate_source = copy.deepcopy(evidence)
        duplicate_source[1][ticket] = original_passport
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "inputs are incomplete"
        ):
            REDUCER.verify(*duplicate_source)

        charged = copy.deepcopy(evidence)
        charged[2][-1]["qualification_charge_micro_usd"] = 1
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is invalid"
        ):
            REDUCER.verify(*charged)

        drifted = copy.deepcopy(evidence)
        drifted[4][ticket]["headRefOid"] = "e" * 40
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is invalid"
        ):
            REDUCER.verify(*drifted)

        replayed_publication = copy.deepcopy(evidence)
        replayed_publication[2].extend([{
            "event": "publication_acquired",
            "factory_sha": manifest["factory_sha"],
            "observed_at_epoch_ns": len(replayed_publication[2]) + 1,
            "ticket": ticket,
        }, {
            "event": "publication_released",
            "factory_sha": manifest["factory_sha"],
            "observed_at_epoch_ns": len(replayed_publication[2]) + 2,
            "ticket": ticket,
        }])
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "serialization proof is incomplete"
        ):
            REDUCER.verify(*replayed_publication)

        duplicated = copy.deepcopy(evidence)
        duplicated[2].append({
            **duplicated[2][-1],
            "observed_at_epoch_ns": len(duplicated[2]) + 1,
        })
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is duplicated"
        ):
            REDUCER.verify(*duplicated)

    def test_protected_terminal_reconciliation_detects_protected_main_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            product = Path(temporary)
            ticket_path = product / "factory/tickets/T-110.md"
            ticket_path.parent.mkdir(parents=True)
            ticket_path.write_text("State: Done\n", encoding="utf-8")
            done_path = product / "factory/attestations/T-110/done.json"
            done_path.parent.mkdir(parents=True)
            done = {"ticket": "T-110"}
            done_path.write_text(json.dumps(done) + "\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(product)], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@nysa.dev"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(product), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(product), "add", "factory"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(product), "commit", "-qm", "terminal"],
                check=True,
            )
            observed = subprocess.run(
                ["git", "-C", str(product), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(product), "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            blob = subprocess.run(
                [
                    "git", "-C", str(product), "rev-parse",
                    "HEAD:factory/tickets/T-110.md",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            event = {
                "done_sha256": hashlib.sha256(
                    REDUCER.canonical(done).encode()
                ).hexdigest(),
                "protected_main_sha": observed,
                "protected_main_tree": tree,
                "protected_ticket_blob": blob,
                "terminal_basis": "attested-emergency-closeout",
            }
            with patch.object(REDUCER, "protected_terminal", return_value={
                "basis": "attested-emergency-closeout", "ticket": "T-110",
            }):
                REDUCER.validate_protected_reconciliation(
                    product, "T-110", event, observed, done,
                )
                ticket_path.write_text("State: Done\nchanged\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(product), "add", str(ticket_path)], check=True,
                )
                subprocess.run(
                    ["git", "-C", str(product), "commit", "-qm", "drift"],
                    check=True,
                )
                current = subprocess.run(
                    ["git", "-C", str(product), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()
                with self.assertRaisesRegex(
                    REDUCER.QualificationError, "reconciliation changed"
                ):
                    REDUCER.validate_protected_reconciliation(
                        product, "T-110", event, current, done,
                    )

    def test_all_terminal_successor_does_not_require_synthetic_relocation(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"].pop()
        manifest.update({
            "budget_usd": "300.000000",
            "capacity": 3,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
            "target_done": 3,
        })
        for values in (passports, terminals, prs, caps):
            del values[removed]
        events[:] = [
            item for item in events
            if item.get("ticket") != removed
            and item.get("event") not in {
                "cell_relocated", "publication_acquired", "publication_released",
            }
        ]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
        manifest_digest = hashlib.sha256(
            REDUCER.canonical(manifest).encode()
        ).hexdigest()
        for ticket in manifest["tickets"]:
            passports.pop(ticket)
            events.append({
                "done_sha256": hashlib.sha256(
                    REDUCER.canonical(terminals[ticket]).encode()
                ).hexdigest(),
                "event": "protected_terminal_reconciled",
                "factory_sha": manifest["factory_sha"],
                "protected_main_sha": "b" * 40,
                "protected_main_tree": "c" * 40,
                "protected_ticket_blob": "d" * 40,
                "qualification_charge_micro_usd": 0,
                "qualification_generation": manifest["generation"],
                "qualification_manifest_sha256": manifest_digest,
                "reconciliation_schema": (
                    REDUCER.PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
                ),
                "terminal_basis": "attested-done",
                "ticket": ticket,
            })
        for epoch, event in enumerate(events, 1):
            event["observed_at_epoch_ns"] = epoch

        report = REDUCER.verify(*evidence)
        self.assertEqual(report["status"], "green")
        self.assertTrue(all(item["roles"] == 0 for item in report["tickets"]))

        duplicate_relocation = copy.deepcopy(evidence)
        duplicate_relocation[2].extend([{
            "event": "cell_relocated",
            "factory_sha": manifest["factory_sha"],
            "observed_at_epoch_ns": len(duplicate_relocation[2]) + number,
            "ticket": manifest["tickets"][0],
        } for number in (1, 2)])
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "relocation, or completion proof is missing"
        ):
            REDUCER.verify(*duplicate_relocation)

        live_without_relocation = list(self.evidence())
        live_without_relocation[2] = [
            item for item in live_without_relocation[2]
            if item.get("event") != "cell_relocated"
        ]
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "relocation, or completion proof is missing"
        ):
            REDUCER.verify(*live_without_relocation)

    def test_successor_emergency_terminal_retains_exact_passport_evidence(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"].pop()
        source = "b" * 40
        candidate = manifest["factory_sha"]
        manifest.update({
            "budget_usd": "300.000000",
            "capacity": 3,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": source,
            "target_done": 3,
        })
        for values in (passports, terminals, prs, caps):
            del values[removed]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
        events[:] = [item for item in events if item.get("ticket") != removed]
        for passport in passports.values():
            passport["factory_release_history"].insert(0, {
                "contract_version": "1.8.0", "factory_sha": source,
            })
            passport["migration_history"] = [{
                "from_factory_sha": source,
                "schema": REDUCER.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": candidate,
            }]

        ticket = manifest["tickets"][0]
        passport = passports[ticket]
        passport_sha = "d" * 64
        terminal_factory = "e" * 40
        for name in ("charge_records", "completed_role_evidence"):
            for item in passport[name]:
                item["factory_sha"] = source
        passport.update({
            "current_state": "Review",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": source,
            }],
            "factory_sha": source,
            "migration_history": [],
            "passport_sha256": passport_sha,
            "publication_state": "validating",
        })
        pr_head = passport["head_sha"]
        merge = terminals[ticket]["merge_commit"]
        pause_file = "e" * 64
        pause_receipt = "f" * 64
        terminals[ticket] = {
            "kit_sha": terminal_factory,
            "merge_commit": merge,
            "plan": {
                "claim": {
                    "blocked_reason": "factory-issue-pause",
                    "parked": True,
                    "receipt": pause_receipt,
                    "role": "factory-paused",
                    "sha256": pause_file,
                    "status": "blocked",
                },
                "execution_basis": "authenticated-passport",
                "kit_sha": terminal_factory,
                "passport": {
                    "current_state": "Review",
                    "factory_sha": source,
                    "head_sha": pr_head,
                    "passport_sha256": passport_sha,
                    "publication_state": "validating",
                },
            },
            "pr_head": pr_head,
            "pr_number": prs[ticket]["number"],
            "required_checks": ["ci"],
            "schema": "nysa.software-factory.ticket-emergency-done/v1",
            "successful_checks": ["ci"],
            "ticket": ticket,
        }
        events[:] = [
            item for item in events
            if not (
                item.get("ticket") == ticket
                and item.get("event") in {"publication_acquired", "publication_released"}
            )
        ]
        events.append({
            "done_sha256": hashlib.sha256(
                REDUCER.canonical(terminals[ticket]).encode()
            ).hexdigest(),
            "event": "emergency_terminal_reconciled",
            "factory_sha": candidate,
            "pause_file_sha256": pause_file,
            "pause_receipt_sha256": pause_receipt,
            "protected_main_sha": "1" * 40,
            "protected_main_tree": "2" * 40,
            "protected_ticket_blob": "3" * 40,
            "qualification_charge_micro_usd": 0,
            "reconciliation_schema": (
                REDUCER.EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA
            ),
            "source_current_state": "Review",
            "source_factory_sha": source,
            "source_head_sha": pr_head,
            "source_passport_sha256": passport_sha,
            "source_publication_state": "validating",
            "terminal_basis": "attested-emergency-closeout",
            "terminal_factory_sha": terminal_factory,
            "terminal_release_receipt_id": "9" * 64,
            "ticket": ticket,
        })
        for epoch, event in enumerate(events, 1):
            event["observed_at_epoch_ns"] = epoch

        report = REDUCER.verify(*evidence)
        retained = next(item for item in report["tickets"] if item["ticket"] == ticket)
        self.assertEqual(retained["evidence_mode"], "passport-emergency-closeout")
        self.assertEqual(retained["roles"], 6)
        self.assertEqual(retained["charge_micro_usd"], 6_000_000)
        self.assertEqual(retained["qualification_charge_micro_usd"], 0)

        drifted = copy.deepcopy(evidence)
        drifted[3][ticket]["plan"]["passport"]["head_sha"] = "4" * 40
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "emergency terminal reconciliation is invalid",
        ):
            REDUCER.verify(*drifted)

        duplicated = copy.deepcopy(evidence)
        duplicated[2].append({
            **next(
                item for item in duplicated[2]
                if item.get("event") == "emergency_terminal_reconciled"
            ),
            "observed_at_epoch_ns": len(duplicated[2]) + 1,
        })
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "reconciliation is duplicated",
        ):
            REDUCER.verify(*duplicated)

    def test_three_ticket_successor_accepts_authenticated_history_and_cap(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"][0]
        manifest["tickets"] = manifest["tickets"][1:]
        manifest["target_done"] = 3
        manifest["capacity"] = 3
        manifest["budget_usd"] = "300.000000"
        manifest["per_ticket_budget_usd"] = "100.000000"
        manifest["per_run_budget_usd"] = "10.000000"
        manifest["mode"] = "successor"
        for values in (passports, terminals, prs, caps):
            del values[removed]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
            elif event.get("event") == "cell_relocated" and event.get("ticket") == removed:
                event["ticket"] = manifest["tickets"][0]
        events[:] = [item for item in events if item.get("ticket") != removed]

        prior = "b" * 40
        intermediate = "c" * 40
        manifest["source_factory_sha"] = prior
        for passport in passports.values():
            passport["factory_release_history"].insert(0, {
                "contract_version": "1.8.0",
                "factory_sha": prior,
            })
            passport["factory_release_history"].insert(1, {
                "contract_version": "1.8.0",
                "factory_sha": intermediate,
            })
            passport["migration_history"] = [{
                "from_factory_sha": prior,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": intermediate,
            }, {
                "from_factory_sha": intermediate,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": manifest["factory_sha"],
            }]

        ticket = manifest["tickets"][0]
        for number in range(10):
            passports[ticket]["charge_records"].append({
                "charge_micro_usd": 2_000_000,
                "contract_version": "1.8.0",
                "factory_sha": prior,
                "manifest_sha256": f"{9000 + number:064x}",
                "run_id": f"{ticket}-failed-{number}",
            })
            passports[ticket]["charge_records"].append({
                "charge_micro_usd": 2_000_000,
                "contract_version": "1.8.0",
                "factory_sha": manifest["factory_sha"],
                "manifest_sha256": f"{9100 + number:064x}",
                "run_id": f"{ticket}-qualification-failed-{number}",
            })
        passports[ticket]["cumulative_charges_micro_usd"] = 46_000_000
        caps[ticket] = 30_000_000
        report = REDUCER.verify(*evidence)
        self.assertEqual(report["status"], "green")
        self.assertEqual(report["qualification_charge_micro_usd"], 38_000_000)

        candidate_native = copy.deepcopy(evidence)
        candidate_passport = candidate_native[1][ticket]
        candidate_passport["factory_release_history"] = [{
            "contract_version": "1.8.0",
            "factory_sha": manifest["factory_sha"],
        }]
        candidate_passport["migration_history"] = []
        with self.assertRaisesRegex(
            REDUCER.QualificationError, f"{ticket} passport is not terminal",
        ):
            REDUCER.verify(*candidate_native)

        passports[ticket]["migration_history"][1]["from_factory_sha"] = "d" * 40
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "successor migration is missing"
        ):
            REDUCER.verify(*evidence)
        passports[ticket]["migration_history"][1]["from_factory_sha"] = intermediate

        caps[ticket] = 25_000_000
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "charges do not match the envelope"
        ):
            REDUCER.verify(*evidence)

    def test_successor_adopts_source_terminal_once_without_publication_replay(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"].pop()
        manifest.update({
            "budget_usd": "300.000000",
            "capacity": 3,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
            "target_done": 3,
        })
        for values in (passports, terminals, prs, caps):
            del values[removed]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
            elif event.get("event") == "cell_relocated" and event.get("ticket") == removed:
                event["ticket"] = manifest["tickets"][1]
        events[:] = [item for item in events if item.get("ticket") != removed]

        source = manifest["source_factory_sha"]
        candidate = manifest["factory_sha"]
        for passport in passports.values():
            passport["factory_release_history"].insert(0, {
                "contract_version": "1.8.0", "factory_sha": source,
            })
            passport["migration_history"] = [{
                "from_factory_sha": source,
                "schema": REDUCER.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": candidate,
            }]

        adopted = manifest["tickets"][0]
        done_kit = "c" * 40
        passport_source = "9" * 40
        source_passport = "d" * 64
        candidate_passport = "e" * 64
        passport = passports[adopted]
        approved_head = terminals[adopted]["approved_pr_head"]
        intermediate_head = "7" * 40
        current_head = "8" * 40
        protected_base = "4" * 40
        route = "5" * 64
        passport["factory_release_history"].insert(0, {
            "contract_version": "1.8.0", "factory_sha": done_kit,
        })
        passport["factory_release_history"].insert(2, {
            "contract_version": "1.8.0", "factory_sha": passport_source,
        })
        for name in ("charge_records", "completed_role_evidence"):
            for item in passport[name]:
                item["factory_sha"] = done_kit
        passport.update({
            "current_state": "Approved",
            "head_sha": current_head,
            "parent_digest": source_passport,
            "parent_file_sha256": "8" * 64,
            "passport_sha256": candidate_passport,
            "protected_base_sha": protected_base,
            "route_plan_sha256": route,
        })
        passport["migration_history"] = [{
            "from_factory_sha": source,
            "from_head_sha": approved_head,
            "from_passport_file_sha256": "2" * 64,
            "from_passport_sha256": "1" * 64,
            "from_protected_base_sha": "3" * 40,
            "from_route_plan_sha256": "6" * 64,
            "schema": REDUCER.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": passport_source,
            "to_head_sha": intermediate_head,
            "to_protected_base_sha": protected_base,
            "to_route_plan_sha256": route,
        }, {
            "from_factory_sha": passport_source,
            "from_head_sha": intermediate_head,
            "from_passport_file_sha256": "8" * 64,
            "from_passport_sha256": source_passport,
            "from_protected_base_sha": protected_base,
            "from_route_plan_sha256": route,
            "schema": REDUCER.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": candidate,
            "to_head_sha": current_head,
            "to_protected_base_sha": protected_base,
            "to_route_plan_sha256": route,
        }]
        terminals[adopted]["kit_sha"] = done_kit
        events[:] = [
            item for item in events
            if not (
                item.get("ticket") == adopted
                and item.get("event")
                in {"publication_acquired", "publication_released"}
            )
        ]
        events.append({
            "adoption_schema": REDUCER.TERMINAL_ADOPTION_SCHEMA,
            "approved_pr_head": terminals[adopted]["approved_pr_head"],
            "candidate_passport_sha256": candidate_passport,
            "done_sha256": hashlib.sha256(
                REDUCER.canonical(terminals[adopted]).encode()
            ).hexdigest(),
            "event": "terminal_adopted",
            "factory_sha": candidate,
            "merge_commit": terminals[adopted]["merge_commit"],
            "passport_source_factory_sha": passport_source,
            "pr_number": terminals[adopted]["pr_number"],
            "source_current_state": "Approved",
            "source_factory_sha": source,
            "source_passport_sha256": source_passport,
            "source_publication_state": "merged",
            "ticket": adopted,
        })
        for epoch, event in enumerate(events, 1):
            event["observed_at_epoch_ns"] = epoch

        receipt, narrators = self.add_latency_evidence(evidence)
        self.assertEqual(
            REDUCER.verify(*evidence, receipt, narrators)["status"], "green"
        )

        ambiguous = copy.deepcopy(passport)
        cycle_head = "f" * 40
        cycle = copy.deepcopy(passport["migration_history"][0])
        cycle.update({
            "to_factory_sha": source,
            "to_head_sha": cycle_head,
            "to_protected_base_sha": cycle["from_protected_base_sha"],
            "to_route_plan_sha256": cycle["from_route_plan_sha256"],
        })
        cycle_back = copy.deepcopy(cycle)
        cycle_back.update({
            "from_head_sha": cycle_head,
            "to_head_sha": approved_head,
        })
        ambiguous["migration_history"][:0] = [cycle, cycle_back]
        self.assertFalse(
            REDUCER.passport_head_lineage(ambiguous, approved_head)
        )

        disconnected = copy.deepcopy(evidence)
        disconnected[1][adopted]["migration_history"][1][
            "from_head_sha"
        ] = "0" * 40
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "protected merge truth does not match"
        ):
            REDUCER.verify(*disconnected)

        substituted = copy.deepcopy(evidence)
        substituted[1][adopted]["parent_digest"] = "0" * 64
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "protected merge truth does not match"
        ):
            REDUCER.verify(*substituted)

        duplicated_completion = copy.deepcopy(evidence)
        duplicated_completion[2].append({
            **next(
                item for item in duplicated_completion[2]
                if item.get("event") == "ticket_complete"
                and item.get("ticket") == adopted
            ),
            "observed_at_epoch_ns": len(duplicated_completion[2]) + 1,
        })
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "completion proof is missing"
        ):
            REDUCER.verify(*duplicated_completion)

        duplicated_adoption = copy.deepcopy(evidence)
        duplicated_adoption[2].append({
            **next(
                item for item in duplicated_adoption[2]
                if item.get("event") == "terminal_adopted"
            ),
            "observed_at_epoch_ns": len(duplicated_adoption[2]) + 1,
        })
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "adoption proof is invalid"
        ):
            REDUCER.verify(*duplicated_adoption)

        duplicated_publication = copy.deepcopy(evidence)
        publication_ticket = manifest["tickets"][1]
        duplicated_publication[2].extend([{
            "event": "publication_acquired",
            "factory_sha": candidate,
            "observed_at_epoch_ns": len(duplicated_publication[2]) + 1,
            "ticket": publication_ticket,
        }, {
            "event": "publication_released",
            "factory_sha": candidate,
            "observed_at_epoch_ns": len(duplicated_publication[2]) + 2,
            "ticket": publication_ticket,
        }])
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "serialization proof is incomplete"
        ):
            REDUCER.verify(*duplicated_publication)

        replayed_adopted_publication = copy.deepcopy(evidence)
        replayed_adopted_publication[2].extend([{
            "event": "publication_acquired",
            "factory_sha": candidate,
            "observed_at_epoch_ns": len(replayed_adopted_publication[2]) + 1,
            "ticket": adopted,
        }, {
            "event": "publication_released",
            "factory_sha": candidate,
            "observed_at_epoch_ns": len(replayed_adopted_publication[2]) + 2,
            "ticket": adopted,
        }])
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "serialization proof is incomplete"
        ):
            REDUCER.verify(*replayed_adopted_publication)

    def test_fresh_ordered_three_ticket_cohort_needs_only_its_restart_boundary(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"].pop()
        manifest["target_done"] = 3
        manifest["capacity"] = 3
        for values in (passports, terminals, prs, caps):
            del values[removed]
        for event in events:
            if event.get("event") in {"restart_boundary", "controller_recovered"}:
                event["tickets"] = manifest["tickets"]
        events[:] = [item for item in events if item.get("ticket") != removed]
        for number, ticket in enumerate(manifest["tickets"]):
            prs[ticket]["createdAt"] = f"2026-07-27T{10 + 2 * number}:00:00Z"
            prs[ticket]["mergedAt"] = f"2026-07-27T{11 + 2 * number}:00:00Z"

        receipt, narrators = self.add_latency_evidence(evidence)
        self.assertEqual(
            REDUCER.verify(*evidence, receipt, narrators)["status"], "green"
        )

    def test_manifest_generation_scopes_reused_controller_events(self):
        manifest, _passports, current, _terminals, _prs, _caps = self.evidence()
        digest = hashlib.sha256(REDUCER.canonical(manifest).encode()).hexdigest()
        for event in current:
            event.update({
                "qualification_generation": manifest["generation"],
                "qualification_manifest_sha256": digest,
            })
        historical = {
            "event": "protected_terminal_reconciled",
            "factory_sha": manifest["factory_sha"],
            "observed_at_epoch_ns": 0,
            "qualification_generation": manifest["generation"] + 1,
            "qualification_manifest_sha256": "f" * 64,
            "ticket": "T-088",
        }
        events = [historical, *current]
        before = copy.deepcopy(events)
        selected = REDUCER.qualification_events(events, manifest)
        self.assertEqual(selected, current)
        self.assertEqual(events, before)

        outsider = {
            **current[-1],
            "event": "protected_terminal_reconciled",
            "observed_at_epoch_ns": current[-1]["observed_at_epoch_ns"] + 1,
            "ticket": "T-088",
        }
        scoped = REDUCER.qualification_events([*events, outsider], manifest)
        self.assertEqual(
            set(REDUCER.protected_reconciliations(
                scoped, manifest["factory_sha"],
            )) - set(manifest["tickets"]),
            {"T-088"},
        )

        malformed = copy.deepcopy(events)
        malformed[-1].pop("qualification_manifest_sha256")
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "boundary is malformed",
        ):
            REDUCER.qualification_events(malformed, manifest)


if __name__ == "__main__":
    unittest.main()
