#!/usr/bin/env python3
"""Focused Contract 1.8 qualification reduction test."""

from __future__ import annotations

import copy
import hashlib
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

        self.assertEqual(REDUCER.verify(*evidence)["status"], "green")

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

        self.assertEqual(REDUCER.verify(*evidence)["status"], "green")


if __name__ == "__main__":
    unittest.main()
