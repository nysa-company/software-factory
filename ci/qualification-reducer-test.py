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
            {"event": "restart_boundary", "tickets": tickets},
            {"event": "controller_recovered", "tickets": tickets},
            {"event": "cell_relocated", "ticket": tickets[0]},
        ]
        for ticket in tickets:
            events.extend([
                {"event": "publication_acquired", "ticket": ticket},
                {"event": "publication_released", "ticket": ticket},
                {"event": "ticket_complete", "ticket": ticket},
            ])
        return manifest, passports, events, terminals, prs

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


if __name__ == "__main__":
    unittest.main()
