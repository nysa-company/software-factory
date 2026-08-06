#!/usr/bin/env python3
"""Focused trust-boundary tests for one-use emergency role admission."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADMIT = load("emergency_admit_test_subject", ROOT / "scripts/emergency-admit.py")
STATE = load("emergency_admit_state", ROOT / "scripts/state-machine.py")
PASSPORT = load("emergency_admit_passport", ROOT / "scripts/ticket-passport.py")


class Fixture:
    def __init__(self, root: Path, role: str, state: str, *, capped: bool = False):
        self.root = root
        self.product = root / "product"
        self.state = root / "controller"
        self.release = root / ("b" * 40)
        self.ticket = "T-901"
        self.role = role
        self.lifecycle = state
        self.lease = "d" * 64
        self.product.mkdir()
        self.state.mkdir(mode=0o700)
        self.release.mkdir()
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/.dispatch-leases").mkdir()
        (self.product / "factory/tickets/T-901.md").write_text(
            f"# T-901\n\nState: {state}\nKit-SHA: {'b' * 40}\n",
            encoding="utf-8",
        )
        (self.product / "factory/route-plans/T-901.json").write_text(
            json.dumps({"kit_sha": "b" * 40, "ticket": self.ticket}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.product, check=True)
        subprocess.run(
            ["git", "config", "user.email", "admit-test@example.invalid"],
            cwd=self.product, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "admit-test"],
            cwd=self.product, check=True,
        )
        subprocess.run(["git", "add", "factory"], cwd=self.product, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.product, check=True)
        subprocess.run(
            ["git", "checkout", "-qb", f"ticket/{self.ticket}"],
            cwd=self.product, check=True,
        )
        self.head = self.git("rev-parse", "HEAD")
        self.key = b"k" * 32
        self.write_bytes(self.state / "passport.key", self.key)
        self.passport = {
            "branch": f"ticket/{self.ticket}",
            "charge_records": [],
            "completed_role_evidence": [],
            "contract_version": "1.8.0",
            "current_stage": f"RUN {role}",
            "current_state": state,
            "factory_sha": "b" * 40,
            "head_sha": self.head,
            "project": "fixture",
            "publication_state": "none",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": self.ticket,
        }
        (self.state / "passports").mkdir(mode=0o700)
        self.write_passport()
        self.write_json(
            self.product / "factory/.dispatch-leases/T-901.json",
            {
                "claimed_epoch": int(time.time()),
                "expires_epoch": int(time.time()) + 900,
                "lease_id": self.lease,
                "schema_version": 1,
                "ticket": self.ticket,
            },
        )
        (self.state / "claims").mkdir(mode=0o700)
        self.write_claim(running=False)
        self.args = argparse.Namespace(
            action="plan",
            approve_hash="",
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="b" * 40,
            kit_dir=self.release,
            lease=self.lease,
            project="fixture",
            receipt="",
            request=None,
            role=role,
            state_dir=self.state,
            ticket=self.ticket,
            workdir=self.product,
        )
        loop = (
            {"attempt": 3, "capped": True, "kind": "contract-repair", "limit": 3}
            if capped else None
        )
        receipt = STATE.issue(self.args, f"RUN {role}", loop)
        self.args.receipt = receipt["receipt_sha256"]
        self.request = root / "request.json"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.write_json(self.request, {
            "expires_at": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "issue": "https://github.com/nysa-company/software-factory/issues/346",
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "operator_id": "qualification-operator",
            "reason": "Exercise the exact bounded pre-provider recovery path.",
            "schema": ADMIT.REQUEST_SCHEMA,
        })
        self.args.request = self.request

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.product), *args],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    @staticmethod
    def write_bytes(path: Path, value: bytes) -> None:
        path.write_bytes(value)
        path.chmod(0o600)

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def write_passport(self) -> None:
        self.passport = PASSPORT.authenticate(
            {
                name: value for name, value in self.passport.items()
                if name not in {"authentication_sha256", "passport_sha256"}
            },
            self.key,
        )
        self.write_json(self.state / "passports/T-901.json", self.passport)

    def write_claim(self, *, running: bool) -> None:
        self.write_json(self.state / "claims/T-901.json", {
            "branch": f"ticket/{self.ticket}",
            "lease": self.lease,
            "priority": "normal",
            "publication_lease": "",
            "receipt": self.args.receipt if running and hasattr(self, "args") else "",
            "role": self.role if running else "",
            "schema": "nysa.software-factory.controller-claim/v1",
            "status": "running" if running else "claimed",
            "ticket": self.ticket,
            "worktree": str(self.product),
        })

    def authority(self) -> dict:
        return ADMIT.request(self.request, current=True)

    def plan_apply(self) -> tuple[dict, str]:
        plan, _ = ADMIT.build_plan(self.args, self.authority())
        approval = ADMIT.digest(plan)
        self.args.approve_hash = approval
        result = ADMIT.apply(self.args, self.authority())
        self.args.approve_hash = ""
        return result, approval

    def consume(self) -> dict:
        self.write_claim(running=True)
        self.args.request = None
        self.args.lease = self.lease
        return ADMIT.consume(self.args)

    def complete(self, run_id: str = "admit-run") -> None:
        manifest = self.product / f"factory/runs/{run_id}.meta"
        manifest.write_text(
            f"run_id={run_id}\n"
            f"ticket={self.ticket}\n"
            f"role={self.role}\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"kit_sha={'b' * 40}\n"
            f"role_head_before={self.head}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        evidence = {
            "contract_version": "1.8.0",
            "factory_sha": "b" * 40,
            "head_before": self.head,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "role": self.role,
            "run_id": run_id,
            "transition_receipt_sha256": self.args.receipt,
        }
        self.passport["charge_records"] = [evidence]
        self.passport["completed_role_evidence"] = [evidence]
        self.write_passport()


class EmergencyAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.environment = {
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.base / "remote.git"),
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_RELEASE_TREE": "a" * 40,
        }
        self.old_environment = {name: os.environ.get(name) for name in self.environment}
        os.environ.update(self.environment)
        self.old_issue = ADMIT.validate_issue
        self.old_protected = ADMIT.protected_identity
        ADMIT.validate_issue = lambda _url: None
        ADMIT.protected_identity = lambda _args, _head: {
            "base": "1" * 40, "main": "2" * 40, "main_tree": "3" * 40,
        }

    def tearDown(self) -> None:
        ADMIT.validate_issue = self.old_issue
        ADMIT.protected_identity = self.old_protected
        for name, value in self.old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def fixture(self, name: str, role: str, state: str, *, capped: bool = False) -> Fixture:
        root = self.base / name
        root.mkdir()
        return Fixture(root, role, state, capped=capped)

    def test_one_use_admission_and_archive_at_each_lifecycle_boundary(self) -> None:
        for index, (role, state) in enumerate((
            ("planner", "Planning"),
            ("builder", "Building"),
            ("reviewer", "Review"),
        )):
            with self.subTest(role=role):
                fixture = self.fixture(str(index), role, state)
                applied, approval = fixture.plan_apply()
                self.assertEqual(applied["status"], "applied")
                fixture.args.approve_hash = approval
                self.assertFalse(
                    ADMIT.apply(fixture.args, fixture.authority())["created"]
                )
                fixture.args.approve_hash = ""
                event_path = (
                    fixture.state / "events"
                    / f"emergency-admission-authorized-{approval}.json"
                )
                event = json.loads(event_path.read_text())
                event_digest = event.pop("event_sha256")
                self.assertEqual(
                    event_digest,
                    hashlib.sha256(ADMIT.canonical(event).rstrip(b"\n")).hexdigest(),
                )
                self.assertEqual(event["event"], "emergency_admission_authorized")
                consumed = fixture.consume()
                self.assertEqual(consumed["approval_sha256"], approval)
                receipt = STATE.safe_receipt(
                    fixture.state / f"{fixture.ticket}.json"
                )
                self.assertTrue(receipt["consumed"])
                with self.assertRaisesRegex(
                    ADMIT.Refusal, "exact unconsumed role admission"
                ):
                    ADMIT.consume(fixture.args)
                fixture.complete()
                archived = ADMIT.archive(fixture.args)
                self.assertEqual(archived["status"], "archived")
                self.assertEqual(ADMIT.archive(fixture.args), archived)
                self.assertFalse(
                    (fixture.state / "emergency-admissions/T-902").exists()
                )

    def test_plan_and_consume_fail_closed_on_drift_or_active_work(self) -> None:
        capped = self.fixture("capped", "builder", "Building", capped=True)
        with self.assertRaisesRegex(ADMIT.Refusal, "exact unconsumed role admission"):
            ADMIT.build_plan(capped.args, capped.authority())

        publication = self.fixture("publication", "builder", "Building")
        claim = json.loads((publication.state / "claims/T-901.json").read_text())
        claim["publication_lease"] = "e" * 64
        publication.write_json(publication.state / "claims/T-901.json", claim)
        with self.assertRaisesRegex(ADMIT.Refusal, "pre-provider boundary"):
            ADMIT.build_plan(publication.args, publication.authority())

        active = self.fixture("active", "reviewer", "Review")
        (active.product / "factory/.active-runs").mkdir()
        (active.product / "factory/.active-runs/T-901.run.pid").write_text("pid=1\n")
        with self.assertRaisesRegex(ADMIT.Refusal, "active provider attempt"):
            ADMIT.build_plan(active.args, active.authority())

        maintenance = self.fixture("maintenance", "planner", "Planning")
        (maintenance.product / "factory/MAINTENANCE").touch()
        with self.assertRaisesRegex(ADMIT.Refusal, "refuses maintenance"):
            ADMIT.build_plan(maintenance.args, maintenance.authority())

        reserved = self.fixture("reserved", "planner", "Planning")
        reservation = (
            reserved.state / "emergency-admissions" / reserved.ticket
        )
        reservation.mkdir(parents=True, mode=0o700)
        reserved.write_json(reservation / f"{'f' * 64}.reservation.json", {})
        with self.assertRaisesRegex(ADMIT.Refusal, "active reservation"):
            ADMIT.build_plan(reserved.args, reserved.authority())

        drift = self.fixture("drift", "builder", "Building")
        drift.plan_apply()
        drift.write_claim(running=True)
        (drift.product / "factory/route-plans/T-901.json").write_text("{}\n")
        drift.args.request = None
        with self.assertRaisesRegex(ADMIT.Refusal, "exact unconsumed role admission"):
            ADMIT.consume(drift.args)

    def test_authorization_is_hash_bound_authenticated_and_non_wildcard(self) -> None:
        fixture = self.fixture("auth", "builder", "Building")
        _, approval = fixture.plan_apply()
        path = (
            fixture.state / "emergency-admissions/T-901"
            / f"{approval}.authorization.json"
        )
        value = json.loads(path.read_text())
        value["plan"]["role"] = "reviewer"
        fixture.write_json(path, value)
        fixture.write_claim(running=True)
        fixture.args.request = None
        with self.assertRaisesRegex(ADMIT.Refusal, "record digest is invalid"):
            ADMIT.consume(fixture.args)

    def test_request_issue_and_approval_authority_fail_closed(self) -> None:
        fixture = self.fixture("authority", "builder", "Building")
        fixture.args.approve_hash = "0" * 64
        with self.assertRaisesRegex(ADMIT.Refusal, "approval hash does not match"):
            ADMIT.apply(fixture.args, fixture.authority())

        value = json.loads(fixture.request.read_text())
        value["operator_id"] = "auto"
        fixture.write_json(fixture.request, value)
        with self.assertRaisesRegex(ADMIT.Refusal, "authority is invalid"):
            fixture.authority()

        closed = subprocess.CompletedProcess(
            [], 0,
            json.dumps({
                "html_url": "https://github.com/nysa-company/software-factory/issues/346",
                "number": 346,
                "state": "closed",
            }),
            "",
        )
        with patch.object(ADMIT.subprocess, "run", return_value=closed):
            with self.assertRaisesRegex(ADMIT.Refusal, "exact open Factory issue"):
                self.old_issue(
                    "https://github.com/nysa-company/software-factory/issues/346"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
