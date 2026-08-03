#!/usr/bin/env python3
"""Focused authenticated passport continuity tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


STATE = module("state_machine", ROOT / "scripts/state-machine.py")
PASSPORT = module("ticket_passport", ROOT / "scripts/ticket-passport.py")
ROLE_OUTPUT = module("role_output", ROOT / "scripts/lib/role_output.py")


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class TicketPassportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.remote = self.root / "remote.git"
        run("git", "init", "--bare", "-q", str(self.remote), cwd=self.root)
        self.product = self.root / "product"
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/PROJECT.env").write_text(
            'GH_REPO=nysa-company/relay-factory\nTEST_PATHS="app/tests/"\n',
            encoding="utf-8",
        )
        (self.product / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Planning\n", encoding="utf-8"
        )
        (self.product / "factory/route-plans/T-110.json").write_text(
            f'{{"kit_sha":"{"a" * 40}","ticket":"T-110"}}\n',
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n", encoding="utf-8"
        )
        run("git", "init", "-q", "-b", "ticket/T-110", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        run("git", "push", "-qu", "origin", "HEAD:main", cwd=self.product)
        run("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.state_dir = STATE.safe_state_dir(self.root / "controller")
        self.state_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            kit_dir=ROOT,
            lease="",
            project="relay",
            receipt="",
            require_used=False,
            role="planner",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.passport_args = argparse.Namespace(
            action="export",
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            project="relay",
            publication_state="none",
            receipt="",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.origin = mock.patch.dict(
            os.environ, {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote)}
        )
        self.origin.start()

    def tearDown(self) -> None:
        self.origin.stop()
        self.temporary.cleanup()

    def terminal(
        self,
        run_id: str,
        role: str,
        receipt: str,
        factory_sha: str,
        content: bytes | None = None,
    ) -> None:
        output_path = self.product / f"factory/runs/{run_id}.out"
        published = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(output_path),
            ],
            input=content if content is not None else f"{role} output\n".encode(),
            capture_output=True,
            check=True,
        )
        output = published.stdout.decode().strip()
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "task_submitted=1\n"
            "effective_cost=1.500000\n"
            "exit_status=0\n"
            "ticket=T-110\n"
            f"role={role}\n"
            "role_exit=ok\n"
            f"role_head_before={run('git', 'rev-parse', 'HEAD', cwd=self.product)}\n"
            f"kit_sha={factory_sha}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output}\n",
            encoding="utf-8",
        )

    def test_role_output_uses_one_streaming_eight_mib_bound(self) -> None:
        existing_size = 5_662_048
        self.terminal(
            "run-existing",
            "planner",
            "e" * 64,
            "a" * 40,
            b"x" * existing_size,
        )
        completed, charges = PASSPORT.run_evidence(
            self.product / "factory", "T-110"
        )
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(charges), 1)
        self.assertEqual(
            (self.product / "factory/runs/run-existing.out").stat().st_size,
            existing_size,
        )
        existing = self.product / "factory/runs/run-existing.out"
        os.chmod(existing, 0o644)
        self.assertEqual(
            len(PASSPORT.run_charges(self.product / "factory", "T-110")), 1
        )
        with self.assertRaisesRegex(ValueError, "unsafe role output"):
            PASSPORT.run_evidence(self.product / "factory", "T-110")
        os.chmod(existing, 0o600)

        refused = self.product / "factory/runs/run-refused.out"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(refused),
            ],
            input=b"x" * (ROLE_OUTPUT.MAX_BYTES + 1),
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 8)
        self.assertIn(b"ROLE_OUTPUT_INVALID", result.stderr)
        self.assertEqual(
            refused.read_text(encoding="utf-8"),
            "ROLE_OUTPUT_INVALID: role output exceeds 8388608-byte limit\n",
        )
        self.assertEqual(
            result.stdout.decode().strip(),
            hashlib.sha256(refused.read_bytes()).hexdigest(),
        )
        symlink_target = self.root / "unrelated"
        symlink_target.write_text("untouched\n", encoding="utf-8")
        replacement = self.product / "factory/runs/replaced.out"
        replacement.symlink_to(symlink_target)
        replaced = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lib/role_output.py"),
                "publish",
                str(replacement),
            ],
            input=b"wrapper output\n",
            capture_output=True,
            check=True,
        )
        self.assertEqual(symlink_target.read_text(encoding="utf-8"), "untouched\n")
        self.assertFalse(replacement.is_symlink())
        self.assertEqual(replacement.read_bytes(), b"wrapper output\n")
        self.assertEqual(
            replaced.stdout.decode().strip(),
            hashlib.sha256(replacement.read_bytes()).hexdigest(),
        )

        oversized = self.product / "factory/runs/run-existing.out"
        oversized.write_bytes(b"x" * (ROLE_OUTPUT.MAX_BYTES + 1))
        os.chmod(oversized, 0o600)
        with self.assertRaisesRegex(ValueError, "8388608-byte limit"):
            PASSPORT.run_evidence(self.product / "factory", "T-110")

    def test_run_agent_terminalizes_oversized_role_output(self) -> None:
        factory_sha = run("git", "rev-parse", "HEAD", cwd=ROOT)
        release = self.root / "release"
        shutil.copytree(ROOT / "scripts", release / "scripts")
        (release / "integrations/hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations/hermes/contract.json",
            release / "integrations/hermes/contract.json",
        )
        adapter = release / "scripts/adapters/mock.sh"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            "python3 - <<'PY'\n"
            "import sys\n"
            "sys.stdout.buffer.write(b'x' * (8 * 1024 * 1024 + 1))\n"
            "PY\n",
            encoding="utf-8",
        )
        os.chmod(adapter, 0o755)
        release_tree = run(
            "bash",
            "-c",
            'source "$1"; factory_directory_tree "$2"',
            "_",
            str(ROOT / "scripts/lib/kit-pin.sh"),
            str(release),
            cwd=self.root,
        )

        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=1.00\n"
            "PER_TICKET_BUDGET_USD=20.00\n"
            "PER_RUN_MAX_TURNS=5\n"
            "PER_RUN_TIMEOUT_MIN=1\n"
            "DAILY_CAP_USD=50.00\n",
            encoding="utf-8",
        )
        (self.product / "factory/KIT_PIN").write_text(
            factory_sha + "\n", encoding="utf-8"
        )
        (self.product / "factory/ledger.csv").write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            f"# T-110\n\nState: Ready\n\nKit-SHA: {factory_sha}\n",
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n"
            "factory/runtime-ledger.csv\n"
            "factory/.active-runs/\n"
            "factory/.launch.lock/\n"
            "factory/.provider.lock/\n"
            "factory/.ledger.lock/\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "oversized output fixture", cwd=self.product)
        run(
            "git",
            "push",
            "-qu",
            "origin",
            "HEAD:ticket/T-110",
            cwd=self.product,
        )

        transition_state = STATE.safe_state_dir(self.root / "run-state")
        transition_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha=factory_sha,
            kit_dir=release,
            lease="",
            project="output-test",
            receipt="",
            require_used=False,
            role="planner",
            state_dir=transition_state,
            ticket="T-110",
            workdir=self.product,
        )
        transition = STATE.issue(transition_args, "RUN planner")
        transition_args.receipt = transition["receipt_sha256"]
        STATE.verify(transition_args, consume=True)

        environment = dict(os.environ)
        environment.update({
            "FACTORY_ADAPTER_OVERRIDE": "mock",
            "FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote),
            "FACTORY_GLOBAL_ENV": str(self.root / "missing-global.env"),
            "FACTORY_PROJECT": "output-test",
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_SHA": factory_sha,
            "FACTORY_RELEASE_TREE": release_tree,
            "FACTORY_ROOT": str(self.product),
            "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_TRANSITION_RECEIPT_SHA256":
                transition["receipt_sha256"],
            "FACTORY_TRANSITION_STATE_DIR": str(transition_state),
        })
        result = subprocess.run(
            [
                str(release / "scripts/run-agent.sh"),
                "--role",
                "planner",
                "--ticket",
                "T-110",
                "--",
                "oversized output",
            ],
            cwd=self.product,
            env=environment,
            capture_output=True,
            check=False,
            # The real oversized-output path hashes, terminalizes, and cleans a
            # multi-megabyte artifact. Keep this harness bound above its
            # measured parallel-suite runtime; production bounds are unchanged.
            timeout=90,
        )
        self.assertEqual(result.returncode, 11, result.stderr.decode())
        self.assertIn(b"ROLE_OUTPUT_INVALID", result.stderr)
        manifests = list((self.product / "factory/runs").glob("*.meta"))
        self.assertEqual(len(manifests), 1)
        fields = PASSPORT.manifest_fields(manifests[0])
        self.assertEqual(fields["accounting_state"], "abandoned_conservative")
        self.assertEqual(fields["effective_cost"], "1.00")
        self.assertEqual(fields["exit_status"], "11")
        self.assertEqual(fields["role_exit"], "role_exit_invalid_output")
        output = manifests[0].with_suffix(".out")
        self.assertEqual(
            fields["output_sha256"],
            hashlib.sha256(output.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            "ROLE_OUTPUT_INVALID: role output exceeds 8388608-byte limit\n",
        )

    def test_passport_chains_receipts_without_replay_or_double_charge(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(exported["cumulative_charges_micro_usd"], 1_500_000)
        self.assertEqual(len(exported["completed_role_evidence"]), 1)

        validated = PASSPORT.validate(self.passport_args, secret)
        self.assertEqual(validated["passport_sha256"], exported["passport_sha256"])
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

        self.passport_args.factory_sha = "b" * 40
        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["factory_sha"], "b" * 40)
        self.assertEqual(len(migrated["migration_history"]), 1)

        self.state_args.factory_sha = "b" * 40
        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.assertRegex(second["passport_sha256"], r"^[0-9a-f]{64}$")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "b" * 40
        )
        self.passport_args.receipt = second["receipt_sha256"]
        upgraded = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(upgraded["cumulative_charges_micro_usd"], 3_000_000)
        self.assertEqual(len(upgraded["completed_role_evidence"]), 2)
        self.assertEqual(
            upgraded["migration_history"], migrated["migration_history"]
        )
        self.assertEqual(
            [item["factory_sha"] for item in upgraded["factory_release_history"]],
            ["a" * 40, "b" * 40],
        )

    def test_terminal_export_accepts_exact_authenticated_release_migration(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )

        route = self.product / "factory/route-plans/T-110.json"
        route.write_text(
            f'{{"kit_sha":"{"b" * 40}","ticket":"T-110"}}\n',
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "migrate release", cwd=self.product)
        self.passport_args.factory_sha = "b" * 40
        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(
            migrated["parent_file_sha256"], second["passport_sha256"]
        )

        self.passport_args.receipt = second["receipt_sha256"]
        passport = self.state_dir / "passports/T-110.json"
        before = passport.read_bytes()
        clean_route = route.read_text(encoding="utf-8")
        route.write_text(clean_route + "dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(PASSPORT.PassportError, "clean execution cell"):
            PASSPORT.export(self.passport_args, secret)
        self.assertEqual(passport.read_bytes(), before)
        route.write_text(clean_route, encoding="utf-8")
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)

    def test_terminal_export_accepts_only_a_contiguous_migration_suffix(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )

        route = self.product / "factory/route-plans/T-110.json"
        for factory_sha in ("b" * 40, "c" * 40):
            route.write_text(
                json.dumps({"kit_sha": factory_sha, "ticket": "T-110"}) + "\n",
                encoding="utf-8",
            )
            run("git", "add", str(route), cwd=self.product)
            run("git", "commit", "-qm", f"migrate to {factory_sha[0]}", cwd=self.product)
            self.passport_args.factory_sha = factory_sha
            migrated = PASSPORT.migrate(self.passport_args, secret)

        self.passport_args.receipt = second["receipt_sha256"]
        passport = self.state_dir / "passports/T-110.json"
        manifest = self.product / "factory/runs/run-2.meta"
        terminal = manifest.read_text(encoding="utf-8")
        manifest.write_text(
            terminal.replace(f"kit_sha={'a' * 40}", f"kit_sha={'f' * 40}"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PASSPORT.PassportError, "terminal role evidence is missing"
        ):
            PASSPORT.export(self.passport_args, secret)
        manifest.write_text(terminal, encoding="utf-8")

        protected = self.root / "protected-base-advance"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        marker = protected / "factory/base-advance"
        marker.write_text("new protected base\n", encoding="utf-8")
        run("git", "add", str(marker), cwd=protected)
        run("git", "commit", "-qm", "advance protected base", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)
        migrated = PASSPORT.migrate(self.passport_args, secret)

        unsigned = {
            name: item for name, item in migrated.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        unsigned["migration_history"] = [
            dict(item) for item in unsigned["migration_history"]
        ]
        unsigned["migration_history"][0]["from_passport_file_sha256"] = "f" * 64
        PASSPORT.write_atomic(passport, PASSPORT.authenticate(unsigned, secret))
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

        PASSPORT.write_atomic(passport, migrated)
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)
        self.assertEqual(len(exported["charge_records"]), 2)

    def test_protected_authorization_bridges_one_exact_legacy_snapshot(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        PASSPORT.export(self.passport_args, secret)

        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "a" * 40
        )
        terminal = self.product / "factory/runs/run-2.meta"
        terminal.write_text(
            terminal.read_text(encoding="utf-8").replace(
                "accounting_state=completed\n",
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=1.500000\n"
                "cost_basis=conservative_reservation\n",
            ),
            encoding="utf-8",
        )

        route = self.product / "factory/route-plans/T-110.json"
        for factory_sha in ("b" * 40, "c" * 40):
            route.write_text(
                json.dumps({"kit_sha": factory_sha, "ticket": "T-110"}) + "\n",
                encoding="utf-8",
            )
            run("git", "add", str(route), cwd=self.product)
            run("git", "commit", "-qm", f"legacy migrate to {factory_sha[0]}", cwd=self.product)
            self.passport_args.factory_sha = factory_sha
            migrated = PASSPORT.migrate(self.passport_args, secret)

        unsigned = {
            name: item for name, item in migrated.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        legacy_fields = {
            "from_factory_sha", "from_head_sha", "from_protected_base_sha",
            "to_factory_sha", "to_head_sha", "to_protected_base_sha",
        }
        unsigned["migration_history"] = [
            {name: item[name] for name in legacy_fields}
            for item in unsigned["migration_history"]
        ]
        legacy = PASSPORT.authenticate(unsigned, secret)
        passport = self.state_dir / "passports/T-110.json"
        PASSPORT.write_atomic(passport, legacy)

        route.write_text(
            json.dumps({"kit_sha": "d" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "target legacy bridge", cwd=self.product)
        self.passport_args.factory_sha = "d" * 40
        self.passport_args.receipt = second["receipt_sha256"]
        authorization = PASSPORT.authorize_lineage(self.passport_args, secret)
        self.assertEqual(
            authorization["terminal"]["accounting_state"],
            "abandoned_conservative",
        )

        protected = self.root / "protected-lineage"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        relative = PASSPORT.lineage_authorization_path("d" * 40, "T-110")
        path = protected / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(PASSPORT.canonical(authorization))
        application = protected / "factory/PROJECT.env"
        application.write_text(
            application.read_text(encoding="utf-8") + "UNRELATED_DRIFT=1\n",
            encoding="utf-8",
        )
        run("git", "add", relative, str(application), cwd=protected)
        run("git", "commit", "-qm", "mix authorization with application", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        with self.assertRaisesRegex(
            PASSPORT.PassportError, "lineage authorization is invalid"
        ):
            PASSPORT.migrate(self.passport_args, secret)

        route.write_text(
            json.dumps({"kit_sha": "e" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "retarget exact legacy bridge", cwd=self.product)
        self.passport_args.factory_sha = "e" * 40
        authorization = PASSPORT.authorize_lineage(self.passport_args, secret)
        relative = PASSPORT.lineage_authorization_path("e" * 40, "T-110")
        path = protected / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PASSPORT.canonical(authorization))
        run("git", "add", relative, cwd=protected)
        run("git", "commit", "-qm", "authorize exact legacy bridge", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)
        bridged = PASSPORT.migrate(self.passport_args, secret)
        edge = bridged["migration_history"][-1]
        self.assertRegex(
            edge["lineage_authorization_sha256"], r"^[0-9a-f]{64}$"
        )
        tampered = {
            name: item for name, item in bridged.items()
            if name not in {"authentication_sha256", "passport_sha256"}
        }
        tampered["migration_history"] = [
            dict(item) for item in tampered["migration_history"]
        ]
        tampered["migration_history"][-1][
            "lineage_authorization_sha256"
        ] = "e" * 64
        PASSPORT.write_atomic(
            passport, PASSPORT.authenticate(tampered, secret)
        )
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)
        PASSPORT.write_atomic(passport, bridged)
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(
            [item["role"] for item in exported["completed_role_evidence"]],
            ["planner", "spec-linter"],
        )
        self.assertEqual(exported["cumulative_charges_micro_usd"], 3_000_000)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

    def test_protected_inflight_authorization_allows_exact_rewrite(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Planning", "State: Blocked-Escalated"
            ) + "\nResume-State: Planning\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize blocked ticket", cwd=self.product)
        secret = PASSPORT.key(self.state_dir)
        receipt = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = receipt["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", receipt["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = receipt["receipt_sha256"]
        previous = PASSPORT.export(self.passport_args, secret)

        rewritten = run(
            "git", "commit-tree", "HEAD^{tree}", "-m", "authorized rewrite",
            cwd=self.product,
        )
        run("git", "reset", "--hard", rewritten, cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.factory_sha = "b" * 40
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        protected = self.root / "protected"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        authorization = protected / (
            "factory/migrations/inflight-release/" + "b" * 40 + ".json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(
            json.dumps({
                "repository": "nysa-company/relay-factory",
                "schema": PASSPORT.INFLIGHT_SCHEMA,
                "source_kit_sha": "a" * 40,
                "target_kit_sha": "b" * 40,
                "tickets": [{
                    "branch": "ticket/T-110",
                    "head": rewritten,
                    "state": "Blocked-Escalated",
                    "ticket": "T-110",
                }],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize rewrite", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["parent_digest"], previous["passport_sha256"])
        self.assertEqual(migrated["factory_sha"], "b" * 40)
        self.assertEqual(migrated["current_state"], "Blocked-Escalated")

    def test_protected_same_release_test_rewrite_is_exact_and_charged(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text("# T-110\n\nState: Building\n", encoding="utf-8")
        test = self.product / "app/tests/detail.test.js"
        test.parent.mkdir(parents=True)
        test.write_text("old test\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "enter building", cwd=self.product)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=self.product)

        secret = PASSPORT.key(self.state_dir)
        prior_receipt = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = prior_receipt["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-prior", "planner", prior_receipt["receipt_sha256"], "a" * 40
        )
        self.passport_args.receipt = prior_receipt["receipt_sha256"]
        previous = PASSPORT.export(self.passport_args, secret)
        old_head = previous["head_sha"]

        self.state_args.role = "test-author"
        repair = STATE.issue(self.state_args, "FIX test-author")
        self.state_args.receipt = repair["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        output = self.product / "factory/runs/run-repair.out"
        output.write_text("repair output\n", encoding="utf-8")
        os.chmod(output, 0o600)
        output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
        (self.product / "factory/runs/run-repair.meta").write_text(
            "run_id=run-repair\n"
            "phase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\n"
            "effective_cost=2.000000\n"
            "exit_status=11\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "role_exit=role_exit_push_failed\n"
            f"role_head_before={old_head}\n"
            f"kit_sha={'a' * 40}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={repair['receipt_sha256']}\n"
            f"output_sha256={output_digest}\n",
            encoding="utf-8",
        )

        test.write_text("repaired test\n", encoding="utf-8")
        ticket.write_text(
            "# T-110\n\nState: Building\n\nTest-author repair recorded.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        tree = run("git", "write-tree", cwd=self.product)
        rewritten = run(
            "git", "commit-tree", tree, "-m", "test repair rewrite",
            cwd=self.product,
        )
        run("git", "reset", "--hard", rewritten, cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        (self.product / "app/server.js").write_text(
            "unknown semantic change\n", encoding="utf-8"
        )
        run("git", "add", ".", cwd=self.product)
        unsafe_tree = run("git", "write-tree", cwd=self.product)
        unsafe = run(
            "git", "commit-tree", unsafe_tree, "-m", "unsafe rewrite",
            cwd=self.product,
        )
        self.assertFalse(PASSPORT.rewrite_delta_allowed(
            self.product, old_head, unsafe, "app/tests/", "T-110"
        ))
        run("git", "reset", "--hard", rewritten, cwd=self.product)

        protected = self.root / "protected-rewrite"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        authorization = (
            protected / f"factory/migrations/ticket-rewrite/{rewritten}.json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(
            json.dumps({
                "branch": "ticket/T-110",
                "factory_sha": "a" * 40,
                "head": rewritten,
                "passport_sha256": previous["passport_sha256"],
                "previous_head": old_head,
                "repository": "nysa-company/relay-factory",
                "role": "test-author",
                "route_plan_sha256": hashlib.sha256(
                    (self.product / "factory/route-plans/T-110.json").read_bytes()
                ).hexdigest(),
                "schema": PASSPORT.REWRITE_SCHEMA,
                "state": "Building",
                "ticket": "T-110",
                "transition_receipt_sha256": repair["receipt_sha256"],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize exact test rewrite", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["factory_sha"], "a" * 40)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["cumulative_charges_micro_usd"], 3_500_000)
        self.assertEqual(len(migrated["completed_role_evidence"]), 1)
        self.assertRegex(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_accepted_late_test_merge_history_normalizes_with_exact_evidence(self) -> None:
        protected = self.root / "protected-normalization"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        (protected / "protected.txt").write_text("protected base\n", encoding="utf-8")
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "protected base advance", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        base = run("git", "rev-parse", "HEAD", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\n\n"
            "## Frozen contract — version 6\n"
            "- **Freeze result — PASS.** Contract version 6 is frozen.\n",
            encoding="utf-8",
        )
        test = self.product / "app/tests/detail.test.js"
        test.parent.mkdir(parents=True)
        test.write_text("v6 initial\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author v6 initial", cwd=self.product)
        implementation = self.product / "app/server.js"
        implementation.write_text("v6 implementation\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "builder v6", cwd=self.product)

        self.state_args.role = "test-author"
        repair = STATE.issue(self.state_args, "FIX test-author")
        self.state_args.receipt = repair["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "accepted-late-test", "test-author", repair["receipt_sha256"],
            "a" * 40,
        )
        test.write_text("v6 initial\nv6 accepted late repair\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author accepted late v6 repair", cwd=self.product)
        run(
            "git", "merge", "-q", "--no-ff", "origin/main", "-m",
            "merge protected base", cwd=self.product,
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\n## Frozen contract — version 7\n"
            + "- **Freeze result — PASS.** Contract version 7 is frozen.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "planner v7", cwd=self.product)
        test.write_text(test.read_text(encoding="utf-8") + "v7\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "test-author v7", cwd=self.product)
        implementation.write_text("v6 implementation\nv7 implementation\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "builder v7", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\n## Frozen contract — version 8\n"
            + "- **Freeze result — PASS.** Contract version 8 is frozen.\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "planner v8", cwd=self.product)

        secret = PASSPORT.key(self.state_dir)
        self.passport_args.receipt = repair["receipt_sha256"]
        predecessor = PASSPORT.export(self.passport_args, secret)
        old_head = predecessor["head_sha"]
        run(
            "git", "push", "-q", "origin",
            f"{old_head}:refs/heads/ticket/T-110", cwd=self.product,
        )
        self.passport_args.factory_sha = "b" * 40
        previous = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(previous["factory_sha"], "b" * 40)
        self.assertEqual(previous["head_sha"], old_head)
        self.assertIn(
            {"contract_version": "1.8.0", "factory_sha": "a" * 40},
            previous["factory_release_history"],
        )
        old_evidence = previous["completed_role_evidence"]
        old_charges = previous["charge_records"]
        subprocess.run(
            [
                "bash", str(ROOT / "scripts/reorder-test-fixes.sh"),
                "--base", base, "--test-paths", "app/tests/",
                "--exempt-paths", "factory/",
            ],
            cwd=self.product, text=True, capture_output=True, check=True,
        )
        rewritten = run("git", "rev-parse", "HEAD", cwd=self.product)
        self.assertNotEqual(rewritten, old_head)
        self.assertEqual(run("git", "rev-parse", "HEAD^{tree}", cwd=self.product), previous["head_tree"])
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        authorization = (
            protected / f"factory/migrations/ticket-rewrite/{rewritten}.json"
        )
        authorization.parent.mkdir(parents=True, exist_ok=True)
        authorization.write_text(json.dumps({
            "accepted_test_factory_sha": "a" * 40,
            "accepted_test_receipt_sha256": repair["receipt_sha256"],
            "accepted_test_run_id": "accepted-late-test",
            "base": base,
            "branch": "ticket/T-110",
            "factory_sha": "b" * 40,
            "head": rewritten,
            "head_tree": previous["head_tree"],
            "mode": "accepted-push-history-normalization",
            "passport_sha256": previous["passport_sha256"],
            "previous_head": old_head,
            "previous_tree": previous["head_tree"],
            "repository": "nysa-company/relay-factory",
            "route_plan_sha256": previous["route_plan_sha256"],
            "schema": PASSPORT.NORMALIZATION_SCHEMA,
            "state": "Building",
            "ticket": "T-110",
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize exact T-100 normalization", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run(
            "git", "push", "-q", "--force-with-lease=refs/heads/ticket/T-110:" + old_head,
            "origin", rewritten + ":refs/heads/ticket/T-110", cwd=self.product,
        )
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["head_tree"], previous["head_tree"])
        self.assertEqual(migrated["route_plan_sha256"], previous["route_plan_sha256"])
        self.assertEqual(migrated["completed_role_evidence"], old_evidence)
        self.assertEqual(migrated["charge_records"], old_charges)
        self.assertRegex(
            migrated["migration_history"][-1]["rewrite_authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(PASSPORT.migrate(self.passport_args, secret), migrated)


if __name__ == "__main__":
    unittest.main()
