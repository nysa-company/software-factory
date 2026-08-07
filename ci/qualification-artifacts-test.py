#!/usr/bin/env python3
"""Regressions for exact qualification historical artifact closure."""

import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_artifacts", ROOT / "scripts/lib/qualification_artifacts.py"
)
assert SPEC and SPEC.loader
ARTIFACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARTIFACTS)


class QualificationArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.product = self.root / "product"
        self.runs = self.product / "factory/runs"
        self.runs.mkdir(parents=True)
        self.state = self.root / "controller"
        (self.state / "passports").mkdir(parents=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self.secret = b"k" * 32
        (self.state / "passport.key").write_bytes(self.secret)
        os.chmod(self.state / "passport.key", 0o600)
        self.ticket = "T-100"
        self.run_id = "historical-spec"
        self.role = "spec-linter"
        self.output = b"preserved private role output\n"
        self.progress = (
            json.dumps({
                "event_sha256": "a" * 64,
                "observed_monotonic_ns": 1,
                "sequence": 1,
                "subtype": "success",
                "type": "result",
            }, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self.receipt = "b" * 64
        self.factory = "c" * 40
        self.head = "d" * 40
        self.write_artifact("out", self.output)
        self.write_artifact("progress.jsonl", self.progress)
        manifest = (
            f"run_id={self.run_id}\n"
            f"ticket={self.ticket}\n"
            f"role={self.role}\n"
            f"kit_sha={self.factory}\n"
            "contract_version=1.8.0\n"
            f"role_head_before={self.head}\n"
            f"transition_receipt_sha256={self.receipt}\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"output_sha256={hashlib.sha256(self.output).hexdigest()}\n"
            "progress_events=1\n"
            f"progress_journal_sha256={hashlib.sha256(self.progress).hexdigest()}\n"
        ).encode()
        self.write_artifact("meta", manifest)
        self.evidence = {
            "contract_version": "1.8.0",
            "factory_sha": self.factory,
            "head_before": self.head,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "output_sha256": hashlib.sha256(self.output).hexdigest(),
            "role": self.role,
            "run_id": self.run_id,
            "transition_receipt_sha256": self.receipt,
        }
        body = {
            "completed_role_evidence": [self.evidence],
            "schema": ARTIFACTS.PASSPORT_SCHEMA,
            "ticket": self.ticket,
        }
        self.write_passport(body)

    def write_passport(self, body) -> None:
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            self.secret, ARTIFACTS.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            ARTIFACTS.canonical(passport)
        ).hexdigest()
        path = self.state / f"passports/{self.ticket}.json"
        path.write_bytes(ARTIFACTS.canonical(passport))
        os.chmod(path, 0o600)

    def write_corrected(
        self, issue: str, role: str, exit_status: str, role_exit: str,
        manifest_progress: bool,
    ) -> dict:
        progress_events = "1" if manifest_progress else ""
        progress_digest = (
            hashlib.sha256(self.progress).hexdigest()
            if manifest_progress else ""
        )
        manifest = (
            f"run_id={self.run_id}\n"
            f"ticket={self.ticket}\n"
            f"role={role}\n"
            f"kit_sha={self.factory}\n"
            "contract_version=1.8.0\n"
            f"role_head_before={self.head}\n"
            f"transition_receipt_sha256={self.receipt}\n"
            f"exit_status={exit_status}\n"
            f"role_exit={role_exit}\n"
            f"output_sha256={hashlib.sha256(self.output).hexdigest()}\n"
            f"progress_events={progress_events}\n"
            f"progress_journal_sha256={progress_digest}\n"
        ).encode()
        self.write_artifact("meta", manifest)
        evidence = {
            **self.evidence,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "role": role,
        }
        correction = {
            "failed_factory_sha": self.factory,
            "issue": issue,
            "output_head_sha": "e" * 40,
            "progress_events": 1,
            "progress_journal_sha256": hashlib.sha256(
                self.progress
            ).hexdigest(),
            "receipt_parent_file_sha256": "f" * 64,
            "recovery_factory_sha": "0" * 40,
            "run_id": self.run_id,
            "schema": ARTIFACTS.CORRECTION_SCHEMA,
            "transition_receipt_sha256": self.receipt,
        }
        body = {
            "completed_role_corrections": [correction],
            "completed_role_evidence": [evidence],
            "schema": ARTIFACTS.PASSPORT_SCHEMA,
            "ticket": self.ticket,
        }
        self.write_passport(body)
        return body

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_artifact(self, kind: str, raw: bytes) -> Path:
        path = self.runs / f"{self.run_id}.{kind}"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        return path

    def test_retains_and_restores_only_exact_passport_closure(self) -> None:
        unrelated = self.runs / "unrelated.out"
        unrelated.write_bytes(b"unrelated\n")
        os.chmod(unrelated, 0o600)
        result = ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)
        self.assertEqual(result, {"artifacts": 3, "runs": 1})
        retained = self.state / "retained-runs"
        self.assertFalse((retained / "unrelated.out").exists())
        self.assertEqual((retained / f"{self.run_id}.out").read_bytes(), self.output)
        self.assertEqual(
            (retained / f"{self.run_id}.out").stat().st_mode & 0o777, 0o600
        )

        (self.runs / f"{self.run_id}.out").unlink()
        ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)
        self.assertEqual((self.runs / f"{self.run_id}.out").read_bytes(), self.output)

    def test_missing_or_changed_artifact_fails_without_content(self) -> None:
        output = self.runs / f"{self.run_id}.out"
        output.unlink()
        with self.assertRaisesRegex(
            ARTIFACTS.ArtifactError,
            rf"{self.ticket} {self.run_id} {self.role} missing out",
        ) as failure:
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)
        self.assertNotIn(self.output.decode().strip(), str(failure.exception))

        self.write_artifact("out", b"wrong\n")
        with self.assertRaisesRegex(ARTIFACTS.ArtifactError, "digest mismatch"):
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)

    def test_symlink_hardlink_and_passport_tamper_fail_closed(self) -> None:
        output = self.runs / f"{self.run_id}.out"
        target = self.root / "outside"
        target.write_bytes(self.output)
        os.chmod(target, 0o600)
        output.unlink()
        output.symlink_to(target)
        with self.assertRaisesRegex(ARTIFACTS.ArtifactError, "unsafe artifact"):
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)

        output.unlink()
        os.link(target, output)
        with self.assertRaisesRegex(ARTIFACTS.ArtifactError, "unsafe artifact"):
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)

        output.unlink()
        self.write_artifact("out", self.output)
        passport = self.state / f"passports/{self.ticket}.json"
        value = json.loads(passport.read_text())
        value["completed_role_evidence"][0]["role"] = "planner"
        passport.write_text(json.dumps(value))
        os.chmod(passport, 0o600)
        with self.assertRaisesRegex(ARTIFACTS.ArtifactError, "passport digest"):
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)

    def test_authenticated_model_identity_correction_retains_artifacts(self) -> None:
        body = self.write_corrected(
            "https://github.com/nysa-company/software-factory/issues/390",
            "spec-linter", "9", "provider_failed", True,
        )
        self.assertEqual(
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket),
            {"artifacts": 3, "runs": 1},
        )
        body["completed_role_corrections"] = []
        self.write_passport(body)
        with self.assertRaisesRegex(
            ARTIFACTS.ArtifactError, "manifest identity mismatch"
        ):
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket)

    def test_authenticated_converged_success_correction_retains_artifacts(
        self,
    ) -> None:
        self.write_corrected(
            "https://github.com/nysa-company/software-factory/issues/218",
            "builder", "128", "", False,
        )
        self.assertEqual(
            ARTIFACTS.ensure_ticket(self.product, self.state, self.ticket),
            {"artifacts": 3, "runs": 1},
        )

    def test_unknown_or_mismatched_correction_fails_closed(self) -> None:
        body = self.write_corrected(
            "https://github.com/nysa-company/software-factory/issues/390",
            "spec-linter", "9", "provider_failed", True,
        )
        for field, value in (
            ("issue", "https://github.com/nysa-company/software-factory/issues/999"),
            ("failed_factory_sha", "1" * 40),
            ("transition_receipt_sha256", "2" * 64),
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(body))
                changed["completed_role_corrections"][0][field] = value
                self.write_passport(changed)
                with self.assertRaisesRegex(
                    ARTIFACTS.ArtifactError, "correction evidence is invalid"
                ):
                    ARTIFACTS.ensure_ticket(
                        self.product, self.state, self.ticket
                    )


if __name__ == "__main__":
    unittest.main()
