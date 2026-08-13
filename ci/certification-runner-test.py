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
CACHE = ROOT / "scripts/lib/certification_cache.py"


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
        cache_input: Path | None = None,
        cache_output: Path | None = None,
        extra_environment: dict[str, str] | None = None,
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
        if cache_input is not None or cache_output is not None:
            result.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        if cache_input is not None:
            environment["FACTORY_CERTIFICATION_CACHE_INPUT"] = str(cache_input)
        if cache_output is not None:
            environment["FACTORY_CERTIFICATION_CACHE_OUTPUT"] = str(cache_output)
        if extra_environment:
            environment.update(extra_environment)
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

    def publish_cache_command(self, store: Path, source: Path, plan: Path) -> list[str]:
        runtime = self.runtime()
        return [
            sys.executable, str(CACHE), "publish", "--store", str(store),
            "--source", str(source), "--plan", str(plan),
            "--factory-sha", "a" * 40, "--factory-tree", "c" * 40,
            "--product-sha", "d" * 40, "--product-tree", "b" * 40,
            "--contract-version", "1.8.0", "--runtime-tuple", json.dumps({
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "factory_tree": "c" * 40,
                "node": runtime["node"],
                "npm": runtime["npm"],
                "product_sha": "d" * 40,
                "product_tree": "b" * 40,
            }),
        ]

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
                    "kind": "build",
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
            invalid["phases"][0]["artifacts"] = ["dist", "dist/output.bin"]
            invalid["phases"][0]["kind"] = "build"
            invalid["phases"][0]["reuse"] = "artifacts"
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
            self.assertEqual(
                json.loads(completed.stderr)["failure"]["reason_code"],
                "certification_plan_invalid",
            )

            invalid["phases"][0]["artifacts"] = ["report"]
            invalid["phases"][0]["kind"] = "test"
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
            self.assertEqual(
                json.loads(completed.stderr)["failure"]["reason_code"],
                "certification_plan_invalid",
            )

            invalid = json.loads(plan.read_text())
            invalid["phases"][0]["artifacts"] = ["artifact"]
            invalid["phases"][0]["kind"] = "build"
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
                "kind": "dependencies",
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

    def test_required_phase_sandboxes_select_exact_network_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trace = root / "profiles"
            wrapper = root / "prefix.py"
            wrapper.write_text(
                "import os,pathlib,sys\n"
                "label,trace,*command=sys.argv[1:]\n"
                "with pathlib.Path(trace).open('a') as stream: stream.write(label+'\\n')\n"
                "os.execvp(command[0], command)\n"
            )
            prefixes = {
                "FACTORY_CERTIFICATION_PHASE_SANDBOX_REQUIRED": "1",
                "FACTORY_CERTIFICATION_NETWORK_DENY_PREFIX": json.dumps([
                    sys.executable, str(wrapper), "deny", str(trace),
                ]),
                "FACTORY_CERTIFICATION_NETWORK_ALLOW_PREFIX": json.dumps([
                    sys.executable, str(wrapper), "allow", str(trace),
                ]),
            }
            phases = [
                {
                    "artifacts": [],
                    "command": ["true"],
                    "depends_on": [],
                    "name": "checks",
                    "network": "denied",
                },
                {
                    "artifacts": [],
                    "command": ["true"],
                    "depends_on": [],
                    "name": "dependencies",
                    "network": "required",
                },
            ]
            completed, result = self.run_plan(
                root, phases, network_reviewed=True,
                extra_environment=prefixes,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(sorted(trace.read_text().splitlines()), ["allow", "deny"])
            self.assertEqual(result["status"], "pass")

            trace.unlink()
            prefixes.pop("FACTORY_CERTIFICATION_NETWORK_DENY_PREFIX")
            completed, result = self.run_plan(
                root, phases, network_reviewed=True,
                extra_environment=prefixes,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIsNone(result)
            self.assertFalse(trace.exists())

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
                "kind": "build",
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
                    "contract_version": "2.0.0",
                },
                {
                    "factory_sha": "c" * 40,
                    "factory_tree": "e" * 40,
                    "product_sha": "f" * 40,
                    "product_tree": "d" * 40,
                    "contract_version": "2.0.0",
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
                product_tree="d" * 40, contract_version="2.0.0",
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
                product_tree="d" * 40, contract_version="2.0.0",
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
                    "kind": "build",
                    "name": "build",
                    "reuse": "artifacts",
                },
                {
                    "artifacts": ["consumed.txt"],
                    "command": [sys.executable, str(consumer)],
                    "depends_on": ["build"],
                    "kind": "build",
                    "name": "consume",
                    "reuse": "artifacts",
                },
            ]
            self.run_plan(root, phases)
            _, cached = self.run_plan(root, phases)
            self.assertTrue(all(phase["cache_hit"] for phase in cached["phases"]))

            (root / "artifact.txt").chmod(0o755)
            completed, mode_drift = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(any(phase["cache_hit"] for phase in mode_drift["phases"]))
            self.assertEqual((root / "count").read_text(), "2")
            self.assertEqual((root / "consumer.count").read_text(), "2")

            (root / "source.txt").write_text("changed")
            (root / "artifact.txt").write_text("drift")
            completed, rerun = self.run_plan(root, phases)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(any(phase["cache_hit"] for phase in rerun["phases"]))
            self.assertEqual((root / "count").read_text(), "3")
            self.assertEqual((root / "consumer.count").read_text(), "3")

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
            self.assertEqual((root / "count").read_text(), "3")

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
                    "kind": "build",
                    "name": "fast",
                    "network": "denied",
                    "reuse": "artifacts",
                },
                {
                    "artifacts": ["slow.ready"],
                    "command": [sys.executable, str(helper), "slow", "1"],
                    "depends_on": [],
                    "kind": "build",
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

    def test_authenticated_artifact_reuse_crosses_disposable_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            helper = base / "build.py"
            counter = base / "count"
            helper.write_text(
                "import os,pathlib,sys,time\n"
                "time.sleep(0.25)\n"
                "counter=pathlib.Path(sys.argv[1])\n"
                "counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')\n"
                "artifact=pathlib.Path('generated/nested/output.bin')\n"
                "artifact.parent.mkdir(parents=True)\n"
                "artifact.write_text('exact')\n"
                "artifact.chmod(0o755)\n"
            )
            phase = {
                "artifacts": ["generated/nested"],
                "command": [sys.executable, str(helper), str(counter)],
                "depends_on": [],
                "kind": "build",
                "name": "build",
                "reuse": "artifacts",
            }
            first_root = base / "first"
            first_root.mkdir()
            first_output = first_root / "results/cache-output"
            first, first_result = self.run_plan(
                first_root, [phase], cache_output=first_output,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(first_result["phases"][0]["cache_hit"])
            self.assertEqual(
                first_result["phases"][0]["saved_phase_wall_seconds"], 0,
            )
            self.assertEqual(counter.read_text(), "1")

            store = base / "store"
            store.mkdir(mode=0o700)
            publish_command = self.publish_cache_command(
                store, first_output, first_root / "plan.json",
            )
            publishers = [
                subprocess.Popen(
                    publish_command,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]
            for process in publishers:
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
            entries = list((store / "entries").glob("[0-9a-f]" * 64))
            self.assertEqual(len(entries), 1)
            self.assertFalse((entries[0] / "output.log").exists())

            second_root = base / "second"
            second_root.mkdir()
            second_input = second_root / "results/cache-input"
            second_input.parent.mkdir(mode=0o700)
            prepared = subprocess.run(
                [sys.executable, str(CACHE), "prepare", "--store", str(store),
                 "--destination", str(second_input)],
                text=True, capture_output=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            second, second_result = self.run_plan(
                second_root, [phase], cache_input=second_input,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            reused = second_result["phases"][0]
            self.assertTrue(reused["cache_hit"])
            self.assertEqual(
                reused["saved_phase_wall_seconds"],
                first_result["phases"][0]["wall_seconds"],
            )
            self.assertGreater(reused["cache_overhead_seconds"], 0)
            self.assertEqual(reused["wall_seconds"], reused["cache_overhead_seconds"])
            self.assertGreater(
                reused["saved_phase_wall_seconds"],
                reused["cache_overhead_seconds"],
            )
            self.assertIn("cache_overhead=", second.stdout)
            self.assertIn("saved_phase=", second.stdout)
            self.assertEqual(counter.read_text(), "1")
            restored = second_root / "generated/nested/output.bin"
            self.assertEqual(restored.read_text(), "exact")
            self.assertEqual(restored.stat().st_mode & 0o777, 0o755)

            drift_root = base / "drift"
            drift_root.mkdir()
            drift_input = drift_root / "results/cache-input"
            drift_input.parent.mkdir(mode=0o700)
            prepared = subprocess.run(
                [sys.executable, str(CACHE), "prepare", "--store", str(store),
                 "--destination", str(drift_input)],
                text=True, capture_output=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            drifted, drifted_result = self.run_plan(
                drift_root, [phase], product_tree="e" * 40,
                cache_input=drift_input,
            )
            self.assertEqual(drifted.returncode, 0, drifted.stderr)
            self.assertFalse(drifted_result["phases"][0]["cache_hit"])
            self.assertEqual(counter.read_text(), "2")

            cached_artifact = entries[0] / "artifacts/generated/nested/output.bin"
            cached_artifact.chmod(0o600)
            cached_artifact.write_text("tampered")
            tamper_root = base / "tamper"
            tamper_root.mkdir()
            tamper_input = tamper_root / "results/cache-input"
            tamper_input.parent.mkdir(mode=0o700)
            prepared = subprocess.run(
                [sys.executable, str(CACHE), "prepare", "--store", str(store),
                 "--destination", str(tamper_input)],
                text=True, capture_output=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            tampered, tampered_result = self.run_plan(
                tamper_root, [phase], cache_input=tamper_input,
            )
            self.assertEqual(tampered.returncode, 0, tampered.stderr)
            self.assertFalse(tampered_result["phases"][0]["cache_hit"])
            self.assertEqual(counter.read_text(), "3")

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
