#!/usr/bin/env python3
"""Regression checks for deterministic qualification driving."""

from __future__ import annotations

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
        self.root = Path(self.temporary.name)
        self.scenario = self.root / "scenario.json"
        self.calls = self.root / "calls.json"
        self.launcher = self.root / "factory-launch"
        self.launcher.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
scenario = json.loads(pathlib.Path(os.environ["QUALIFICATION_RUN_SCENARIO"]).read_text())
calls_path = pathlib.Path(os.environ["QUALIFICATION_RUN_CALLS"])
calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
action = sys.argv[2]
index = sum(item == action for item in calls)
calls.append(action)
calls_path.write_text(json.dumps(calls))
value = scenario[action]
if action == "reconcile":
    value = value[index]
code = value.pop("_returncode", 0) if isinstance(value, dict) else 0
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
raise SystemExit(code)
""",
            encoding="utf-8",
        )
        self.launcher.chmod(stat.S_IRWXU)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def doctor(status: str = "ok") -> dict[str, object]:
        return {
            "schema": "nysa.software-factory.doctor/v2",
            "overall_status": status,
        }

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
            "reconcile": [self.controller("ok")],
            "qualification": report,
        })
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "error")
        self.assertEqual(self.called(), ["doctor", "reconcile", "qualification"])


if __name__ == "__main__":
    unittest.main()
