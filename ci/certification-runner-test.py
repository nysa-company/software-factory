#!/usr/bin/env python3
"""Focused tests for measured, bounded product certification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/certification-runner.py"


class CertificationRunnerTest(unittest.TestCase):
    def runtime(self):
        return {
            "node": subprocess.run(
                ["node", "--version"], text=True, capture_output=True, check=True
            ).stdout.strip(),
            "npm": subprocess.run(
                ["npm", "--version"], text=True, capture_output=True, check=True
            ).stdout.strip(),
        }

    def run_plan(
        self, root: Path, phases: list[dict], workers: int = 2,
        network_reviewed: bool = False,
        factory_sha: str = "a" * 40,
        factory_tree: str = "c" * 40,
        product_sha: str = "d" * 40,
        product_tree: str = "b" * 40,
        contract_version: str = "1.8.0",
        runtime: dict | None = None,
    ):
        phases = [{**phase, "network": phase.get("network", "denied")} for phase in phases]
        runtime = runtime or self.runtime()
        plan = root / "plan.json"
        plan.write_text(json.dumps({
            "schema": "nysa.software-factory.certification-plan/v2",
            "phases": phases,
            "runtime": runtime,
        }))
        result = root / "results" / "result.json"
        environment = os.environ.copy()
        environment.update(
            FACTORY_KIT_SHA=factory_sha,
            FACTORY_KIT_TREE=factory_tree,
            FACTORY_PRODUCT_SHA=product_sha,
            FACTORY_PRODUCT_TREE=product_tree,
            FACTORY_CONTRACT_VERSION=contract_version,
            FACTORY_CERTIFICATION_TUPLE=json.dumps({
                "contract_version": contract_version,
                "factory_sha": factory_sha,
                "factory_tree": factory_tree,
                "node": runtime["node"],
                "npm": runtime["npm"],
                "product_sha": product_sha,
                "product_tree": product_tree,
            }),
            FACTORY_CERTIFICATION_NETWORK_REVIEWED=(
                "1" if network_reviewed else "0"
            ),
        )
        completed = subprocess.run(
            [
                sys.executable, str(RUNNER), "--plan", str(plan),
                "--result", str(result), "--workers", str(workers),
            ],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return completed, json.loads(result.read_text()) if result.exists() else None

    def test_two_independent_phases_run_concurrently_and_bind_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            helper = root / "peer.py"
            helper.write_text(
                "import pathlib,sys,time\n"
                "mine,other=map(pathlib.Path,sys.argv[1:])\n"
                "mine.write_text('ready')\n"
                "for _ in range(100):\n"
                "  if other.exists(): break\n"
                "  time.sleep(.02)\n"
                "else: raise SystemExit(9)\n"
            )
            phases = [
                {
                    "artifacts": ["a.ready"],
                    "command": [sys.executable, str(helper), "a.ready", "b.ready"],
                    "depends_on": [],
                    "name": "api",
                },
                {
                    "artifacts": ["b.ready"],
                    "command": [sys.executable, str(helper), "b.ready", "a.ready"],
                    "depends_on": [],
                    "name": "web",
                },
            ]
            completed, result = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["factory_sha"], "a" * 40)
            self.assertEqual(result["product_tree"], "b" * 40)
            self.assertEqual(
                {item["name"] for item in result["phases"]}, {"api", "web"}
            )
            self.assertTrue(
                all(item["artifact_sha256"] for item in result["phases"])
            )
            self.assertTrue(
                all(item["cache_hit"] is False for item in result["phases"])
            )
            self.assertTrue(
                all(item["network_granted"] is False for item in result["phases"])
            )

    def test_failed_phase_cancels_sibling_and_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            phases = [
                {
                    "artifacts": [],
                    "command": [
                        sys.executable,
                        "-c",
                        "print('exact failure detail');raise SystemExit(7)",
                    ],
                    "depends_on": [],
                    "name": "fail",
                },
                {
                    "artifacts": [],
                    "command": [
                        sys.executable, "-c", "import time;time.sleep(5)"
                    ],
                    "depends_on": [],
                    "name": "sibling",
                },
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["fail"],
                    "name": "downstream",
                },
            ]
            completed, result = self.run_plan(root, phases)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(result["status"], "fail")
            self.assertIn("failed-phase-output:", completed.stdout)
            self.assertIn("exact failure detail", completed.stdout)
            by_name = {item["name"]: item for item in result["phases"]}
            self.assertEqual(by_name["fail"]["exit_status"], 7)
            self.assertIsNone(by_name["downstream"]["exit_status"])

    def test_invalid_or_cyclic_plan_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            phases = [
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["two"],
                    "name": "one",
                },
                {
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": ["one"],
                    "name": "two",
                },
            ]
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema": "nysa.software-factory.certification-plan/v2",
                "phases": [{**phase, "network": "denied"} for phase in phases],
                "runtime": self.runtime(),
            }))
            environment = os.environ.copy()
            environment.update(
                FACTORY_KIT_SHA="a" * 40,
                FACTORY_KIT_TREE="c" * 40,
                FACTORY_PRODUCT_SHA="d" * 40,
                FACTORY_PRODUCT_TREE="b" * 40,
                FACTORY_CONTRACT_VERSION="1.8.0",
            )
            completed = subprocess.run(
                [
                    sys.executable, str(RUNNER), "--plan", str(plan),
                    "--result", str(root / "result.json"),
                ],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            failure = json.loads(completed.stderr)["failure"]
            self.assertEqual(failure["reason_code"], "certification_plan_invalid")
            self.assertEqual(failure["field"], "certification_plan")
            self.assertFalse((root / "result.json").exists())

            plan.write_text(json.dumps({
                "schema": "nysa.software-factory.certification-plan/v2",
                "phases": [{
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "depends_on": [],
                    "name": "application-test",
                    "network": "denied",
                    "reuse": "artifacts",
                }],
                "runtime": self.runtime(),
            }))
            completed = subprocess.run(
                [
                    sys.executable, str(RUNNER), "--plan", str(plan),
                    "--result", str(root / "result.json"),
                ],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            failure = json.loads(completed.stderr)["failure"]
            self.assertEqual(failure["reason_code"], "certification_plan_invalid")
            self.assertFalse((root / "result.json").exists())

            invalid = json.loads(plan.read_text())
            invalid["phases"][0]["artifacts"] = ["artifact"]
            invalid["phases"][0]["reuse"] = "unknown"
            plan.write_text(json.dumps(invalid))
            completed = subprocess.run(
                [
                    sys.executable, str(RUNNER), "--plan", str(plan),
                    "--result", str(root / "result.json"),
                ],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            failure = json.loads(completed.stderr)["failure"]
            self.assertEqual(failure["reason_code"], "certification_plan_invalid")
            self.assertFalse((root / "result.json").exists())

    def test_required_network_fails_before_phase_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sentinel = root / "spawned"
            phases = [{
                "artifacts": ["spawned"],
                "command": [sys.executable, "-c", f"open({str(sentinel)!r},'w').close()"],
                "depends_on": [],
                "name": "npm-ci",
                "network": "required",
                "reuse": "artifacts",
            }]
            completed, result = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(result["failure"]["reason_code"], "reviewed_network_required")
            self.assertFalse(sentinel.exists())

            completed, result = self.run_plan(
                root, phases, network_reviewed=True
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(sentinel.exists())
            self.assertTrue(result["phases"][0]["network_granted"])

    def test_exact_phase_evidence_reuse_and_input_invalidators(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            counter = root / "count"
            helper = root / "phase.py"
            helper.write_text(
                "import pathlib,sys\n"
                "path=pathlib.Path(sys.argv[1])\n"
                "path.write_text(str(int(path.read_text())+1) if path.exists() else '1')\n"
                "pathlib.Path('compiled.out').write_text('exact')\n"
            )
            phase = {
                "artifacts": ["compiled.out"],
                "command": [sys.executable, str(helper), str(counter)],
                "depends_on": [],
                "name": "compile",
                "network": "optional",
                "reuse": "artifacts",
            }
            first, first_result = self.run_plan(root, [phase])
            second, second_result = self.run_plan(root, [phase])
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(first_result["phases"][0]["cache_hit"])
            self.assertTrue(second_result["phases"][0]["cache_hit"])
            self.assertRegex(
                second_result["phases"][0]["cache_record_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(counter.read_text(), "1")

            invalidators = (
                {"factory_sha": "c" * 40},
                {"factory_sha": "c" * 40, "factory_tree": "e" * 40},
                {
                    "factory_sha": "c" * 40,
                    "factory_tree": "e" * 40,
                    "product_sha": "f" * 40,
                },
                {
                    "factory_sha": "c" * 40,
                    "factory_tree": "e" * 40,
                    "product_sha": "f" * 40,
                    "product_tree": "d" * 40,
                },
                {
                    "factory_sha": "c" * 40,
                    "factory_tree": "e" * 40,
                    "product_sha": "f" * 40,
                    "product_tree": "d" * 40,
                    "contract_version": "1.9.0",
                },
                {
                    "factory_sha": "c" * 40,
                    "factory_tree": "e" * 40,
                    "product_sha": "f" * 40,
                    "product_tree": "d" * 40,
                    "contract_version": "1.9.0",
                    "network_reviewed": True,
                },
            )
            prior_digest = second_result["phases"][0]["input_sha256"]
            for expected, changes in enumerate(invalidators, 2):
                completed, result = self.run_plan(root, [phase], **changes)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertFalse(result["phases"][0]["cache_hit"])
                self.assertNotEqual(
                    result["phases"][0]["input_sha256"], prior_digest
                )
                prior_digest = result["phases"][0]["input_sha256"]
                self.assertEqual(counter.read_text(), str(expected))

            changed_phase = {
                **phase,
                "command": [sys.executable, str(helper), str(counter), "changed"],
            }
            completed, result = self.run_plan(
                root, [changed_phase], factory_sha="c" * 40,
                factory_tree="e" * 40, product_sha="f" * 40,
                product_tree="d" * 40, contract_version="1.9.0",
                network_reviewed=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(result["phases"][0]["cache_hit"])
            self.assertNotEqual(result["phases"][0]["input_sha256"], prior_digest)
            self.assertEqual(counter.read_text(), "8")

            mismatched = dict(self.runtime())
            mismatched["npm"] = "99.0.0"
            completed, result = self.run_plan(
                root, [changed_phase], factory_sha="c" * 40,
                factory_tree="e" * 40, product_sha="f" * 40,
                product_tree="d" * 40, contract_version="1.9.0",
                network_reviewed=True,
                runtime=mismatched,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIsNone(result)
            failure = json.loads(completed.stderr)["failure"]
            self.assertEqual(failure["reason_code"], "runtime_tuple_mismatch")
            self.assertEqual(failure["field"], "npm")
            self.assertEqual(counter.read_text(), "8")

    def test_artifact_drift_reruns_and_tampered_evidence_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.txt").write_text("exact")
            helper = root / "build.py"
            helper.write_text(
                "import pathlib\n"
                "count=pathlib.Path('count')\n"
                "count.write_text(str(int(count.read_text())+1) if count.exists() else '1')\n"
                "pathlib.Path('artifact.txt').write_text(pathlib.Path('source.txt').read_text())\n"
            )
            consumer = root / "consume.py"
            consumer.write_text(
                "import pathlib\n"
                "count=pathlib.Path('consumer.count')\n"
                "count.write_text(str(int(count.read_text())+1) if count.exists() else '1')\n"
                "pathlib.Path('consumed.txt').write_text(pathlib.Path('artifact.txt').read_text())\n"
            )
            phases = [
                {
                    "artifacts": ["artifact.txt"],
                    "command": [sys.executable, str(helper)],
                    "depends_on": [],
                    "name": "build",
                    "reuse": "artifacts",
                },
                {
                    "artifacts": ["consumed.txt"],
                    "command": [sys.executable, str(consumer)],
                    "depends_on": ["build"],
                    "name": "consume",
                    "reuse": "artifacts",
                },
            ]
            self.run_plan(root, phases)
            _, cached = self.run_plan(root, phases)
            self.assertTrue(all(phase["cache_hit"] for phase in cached["phases"]))
            (root / "source.txt").write_text("changed")
            (root / "artifact.txt").write_text("drift")
            completed, rerun = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(any(phase["cache_hit"] for phase in rerun["phases"]))
            self.assertEqual((root / "count").read_text(), "2")
            self.assertEqual((root / "consumer.count").read_text(), "2")

            evidence = (
                root / "results" / "certification-phases" / "build" / "evidence.json"
            )
            value = json.loads(evidence.read_text())
            value["phase"]["output_sha256"] = "0" * 64
            evidence.write_text(json.dumps(value))
            os.chmod(evidence, 0o600)
            completed, _ = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("phase evidence is invalid", completed.stderr)
            self.assertEqual((root / "count").read_text(), "2")

    def test_interrupted_restart_reuses_only_completed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            helper = root / "interrupt.py"
            helper.write_text(
                "import pathlib,sys,time\n"
                "name,delay=sys.argv[1],float(sys.argv[2])\n"
                "count=pathlib.Path(name+'.count')\n"
                "count.write_text(str(int(count.read_text())+1) if count.exists() else '1')\n"
                "time.sleep(delay)\n"
                "pathlib.Path(name+'.ready').write_text('ready')\n"
            )
            phases = [
                {
                    "artifacts": ["fast.ready"],
                    "command": [sys.executable, str(helper), "fast", "0"],
                    "depends_on": [],
                    "name": "fast",
                    "network": "denied",
                    "reuse": "artifacts",
                },
                {
                    "artifacts": ["slow.ready"],
                    "command": [sys.executable, str(helper), "slow", "1"],
                    "depends_on": [],
                    "name": "slow",
                    "network": "denied",
                    "reuse": "artifacts",
                },
            ]
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema": "nysa.software-factory.certification-plan/v2",
                "phases": phases,
                "runtime": self.runtime(),
            }))
            result = root / "results" / "result.json"
            runtime = self.runtime()
            environment = {
                **os.environ,
                "FACTORY_KIT_SHA": "a" * 40,
                "FACTORY_KIT_TREE": "c" * 40,
                "FACTORY_PRODUCT_SHA": "d" * 40,
                "FACTORY_PRODUCT_TREE": "b" * 40,
                "FACTORY_CONTRACT_VERSION": "1.8.0",
                "FACTORY_CERTIFICATION_TUPLE": json.dumps({
                    "contract_version": "1.8.0",
                    "factory_sha": "a" * 40,
                    "factory_tree": "c" * 40,
                    "node": runtime["node"],
                    "npm": runtime["npm"],
                    "product_sha": "d" * 40,
                    "product_tree": "b" * 40,
                }),
                "FACTORY_CERTIFICATION_NETWORK_REVIEWED": "0",
            }
            running = subprocess.Popen(
                [sys.executable, str(RUNNER), "--plan", str(plan),
                 "--result", str(result), "--workers", "2"],
                cwd=root, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            evidence = (
                root / "results" / "certification-phases" / "fast" / "evidence.json"
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if evidence.exists():
                    break
                time.sleep(0.05)
            else:
                running.kill()
                running.communicate(timeout=2)
                self.fail("fast phase evidence was not persisted")
            running.terminate()
            running.communicate(timeout=5)
            self.assertEqual(running.returncode, 143)
            self.assertFalse((root / "slow.ready").exists())
            interrupted = json.loads(result.read_text())
            self.assertEqual(interrupted["status"], "fail")
            self.assertFalse(
                (
                    root / "results" / "certification-phases" / "slow"
                    / "evidence.json"
                ).exists()
            )
            completed = subprocess.run(
                [sys.executable, str(RUNNER), "--plan", str(plan),
                 "--result", str(result), "--workers", "2"],
                cwd=root, env=environment, text=True, capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            value = json.loads(result.read_text())
            by_name = {phase["name"]: phase for phase in value["phases"]}
            self.assertTrue(by_name["fast"]["cache_hit"])
            self.assertFalse(by_name["slow"]["cache_hit"])
            self.assertEqual((root / "fast.count").read_text(), "1")
            self.assertEqual((root / "slow.count").read_text(), "2")

    def test_missing_runtime_is_a_typed_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "phases": [{
                    "artifacts": [],
                    "command": [sys.executable, "-c", "raise SystemExit(99)"],
                    "depends_on": [],
                    "name": "fixture",
                    "network": "denied",
                }],
                "runtime": self.runtime(),
                "schema": "nysa.software-factory.certification-plan/v2",
            }))
            result = root / "result.json"
            environment = os.environ.copy()
            environment.update(
                FACTORY_KIT_SHA="a" * 40,
                FACTORY_KIT_TREE="c" * 40,
                FACTORY_PRODUCT_SHA="d" * 40,
                FACTORY_PRODUCT_TREE="b" * 40,
                FACTORY_CONTRACT_VERSION="1.8.0",
                FACTORY_CERTIFICATION_TUPLE=json.dumps({
                    "contract_version": "1.8.0",
                    "factory_sha": "a" * 40,
                    "factory_tree": "c" * 40,
                    "node": self.runtime()["node"],
                    "npm": self.runtime()["npm"],
                    "product_sha": "d" * 40,
                    "product_tree": "b" * 40,
                }),
                FACTORY_CERTIFICATION_NETWORK_REVIEWED="0",
                PATH=str(root),
            )
            completed = subprocess.run(
                [sys.executable, str(RUNNER), "--plan", str(plan),
                 "--result", str(result), "--workers", "1"],
                cwd=root, env=environment, text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(result.exists())
            self.assertEqual(
                json.loads(completed.stderr)["failure"]["reason_code"],
                "runtime_tuple_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
