#!/usr/bin/env python3
"""Human CLI contract: preferences route; the exact launcher authorizes."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "factory-cli.py"
SPEC = importlib.util.spec_from_file_location("factory_cli", CLI_PATH)
CLI = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLI)


class FactoryHumanCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.targets = self.root / "targets"
        self.targets.mkdir(mode=0o700)
        self.selection = self.root / "current-target"
        self.log = self.root / "launcher.log"
        self.snapshots = self.root / "snapshots.json"
        self.values = {
            "production": {
                "doctor": {
                    "checks": {
                        "controller": {"status": "ok"},
                        "isolated_provider": {
                            "active_attempts": 0,
                            "status": "ok",
                            "unknown_workers": 0,
                        },
                        "runtime": {"max_concurrent_tickets": 3, "status": "ok"},
                    },
                    "overall_status": "ok",
                    "schema": "nysa.software-factory.doctor/v2",
                },
                "workflow": {
                    "label": "Nysa",
                    "mode": "production",
                    "project": "alpha",
                    "schema": "factory-operator-workflow/v1",
                    "tickets": [
                        {
                            "depends_on": ["T-99"],
                            "priority": "high",
                            "state": "Backlog",
                            "ticket": "T-10",
                            "title": "Blocked admin controls",
                        },
                        {
                            "depends_on": [],
                            "priority": "high",
                            "state": "Backlog",
                            "ticket": "T-2",
                            "title": "Ship invoice export",
                        },
                        {
                            "depends_on": [],
                            "priority": "urgent",
                            "state": "Backlog",
                            "ticket": "T-4",
                            "title": "Fix \u001b[31msecurity\r\nnow",
                        },
                        {
                            "depends_on": [],
                            "priority": "normal",
                            "state": "Awaiting Approval",
                            "ticket": "T-8",
                            "title": "Approve account recovery",
                        },
                        {
                            "depends_on": [],
                            "priority": "urgent",
                            "state": "Done",
                            "ticket": "T-7",
                            "title": "Already shipped",
                        },
                    ],
                },
            },
            "qualification": {
                "doctor": {
                    "checks": {},
                    "overall_status": "ok",
                    "schema": "nysa.software-factory.doctor/v2",
                },
                "workflow": {
                    "label": "Nysa",
                    "mode": "qualification",
                    # Production and qualification can legitimately share a slug.
                    "project": "alpha",
                    "schema": "factory-operator-workflow/v1",
                    "tickets": [
                        {
                            "depends_on": [],
                            "priority": "high",
                            "state": "Awaiting Approval",
                            "ticket": f"T-{number}",
                            "title": title,
                        }
                        for number, title in (
                            (21, "Add invoice export"),
                            (22, "Fix account recovery"),
                            (23, "Improve onboarding"),
                        )
                    ],
                },
            },
        }
        self.write_snapshots()
        self.launchers = {
            lane: self.make_launcher(lane) for lane in ("production", "qualification")
        }
        for lane in self.launchers:
            self.write_target(lane)

    def tearDown(self):
        self.temporary.cleanup()

    def write_snapshots(self):
        self.snapshots.write_text(
            json.dumps(self.values, sort_keys=True) + "\n", encoding="utf-8"
        )

    def make_launcher(self, lane):
        launcher = self.root / f"{lane}-factory-launch"
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"lane = {lane!r}\n"
            f"snapshots = pathlib.Path({str(self.snapshots)!r})\n"
            f"log = pathlib.Path({str(self.log)!r})\n"
            "args = sys.argv[1:]\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps({'lane': lane, 'argv': args}) + '\\n')\n"
            "values = json.loads(snapshots.read_text(encoding='utf-8'))[lane]\n"
            "project, command = args[0], args[1:]\n"
            "if project != values['workflow']['project']:\n"
            "    raise SystemExit('wrong project')\n"
            "if command == ['operator-snapshot', 'workflow', '--json']:\n"
            "    result = values['workflow']\n"
            "elif command == ['doctor', '--json']:\n"
            "    result = values['doctor']\n"
            "elif command == ['qualification-finish', '--json']:\n"
            "    result = values.get('qualification_result', {'project': project, 'status': 'green'})\n"
            "elif len(command) == 5 and command[:3] == ['operator', 'ready', '--ticket'] and command[4] == '--json':\n"
            "    result = {'project': project, 'status': 'pass', 'ticket': command[3]}\n"
            "elif len(command) == 5 and command[:3] == ['operator', 'approve', '--ticket'] and command[4] == '--json':\n"
            "    result = {'project': project, 'status': 'pass', 'ticket': command[3]}\n"
            "else:\n"
            "    raise SystemExit('unexpected command: ' + repr(command))\n"
            "print(json.dumps(result))\n"
            "if command == ['doctor', '--json'] and result.get('overall_status') == 'error':\n"
            "    raise SystemExit(1)\n"
            "if command == ['qualification-finish', '--json']:\n"
            "    status = result.get('status')\n"
            "    if status == 'error': raise SystemExit(2)\n"
            "    if isinstance(status, str) and status in {'waiting', 'blocked'}: raise SystemExit(3)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher

    def write_target(self, lane):
        target = self.targets / f"{lane}.json"
        target.write_text(
            json.dumps({"launcher": str(self.launchers[lane]), "project": "alpha"})
            + "\n",
            encoding="utf-8",
        )
        target.chmod(0o600)

    def select(self, target):
        self.selection.write_text(target + "\n", encoding="utf-8")
        self.selection.chmod(0o600)

    def invoke(self, arguments, supplied=""):
        output, error = io.StringIO(), io.StringIO()
        code = CLI.run(
            arguments,
            targets_dir=self.targets,
            selection_file=self.selection,
            stdin=io.StringIO(supplied),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def invocations(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def qualification_launcher(self, tag, project="alpha"):
        temporary = tempfile.TemporaryDirectory(
            prefix=f"nysa-sf-qualification.{tag}.", dir="/private/tmp",
        )
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        sha = "a" * 40
        release = root / "releases" / sha
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        (root / "releases").chmod(0o700)
        launcher = scripts / "factory-launch"
        payload = scripts / "result.json"
        payload.write_text('{"status":"ok"}\n', encoding="utf-8")
        payload.chmod(0o444)
        launcher.write_text(
            '#!/bin/sh\ncat "$(dirname "$0")/result.json"\n', encoding="utf-8",
        )
        launcher.chmod(0o555)
        tree = CLI._git_tree(release)
        scripts.chmod(0o555)
        release.chmod(0o555)
        projects = root / "projects" / project
        receipts = root / "receipts"
        projects.mkdir(parents=True, mode=0o700)
        (root / "projects").chmod(0o700)
        receipts.mkdir(mode=0o700)
        product = root / "product"
        product.mkdir(mode=0o700)
        subprocess.run(["/usr/bin/git", "init", "-q", str(product)], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(product), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(product), "config", "user.name", "Factory Test"],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(product), "remote", "add", "origin",
                "https://github.com/test/product.git",
            ],
            check=True,
        )
        (product / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(
            ["/usr/bin/git", "-C", str(product), "add", "README.md"], check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(product), "commit", "-qm", "fixture"],
            check=True,
        )
        product_sha, product_tree = subprocess.run(
            ["/usr/bin/git", "-C", str(product), "rev-parse", "HEAD", "HEAD^{tree}"],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        authority = root / "authority"
        controller = authority / "controller"
        provider = authority / "provider"
        operator = authority / "operator"
        for path in (authority, controller, provider, operator):
            path.mkdir(mode=0o700)
        operator_map = operator / "operator-map.json"
        operator_map.write_text("{}\n", encoding="utf-8")
        operator_map.chmod(0o600)
        runtime_ledger = operator / "runtime-ledger.csv"
        runtime_ledger.write_text("", encoding="utf-8")
        runtime_ledger.chmod(0o600)
        shared = {
            "contract_version": "2.0.0",
            "kit_sha": sha,
            "kit_tree": tree,
            "product_path": str(product),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": project,
            "provider_policy_sha256": "d" * 64,
            "fallback_readiness_sha256": "e" * 64,
            "qualification_mode": "isolated",
            "operator_map_path": str(operator_map),
            "controller_state_path": str(controller),
            "provider_state_path": str(provider),
            "runtime_ledger_path": str(runtime_ledger),
        }
        receipt = {
            **shared, "product_origin": "https://github.com/test/product.git",
            "status": "pass",
        }
        receipt_id = hashlib.sha256(CLI._canonical(receipt)).hexdigest()
        receipt["receipt_id"] = receipt_id
        active = {
            **shared,
            "receipt_id": receipt_id,
            "release_path": str(release),
        }
        authority_value = {
            "contract_version": shared["contract_version"],
            "controller_state_path": shared["controller_state_path"],
            "factory_sha": sha,
            "factory_tree": tree,
            "manifest_sha256": "f" * 64,
            "operator_map_path": shared["operator_map_path"],
            "product_origin": receipt["product_origin"],
            "product_path": str(product),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": project,
            "provider_state_path": shared["provider_state_path"],
            "runtime_ledger_path": shared["runtime_ledger_path"],
            "runtime_tuple": None,
            "schema": "nysa.software-factory.qualification-authority/v1",
        }
        authority_value["authority_sha256"] = hashlib.sha256(
            CLI._canonical(authority_value)
        ).hexdigest()
        authority_path = authority / "authority.json"
        authority_path.write_bytes(CLI._canonical(authority_value))
        authority_path.chmod(0o600)
        for path, value in (
            (
                root / "marker.json",
                {
                    "mode": "qualification",
                    "schema": "nysa.software-factory.qualification-environment/v1",
                },
            ),
            (projects / "active.json", active),
            (receipts / f"{receipt_id}.json", receipt),
        ):
            path.write_bytes(CLI._canonical(value))
            path.chmod(0o600)
        return launcher

    def test_use_lists_friendly_targets_without_exposing_internal_routing(self):
        code, output, error = self.invoke(["use"], "2\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Nysa · Production", output)
        self.assertIn("Nysa · Qualification · 3 tickets", output)
        self.assertNotIn("alpha", output)
        self.assertNotIn(str(self.launchers["production"]), output)
        self.assertNotIn(str(self.launchers["qualification"]), output)
        self.assertEqual(self.selection.read_text(encoding="utf-8"), "qualification\n")
        self.assertEqual(stat.S_IMODE(self.selection.stat().st_mode), 0o600)

    def test_use_ignores_malformed_targets_without_rewriting_them(self):
        invalid = {
            "BAD!.json": b'{"launcher":"sentinel-name"}\n',
            "malformed.json": b"not-json\n",
            "nested.json": b"[" * 1500 + b"]" * 1500,
            "unsafe.json": b'{"launcher":"sentinel-mode","project":"alpha"}\n',
        }
        for name, raw in invalid.items():
            path = self.targets / name
            path.write_bytes(raw)
            path.chmod(0o644 if name == "unsafe.json" else 0o600)
        before = {
            name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for name in invalid for path in (self.targets / name,)
        }

        code, output, error = self.invoke(["use"], "1\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Ignored 4 unavailable or invalid target records", output)
        self.assertIn("Nysa · Production", output)
        self.assertIn("Nysa · Qualification", output)
        self.assertNotIn("sentinel", output)
        self.assertEqual(self.selection.read_text(encoding="utf-8"), "production\n")
        self.assertEqual(
            before,
            {
                name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for name in invalid for path in (self.targets / name,)
            },
        )

        for lane in self.launchers:
            (self.targets / f"{lane}.json").unlink()
        code, output, error = self.invoke(["use"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("no valid targets", error)
        self.assertIn("Factory setup or qualification preparation", error)
        self.assertNotIn("sentinel", error)

    def test_backlog_is_read_only_ranked_and_uses_the_selected_launcher(self):
        self.select("production")
        code, output, error = self.invoke(["backlog"])
        self.assertEqual((code, error), (0, ""))
        self.assertLess(output.index("T-4"), output.index("T-2"))
        self.assertLess(output.index("T-2"), output.index("T-10"))
        self.assertIn("Fix security now", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("T-7", output)
        self.assertNotIn("Already shipped", output)
        self.assertEqual(
            self.invocations(),
            [
                {
                    "lane": "production",
                    "argv": ["alpha", "operator-snapshot", "workflow", "--json"],
                }
            ],
        )

    def test_doctor_keeps_its_name_and_uses_the_selected_launcher(self):
        self.select("production")
        code, output, error = self.invoke(["doctor"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Doctor passed", output)
        self.assertNotIn("Health", output)
        self.assertEqual(
            self.invocations(),
            [{"lane": "production", "argv": ["alpha", "doctor", "--json"]}],
        )

    def test_doctor_failure_names_the_check_and_exact_evidence_command(self):
        self.values["production"]["doctor"] = {
            "checks": {
                "runtime": {
                    "reason_code": "stale_runs", "stale_runs": 1,
                    "status": "error",
                },
            },
            "overall_status": "error",
            "project": "alpha",
            "schema": "nysa.software-factory.doctor/v2",
        }
        self.write_snapshots()
        self.select("production")
        code, output, error = self.invoke(["doctor"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("runtime=error", error)
        self.assertNotIn("stale_runs", error)
        self.assertIn("Impact: do not continue Factory mutations", error)
        self.assertIn(str(self.launchers["production"]), error)
        self.assertIn("alpha doctor --json", error)

        with mock.patch.object(
            CLI, "_invoke", side_effect=CLI.LauncherRefused("launcher_refused"),
        ):
            code, output, error = self.invoke(["doctor"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("Doctor could not produce a report", error)
        self.assertIn("Impact: do not continue Factory mutations", error)
        self.assertIn(str(self.launchers["production"]), error)
        self.assertIn("alpha doctor --json", error)
        self.assertNotIn("launcher_refused", error)

    def test_doctor_refuses_contradictions_and_redacts_malformed_values(self):
        self.select("production")
        for report in (
            {
                "checks": {"runtime": {"status": "error"}},
                "overall_status": "ok",
                "schema": "nysa.software-factory.doctor/v2",
            },
            {
                "checks": [], "overall_status": "ok",
                "schema": "nysa.software-factory.doctor/v2",
            },
            {
                "checks": {"runtime": {"status": {}}},
                "overall_status": "ok",
                "schema": "nysa.software-factory.doctor/v2",
            },
            {
                "checks": {"runtime": {"status": []}},
                "overall_status": "ok",
                "schema": "nysa.software-factory.doctor/v2",
            },
            {
                "checks": {"runtime": {"status": "ok"}},
                "overall_status": {},
                "schema": "nysa.software-factory.doctor/v2",
            },
        ):
            self.values["production"]["doctor"] = report
            self.write_snapshots()
            code, _, error = self.invoke(["doctor"])
            self.assertEqual(code, 2)
            self.assertIn("Doctor report is invalid", error)

        warning = {
            "checks": {"clis": {"status": "unknown"}},
            "overall_status": "warning",
            "schema": "nysa.software-factory.doctor/v2",
        }
        with mock.patch.object(CLI, "_invoke", return_value=(warning, 0)):
            code, _, error = self.invoke(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("Doctor warning: clis=unknown", error)

        for report, returncode in (
            ({**warning, "overall_status": "error"}, 0),
            (warning, 1),
            ({
                "checks": {"clis": {"status": "blocked"}},
                "overall_status": "error",
                "schema": "nysa.software-factory.doctor/v2",
            }, 1),
        ):
            with mock.patch.object(
                CLI, "_invoke", return_value=(report, returncode),
            ):
                code, _, error = self.invoke(["doctor"])
            self.assertEqual(code, 2)
            self.assertIn("Doctor report is invalid", error)
        self.values["production"]["doctor"] = {
            "checks": {
                "runtime": {
                    "reason_code": "fake-secret-sentinel",
                    "status": "error",
                },
            },
            "overall_status": "error",
            "error": "fake-secret-sentinel",
            "schema": "nysa.software-factory.doctor/v2",
        }
        self.write_snapshots()
        code, _, error = self.invoke(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("runtime=error", error)
        self.assertNotIn("fake-secret-sentinel", error)

        self.values["production"]["doctor"] = {
            "checks": {
                "isolated_provider": {
                    "status": "ok", "unknown_workers": "fake-secret-sentinel",
                },
            },
            "overall_status": "ok",
            "schema": "nysa.software-factory.doctor/v2",
        }
        self.write_snapshots()
        code, _, error = self.invoke(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("Doctor report is invalid", error)
        self.assertNotIn("fake-secret-sentinel", error)

    def test_common_stop_paths_name_safe_recovery_without_raw_values(self):
        self.select("production")
        self.values["production"]["workflow"]["mode"] = "broken"
        self.write_snapshots()
        code, _, error = self.invoke(["backlog"])
        self.assertEqual(code, 2)
        self.assertIn("workflow snapshot is invalid", error)
        self.assertIn("Impact: do not continue Factory mutations", error)
        self.assertIn("operator-snapshot workflow --json", error)
        self.assertIn("run factory doctor", error)

        self.values["production"]["workflow"]["mode"] = "production"
        self.write_snapshots()
        with mock.patch.object(
            CLI, "_call", side_effect=CLI.LauncherRefused(
                "fake-secret-sentinel", {"reason": "fake-secret-sentinel"},
            ),
        ):
            code, _, error = self.invoke(["backlog"])
        self.assertEqual(code, 2)
        self.assertIn("workflow snapshot could not be produced", error)
        self.assertNotIn("fake-secret-sentinel", error)

        self.select("qualification")
        self.values["qualification"]["qualification_result"] = {
            "project": "alpha", "reason": "fake-secret-sentinel",
            "status": "waiting",
        }
        self.write_snapshots()
        code, _, error = self.invoke([], "1\nyes\n")
        self.assertEqual(code, 2)
        self.assertIn("Qualification is waiting", error)
        self.assertIn("run factory doctor", error)
        self.assertIn("rerun factory", error)
        self.assertNotIn("fake-secret-sentinel", error)

        with mock.patch.object(
            CLI, "_selected", side_effect=CLI.CliError(
                "production target trust evidence is invalid",
            ),
        ):
            code, _, error = self.invoke(["doctor"])
        self.assertEqual(code, 2)
        self.assertEqual(error, "factory: selected target is unusable; run factory use\n")

        with tempfile.TemporaryDirectory(
            prefix="factory-missing-targets.", dir="/private/tmp",
        ) as raw:
            root = Path(raw)
            output, stderr = io.StringIO(), io.StringIO()
            code = CLI.run(
                ["use"], targets_dir=root / "missing",
                selection_file=root / "current-target", stdout=output,
                stderr=stderr,
            )
            self.assertEqual((code, output.getvalue()), (2, ""))
            self.assertIn("Factory setup or qualification preparation", stderr.getvalue())
            stderr = io.StringIO()
            code = CLI.run(
                ["bogus"], targets_dir=root / "missing",
                selection_file=root / "current-target", stdout=io.StringIO(),
                stderr=stderr,
            )
            self.assertEqual(code, 2)
            self.assertIn("usage: factory", stderr.getvalue())

    def test_invalid_text_depth_and_account_home_are_bounded(self):
        self.launchers["production"].write_text(
            "#!/usr/bin/python3\nimport sys\nsys.stdout.buffer.write(b'\\xff')\n",
            encoding="utf-8",
        )
        self.launchers["production"].chmod(0o700)
        self.select("production")
        code, _, error = self.invoke(["doctor"])
        self.assertEqual(code, 2)
        self.assertIn("Doctor could not produce a report", error)
        self.assertIn("Impact: do not continue Factory mutations", error)
        self.assertNotIn("codec", error)

        code, output, error = self.invoke(["use"], "1\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Ignored 1 unavailable or invalid target records", output)
        self.assertIn("Qualification", output)

        nested = self.root / "nested-factory-launch"
        nested.write_text(
            "#!/usr/bin/python3\nprint('[' * 1500 + ']' * 1500)\n",
            encoding="utf-8",
        )
        nested.chmod(0o700)
        launcher = CLI._launcher(str(nested))
        try:
            with self.assertRaisesRegex(CLI.CliError, "invalid JSON"):
                CLI._invoke(launcher, "alpha", ["doctor", "--json"])
        finally:
            launcher.close()

        with mock.patch.object(
            CLI, "_account_home", side_effect=CLI.CliError(
                "account home is unavailable",
            ),
        ):
            output, stderr = io.StringIO(), io.StringIO()
            code = CLI.run(["doctor"], stdout=output, stderr=stderr)
        self.assertEqual((code, output.getvalue()), (2, ""))
        self.assertEqual(stderr.getvalue(), "factory: account home is unavailable\n")

    def test_bare_command_closes_one_qualification_cohort_without_extra_doctor(self):
        self.select("qualification")
        code, output, error = self.invoke([], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        for title in ("Add invoice export", "Fix account recovery", "Improve onboarding"):
            self.assertIn(title, output)
        calls = self.invocations()
        self.assertEqual(
            [call for call in calls if call["argv"][1:] == ["qualification-finish", "--json"]],
            [
                {
                    "lane": "qualification",
                    "argv": ["alpha", "qualification-finish", "--json"],
                }
            ],
        )
        self.assertFalse(any(call["argv"][1:] == ["doctor", "--json"] for call in calls))

    def test_bare_command_approves_exact_selected_production_ticket(self):
        self.select("production")
        code, output, error = self.invoke([], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Approve account recovery", output)
        self.assertEqual(
            self.invocations()[-1],
            {
                "lane": "production",
                "argv": [
                    "alpha",
                    "operator",
                    "approve",
                    "--ticket",
                    "T-8",
                    "--json",
                ],
            },
        )

    def test_next_recommends_highest_eligible_ticket_and_can_mark_ready(self):
        self.values["production"]["workflow"]["tickets"] = [
            ticket
            for ticket in self.values["production"]["workflow"]["tickets"]
            if ticket["state"] != "Awaiting Approval"
        ]
        self.write_snapshots()
        self.select("production")
        code, output, error = self.invoke(["next"], "1\nyes\n")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Fix security now", output)
        self.assertEqual(
            self.invocations()[-1],
            {
                "lane": "production",
                "argv": [
                    "alpha",
                    "operator",
                    "ready",
                    "--ticket",
                    "T-4",
                    "--json",
                ],
            },
        )

    def test_stale_target_and_malformed_ticket_refuse_before_mutation(self):
        self.select("production")
        (self.targets / "production.json").unlink()
        code, _, error = self.invoke(["backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("select", error.lower())
        self.assertEqual(self.invocations(), [])

        self.select("qualification")
        self.values["qualification"]["workflow"]["tickets"][0]["ticket"] = "../../bad"
        self.write_snapshots()
        code, _, error = self.invoke([], "1\nyes\n")
        self.assertNotEqual(code, 0)
        self.assertIn("ticket", error.lower())
        self.assertFalse(
            any(
                call["argv"][1:] == ["qualification-finish", "--json"]
                for call in self.invocations()
            )
        )

    def test_unhashable_workflow_fields_and_result_status_refuse_cleanly(self):
        original = json.loads(json.dumps(self.values["production"]["workflow"]))
        for field, value in (("mode", {}), ("priority", {}), ("state", [])):
            with self.subTest(field=field):
                workflow = json.loads(json.dumps(original))
                if field == "mode":
                    workflow[field] = value
                else:
                    workflow["tickets"][0][field] = value
                self.values["production"]["workflow"] = workflow
                self.write_snapshots()
                self.select("production")
                code, _, error = self.invoke(["backlog"])
                self.assertEqual(code, 2)
                self.assertIn("invalid", error)

        self.values["qualification"]["qualification_result"] = {
            "project": "alpha", "status": {},
        }
        self.write_snapshots()
        self.select("qualification")
        code, _, error = self.invoke([], "1\nyes\n")
        self.assertEqual(code, 2)
        self.assertIn("qualification result is invalid", error)

        original_invoke = CLI._invoke
        for status, returncode in (
            ("green", 1), ("waiting", 0), ("blocked", 2), ("error", 3),
        ):
            def contradictory(launcher, project, arguments):
                if arguments == ["qualification-finish", "--json"]:
                    return {"project": project, "status": status}, returncode
                return original_invoke(launcher, project, arguments)

            with mock.patch.object(CLI, "_invoke", side_effect=contradictory):
                code, _, error = self.invoke([], "1\nyes\n")
            self.assertEqual(code, 2)
            self.assertIn("qualification result is invalid", error)
            self.assertIn("outcome is unknown", error)
            self.assertIn("do not repeat", error)

    def test_post_confirmation_exceptions_report_unknown_outcome(self):
        original = CLI._invoke
        for failure in (
            CLI.CliError("FAKE_CLI_SENTINEL"),
            OSError("FAKE_OS_SENTINEL"),
            UnicodeError("FAKE_UNICODE_SENTINEL"),
            RecursionError("FAKE_RECURSION_SENTINEL"),
        ):
            def fail_mutation(launcher, project, arguments):
                if arguments[0] in {"qualification-finish", "operator"}:
                    raise failure
                return original(launcher, project, arguments)

            with mock.patch.object(CLI, "_invoke", side_effect=fail_mutation):
                for target in ("qualification", "production"):
                    self.select(target)
                    code, _, error = self.invoke([], "1\nyes\n")
                    self.assertEqual(code, 2)
                    self.assertIn("outcome is unknown", error)
                    self.assertIn("do not repeat", error)
                    self.assertIn("run factory doctor", error)
                    self.assertNotIn("FAKE_", error)

    def test_selection_and_target_preferences_cannot_be_symlinks(self):
        target = self.root / "selection-target"
        target.write_text("production\n", encoding="utf-8")
        target.chmod(0o600)
        self.selection.symlink_to(target)
        code, _, error = self.invoke(["backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("factory use", error.lower())
        self.assertEqual(self.invocations(), [])

        self.selection.unlink()
        self.select("production")
        record = self.targets / "production.json"
        real_record = self.root / "production-target.json"
        record.rename(real_record)
        record.symlink_to(real_record)
        code, _, error = self.invoke(["backlog"])
        self.assertNotEqual(code, 0)
        self.assertIn("target", error.lower())
        self.assertEqual(self.invocations(), [])

    def test_trusted_production_target_accepts_only_an_exact_sealed_release(self):
        home = self.root / "home"
        sha = "a" * 40
        release = home / ".factory/kits/releases" / sha
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        launcher_path = scripts / "factory-launch"
        raw = b"#!/bin/sh\n"
        launcher_path.write_bytes(raw)
        launcher_path.chmod(0o500)
        active = home / ".factory/kits/projects/alpha/active.json"
        manifest = home / f".factory/kits/manifests/{sha}.json"
        active.parent.mkdir(parents=True)
        manifest.parent.mkdir(parents=True)
        active.write_text(json.dumps({"kit_sha": sha, "project": "alpha"}), encoding="utf-8")
        manifest.write_text(json.dumps({
            "git_tree": "b" * 40,
            "kit_sha": sha,
            "launcher_sha256": CLI.hashlib.sha256(raw).hexdigest(),
            "schema_version": 1,
            "sealed_release_path": str(release),
        }), encoding="utf-8")
        active.chmod(0o600)
        manifest.chmod(0o600)
        lock = home / ".factory/.launcher-pin.lock"
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o600)
        launcher = CLI._launcher(str(launcher_path))
        try:
            with mock.patch.object(CLI, "_account_home", return_value=home):
                CLI._trusted_launcher(launcher, "alpha")
            self.assertEqual(launcher.lock_path, lock)
            with mock.patch.object(CLI, "_account_home", return_value=home):
                with self.assertRaises(CLI.CliError):
                    CLI._trusted_launcher(launcher, "beta")
            manifest.write_text(json.dumps({
                "git_tree": "b" * 40,
                "kit_sha": sha,
                "launcher_sha256": "0" * 64,
                "schema_version": 1,
                "sealed_release_path": str(release),
            }), encoding="utf-8")
            manifest.chmod(0o600)
            with mock.patch.object(CLI, "_account_home", return_value=home):
                with self.assertRaisesRegex(CLI.CliError, "trust evidence"):
                    CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()

    def test_qualification_target_requires_active_receipt_and_exact_tree(self):
        launcher_path = self.qualification_launcher("trust")
        launcher = CLI._launcher(str(launcher_path))
        try:
            CLI._trusted_launcher(launcher, "alpha")
            payload = launcher_path.parent / "result.json"
            payload.chmod(0o644)
            payload.write_text('{"status":"changed"}\n', encoding="utf-8")
            payload.chmod(0o444)
            with self.assertRaisesRegex(CLI.CliError, "trust evidence"):
                CLI._invoke(launcher, "alpha", ["doctor", "--json"])
        finally:
            launcher.close()

        launcher_path = self.qualification_launcher("writable")
        launcher_path.parents[1].chmod(0o700)
        launcher = CLI._launcher(str(launcher_path))
        try:
            with self.assertRaisesRegex(CLI.CliError, "sealed qualification release"):
                CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()

        launcher_path = self.qualification_launcher("contract")
        root = launcher_path.parents[3]
        active_path = root / "projects/alpha/active.json"
        active = json.loads(active_path.read_text(encoding="utf-8"))
        receipt_path = root / f"receipts/{active['receipt_id']}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.pop("receipt_id")
        active["contract_version"] = receipt["contract_version"] = "9.9.9"
        receipt_id = hashlib.sha256(CLI._canonical(receipt)).hexdigest()
        receipt["receipt_id"] = active["receipt_id"] = receipt_id
        receipt_path.unlink()
        receipt_path = root / f"receipts/{receipt_id}.json"
        receipt_path.write_bytes(CLI._canonical(receipt))
        receipt_path.chmod(0o600)
        active_path.write_bytes(CLI._canonical(active))
        active_path.chmod(0o600)
        launcher = CLI._launcher(str(launcher_path))
        try:
            with self.assertRaisesRegex(CLI.CliError, "trust evidence"):
                CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()

        launcher_path = self.qualification_launcher("symlink")
        release = launcher_path.parents[1]
        release.chmod(0o755)
        (release / "escape").symlink_to("/private/tmp")
        release.chmod(0o555)
        launcher = CLI._launcher(str(launcher_path))
        try:
            with self.assertRaisesRegex(CLI.CliError, "sealed qualification release"):
                CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()
            release.chmod(0o755)
            (release / "escape").unlink()
            release.chmod(0o555)

        launcher_path = self.qualification_launcher("changed")
        launcher_path.chmod(0o700)
        launcher_path.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        launcher_path.chmod(0o555)
        launcher = CLI._launcher(str(launcher_path))
        try:
            with self.assertRaisesRegex(CLI.CliError, "trust evidence"):
                CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()

        arbitrary = self.qualification_launcher("missing", project="other")
        launcher = CLI._launcher(str(arbitrary))
        try:
            with self.assertRaises(CLI.CliError):
                CLI._trusted_launcher(launcher, "alpha")
        finally:
            launcher.close()

    def test_registration_retires_malformed_and_legacy_qualification_targets(self):
        launcher = self.qualification_launcher("retire")
        targets = self.root / "retire-targets"
        targets.mkdir(mode=0o700)
        malformed = targets / "qualification-stale.json"
        malformed.write_text("{}\n", encoding="utf-8")
        malformed.chmod(0o600)
        malformed_name = targets / "qualification-BAD.json"
        malformed_name.write_text("{}\n", encoding="utf-8")
        malformed_name.chmod(0o600)
        legacy = targets / "legacy.json"
        legacy.write_text(json.dumps({
            "launcher": str(launcher), "project": "alpha",
        }) + "\n", encoding="utf-8")
        legacy.chmod(0o600)
        CLI.register("qualification-current", str(launcher), "alpha", targets)
        self.assertEqual(
            [path.name for path in targets.iterdir()],
            ["qualification-current.json"],
        )

    def test_registration_refuses_replaced_target_directory_without_residue(self):
        launcher = self.qualification_launcher("swap")
        targets = self.root / "swap-targets"
        displaced = self.root / "displaced-targets"
        targets.mkdir(mode=0o700)
        original = CLI._registry_lock

        def replace_after_lock(path):
            parent, directory = original(path)
            path.rename(displaced)
            path.mkdir(mode=0o700)
            return parent, directory

        with mock.patch.object(CLI, "_registry_lock", side_effect=replace_after_lock):
            with self.assertRaisesRegex(CLI.CliError, "directory changed"):
                CLI.register(
                    "qualification-current", str(launcher), "alpha", targets,
                )
        self.assertEqual(list(targets.iterdir()), [])
        self.assertEqual(list(displaced.iterdir()), [])

    def test_registration_never_rolls_back_over_a_newer_target(self):
        launcher_path = self.qualification_launcher("foreign")
        targets = self.root / "foreign-targets"
        targets.mkdir(mode=0o700)
        target = targets / "qualification-current.json"
        candidate = CLI._launcher(str(launcher_path))
        calls = 0

        def replace_after_publish():
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text('{"foreign":"newer"}\n', encoding="utf-8")
                target.chmod(0o600)
                raise CLI.CliError("forced post-publish failure")

        with mock.patch.object(CLI, "_launcher", return_value=candidate), mock.patch.object(
            CLI, "_trusted_launcher",
        ), mock.patch.object(candidate, "check", side_effect=replace_after_publish):
            with self.assertRaisesRegex(CLI.CliError, "outcome is unknown"):
                CLI.register(
                    "qualification-current", str(launcher_path), "alpha", targets,
                )
        self.assertEqual(
            target.read_text(encoding="utf-8"), '{"foreign":"newer"}\n',
        )

    def test_registration_replays_after_atomic_publish_response_loss(self):
        launcher = self.qualification_launcher("publish-loss")
        targets = self.root / "publish-loss-targets"
        targets.mkdir(mode=0o700)
        original = CLI._atomic_at

        def fail_after_publish(directory, name, raw):
            original(directory, name, raw)
            raise OSError("forced response loss after publication")

        with mock.patch.object(CLI, "_atomic_at", side_effect=fail_after_publish):
            with self.assertRaisesRegex(CLI.CliError, "outcome is unknown"):
                CLI.register(
                    "qualification-current", str(launcher), "alpha", targets,
                )
        CLI.register("qualification-current", str(launcher), "alpha", targets)
        self.assertEqual(
            json.loads(
                (targets / "qualification-current.json").read_text(
                    encoding="utf-8",
                )
            ),
            {"launcher": str(launcher), "project": "alpha"},
        )

    def test_concurrent_qualification_registration_leaves_one_target(self):
        launchers = [
            self.qualification_launcher("race-a"),
            self.qualification_launcher("race-b"),
        ]
        targets = self.root / "race-targets"
        targets.mkdir(mode=0o700)
        source = (
            "import importlib.util,pathlib,sys;"
            "s=importlib.util.spec_from_file_location('factory_cli',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "m.register(sys.argv[2],sys.argv[3],sys.argv[4],pathlib.Path(sys.argv[5]))"
        )
        for _ in range(8):
            for path in targets.glob("qualification-race-*.json"):
                path.unlink()
            processes = [
                subprocess.Popen([
                    sys.executable, "-c", source, str(CLI_PATH),
                    f"qualification-race-{index}", str(launcher), "alpha",
                    str(targets),
                ])
                for index, launcher in enumerate(launchers)
            ]
            self.assertEqual([process.wait() for process in processes], [0, 0])
            self.assertEqual(
                len(list(targets.glob("qualification-race-*.json"))), 1,
            )
        self.assertEqual(
            {path.suffix for path in targets.iterdir()}, {".json"},
        )


if __name__ == "__main__":
    unittest.main()
