#!/usr/bin/env python3
"""Focused Contract 1.8 qualification reduction test."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_reducer", ROOT / "scripts/qualification-reducer.py"
)
assert SPEC and SPEC.loader
REDUCER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDUCER)


class QualificationReducerTest(unittest.TestCase):
    def evidence(self):
        candidate = "a" * 40
        tickets = [f"T-{number}" for number in range(110, 114)]
        manifest = {
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": candidate,
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
                    "manifest_sha256": digest,
                    "run_id": run_id,
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

    def test_three_ticket_successor_accepts_authenticated_history_and_cap(self):
        evidence = list(self.evidence())
        manifest, passports, events, terminals, prs, caps = evidence
        removed = manifest["tickets"][0]
        manifest["tickets"] = manifest["tickets"][1:]
        manifest["target_done"] = 3
        for values in (passports, terminals, prs, caps):
            del values[removed]

        prior = "b" * 40
        for passport in passports.values():
            passport["factory_release_history"].insert(0, {
                "contract_version": "1.8.0",
                "factory_sha": prior,
            })
        for event in events[:3]:
            event["factory_sha"] = prior

        ticket = manifest["tickets"][0]
        for number in range(10):
            passports[ticket]["charge_records"].append({
                "charge_micro_usd": 2_000_000,
                "contract_version": "1.8.0",
                "factory_sha": prior,
                "manifest_sha256": f"{9000 + number:064x}",
                "run_id": f"{ticket}-failed-{number}",
            })
        passports[ticket]["cumulative_charges_micro_usd"] = 26_000_000
        caps[ticket] = 30_000_000
        self.assertEqual(REDUCER.verify(*evidence)["status"], "green")

        caps[ticket] = 25_000_000
        with self.assertRaisesRegex(
            REDUCER.QualificationError, "charges do not match the envelope"
        ):
            REDUCER.verify(*evidence)


if __name__ == "__main__":
    unittest.main()
