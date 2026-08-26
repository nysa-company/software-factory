#!/usr/bin/env python3
"""Regression checks for deterministic qualification driving."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import operator_receipt  # noqa: E402

RUNNER = ROOT / "scripts/qualification-run.py"
SCHEMA = "nysa.software-factory.qualification-run/v1"
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "qualification_run", RUNNER,
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER_MODULE = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER_MODULE)


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
        self.operator_map = self.root / "operator/operator-map.json"
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
import hashlib, json, os, pathlib, sys
scenario = json.loads(pathlib.Path(os.environ["QUALIFICATION_RUN_SCENARIO"]).read_text())
calls_path = pathlib.Path(os.environ["QUALIFICATION_RUN_CALLS"])
calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
action = sys.argv[2]
key = (
    f"models:{sys.argv[3]}" if action == "models"
    else f"state-machine:{sys.argv[3]}" if action == "state-machine"
    else action
)
index = sum(item == key for item in calls)
calls.append(key)
calls_path.write_text(json.dumps(calls))
value = scenario[key]
if isinstance(value, list):
    value = value[index]
code = value.pop("_returncode", 0) if isinstance(value, dict) else 0
event = value.pop("_event", None) if isinstance(value, dict) else None
manifest = value.pop("_manifest", None) if isinstance(value, dict) else None
if isinstance(manifest, dict):
    pathlib.Path(os.environ["FACTORY_QUALIFICATION_MANIFEST"]).write_text(
        json.dumps(manifest)
    )
if isinstance(event, dict):
    supplied_digest = event.pop("event_sha256", None)
    event["event_sha256"] = supplied_digest or hashlib.sha256(json.dumps(
        event, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    events = pathlib.Path(os.environ["FACTORY_CONTROLLER_STATE_DIR"]) / "events"
    events.mkdir(mode=0o700, exist_ok=True)
    path = events / f"{len(calls):020d}.json"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
raise SystemExit(code)
""",
            encoding="utf-8",
        )
        self.launcher.chmod(stat.S_IRWXU)
        (self.controller_state / "claims").mkdir(parents=True, mode=0o700)
        self.controller_state.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def doctor(status: str = "ok") -> dict[str, object]:
        required = {
            "active_binding", "authenticated_artifacts", "clis", "contract_resume", "credentials",
            "fallback_readiness", "isolated_provider", "kit", "kit_pin",
            "provider_cli_pins", "qualification_identity", "qualification_ticket_readiness",
            "transition_receipts",
        }
        value = {
            "checks": {
                **{name: {"status": "ok"} for name in required},
                **{
                    name: {"status": "not_applicable"}
                    for name in ("controller", "model_readiness")
                },
                "qualification_ticket_readiness": {
                    "reason_code": None,
                    "status": "ok",
                    "tickets": [
                        {"reason_code": None, "status": "ok", "ticket": ticket}
                        for ticket in ("T-1", "T-2", "T-3")
                    ],
                },
                "runtime": {
                    "active_run_claims": 0,
                    "active_run_tickets": [],
                    "active_runs": 0,
                    "dispatch_lease_records": 0,
                    "dispatch_leases": [],
                    "locks": {
                        "global_ledger": False, "launch": False,
                        "ledger": False, "provider": False,
                    },
                    "maintenance": False,
                    "malformed_active_run_claims": 0,
                    "malformed_dispatch_leases": 0,
                    "malformed_runs": 0,
                    "provider_lock_state": "absent",
                    "run_records": 0,
                    "runs": [],
                    "stale_dispatch_leases": 0,
                    "stale_runs": 0,
                    "status": status,
                },
            },
            "contract_version": "2.0.0",
            "schema": "nysa.software-factory.doctor/v2",
            "schema_version": 2,
            "project": "relay",
            "overall_status": status,
        }
        value["checks"]["isolated_provider"].update({
            "active_attempts": 0,
            "active_tokens": 0,
            "legacy_intervals": 0,
            "unknown_workers": 0,
        })
        return value

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

    @classmethod
    def live_doctor(cls, count: int = 1) -> dict[str, object]:
        value = cls.inflight_doctor()
        tickets = [f"T-{index}" for index in range(1, count + 1)]
        runtime = value["checks"]["runtime"]
        runtime.update({
            "active_run_claims": count,
            "active_run_tickets": tickets,
            "active_runs": count,
            "dispatch_lease_records": count,
            "dispatch_leases": [
                {"state": "active", "ticket": ticket} for ticket in tickets
            ],
            "run_records": count,
            "runs": [
                {
                    "recovery_command": None,
                    "recovery_reason": None,
                    "run_id": f"live-{index}",
                    "state": "active",
                    "ticket": None,
                }
                for index in range(1, count + 1)
            ],
        })
        value["checks"]["isolated_provider"]["active_attempts"] = count
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

    def run_scenario(
        self, scenario: dict[str, object],
        resume: tuple[str, str] | None = None,
        qualification_mode: str = "isolated",
        finish: bool = False,
        operator_map: Path | None = None,
        release_path: Path | None = None,
    ) -> tuple[int, dict[str, object]]:
        self.scenario.write_text(json.dumps(scenario), encoding="utf-8")
        command = [
            sys.executable, str(RUNNER), "--launcher", str(self.launcher),
            "--project", "relay",
        ]
        if resume:
            command.extend((
                "--resume-ticket", resume[0], "--resume-receipt", resume[1],
            ))
        if finish:
            command.append("--finish")
        command.append("--json")
        result = subprocess.run(
            command,
            capture_output=True, check=False, text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "FACTORY_CONTROLLER_STATE_DIR": str(self.controller_state),
                "FACTORY_OPERATOR_MAP": str(operator_map or self.operator_map),
                "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
                "FACTORY_QUALIFICATION_MODE": qualification_mode,
                "FACTORY_TEST_FINISH_POLL_SECONDS": "0",
                "FACTORY_TEST_MODE": "1",
                "FACTORY_TRUSTED_TEST_HARNESS": "1",
                "FACTORY_QUALIFICATION_MANIFEST": str(self.manifest),
                "FACTORY_QUALIFICATION_PRODUCT_SHA": getattr(
                    self, "product_sha", "",
                ),
                "FACTORY_RELEASE_SHA": "a" * 40,
                **(
                    {"FACTORY_RELEASE_PATH": str(release_path)}
                    if release_path else {}
                ),
                "QUALIFICATION_RUN_CALLS": str(self.calls),
                "QUALIFICATION_RUN_SCENARIO": str(self.scenario),
            },
        )
        return result.returncode, json.loads(result.stdout)

    def activation_chain(self, predecessor: str) -> Path:
        root = self.root / "activation"
        release = root / "releases" / ("a" * 40)
        for path in (
            root, root / "releases", release, root / "projects",
            root / "projects/relay", root / "receipts",
        ):
            path.mkdir(mode=0o700, exist_ok=True)

        def receipt(kit_sha: str, previous: str | None) -> str:
            value = {
                "contract_version": "2.0.0",
                "kit_sha": kit_sha,
                "kit_tree": "1" * 40,
                "product_path": str(self.manifest.parent.parent),
                "project": "relay",
                "provider_policy_sha256": "2" * 64,
                "qualification_mode": "isolated",
                "status": "pass",
            }
            if previous:
                value["previous_receipt_id"] = previous
            receipt_id = hashlib.sha256(canonical(value) + b"\n").hexdigest()
            value["receipt_id"] = receipt_id
            path = root / "receipts" / f"{receipt_id}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
            return receipt_id

        prior = receipt(predecessor, None)
        current = receipt("a" * 40, prior)
        active = root / "projects/relay/active.json"
        active.write_text(json.dumps({
            "kit_sha": "a" * 40,
            "project": "relay",
            "receipt_id": current,
            "release_path": str(release),
        }), encoding="utf-8")
        active.chmod(0o600)
        return release

    def approval_fixture(
        self, *, dirty: bool = False, foreign: bool = False,
        state: str = "Awaiting Approval", ticket: str = "T-1",
    ) -> Path:
        parked = self.controller_state / "parked"
        parked.mkdir(mode=0o700, exist_ok=True)
        worktree = self.root / f"foreign-{ticket}" if foreign else parked / ticket
        (worktree / "factory/tickets").mkdir(parents=True)
        (worktree / f"factory/attestations/{ticket}").mkdir(parents=True)
        (worktree / f"factory/tickets/{ticket}.md").write_text(
            f"# {ticket}\n\nState: {state}\n", encoding="utf-8",
        )
        (worktree / f"factory/tickets/{ticket}-bundle.md").write_text(
            f"# {ticket} bundle\n", encoding="utf-8",
        )
        (worktree / f"factory/attestations/{ticket}/bundle.json").write_text(
            json.dumps({"schema": "fixture", "ticket": ticket}) + "\n",
            encoding="utf-8",
        )
        for command in (
            ("init", "-q"),
            ("config", "user.name", "Qualification Test"),
            ("config", "user.email", "qualification@test.invalid"),
            ("checkout", "-qb", f"ticket/{ticket}"),
            ("add", "factory"),
            ("commit", "-qm", "seed approval checkpoint"),
        ):
            subprocess.run(
                ["git", "-C", str(worktree), *command], check=True,
                capture_output=True, text=True,
            )
        claim = self.controller_state / "claims" / f"{ticket}.json"
        claim.write_text(json.dumps({
            "branch": f"ticket/{ticket}",
            "lease": "",
            "parked": True,
            "receipt": "",
            "role": "",
            "status": "waiting",
            "ticket": ticket,
            "worktree": str(worktree),
        }), encoding="utf-8")
        claim.chmod(0o600)
        self.operator_authority(ticket)
        if dirty:
            (worktree / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        return worktree

    def operator_authority(self, *tickets: str) -> None:
        self.operator_map.parent.mkdir(mode=0o700, exist_ok=True)
        mapping = (
            json.loads(self.operator_map.read_text(encoding="utf-8"))
            if self.operator_map.exists()
            else {"_config": None, "_sync": {}, "initiatives": {}, "tickets": {}}
        )
        for ticket in tickets:
            mapping["tickets"][ticket] = {"operator_fields_initialized": True}
        self.operator_map.write_text(json.dumps(mapping), encoding="utf-8")
        self.operator_map.chmod(0o600)

    def called(self) -> list[str]:
        return json.loads(self.calls.read_text())

    def contract_recovery_fixture(
        self, state: str = "Blocked-Escalated",
    ) -> tuple[dict[str, object], Path, str, str]:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update({
            "budget_usd": "300.000000",
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
        })
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        worktree = self.root / "contract-recovery"
        (worktree / "factory/tickets").mkdir(parents=True)
        (worktree / "factory/tickets/T-1.md").write_text(
            f"# T-1\n\nState: {state}\nResume-State: Building\n",
            encoding="utf-8",
        )
        for command in (
            ("init", "-q"),
            ("config", "user.name", "Qualification Test"),
            ("config", "user.email", "qualification@test.invalid"),
            ("checkout", "-qb", "ticket/T-1"),
            ("add", "factory/tickets/T-1.md"),
            ("commit", "-qm", "seed contract recovery"),
        ):
            subprocess.run(
                ["git", "-C", str(worktree), *command], check=True,
                capture_output=True, text=True,
            )
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        receipt = "c" * 64
        claim = self.controller_state / "claims/T-1.json"
        claim.write_text(json.dumps({
            "branch": "ticket/T-1",
            "lease": "",
            "parked": True,
            "receipt": receipt,
            "role": "test-author",
            "status": "blocked",
            "ticket": "T-1",
            "worktree": str(worktree),
        }), encoding="utf-8")
        claim.chmod(0o600)
        transition = self.controller_state / "T-1.json"
        transition.write_text(json.dumps({
            "branch": "ticket/T-1",
            "consumed": True,
            "project": "relay",
            "receipt_sha256": receipt,
            "role": "test-author",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "ticket": "T-1",
        }), encoding="utf-8")
        transition.chmod(0o600)
        doctor = self.doctor("warning")
        doctor["checks"]["runtime"]["status"] = "ok"
        doctor["checks"]["transition_receipts"] = {
            "incidents": [{
                "active_factory_sha": "a" * 40,
                "observed_at_epoch_ns": 1,
                "reason_code": "prior_kit_receipt",
                "receipt_factory_sha": "b" * 40,
                "ticket": "T-1",
                "transition_receipt_sha256": receipt,
            }],
            "status": "warning",
        }
        doctor["checks"]["contract_resume"] = {
            "incidents": [{
                "actual_bytes": 10,
                "blocked_receipt_sha256": receipt,
                "changed_path_count": 1,
                "expected_bytes": 11,
                "first_differing_line": 2,
                "observed_at_epoch_ns": 2,
                "reason_code": "resume_commit_content_mismatch",
                "ticket": "T-1",
            }],
            "status": "warning",
        }
        return doctor, worktree, head, receipt

    def test_restart_boundary_then_reduction_is_one_command(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update({
            "budget_usd": "300.000000",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
        })
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
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
        claim["recovery_attempt"]["count"] = 1
        partial.write_text(json.dumps(claim), encoding="utf-8")
        partial.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        claim["recovery_attempt"]["count"] = 3
        claim["publication_lease"] = "8" * 64
        partial.write_text(json.dumps(claim), encoding="utf-8")
        partial.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        claim.pop("publication_lease")
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

        self.calls.unlink()
        partial.unlink()
        marker = (
            self.controller_state
            / f"qualification-terminal-adoption-{'a' * 40}-T-3.json"
        )
        marker.write_text(json.dumps({
            "approved_pr_head": "1" * 40,
            "candidate_passport_sha256": "2" * 64,
            "done_sha256": "3" * 64,
            "factory_sha": "a" * 40,
            "merge_commit": "4" * 40,
            "passport_source_factory_sha": "b" * 40,
            "pr_number": 3,
            "schema": (
                "nysa.software-factory.qualification-terminal-adoption/v2"
            ),
            "source_current_state": "Approved",
            "source_factory_sha": "b" * 40,
            "source_passport_sha256": "5" * 64,
            "source_publication_state": "merged",
            "ticket": "T-3",
        }), encoding="utf-8")
        marker.chmod(0o600)
        terminal_plan = copy.deepcopy(plan)
        terminal_plan["items"] = [
            item for item in terminal_plan["items"] if item["ticket"] != "T-3"
        ]
        terminal_plan["max_workers"] = 2
        terminal_plan["approval_sha256"] = hashlib.sha256(
            canonical({
                key: item for key, item in terminal_plan.items()
                if key != "approval_sha256"
            }) + b"\n",
        ).hexdigest()
        terminal_journal = copy.deepcopy(journal)
        terminal_journal["plan"] = terminal_plan
        terminal_journal["results"].pop("T-3")
        terminal_journal["record_sha256"] = hashlib.sha256(
            canonical({
                key: item for key, item in terminal_journal.items()
                if key != "record_sha256"
            }) + b"\n",
        ).hexdigest()
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "models:migrate-batch-plan": terminal_plan,
            "models:migrate-batch": terminal_journal,
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
        completed = (
            self.controller_state
            / f"passport-route-migration-complete-T-2-{'a' * 40}.json"
        )
        completed_value = {
            "factory_sha": "a" * 40,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-2",
        }
        completed.write_text(json.dumps(completed_value), encoding="utf-8")
        completed.chmod(0o600)
        completed_claim = self.controller_state / "claims/T-2.json"
        claim = json.loads(completed_claim.read_text(encoding="utf-8"))
        claim.update({"blocked_reason": None, "status": "claimed"})
        claim.pop("recovery_attempt", None)
        claim.pop("lease_released", None)
        completed_claim.write_text(json.dumps(claim), encoding="utf-8")
        completed_claim.chmod(0o600)
        mixed_plan = copy.deepcopy(terminal_plan)
        mixed_plan["items"] = [
            item for item in mixed_plan["items"] if item["ticket"] == "T-1"
        ]
        mixed_plan["max_workers"] = 1
        mixed_plan["approval_sha256"] = hashlib.sha256(
            canonical({
                key: item for key, item in mixed_plan.items()
                if key != "approval_sha256"
            }) + b"\n",
        ).hexdigest()
        mixed_journal = copy.deepcopy(terminal_journal)
        mixed_journal["plan"] = mixed_plan
        mixed_journal["results"] = {"T-1": {}}
        mixed_journal["record_sha256"] = hashlib.sha256(
            canonical({
                key: item for key, item in mixed_journal.items()
                if key != "record_sha256"
            }) + b"\n",
        ).hexdigest()
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "models:migrate-batch-plan": mixed_plan,
            "models:migrate-batch": mixed_journal,
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
        completed_value["factory_sha"] = "b" * 40
        completed.write_text(json.dumps(completed_value), encoding="utf-8")
        completed.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 2)
        self.assertEqual(
            value["error"],
            "qualification route migration completion is invalid",
        )
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        completed.unlink()
        completed_target = self.root / "completion-marker.json"
        completed_value["factory_sha"] = "a" * 40
        completed_target.write_text(
            json.dumps(completed_value), encoding="utf-8",
        )
        completed_target.chmod(0o600)
        completed.symlink_to(completed_target)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["error"], "qualification claim state is invalid")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        completed.unlink()
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "authenticated_wait")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        marker.unlink()
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [self.controller("ok")],
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["error"], "qualification claim state is invalid")
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

    def test_doctor_error_returns_exact_report_before_controller_mutation(self) -> None:
        doctor = self.doctor("error")
        doctor["checks"]["fallback_readiness"]["status"] = "error"
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(value["reason"], "doctor_not_ready")
        self.assertEqual(value["doctor"], doctor)
        self.assertEqual(self.called(), ["doctor"])

    def test_provider_pin_not_ready_blocks_before_controller_mutation(self) -> None:
        for status in ("not_applicable", "warning", "error"):
            with self.subTest(status=status):
                self.calls.unlink(missing_ok=True)
                doctor = self.doctor()
                doctor["checks"]["provider_cli_pins"]["status"] = status
                code, value = self.run_scenario({
                    "doctor": doctor,
                    "reconcile": [self.controller("ok")],
                    "qualification": self.report(),
                })
                self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
                self.assertEqual(self.called(), ["doctor"])

    def test_ticket_readiness_blocks_before_controller_mutation(self) -> None:
        doctor = self.doctor("error")
        doctor["checks"]["qualification_ticket_readiness"] = {
            "reason_code": "ticket_state_conflict",
            "status": "error",
            "tickets": [
                {"reason_code": None, "status": "ok", "ticket": "T-1"},
                {
                    "reason_code": "ticket_state_conflict",
                    "status": "error",
                    "ticket": "T-2",
                },
                {"reason_code": None, "status": "ok", "ticket": "T-3"},
            ],
        }
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("ok")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(value["doctor"], doctor)
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

        self.calls.unlink()
        source = copy.deepcopy(doctor)
        source["checks"]["transition_receipts"]["incidents"][0].update({
            "active_factory_sha": "b" * 40,
            "receipt_factory_sha": "c" * 40,
        })
        code, value = self.run_scenario({
            "doctor": source,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "cohort_not_accounted")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        self.calls.unlink()
        stale = copy.deepcopy(doctor)
        stale["checks"]["transition_receipts"]["incidents"] = [
            {
                "active_factory_sha": "d" * 40,
                "observed_at_epoch_ns": index,
                "reason_code": "prior_kit_receipt",
                "receipt_factory_sha": "b" * 40,
                "ticket": ticket,
                "transition_receipt_sha256": f"{index}" * 64,
            }
            for index, ticket in enumerate(("T-1", "T-2", "T-3"), 1)
        ]
        code, value = self.run_scenario({
            "doctor": stale,
            "reconcile": [
                self.controller("restart_required", active=3),
                self.controller("waiting_for_target", active=3),
            ],
            "qualification": self.report(),
        })
        self.assertEqual(code, 3)
        self.assertEqual(value["reason"], "cohort_not_accounted")
        self.assertEqual(self.called(), ["doctor", "reconcile", "reconcile"])

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

    def test_chained_successor_admits_only_its_authenticated_predecessor(self) -> None:
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
                "active_factory_sha": "d" * 40,
                "observed_at_epoch_ns": 1,
                "reason_code": "prior_kit_receipt",
                "receipt_factory_sha": "b" * 40,
                "ticket": "T-1",
                "transition_receipt_sha256": "c" * 64,
            }],
            "status": "warning",
        }
        release = self.activation_chain("d" * 40)

        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        }, release_path=release)
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])

        unrelated = copy.deepcopy(doctor)
        unrelated["checks"]["transition_receipts"]["incidents"][0][
            "active_factory_sha"
        ] = "e" * 40
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": unrelated,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        }, release_path=release)
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])

        active = release.parent.parent / "projects/relay/active.json"
        active_value = json.loads(active.read_text(encoding="utf-8"))
        active_value["release_path"] = "/tmp/foreign"
        active.write_text(json.dumps(active_value), encoding="utf-8")
        active.chmod(0o600)
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        }, release_path=release)
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])

        release = self.activation_chain("d" * 40)
        active_value = json.loads(active.read_text(encoding="utf-8"))
        current = release.parent.parent / "receipts" / f"{active_value['receipt_id']}.json"
        current_value = json.loads(current.read_text(encoding="utf-8"))
        predecessor = release.parent.parent / "receipts" / (
            f"{current_value['previous_receipt_id']}.json"
        )
        predecessor_value = json.loads(predecessor.read_text(encoding="utf-8"))
        predecessor_value["kit_sha"] = "e" * 40
        predecessor.write_text(json.dumps(predecessor_value), encoding="utf-8")
        predecessor.chmod(0o600)
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        }, release_path=release)
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])

    def test_successor_prior_receipt_allows_its_exact_contract_recovery(self) -> None:
        doctor, worktree, head, receipt = self.contract_recovery_fixture()
        checked = {
            "action": "repair-check",
            "current_state": "Blocked-Escalated",
            "head": head,
            "repair_role": "planner",
            "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready",
            "ticket": "T-1",
        }
        stale = operator_receipt.issue(
            self.controller_state, "T-1", "resume", {
                "blocked_receipt_sha256": "d" * 64,
                "resume_stage": "Building",
            },
        )

        code, value = self.run_scenario({
            "doctor": doctor,
            "state-machine:repair-check": checked,
            "reconcile": [
                self.controller("waiting_for_target"),
                self.controller("waiting_for_target"),
            ],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(
            self.called(), [
                "doctor", "reconcile", "doctor",
                "state-machine:repair-check", "reconcile",
            ],
        )
        mapping = json.loads(self.operator_map.read_text(encoding="utf-8"))
        operator = mapping["tickets"]["T-1"]["operator"]
        self.assertEqual(
            (operator["state"], operator["state_base"]),
            ("Building", "blocked-escalated"),
        )
        receipt_path = next(
            path for path in (
                self.controller_state / "operator-receipts/T-1"
            ).glob("resume-*.json")
            if json.loads(path.read_text(encoding="utf-8"))[
                "receipt_sha256"
            ] == operator["receipt_sha256"]
        )
        projected = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertFalse(projected["consumed"])
        self.assertEqual(projected["payload"], {
            "blocked_receipt_sha256": receipt,
            "resume_stage": "Building",
        })
        self.assertNotEqual(projected["receipt_sha256"], stale["receipt_sha256"])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1"],
                check=True, capture_output=True, text=True,
            ).stdout,
            "",
        )
        self.assertFalse((worktree / "factory/receipts").exists())

        for label, key, changed in (
            ("wrong-receipt", "blocked_receipt_sha256", "d" * 64),
            ("wrong-reason", "reason_code", "resume_receipt_mismatch"),
            ("multiple-paths", "changed_path_count", 2),
            ("wide-byte-delta", "expected_bytes", 12),
            ("extra-field", "local_head", "e" * 40),
        ):
            with self.subTest(label=label):
                self.calls.unlink(missing_ok=True)
                changed_doctor = copy.deepcopy(doctor)
                changed_doctor["checks"]["contract_resume"]["incidents"][0][
                    key
                ] = changed
                code, value = self.run_scenario({
                    "doctor": changed_doctor,
                    "reconcile": [self.controller("waiting_for_target")],
                    "qualification": self.report(),
                })
                self.assertEqual(
                    (code, value["reason"]), (3, "doctor_not_ready")
                )
                self.assertEqual(self.called(), ["doctor"])

    def test_historical_resume_warning_reaches_reconcile_without_new_authority(
        self,
    ) -> None:
        doctor, _worktree, _head, receipt = self.contract_recovery_fixture()
        incident = {
            "blocked_receipt_sha256": receipt,
            "observed_at_epoch_ns": 0,
            "reason_code": "resume_receipt_mismatch",
            "ticket": "T-1",
        }
        doctor["checks"]["contract_resume"] = {
            "incidents": [incident], "status": "warning",
        }
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse(self.operator_map.exists())

        claim_path = self.controller_state / "claims/T-1.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["receipt"] = "d" * 64
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])
        claim["receipt"] = receipt
        claim_path.write_text(json.dumps(claim), encoding="utf-8")

        mismatched_transition = copy.deepcopy(doctor)
        mismatched_transition["checks"]["transition_receipts"]["incidents"][0][
            "transition_receipt_sha256"
        ] = "d" * 64
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": mismatched_transition,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])

        resolved = copy.deepcopy(doctor)
        resolved["checks"]["transition_receipts"] = {
            "incidents": [], "status": "ok",
        }
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": resolved,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse(self.operator_map.exists())

        claim["receipt"] = "d" * 64
        claim_path.write_text(json.dumps(claim), encoding="utf-8")
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": resolved,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])
        claim["receipt"] = receipt
        claim_path.write_text(json.dumps(claim), encoding="utf-8")

        transition_path = self.controller_state / "T-1.json"
        transition = json.loads(transition_path.read_text(encoding="utf-8"))
        transition["receipt_sha256"] = "d" * 64
        transition_path.write_text(json.dumps(transition), encoding="utf-8")
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": resolved,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])
        transition["receipt_sha256"] = receipt
        transition_path.write_text(json.dumps(transition), encoding="utf-8")

        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": resolved,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        }, qualification_mode="takeover")
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])
        self.assertFalse(self.operator_map.exists())

        for label, key, changed in (
            ("wrong-receipt", "blocked_receipt_sha256", "d" * 64),
            ("wrong-reason", "reason_code", "resume_commit_not_pushed"),
            ("invalid-time", "observed_at_epoch_ns", -1),
            ("foreign-ticket", "ticket", "T-9"),
            ("extra-field", "local_head", "e" * 40),
        ):
            with self.subTest(label=label):
                self.calls.unlink(missing_ok=True)
                changed_doctor = copy.deepcopy(doctor)
                changed_doctor["checks"]["contract_resume"]["incidents"][0][
                    key
                ] = changed
                code, value = self.run_scenario({
                    "doctor": changed_doctor,
                    "reconcile": [self.controller("waiting_for_target")],
                    "qualification": self.report(),
                })
                self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
                self.assertEqual(self.called(), ["doctor"])

    def test_explicit_qualification_resume_projects_exact_repair(self) -> None:
        doctor, worktree, head, receipt = self.contract_recovery_fixture()
        doctor["checks"]["contract_resume"] = {
            "incidents": [{
                "blocked_receipt_sha256": receipt,
                "observed_at_epoch_ns": 0,
                "reason_code": "resume_receipt_mismatch",
                "ticket": "T-1",
            }],
            "status": "warning",
        }
        checked = {
            "action": "repair-check", "current_state": "Blocked-Escalated",
            "head": head, "repair_role": "planner", "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready", "ticket": "T-1",
        }

        code, value = self.run_scenario({
            "doctor": doctor, "state-machine:repair-check": checked,
        }, resume=("T-1", receipt))
        self.assertEqual((code, value["status"], value["reason"]), (
            0, "projected", "operator_resume_projected",
        ))
        self.assertEqual(self.called(), ["doctor", "state-machine:repair-check"])
        mapping = json.loads(self.operator_map.read_text(encoding="utf-8"))
        projected = operator_receipt.read_exact(
            self.controller_state, "T-1", "resume",
            mapping["tickets"]["T-1"]["operator"]["receipt_sha256"],
            {"blocked_receipt_sha256": receipt, "resume_stage": "Building"},
        )
        self.assertIsNotNone(projected)
        self.assertFalse(projected["consumed"])
        self.assertFalse((worktree / "factory/receipts").exists())
        self.assertEqual(subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1"],
            check=True, capture_output=True, text=True,
        ).stdout, "")

        claim_path = self.controller_state / "claims/T-1.json"
        baseline_claim = json.loads(claim_path.read_text(encoding="utf-8"))
        for label, changed in (
            ("active-lease", {"lease": "1" * 64}),
            ("not-parked", {"parked": False}),
        ):
            with self.subTest(label=label):
                invalid_claim = {**baseline_claim, **changed}
                claim_path.write_text(json.dumps(invalid_claim), encoding="utf-8")
                self.calls.unlink(missing_ok=True)
                code, refused = self.run_scenario({
                    "doctor": doctor, "state-machine:repair-check": checked,
                }, resume=("T-1", receipt))
                self.assertEqual(
                    (code, refused["status"], refused["reason"]),
                    (3, "blocked", "doctor_not_ready"),
                )
                self.assertEqual(self.called(), ["doctor"])
        claim_path.write_text(json.dumps(baseline_claim), encoding="utf-8")

        for source, target in (
            (self.controller_state / "claims/T-1.json",
             self.controller_state / "claims/T-2.json"),
            (self.controller_state / "T-1.json",
             self.controller_state / "T-2.json"),
        ):
            value = json.loads(source.read_text(encoding="utf-8"))
            value.update(ticket="T-2", branch="ticket/T-2")
            target.write_text(json.dumps(value), encoding="utf-8")
            target.chmod(0o600)
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor, "state-machine:repair-check": checked,
        }, resume=("T-2", receipt))
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor"])

        doctor["checks"]["contract_resume"] = {
            "incidents": [], "status": "ok",
        }
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor, "state-machine:repair-check": checked,
        }, resume=("T-1", receipt))
        self.assertEqual((code, value["status"]), (0, "projected"))
        self.assertEqual(self.called(), ["doctor", "state-machine:repair-check"])

        self.calls.unlink()
        code, value = self.run_scenario(
            {"doctor": doctor}, resume=("T-1", "d" * 64),
        )
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor"])

    def test_explicit_qualification_resume_surfaces_operator_refusal(self) -> None:
        doctor, _worktree, head, receipt = self.contract_recovery_fixture()
        doctor["checks"]["contract_resume"] = {
            "incidents": [{
                "blocked_receipt_sha256": receipt,
                "observed_at_epoch_ns": 0,
                "reason_code": "resume_receipt_mismatch",
                "ticket": "T-1",
            }],
            "status": "warning",
        }
        checked = {
            "action": "repair-check", "current_state": "Blocked-Escalated",
            "head": head, "repair_role": "planner", "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready", "ticket": "T-1",
        }
        self.operator_authority("T-1")
        lock = self.operator_map.parent / ".operator-map.lock"
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o644)
        mapping = self.operator_map.read_bytes()
        receipts = sorted(self.controller_state.glob("operator-receipts/*.json"))

        code, value = self.run_scenario({
            "doctor": doctor, "state-machine:repair-check": checked,
        }, resume=("T-1", receipt))
        self.assertEqual((code, value["status"], value["error"]), (
            2, "error", "qualification operator resume projection refused",
        ))
        self.assertEqual(self.called(), ["doctor", "state-machine:repair-check"])
        self.assertEqual(self.operator_map.read_bytes(), mapping)
        self.assertEqual(
            sorted(self.controller_state.glob("operator-receipts/*.json")), receipts,
        )
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)

    def test_mixed_source_incident_uses_exact_ticket_authorization(self) -> None:
        doctor, _worktree, head, _receipt = self.contract_recovery_fixture()
        doctor["checks"]["transition_receipts"]["incidents"][0].update({
            "active_factory_sha": "c" * 40,
            "receipt_factory_sha": "d" * 40,
        })
        checked = {
            "action": "repair-check", "current_state": "Blocked-Escalated",
            "head": head, "repair_role": "planner", "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready", "ticket": "T-1",
        }
        product = self.root / "product"
        factory = product / "factory"
        authorization_path = (
            factory / "migrations/inflight-release" / f'{"a" * 40}.json'
        )
        authorization_path.parent.mkdir(parents=True)
        self.manifest = factory / "QUALIFICATION.json"
        self.manifest.write_text(json.dumps({
            "budget_usd": "300.000000", "capacity": 3,
            "contract_version": "2.0.0", "factory_sha": "a" * 40,
            "generation": 1, "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": "b" * 40, "target_done": 3,
            "tickets": ["T-1", "T-2", "T-3"],
        }), encoding="utf-8")
        (factory / "PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        authorization = {
            "repository": "example/product",
            "schema": (
                "nysa.software-factory.inflight-release-authorization/v2"
            ),
            "source_kit_sha": "b" * 40,
            "target_kit_sha": "a" * 40,
            "tickets": [{
                "branch": f"ticket/T-{index}", "head": f"{index}" * 40,
                "source_kit_sha": "c" * 40 if index == 1 else "b" * 40,
                "state": "Blocked-Escalated" if index == 1 else "Building",
                "ticket": f"T-{index}",
            } for index in range(1, 4)],
        }
        authorization_path.write_text(
            json.dumps(authorization, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        for command in (
            ("init", "-q"),
            ("config", "user.name", "Qualification Test"),
            ("config", "user.email", "qualification@test.invalid"),
            ("add", "."),
            ("commit", "-qm", "qualification source authorization"),
        ):
            subprocess.run(
                ["git", "-C", str(product), *command], check=True,
                capture_output=True, text=True,
            )
        self.product_sha = subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        code, value = self.run_scenario({
            "doctor": doctor, "state-machine:repair-check": checked,
            "reconcile": [
                self.controller("waiting_for_target"),
                self.controller("waiting_for_target"),
            ],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "doctor", "state-machine:repair-check",
            "reconcile",
        ])

        authorization["tickets"][0]["source_kit_sha"] = "e" * 40
        authorization_path.write_text(
            json.dumps(authorization, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(product), "add", str(authorization_path)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "commit", "-qm", "drift source"],
            check=True, capture_output=True, text=True,
        )
        self.product_sha = subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": [self.controller("waiting_for_target")],
            "qualification": self.report(),
        })
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(self.called(), ["doctor"])

    def test_contract_resume_projection_replays_after_receipt_consumption(self) -> None:
        doctor, worktree, _head, _receipt = self.contract_recovery_fixture(
            "Blocked-Escalated",
        )
        checked = {
            "action": "repair-check",
            "current_state": "Blocked-Escalated",
            "head": subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "repair_role": "planner",
            "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready",
            "ticket": "T-1",
        }
        stale = operator_receipt.issue(
            self.controller_state, "T-1", "resume", {
                "blocked_receipt_sha256": "d" * 64,
                "resume_stage": "Building",
            },
        )
        operator_receipt.verify_consume_exact(
            self.controller_state, "T-1", "resume",
            stale["receipt_sha256"], stale["payload"],
        )
        self.run_scenario({
            "doctor": doctor,
            "state-machine:repair-check": checked,
            "reconcile": [
                self.controller("waiting_for_target"),
                self.controller("waiting_for_target"),
            ],
        })
        subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/lib/operator_receipt.py"),
                "--state-dir", str(self.controller_state), "consume",
                "--ticket", "T-1", "--action", "resume",
                "--payload", json.dumps({
                    "blocked_receipt_sha256": _receipt,
                    "resume_stage": "Building",
                }, sort_keys=True, separators=(",", ":")),
            ],
            check=True, capture_output=True, text=True,
        )
        mapping = json.loads(self.operator_map.read_text(encoding="utf-8"))
        mapping["tickets"]["T-1"].pop("operator")
        self.operator_map.write_text(json.dumps(mapping), encoding="utf-8")
        ticket = worktree / "factory/tickets/T-1.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Blocked-Escalated", "State: Building",
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(worktree), "add", str(ticket)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-qm", "materialize resume"],
            check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        exact_receipt = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in (
                self.controller_state / "operator-receipts/T-1"
            ).glob("resume-*.json")
            if json.loads(path.read_text(encoding="utf-8"))["payload"].get(
                "blocked_receipt_sha256"
            ) == _receipt
        )
        replay = {
            **checked, "current_state": "Building", "head": head,
            "operator_resume_projection_pending": False,
            "operator_resume_receipt_consumed": True,
            "operator_resume_receipt_sha256": exact_receipt["receipt_sha256"],
        }
        resumed = {
            "action": "resume", "head": head, "repair_role": "planner",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready", "ticket": "T-1",
        }
        self.calls.unlink()
        code, value = self.run_scenario({
            "doctor": doctor,
            "state-machine:repair-check": replay,
            "state-machine:resume": resumed,
            "reconcile": [
                self.controller("waiting_for_target"),
                self.controller("waiting_for_target"),
            ],
        })
        self.assertEqual((code, value["reason"]), (3, "cohort_not_accounted"))
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "doctor", "state-machine:repair-check",
            "state-machine:resume", "reconcile",
        ])

    def test_contract_resume_projection_rejects_drift_and_nonqualification_use(self) -> None:
        doctor, worktree, head, _receipt = self.contract_recovery_fixture()
        checked = {
            "action": "repair-check",
            "current_state": "Blocked-Escalated",
            "head": head,
            "repair_role": "planner",
            "resume_state": "Building",
            "role": "test-author",
            "schema": "nysa.software-factory.state-machine/v1",
            "status": "ready",
            "ticket": "T-1",
        }
        for label, key, changed in (
            ("head", "head", "d" * 40),
            ("state", "current_state", "Planning"),
            ("role", "role", "builder"),
        ):
            with self.subTest(label=label):
                self.calls.unlink(missing_ok=True)
                invalid = {**checked, key: changed}
                code, value = self.run_scenario({
                    "doctor": doctor,
                    "state-machine:repair-check": invalid,
                    "reconcile": [self.controller("waiting_for_target")],
                })
                self.assertEqual((code, value["status"]), (2, "error"))
                self.assertFalse(self.operator_map.exists())
                self.assertFalse(
                    (self.controller_state / "operator-receipts").exists()
                )
        self.calls.unlink(missing_ok=True)
        scratch = worktree / "untracked.txt"
        scratch.write_text("local work\n", encoding="utf-8")
        code, value = self.run_scenario({
            "doctor": doctor,
            "state-machine:repair-check": checked,
            "reconcile": [self.controller("waiting_for_target")],
        })
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertFalse(self.operator_map.exists())
        self.assertFalse((self.controller_state / "operator-receipts").exists())
        scratch.unlink()
        result = subprocess.run(
            [
                sys.executable, "-I", "-S", str(ROOT / "scripts/operator-cli.py"),
                "--product", str(worktree), "--state-dir",
                str(self.controller_state), "--qualification-runtime",
                "--qualification-receipt", "c" * 64, "resume",
                "--ticket", "T-1", "--stage", "Building",
            ],
            capture_output=True, check=False, text=True,
            env={"PATH": "/usr/bin:/bin", "FACTORY_OPERATOR_MAP": str(self.operator_map)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("qualification runtime authority is unavailable", result.stderr)
        self.assertFalse(self.operator_map.exists())

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

    def test_live_replay_returns_typed_wait_without_reconcile(self) -> None:
        doctor = self.live_doctor(3)

        code, value = self.run_scenario({"doctor": doctor})

        self.assertEqual((code, value["status"]), (3, "waiting"))
        self.assertEqual(value["reason"], "active_role_wait")
        self.assertEqual(value["doctor"], doctor)
        self.assertEqual(value["evidence_location"], "doctor.checks.runtime")
        self.assertEqual(
            value["retry_condition"],
            "active_runs=active_run_claims=active_attempts=0",
        )
        self.assertEqual(value["retry_argv"], [
            str(self.launcher), "relay", "qualification-finish", "--json",
        ])
        self.assertEqual(self.called(), ["doctor"])

    def test_live_replay_mismatches_remain_blocked(self) -> None:
        cases = {
            "run-count": [("checks.runtime.run_records", 2)],
            "claim-count": [("checks.runtime.active_run_claims", 2)],
            "attempt-count": [("checks.isolated_provider.active_attempts", 2)],
            "boolean-attempt-count": [
                ("checks.isolated_provider.active_attempts", True),
            ],
            "ticket-count": [("checks.runtime.active_run_tickets", [])],
            "foreign-ticket": [("checks.runtime.active_run_tickets.0", "T-9")],
            "missing-lease": [("checks.runtime.dispatch_leases.0.ticket", "T-2")],
            "run-state": [("checks.runtime.runs.0.state", "stale")],
            "run-ticket": [("checks.runtime.runs.0.ticket", "T-1")],
            "run-identity": [("checks.runtime.runs.0.run_id", "bad/run")],
            "provider-token": [("checks.isolated_provider.active_tokens", 1)],
            "launch-lock": [("checks.runtime.locks.launch", True)],
            "stale-run": [("checks.runtime.stale_runs", 1)],
            "stale-lease": [("checks.runtime.dispatch_leases.0.state", "stale")],
            "transition-warning": [
                ("checks.transition_receipts.status", "warning"),
            ],
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.calls.unlink(missing_ok=True)
                doctor = self.live_doctor()
                for dotted, replacement in changes:
                    parent = doctor
                    parts = dotted.split(".")
                    for part in parts[:-1]:
                        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                    if isinstance(parent, list):
                        parent[int(parts[-1])] = replacement
                    else:
                        parent[parts[-1]] = replacement
                code, value = self.run_scenario({"doctor": doctor})
                self.assertEqual((code, value["status"]), (3, "blocked"))
                self.assertEqual(value["reason"], "doctor_not_ready")
                self.assertEqual(self.called(), ["doctor"])

    def test_successor_stale_selected_lease_reaches_controller_recovery(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update({
            "budget_usd": "300.000000",
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "source_factory_sha": "b" * 40,
        })
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        doctor = self.inflight_doctor()
        doctor["checks"]["runtime"]["stale_dispatch_leases"] = 1
        doctor["checks"]["runtime"]["dispatch_leases"][0]["state"] = "stale"

        code, value = self.run_scenario({
            "doctor": doctor,
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

    def test_top_level_controller_error_is_typed_and_preserved(self) -> None:
        controller = {
            "error": "typed top-level failure",
            "schema": "nysa.software-factory.controller/v1",
            "status": "error",
            "_returncode": 1,
        }
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [controller],
            "qualification": self.report(),
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["reason"], "controller_error")
        self.assertEqual(value["controller"]["error"], "typed top-level failure")
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_finish_projects_exact_approval_and_continues_to_green(self) -> None:
        worktree = self.approval_fixture()
        before = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        waiting = self.controller("ok", results=[
            {"status": "waiting", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [waiting, complete],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"))
        self.assertEqual(value["approvals"], ["T-1"])
        self.assertEqual(value["restarts"], 0)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "qualification", "doctor",
        ])
        mapping = json.loads(self.operator_map.read_text(encoding="utf-8"))
        operator = mapping["tickets"]["T-1"]["operator"]
        self.assertEqual(
            (operator["state"], operator["state_base"], operator["approval"]),
            ("Approved", "awaiting approval", "Receipt"),
        )
        receipts = list(
            (self.controller_state / "operator-receipts/T-1").glob("approve-*.json")
        )
        self.assertEqual(len(receipts), 1)
        receipt = operator_receipt.safe_receipt(receipts[0])
        self.assertEqual(receipt["receipt_sha256"], operator["receipt_sha256"])
        self.assertFalse(receipt["consumed"])
        self.assertNotIn("nonce", value)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            before,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                check=True, capture_output=True,
            ).stdout,
            b"",
        )

    def test_finish_approves_waiting_ticket_with_blocked_sibling(self) -> None:
        self.approval_fixture(ticket="T-1")
        approval_wait = self.controller("ok", results=[
            {"status": "waiting", "ticket": "T-1"},
            {"status": "blocked", "ticket": "T-2"},
        ])
        protected_wait = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": "T-1",
                "wait_reason": "protected-merge",
            },
            {"status": "blocked", "ticket": "T-2"},
        ])
        partial = self.controller("ok", results=[
            {"status": "complete", "ticket": "T-1"},
            {"status": "blocked", "ticket": "T-2"},
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [approval_wait, protected_wait, partial, partial],
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "blocked"))
        self.assertEqual(value["approvals"], ["T-1"])
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile", "reconcile",
        ])
        self.assertEqual(
            len(list(
                (self.controller_state / "operator-receipts/T-1")
                .glob("approve-*.json")
            )),
            1,
        )
        self.assertFalse(
            (self.controller_state / "operator-receipts/T-2").exists()
        )

    def test_finish_continues_on_completion_without_an_approval(self) -> None:
        self.operator_authority()
        partial = self.controller("ok", results=[
            {"status": "complete", "ticket": "T-1"},
            {"status": "waiting", "ticket": "T-2"},
            {"status": "waiting", "ticket": "T-3"},
        ])
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [partial, complete],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"))
        self.assertEqual(value["approvals"], [])
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "qualification", "doctor",
        ])

    def test_finish_polls_only_typed_authenticated_github_waits(self) -> None:
        self.operator_authority()
        waiting = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": ticket,
                "wait_reason": "pr-gate",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [waiting, complete],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"))
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "qualification", "doctor",
        ])

    def test_finish_continues_on_authenticated_refresh_without_an_approval(self) -> None:
        self.operator_authority()
        waiting = self.controller("ok", results=[
            {"status": "waiting", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        waiting["_event"] = {
            "event": "protected_base_refreshed",
            "factory_sha": "a" * 40,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-2",
        }
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [waiting, complete],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"))
        self.assertEqual(value["approvals"], [])
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "qualification", "doctor",
        ])

    def test_finish_unchanged_wait_uses_one_bounded_poll_budget(self) -> None:
        self.operator_authority()
        waiting = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": ticket,
                "wait_reason": "closeout",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": waiting,
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile",
        ])

    def test_finish_live_role_preserves_external_poll_budget(self) -> None:
        self.operator_authority()
        live = self.controller("ok", results=[
            {
                "role": "narrator", "status": "waiting", "ticket": ticket,
                "transition_receipt_sha256": "a" * 64,
                "wait_reason": "live-role",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])
        external = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": ticket,
                "wait_reason": "closeout",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [
                external, external, live, live, external, external, complete,
            ],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"), value)
        self.assertEqual(self.called(), [
            "doctor", *(["reconcile"] * 7), "qualification", "doctor",
        ])

    def test_finish_unchanged_live_role_wait_is_bounded(self) -> None:
        self.operator_authority()
        live = self.controller("ok", results=[
            {
                "role": "narrator", "status": "waiting", "ticket": ticket,
                "transition_receipt_sha256": "a" * 64,
                "wait_reason": "live-role",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": live,
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile",
        ])

    def test_finish_new_live_receipt_replenishes_live_poll_budget(self) -> None:
        self.operator_authority()

        def live(receipt: str) -> dict[str, object]:
            return self.controller("ok", results=[
                {
                    "role": "narrator", "status": "waiting",
                    "ticket": ticket,
                    "transition_receipt_sha256": receipt,
                    "wait_reason": "live-role",
                }
                for ticket in ("T-1", "T-2", "T-3")
            ])

        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [
                live("a" * 64), live("a" * 64),
                live("b" * 64), live("b" * 64), complete,
            ],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"), value)
        self.assertEqual(self.called(), [
            "doctor", *(["reconcile"] * 5), "qualification", "doctor",
        ])

    def test_finish_sibling_receipt_does_not_replenish_stale_live_role(self) -> None:
        self.operator_authority()

        def live(second: str) -> dict[str, object]:
            return self.controller("ok", results=[
                {
                    "role": "narrator", "status": "waiting", "ticket": ticket,
                    "transition_receipt_sha256": receipt,
                    "wait_reason": "live-role",
                }
                for ticket, receipt in (
                    ("T-1", "a" * 64), ("T-2", second), ("T-3", "c" * 64),
                )
            ])

        first = live("b" * 64)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [first, first, live("d" * 64)],
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile",
        ])

    def test_finish_malformed_live_role_wait_is_not_retried(self) -> None:
        self.operator_authority()
        malformed = self.controller("ok", results=[
            {
                "role": "narrator", "status": "waiting", "ticket": ticket,
                "transition_receipt_sha256": "invalid",
                "wait_reason": "live-role",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": malformed,
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_finish_live_role_does_not_hide_external_wait(self) -> None:
        self.operator_authority()
        mixed = self.controller("ok", results=[
            {
                "role": "narrator", "status": "waiting", "ticket": "T-1",
                "transition_receipt_sha256": "a" * 64,
                "wait_reason": "live-role",
            },
            {"status": "waiting", "ticket": "T-2", "wait_reason": "closeout"},
            {"status": "waiting", "ticket": "T-3", "wait_reason": "closeout"},
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": mixed,
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile",
        ])

    def test_finish_authenticated_progress_replenishes_poll_budget(self) -> None:
        self.operator_authority()
        waiting = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": ticket,
                "wait_reason": "closeout",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])
        refreshed = copy.deepcopy(waiting)
        refreshed["_event"] = {
            "event": "protected_base_refreshed",
            "factory_sha": "a" * 40,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-2",
        }
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [waiting, waiting, refreshed, waiting, waiting, complete],
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (0, "green"), value)
        self.assertEqual(self.called(), [
            "doctor", *(["reconcile"] * 6), "qualification", "doctor",
        ])

    def test_finish_unrelated_event_does_not_replenish_poll_budget(self) -> None:
        self.operator_authority()
        waiting = self.controller("ok", results=[
            {
                "status": "waiting", "ticket": ticket,
                "wait_reason": "closeout",
            }
            for ticket in ("T-1", "T-2", "T-3")
        ])
        noisy = copy.deepcopy(waiting)
        noisy["_event"] = {
            "event": "controller_started",
            "factory_sha": "a" * 40,
            "schema": "nysa.software-factory.controller-event/v1",
        }

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": [noisy, waiting, waiting],
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "waiting"), value)
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile", "reconcile",
        ])

    def test_finish_pre_wait_runtime_does_not_consume_poll_budget(self) -> None:
        basis = ({"T-1"}, 1, False, "a" * 40, "", {}, "b" * 64)
        waiting = {
            "controller": self.controller("ok", results=[{
                "status": "waiting", "ticket": "T-1",
                "wait_reason": "closeout",
            }]),
            "phases": [], "restarts": 0,
            "status": "waiting", "reason": "authenticated_wait",
        }
        green = {"phases": [], "restarts": 0, "status": "green"}
        args = SimpleNamespace(
            launcher=self.launcher, project="relay",
            resume_receipt="", resume_ticket="",
        )

        with (
            patch.object(RUNNER_MODULE, "execute", side_effect=[waiting, green])
            as execute,
            patch.object(RUNNER_MODULE, "finish_poll_limit", return_value=1),
            patch.object(RUNNER_MODULE, "invoke", return_value=(0, self.doctor())),
            patch.object(RUNNER_MODULE, "launcher_path", return_value=self.launcher),
            patch.object(RUNNER_MODULE, "project_qualification_approvals", return_value=[]),
            patch.object(RUNNER_MODULE, "qualification_basis", return_value=basis),
            patch.object(RUNNER_MODULE, "qualification_event_names", return_value=set()),
            patch.object(RUNNER_MODULE, "terminal_doctor", return_value=True),
            patch.object(
                RUNNER_MODULE.time, "monotonic", side_effect=[0, 601, 601],
            ),
            patch.object(RUNNER_MODULE.time, "sleep"),
        ):
            value = RUNNER_MODULE.execute_finish(args)

        self.assertEqual(value["status"], "green")
        self.assertEqual(execute.call_count, 2)

    def test_finish_unchanged_wait_wall_time_is_bounded(self) -> None:
        basis = ({"T-1"}, 1, False, "a" * 40, "", {}, "b" * 64)
        args = SimpleNamespace(
            launcher=self.launcher, project="relay",
            resume_receipt="", resume_ticket="",
        )
        for reason in ("closeout", "live-role", "untyped"):
            with self.subTest(reason=reason):
                item = {
                    "status": "waiting", "ticket": "T-1",
                }
                if reason != "untyped":
                    item["wait_reason"] = reason
                if reason == "live-role":
                    item.update(
                        role="narrator",
                        transition_receipt_sha256="a" * 64,
                    )
                waiting = {
                    "controller": self.controller("ok", results=[item]),
                    "phases": [], "restarts": 0,
                    "status": "waiting", "reason": "authenticated_wait",
                }
                with (
                    patch.object(
                        RUNNER_MODULE, "execute",
                        side_effect=[waiting, waiting],
                    ) as execute,
                    patch.object(
                        RUNNER_MODULE, "finish_poll_limit", return_value=10,
                    ),
                    patch.object(
                        RUNNER_MODULE, "invoke",
                        return_value=(0, self.doctor()),
                    ),
                    patch.object(
                        RUNNER_MODULE, "launcher_path",
                        return_value=self.launcher,
                    ),
                    patch.object(
                        RUNNER_MODULE, "project_qualification_approvals",
                        return_value=[],
                    ),
                    patch.object(
                        RUNNER_MODULE, "qualification_basis",
                        return_value=basis,
                    ),
                    patch.object(
                        RUNNER_MODULE, "qualification_event_names",
                        return_value=set(),
                    ),
                    patch.object(
                        RUNNER_MODULE.time, "monotonic",
                        side_effect=[0, 0, 601, 601],
                    ),
                    patch.object(RUNNER_MODULE.time, "sleep"),
                ):
                    value = RUNNER_MODULE.execute_finish(args)

                self.assertEqual(value["status"], "waiting")
                expected_waits = {
                    "closeout": [0, 595],
                    "live-role": [0, 0],
                    "untyped": [0],
                }[reason]
                self.assertEqual(
                    [call.args[3] for call in execute.call_args_list],
                    expected_waits,
                )

    def test_finish_warm_wait_consumes_one_external_epoch(self) -> None:
        basis = ({"T-1"}, 1, False, "a" * 40, "", {}, "b" * 64)
        waiting = {
            "controller": self.controller("ok", results=[{
                "status": "waiting", "ticket": "T-1",
                "wait_reason": "closeout",
            }]),
            "phases": [], "restarts": 0,
            "status": "waiting", "reason": "authenticated_wait",
        }
        args = SimpleNamespace(
            launcher=self.launcher, project="relay",
            resume_receipt="", resume_ticket="",
        )
        with (
            patch.object(
                RUNNER_MODULE, "execute",
                side_effect=[waiting, waiting, waiting],
            ) as execute,
            patch.object(RUNNER_MODULE, "finish_poll_limit", return_value=2),
            patch.object(RUNNER_MODULE, "finish_poll_seconds", return_value=5),
            patch.object(RUNNER_MODULE, "invoke", return_value=(0, self.doctor())),
            patch.object(RUNNER_MODULE, "launcher_path", return_value=self.launcher),
            patch.object(RUNNER_MODULE, "project_qualification_approvals", return_value=[]),
            patch.object(RUNNER_MODULE, "qualification_basis", return_value=basis),
            patch.object(RUNNER_MODULE, "qualification_event_names", return_value=set()),
            patch.object(
                RUNNER_MODULE.time, "monotonic",
                side_effect=[0, 0, 10, 20, 20],
            ),
            patch.object(RUNNER_MODULE.time, "sleep"),
        ):
            value = RUNNER_MODULE.execute_finish(args)

        self.assertEqual(value["status"], "waiting")
        self.assertEqual(
            [call.args[3] for call in execute.call_args_list],
            [0, 595, 585],
        )

    def test_finish_new_live_receipts_and_closeout_get_fresh_wall_time(self) -> None:
        basis = ({"T-1"}, 1, False, "a" * 40, "", {}, "b" * 64)
        args = SimpleNamespace(
            launcher=self.launcher, project="relay",
            resume_receipt="", resume_ticket="",
        )

        def waiting(role: str, receipt: str) -> dict[str, object]:
            return {
                "controller": self.controller("ok", results=[{
                    "role": role, "status": "waiting", "ticket": "T-1",
                    "transition_receipt_sha256": receipt,
                    "wait_reason": "live-role",
                }]),
                "phases": [], "restarts": 0,
                "status": "waiting", "reason": "authenticated_wait",
            }

        closeout = {
            "controller": self.controller("ok", results=[{
                "status": "waiting", "ticket": "T-1",
                "wait_reason": "closeout",
            }]),
            "phases": [], "restarts": 0,
            "status": "waiting", "reason": "authenticated_wait",
        }
        green = {"phases": [], "restarts": 0, "status": "green"}
        with (
            patch.object(
                RUNNER_MODULE, "execute",
                side_effect=[
                    waiting("planner", "c" * 64),
                    waiting("narrator", "d" * 64), closeout, green,
                ],
            ) as execute,
            patch.object(RUNNER_MODULE, "finish_poll_limit", return_value=10),
            patch.object(RUNNER_MODULE, "invoke", return_value=(0, self.doctor())),
            patch.object(RUNNER_MODULE, "launcher_path", return_value=self.launcher),
            patch.object(RUNNER_MODULE, "project_qualification_approvals", return_value=[]),
            patch.object(RUNNER_MODULE, "qualification_basis", return_value=basis),
            patch.object(RUNNER_MODULE, "qualification_event_names", return_value=set()),
            patch.object(RUNNER_MODULE, "terminal_doctor", return_value=True),
            patch.object(
                RUNNER_MODULE.time, "monotonic",
                side_effect=[0, 0, 400, 800, 801],
            ),
            patch.object(RUNNER_MODULE.time, "sleep"),
        ):
            value = RUNNER_MODULE.execute_finish(args)

        self.assertEqual(value["status"], "green")
        self.assertEqual(execute.call_count, 4)
        self.assertEqual(
            [call.args[3] for call in execute.call_args_list],
            [0, 0, 0, 595],
        )

    def test_finish_requires_a_fresh_final_doctor_for_green(self) -> None:
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])

        code, value = self.run_scenario({
            "doctor": [self.doctor(), self.doctor("error")],
            "reconcile": complete,
            "qualification": self.report(),
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "blocked"))
        self.assertEqual(value["reason"], "final_doctor_not_ready")
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "qualification", "doctor",
        ])

    def test_finish_rejects_forged_refresh_progress(self) -> None:
        waiting = self.controller("ok", results=[
            {"status": "waiting", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        waiting["_event"] = {
            "event": "protected_base_refreshed",
            "event_sha256": "0" * 64,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-2",
        }

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": waiting,
        }, finish=True)

        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_finish_refuses_qualification_basis_drift(self) -> None:
        waiting = self.controller("ok", results=[
            {"status": "waiting", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        waiting["_event"] = {
            "event": "protected_base_refreshed",
            "factory_sha": "a" * 40,
            "schema": "nysa.software-factory.controller-event/v1",
            "ticket": "T-2",
        }
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["tickets"] = ["T-1", "T-2", "T-4"]
        waiting["_manifest"] = manifest

        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": waiting,
        }, finish=True)

        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(
            value["error"], "qualification basis changed during finish",
        )
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_finish_stops_without_approval_when_ticket_is_not_ready(self) -> None:
        self.approval_fixture(state="Review")
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
            ]),
        }, finish=True)
        self.assertEqual((code, value["status"]), (3, "waiting"))
        self.assertEqual(value["approvals"], [])
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_refuses_dirty_approval_claim(self) -> None:
        self.approval_fixture(dirty=True)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
            ]),
        }, finish=True)
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_prevalidates_all_approvals_before_issuing_any(self) -> None:
        self.approval_fixture(ticket="T-1")
        self.approval_fixture(ticket="T-2", dirty=True)

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
                {"status": "waiting", "ticket": "T-2"},
            ]),
        }, finish=True)

        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertFalse((self.controller_state / "operator-receipts").exists())
        mapping = json.loads(self.operator_map.read_text(encoding="utf-8"))
        self.assertNotIn("operator", mapping["tickets"]["T-1"])
        self.assertNotIn("operator", mapping["tickets"]["T-2"])

    def test_finish_does_not_approve_blocked_or_active_claims(self) -> None:
        self.approval_fixture(ticket="T-1")
        self.approval_fixture(ticket="T-2")
        active_path = self.controller_state / "claims/T-2.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        active.update(parked=False, receipt="d" * 64, role="narrator")
        active_path.write_text(json.dumps(active), encoding="utf-8")
        active_path.chmod(0o600)

        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "blocked", "ticket": "T-1"},
                {"status": "active", "ticket": "T-2"},
            ]),
        }, finish=True)

        self.assertEqual((code, value["status"]), (3, "blocked"))
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_refuses_foreign_approval_claim(self) -> None:
        self.approval_fixture(foreign=True)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
            ]),
        }, finish=True)
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_refuses_foreign_operator_authority(self) -> None:
        self.approval_fixture()
        foreign_map = self.root / "operator-map.json"
        foreign_map.write_bytes(self.operator_map.read_bytes())
        foreign_map.chmod(0o600)
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
            ]),
        }, finish=True, operator_map=foreign_map)
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_refuses_concurrent_controller(self) -> None:
        self.approval_fixture()
        lock = self.controller_state / "reconcile.lock"
        descriptor = lock.open("a+")
        lock.chmod(0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code, value = self.run_scenario({
                "doctor": self.doctor(),
                "reconcile": self.controller("ok", results=[
                    {"status": "waiting", "ticket": "T-1"},
                ]),
            }, finish=True)
        finally:
            descriptor.close()
        self.assertEqual((code, value["status"]), (2, "error"))
        self.assertEqual(self.called(), ["doctor", "reconcile"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_does_not_approve_before_doctor_passes(self) -> None:
        self.approval_fixture()
        doctor = self.doctor("error")
        code, value = self.run_scenario({
            "doctor": doctor,
            "reconcile": self.controller("ok", results=[
                {"status": "waiting", "ticket": "T-1"},
            ]),
        }, finish=True)
        self.assertEqual((code, value["reason"]), (3, "doctor_not_ready"))
        self.assertEqual(value["approvals"], [])
        self.assertEqual(self.called(), ["doctor"])
        self.assertFalse((self.controller_state / "operator-receipts").exists())

    def test_finish_replays_one_approval_without_duplicating_it(self) -> None:
        self.approval_fixture()
        waiting = self.controller("ok", results=[
            {"status": "waiting", "ticket": "T-1"},
        ])
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": [waiting, waiting],
        }, finish=True)
        self.assertEqual((code, value["status"]), (3, "waiting"))
        self.assertEqual(value["approvals"], ["T-1"])
        self.assertEqual(self.called(), [
            "doctor", "reconcile", "reconcile",
        ])
        self.assertEqual(
            len(list(
                (self.controller_state / "operator-receipts/T-1")
                .glob("approve-*.json")
            )),
            1,
        )

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

    def test_reducer_network_wait_preserves_completed_controller_evidence(self) -> None:
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        code, value = self.run_scenario({
            "doctor": self.doctor(),
            "reconcile": complete,
            "qualification": {
                "_returncode": 75,
                "reason_code": "external_unavailable",
                "status": "wait",
            },
        })

        self.assertEqual((code, value["status"]), (3, "waiting"))
        self.assertEqual(value["reason"], "external_unavailable")
        self.assertEqual(value["controller"], complete)
        self.assertEqual(self.called(), ["doctor", "reconcile", "qualification"])

    def test_controller_network_wait_stops_before_reduction(self) -> None:
        wait = {
            "_returncode": 75,
            "reason_code": "external_unavailable",
            "status": "wait",
        }
        code, value = self.run_scenario({
            "doctor": self.doctor(), "reconcile": wait,
        })

        self.assertEqual((code, value["status"]), (3, "waiting"))
        self.assertEqual(value["reason"], "external_unavailable")
        self.assertEqual(value["controller"], {
            "reason_code": "external_unavailable", "status": "wait",
        })
        self.assertEqual(self.called(), ["doctor", "reconcile"])

    def test_reducer_refusal_returns_only_a_bounded_reason_code(self) -> None:
        complete = self.controller("ok", results=[
            {"status": "complete", "ticket": ticket}
            for ticket in ("T-1", "T-2", "T-3")
        ])
        for error, expected in (
            (
                "T-2 role evidence was replayed or is incomplete",
                {"reducer_reason_code": "role_evidence_replayed", "ticket": "T-2"},
            ),
            (
                "qualification activation receipt is invalid",
                {"reducer_reason_code": "activation_receipt_invalid"},
            ),
            (
                "provider accounting attempts do not reconcile",
                {"reducer_reason_code": "provider_attempts_mismatch"},
            ),
            (
                "immutable qualification report is unsafe",
                {"reducer_reason_code": "report_unsafe"},
            ),
            (
                "qualification latency target exceeded",
                {
                    "metric": "prepared_to_all_planners",
                    "observed_ms": 240001,
                    "reducer_reason_code": "latency_target_exceeded",
                    "target_ms": 240000,
                },
            ),
            (
                "qualification latency target exceeded",
                {
                    "metric": "final_narrator_to_done",
                    "observed_ms": 480001,
                    "reducer_reason_code": "latency_target_exceeded",
                    "target_ms": 480000,
                    "ticket": "T-2",
                },
            ),
            (
                "qualification latency target exceeded",
                {"reducer_reason_code": "invalid_reducer_error"},
            ),
            (
                "provider-private-detail-123",
                {"reducer_reason_code": "unclassified"},
            ),
        ):
            with self.subTest(error=error):
                self.calls.unlink(missing_ok=True)
                reducer = {
                    "_returncode": 1,
                    "error": error,
                    "schema": "nysa.software-factory.qualification-report/v1",
                    "status": "error",
                }
                reducer.update({
                    key: expected[key]
                    for key in ("metric", "observed_ms", "target_ms", "ticket")
                    if key in expected
                })
                code, value = self.run_scenario({
                    "doctor": self.doctor(),
                    "reconcile": complete,
                    "qualification": reducer,
                })
                self.assertEqual((code, value["status"]), (2, "error"))
                self.assertEqual(value["reason"], "qualification_reduction_failed")
                self.assertEqual(
                    {key: value[key] for key in expected}, expected,
                )
                self.assertNotIn(error, json.dumps(value, sort_keys=True))
                self.assertEqual(
                    self.called(), ["doctor", "reconcile", "qualification"],
                )

        for malformed in (
            {
                "metric": [], "observed_ms": 240001, "target_ms": 240000,
            },
            {
                "metric": "final_narrator_to_done", "observed_ms": 480001,
                "target_ms": 480000, "ticket": [],
            },
        ):
            with self.subTest(malformed=malformed):
                self.calls.unlink(missing_ok=True)
                reducer = {
                    "_returncode": 1,
                    "error": "qualification latency target exceeded",
                    "schema": "nysa.software-factory.qualification-report/v1",
                    "status": "error",
                    **malformed,
                }
                code, value = self.run_scenario({
                    "doctor": self.doctor(),
                    "reconcile": complete,
                    "qualification": reducer,
                })
                self.assertEqual((code, value["status"]), (2, "error"))
                self.assertEqual(
                    value["reducer_reason_code"], "invalid_reducer_error",
                )


if __name__ == "__main__":
    unittest.main()
