#!/usr/bin/env python3
"""Regression checks for deterministic qualification driving."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/qualification-run.py"
SCHEMA = "nysa.software-factory.qualification-run/v1"


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


class QualificationRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.scenario = self.root / "scenario.json"
        self.calls = self.root / "calls.json"
        self.manifest = self.root / "QUALIFICATION.json"
        self.controller_state = self.root / "controller"
        self.launcher = self.root / "factory-launch"
        self.manifest.write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "2.0.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }), encoding="utf-8")
        self.launcher.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
scenario = json.loads(pathlib.Path(os.environ["QUALIFICATION_RUN_SCENARIO"]).read_text())
calls_path = pathlib.Path(os.environ["QUALIFICATION_RUN_CALLS"])
calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
action = sys.argv[2]
key = f"models:{sys.argv[3]}" if action == "models" else action
index = sum(item == key for item in calls)
calls.append(key)
calls_path.write_text(json.dumps(calls))
value = scenario[key]
if isinstance(value, list):
    value = value[index]
code = value.pop("_returncode", 0) if isinstance(value, dict) else 0
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
raise SystemExit(code)
""",
            encoding="utf-8",
        )
        self.launcher.chmod(stat.S_IRWXU)
        (self.controller_state / "claims").mkdir(parents=True, mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def doctor(status: str = "ok") -> dict[str, object]:
        required = {
            "active_binding", "clis", "contract_resume", "credentials",
            "fallback_readiness", "isolated_provider", "kit", "kit_pin",
            "transition_receipts",
        }
        return {
            "checks": {
                **{name: {"status": "ok"} for name in required},
                **{
                    name: {"status": "not_applicable"}
                    for name in ("controller", "model_readiness", "provider_cli_pins")
                },
                "runtime": {"status": status},
            },
            "contract_version": "2.0.0",
            "schema": "nysa.software-factory.doctor/v2",
            "schema_version": 2,
            "project": "relay",
            "overall_status": status,
        }

    @classmethod
    def inflight_doctor(cls, *, active_runs: int = 0) -> dict[str, object]:
        value = cls.doctor("warning")
        runtime = value["checks"]["runtime"]
        runtime.update({
            "active_runs": active_runs,
            "active_run_claims": 0,
            "active_run_tickets": [],
            "dispatch_lease_records": 1,
            "dispatch_leases": [{"state": "active", "ticket": "T-1"}],
            "locks": {
                "global_ledger": False, "launch": False,
                "ledger": False, "provider": False,
            },
            "maintenance": False,
            "malformed_dispatch_leases": 0,
            "malformed_active_run_claims": 0,
            "malformed_runs": 0,
            "max_concurrent_tickets": 3,
            "provider_lock_state": "absent",
            "run_records": active_runs,
            "runs": (
                [{"run_id": "T-1-planner-1", "state": "active"}]
                if active_runs else []
            ),
            "stale_dispatch_leases": 0,
            "stale_runs": 0,
        })
        value["checks"]["isolated_provider"].update({
            "active_attempts": 0,
            "active_tokens": 0,
            "legacy_intervals": 0,
            "unknown_workers": 0,
        })
        return value

    @staticmethod
    def controller(
        status: str, *, active: int = 0,
        results: list[object] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": "nysa.software-factory.controller/v1",
            "status": status,
        }
        if status != "busy":
            value.update(active=active, results=results or [])
        return value

    @staticmethod
    def report() -> dict[str, object]:
        value: dict[str, object] = {
            "factory_sha": "a" * 40,
            "protected_main_sha": "b" * 40,
            "qualification_charge_micro_usd": 0,
            "schema": "nysa.software-factory.qualification-report/v1",
            "status": "green",
            "tickets": [],
            "total_charge_micro_usd": 0,
        }
        value["report_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
        return value

    def run_scenario(self, scenario: dict[str, object]) -> tuple[int, dict[str, object]]:
        self.scenario.write_text(json.dumps(scenario), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--launcher", str(self.launcher),
             "--project", "relay", "--json"],
            capture_output=True, check=False, text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "FACTORY_CONTROLLER_STATE_DIR": str(self.controller_state),
                "FACTORY_QUALIFICATION_MANIFEST": str(self.manifest),
                "FACTORY_RELEASE_SHA": "a" * 40,
                "QUALIFICATION_RUN_CALLS": str(self.calls),
                "QUALIFICATION_RUN_SCENARIO": str(self.scenario),
            },
        )
        return result.returncode, json.loads(result.stdout)

    def called(self) -> list[str]:
        return json.loads(self.calls.read_text())

    def test_restart_boundary_then_reduction_is_one_command(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [
                self.controller("restart_required", active=3),
                self.controller(
                    "ok", results=[
                        {"status": "complete", "ticket": ticket}
                        for ticket in ("T-1", "T-2", "T-3")
                    ],
                ),
            ],
            "qualification": self.report(),
        })
        self.assertEqual(code, 0)
        self.assertEqual(value["schema"], SCHEMA)
        self.assertEqual(value["status"], "green")
        self.assertEqual(value["restarts"], 1)
        self.assertEqual(
            self.called(), ["doctor", "reconcile", "reconcile", "qualification"],
        )
        self.assertEqual(
            [item["name"] for item in value["phases"]], self.called(),
        )

    def test_authenticated_wait_is_not_retried_or_reduced(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller(
                "ok", active=1,
                results=[{"status": "waiting", "ticket": "T-1"}],
            )],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["status"], "waiting")
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_empty_controller_result_is_not_reduced(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_successor_route_migration_is_planned_applied_and_reconciled(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update({
            "budget_usd": "300.000000",
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
        })
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        workdirs = {}
        for ticket in ("T-1", "T-2", "T-3"):
            workdir = self.root / f"worktree-{ticket}"
            workdir.mkdir(mode=0o700)
            workdirs[ticket] = workdir
            claim = self.controller_state / "claims" / f"{ticket}.json"
            claim.write_text(json.dumps({
                "blocked_reason": "route-migration-required",
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(workdir),
            }), encoding="utf-8")
            claim.chmod(0o600)
        plan = {
            "factory_sha": "a" * 40,
            "items": [{
                "branch": f"ticket/{ticket}",
                "head": "d" * 40,
                "migration": {},
                "ticket": ticket,
                "workdir": str(workdirs[ticket]),
            } for ticket in ("T-1", "T-2", "T-3")],
            "max_workers": 3,
            "protected_main": "e" * 40,
            "schema": "nysa.software-factory.model-migration-batch-preview/v1",
        }
        plan["approval_sha256"] = hashlib.sha256(
            canonical(plan) + b"\n",
        ).hexdigest()
        journal = {
            "approved_by": "qualification-run",
            "created_at": "2026-08-15T00:00:00Z",
            "plan": plan,
            "results": {ticket: {} for ticket in ("T-1", "T-2", "T-3")},
            "schema": "nysa.software-factory.model-migration-batch-journal/v1",
            "status": "pass",
            "updated_at": "2026-08-15T00:00:01Z",
        }
        journal["record_sha256"] = hashlib.sha256(
            canonical(journal) + b"\n",
        ).hexdigest()
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "models:migrate-batch-plan": plan,
            "models:migrate-batch": journal,
            "reconcile": [
                self.controller("ok"),
                self.controller("ok", results=[
                    {"status": "complete", "ticket": ticket}
                    for ticket in ("T-1", "T-2", "T-3")
                ]),
            ],
            "qualification": self.report(),
        })
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "green")
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "models:migrate-batch-plan",
            "models:migrate-batch", "reconcile", "qualification",
        ])

        self.calls.unlink()
        for ticket in ("T-1", "T-2", "T-3"):
            path = self.controller_state / "claims" / f"{ticket}.json"
            claim = json.loads(path.read_text(encoding="utf-8"))
            claim.update({
                "blocked_reason": "recovery-abandoned:release-upgrade",
                "lease_released": True,
                "recovery_attempt": {
                    "count": 3,
                    "factory_sha": "a" * 40,
                    "input_sha256": "f" * 64,
                    "outcome_sha256": "9" * 64,
                    "phase": "abandoned",
                    "recovery": "release-upgrade",
                    "retry_reason": "route-migration-required",
                    "retry_status": "blocked",
                },
            })
            path.write_text(json.dumps(claim), encoding="utf-8")
            path.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "models:migrate-batch-plan": plan,
            "models:migrate-batch": journal,
            "reconcile": [
                self.controller("ok"),
                self.controller("ok", results=[
                    {"status": "complete", "ticket": ticket}
                    for ticket in ("T-1", "T-2", "T-3")
                ]),
            ],
            "qualification": self.report(),
        })
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "green")
        self.assertIn("models:migrate-batch", self.called())

        self.calls.unlink()
        partial = self.controller_state / "claims/T-3.json"
        claim = json.loads(partial.read_text(encoding="utf-8"))
        claim["status"] = "waiting"
        partial.write_text(json.dumps(claim), encoding="utf-8")
        partial.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_cohort_and_busy_waits_are_typed_and_single_attempt(self) -> None:
        for controller, reason in (
            (self.controller("waiting_for_target"), "cohort_not_accounted"),
            (self.controller("busy"), "controller_busy"),
        ):
            with self.subTest(reason=reason):
                self.calls.unlink(missing_ok=True)
                code, value = self.run_scenario({
                    "doctor": self.doctor(),
                    "reconcile": [controller],
                    "qualification": self.report(),
                })
                self.assertEqual(code, 3)
                self.assertEqual(value["reason"], reason)
                self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_doctor_warning_blocks_before_controller_mutation(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.doctor("warning"),
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(value["reason"], "doctor_not_ready")
        self.assertEqual(self.called(), ["doctor"])

    def test_successor_prior_receipts_reach_only_controller_recovery(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update({
            "budget_usd": "300.000000",
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
        })
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        doctor = self.doctor("warning")
        doctor["checks"]["runtime"]["status"] = "ok"
        doctor["checks"]["transition_receipts"] = {
            "incidents": [{
                "active_factory_sha": "a" * 40,
                "observed_at_epoch_ns": 1,
                "reason_code": "prior_kit_receipt",
                "receipt_factory_sha": "b" * 40,
                "ticket": "T-1",
                "transition_receipt_sha256": "c" * 64,
            }],
            "status": "warning",
        }

        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })

        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "cohort_not_accounted")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        for label, change in {
            "foreign-ticket": ("ticket", "T-9"),
            "wrong-active-factory": ("active_factory_sha", "d" * 40),
            "wrong-reason": ("reason_code", "receipt_identity_invalid"),
            "wrong-receipt": ("transition_receipt_sha256", "bad"),
        }.items():
            with self.subTest(label=label):
                self.calls.unlink(missing_ok=True)
                changed = copy.deepcopy(doctor)
                changed["checks"]["transition_receipts"]["incidents"][0][
                    change[0]
                ] = change[1]
                code, value = self.run_scenario({
                    "doctor": changed,
                    "reconcile": [self.controller("ok")],
                    "qualification": self.report(),
                })
                self.assertEqual(code, 3)
                self.assertEqual(value["reason"], "doctor_not_ready")
                self.assertEqual(self.called(), ["doctor"])

    def test_bounded_inflight_doctor_warning_reaches_controller(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.inflight_doctor(),
            "reconcile": [self.controller("waiting_for_target", active=1)],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "cohort_not_accounted")
        self.assertEqual(value["doctor_status"], "warning")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_unsafe_doctor_warnings_remain_blocked(self) -> None:
        cases = {
            "wrong-project": [("project", "other")],
            "wrong-schema": [("schema", "other")],
            "wrong-schema-version": [("schema_version", 1)],
            "wrong-contract": [("contract_version", "1.8.0")],
            "missing-check": [("checks.controller", None)],
            "unrelated-warning": [("checks.credentials.status", "warning")],
            "neutral-check-active": [("checks.controller.status", "ok")],
            "runtime-status": [("checks.runtime.status", "ok")],
            "overall-status": [("overall_status", "ok")],
            "maintenance": [("checks.runtime.maintenance", True)],
            "lock": [("checks.runtime.locks.launch", True)],
            "lock-shape": [("checks.runtime.locks.provider", None)],
            "provider-active": [("checks.runtime.provider_lock_state", "active")],
            "stale-run": [("checks.runtime.stale_runs", 1)],
            "malformed-run": [("checks.runtime.malformed_runs", 1)],
            "active-run-claim-count": [("checks.runtime.active_run_claims", 1)],
            "active-run-ticket-projection": [
                ("checks.runtime.active_run_tickets", ["T-1"]),
            ],
            "malformed-run-claim": [
                ("checks.runtime.malformed_active_run_claims", 1),
            ],
            "stale-lease": [("checks.runtime.stale_dispatch_leases", 1)],
            "malformed-lease": [("checks.runtime.malformed_dispatch_leases", 1)],
            "malformed-lease-item": [("checks.runtime.dispatch_leases", ["bad"])],
            "boolean-counter": [("checks.runtime.run_records", True)],
            "zero-leases": [
                ("checks.runtime.dispatch_lease_records", 0),
                ("checks.runtime.dispatch_leases", []),
            ],
            "over-capacity": [("checks.runtime.max_concurrent_tickets", 0)],
            "capacity-drift": [("checks.runtime.max_concurrent_tickets", 4)],
            "orphan-run": [
                ("checks.runtime.active_runs", 1),
                ("checks.runtime.run_records", 1),
                ("checks.runtime.runs", [
                    {"run_id": "one", "state": "active"},
                ]),
            ],
            "foreign-lease": [("checks.runtime.dispatch_leases.0.ticket", "T-9")],
            "inactive-lease": [("checks.runtime.dispatch_leases.0.state", "stale")],
            "invalid-ticket": [("checks.runtime.dispatch_leases.0.ticket", "bad")],
            "duplicate-lease": [
                ("checks.runtime.dispatch_lease_records", 2),
                ("checks.runtime.dispatch_leases", [
                    {"state": "active", "ticket": "T-1"},
                    {"state": "active", "ticket": "T-1"},
                ]),
            ],
            "provider-attempt": [("checks.isolated_provider.active_attempts", 1)],
            "provider-token": [("checks.isolated_provider.active_tokens", 1)],
            "provider-worker": [("checks.isolated_provider.unknown_workers", 1)],
            "provider-legacy": [("checks.isolated_provider.legacy_intervals", 1)],
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.calls.unlink(missing_ok=True)
                doctor = copy.deepcopy(self.inflight_doctor())
                for dotted, replacement in changes:
                    parent = doctor
                    parts = dotted.split(".")
                    for part in parts[:-1]:
                        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                    if replacement is None:
                        parent.pop(parts[-1])
                    elif isinstance(parent, list):
                        parent[int(parts[-1])] = replacement
                    else:
                        parent[parts[-1]] = replacement
                code, value = self.run_scenario({
                    "doctor": doctor,
                    "reconcile": [self.controller("ok")],
                    "qualification": self.report(),
                })
                self.assertEqual(code, 3)
                self.assertEqual(value["reason"], "doctor_not_ready")
                self.assertEqual(self.called(), ["doctor"])

    def test_manifest_and_doctor_process_fail_closed(self) -> None:
        valid_manifest = self.manifest.read_bytes()
        self.manifest.write_text("{}\n", encoding="utf-8")
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "error")
        self.assertFalse(self.calls.exists())

        self.manifest.write_bytes(valid_manifest)
        doctor = self.inflight_doctor()
        doctor["_returncode"] = 1
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "doctor_not_ready")
        self.assertEqual(self.called(), ["doctor"])

    def test_ticket_block_is_preserved_without_reduction(self) -> None:
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller(
                "ok", results=[{"status": "budget", "ticket": "T-1"}],
            )],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(value["reason"], "ticket_blocked")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_controller_error_is_typed_and_preserved(self) -> None:
        controller = self.controller(
            "error", results=[{
                "error": "typed fixture failure", "status": "error", "ticket": "T-1",
            }],
        )
        controller["_returncode"] = 1
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [controller],
            "qualification": self.report(),
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["reason"], "controller_error")
        self.assertEqual(value["controller"]["results"][0]["ticket"], "T-1")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_malformed_result_and_repeated_restart_fail_closed(self) -> None:
        scenarios = (
            {
                "doctor": self.doctor(),
                "reconcile": ["not-an-object"],
                "qualification": self.report(),
            },
            {
                "doctor": self.doctor(),
                "reconcile": [
                    self.controller("restart_required", active=3),
                    self.controller("restart_required", active=3),
                ],
                "qualification": self.report(),
            },
            {
                "doctor": self.doctor(),
                "reconcile": [self.controller("ok", results=["bad"])],
                "qualification": self.report(),
            },
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["reconcile"]):
                self.calls.unlink(missing_ok=True)
                code, value = self.run_scenario(scenario)
                self.assertEqual(code, 2)
                self.assertEqual(value["status"], "error")

    def test_changed_reducer_digest_fails_closed(self) -> None:
        report = self.report()
        report["report_sha256"] = "0" * 64
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok", results=[
                {"status": "complete", "ticket": ticket}
                for ticket in ("T-1", "T-2", "T-3")
            ])],
            "qualification": report,
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "error")
        self.assertEqual(self.called(), ["doctor", "reconcile", "qualification"])


if __name__ == "__main__":
    unittest.main()
