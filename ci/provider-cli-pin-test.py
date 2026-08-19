#!/usr/bin/env python3
"""Focused exact provider CLI pin transaction regressions."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
SHA = "a" * 40
TREE = "b" * 40
SHA_B = "c" * 40
TREE_B = "d" * 40
SHA_LEGACY = "e" * 40
TREE_LEGACY = "f" * 40


class ProviderCliPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="provider-cli-pin-test.", dir=Path.home())
        root = Path(self.temp.name).resolve()
        self.home = root / "home"
        self.factory = self.home / ".factory"
        self.kits = self.factory / "kits"
        self.release = self.kits / "releases" / SHA
        self.vendor = self.home / "vendor/v1/bin"
        self.qualifications = root / "qualifications"
        for path in (
            self.factory / "bin", self.kits / "projects", self.kits / "manifests",
            self.release,
            self.vendor, self.qualifications,
        ):
            path.mkdir(parents=True, mode=0o700)
        self.copy_release_files(self.release)
        self.write_manifest(self.release, SHA, TREE)
        self.seal(self.release)
        self.helper = self.release / "scripts/owner-provider-cli-pin.py"
        (self.factory / "global.env").write_text(
            "export GLOBAL_DAILY_CAP_USD=50.00\n"
            "export CURSOR_AGENT_BIN=agent\n"
        )
        (self.factory / "global.env").chmod(0o600)
        self.write_candidates(self.vendor, "2.1.226", "0.147.0", "2026.08.01")
        self.env = {
            **os.environ, "HOME": str(self.home), "FACTORY_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_PROVIDER_CLI_PIN_TEST_QUALIFICATION_ROOT": str(self.qualifications),
        }

    def write_manifest(self, release: Path, sha: str, tree: str) -> None:
        manifest = self.kits / "manifests" / f"{sha}.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "kit_sha": sha,
            "canonical_origin": "github.com/nysa-company/software-factory",
            "git_tree": tree, "sealed_release_path": str(release),
            "created_at": "2026-08-09T00:00:00Z",
        }))
        manifest.chmod(0o600)

    @staticmethod
    def seal(release: Path) -> None:
        for path in (release, *release.rglob("*")):
            path.chmod(path.stat().st_mode & ~0o222)

    def tearDown(self) -> None:
        for release in (self.kits / "releases").iterdir():
            for path in (release, *release.rglob("*")):
                path.chmod(path.stat().st_mode | 0o200)
        self.temp.cleanup()

    def copy_release_files(self, release: Path) -> None:
        paths = (
            "factory-contract.json",
            "scripts/factory-launch",
            "scripts/lib/plain-config.sh",
            "scripts/lib/backend-policy.sh",
            "scripts/lib/provider-cli-version.sh",
            "scripts/adapters/claude-code.sh",
            "scripts/adapters/codex.sh",
            "scripts/adapters/cursor-agent.sh",
            "scripts/owner-provider-cli-pin.py",
        )
        for relative in paths:
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)

    @staticmethod
    def tool(path: Path, version: str, help_text: str, *, name: str) -> None:
        if name == "codex":
            body = f'''#!/bin/sh
if [ "$1" = --version ]; then echo "codex-cli {version}"; exit 0; fi
if [ "$1" = exec ] && [ "$2" = --help ]; then echo "{help_text}"; exit 0; fi
exit 2
'''
        elif name == "claude":
            body = f'''#!/bin/sh
if [ "$1" = --version ]; then echo "{version} (Claude Code)"; exit 0; fi
if [ "$1" = --help ]; then echo "{help_text}"; exit 0; fi
exit 2
'''
        else:
            body = f'''#!/bin/sh
if [ "$1" = --version ]; then echo "agent {version}"; exit 0; fi
if [ "$1" = --help ]; then echo "{help_text}"; exit 0; fi
exit 2
'''
        path.write_text(body)
        path.chmod(0o755)

    def write_candidates(self, root: Path, claude: str, codex: str, agent: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.tool(
            root / "claude", claude,
            "--max-budget-usd --output-format --append-system-prompt --model --effort",
            name="claude",
        )
        self.tool(root / "codex", codex, "--json --model", name="codex")
        host = root / "codex-code-mode-host"
        host.write_text("#!/bin/sh\necho --listen\n")
        host.chmod(0o755)
        self.tool(
            root / "agent", agent,
            "--print --output-format --workspace --model --force --trust",
            name="agent",
        )

    def command(
        self, action: str, *, root: Path | None = None, approval: str = "", env=None,
        helper: Path | None = None, release: Path | None = None,
        sha: str = SHA, tree: str = TREE, stdin=None,
    ):
        candidate = root or self.vendor
        requested = release or self.release
        command = [
            "python3", "-I", "-S", str(helper or self.helper),
            "--kits-root", str(self.kits), "--sha", sha, "--tree", tree,
            "--release", str(requested), action,
        ]
        if action != "check":
            command += [
                "--claude-bin", str(candidate / "claude"),
                "--codex-bin", str(candidate / "codex"),
                "--cursor-bin", str(candidate / "agent"),
                "--operator-id", "test-operator",
            ]
        if action == "apply":
            command += ["--approve-hash", approval]
        return subprocess.run(
            command, capture_output=True, text=True, check=False,
            env=env or self.env, stdin=stdin, timeout=30,
        )

    def plan(self, root: Path | None = None) -> dict:
        result = self.command("plan", root=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def apply(self, root: Path | None = None, env=None) -> subprocess.CompletedProcess[str]:
        plan = self.plan(root)
        return self.command("apply", root=root, approval=plan["approval_sha256"], env=env)

    def test_healthy_links_exact_receipt_and_idempotent_check(self) -> None:
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        first = self.command("check")
        second = self.command("check")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        self.assertEqual(
            {item["reason"] for item in json.loads(first.stdout)["items"]},
            {"exact_pin_ready"},
        )
        for name in ("claude", "codex", "codex-code-mode-host", "agent"):
            self.assertEqual(os.readlink(self.factory / "bin" / name), str(self.vendor / name))

    def test_check_is_independent_of_a_controlling_terminal(self) -> None:
        codex = self.vendor / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then echo 'codex-cli 0.147.0'; exit 0; fi\n"
            "if [ \"$1\" = exec ] && [ \"$2\" = --help ]; then\n"
            "  [ -t 0 ] && echo '--json --model terminal' || echo '--json --model'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n"
        )
        codex.chmod(0o755)
        host = self.vendor / "codex-code-mode-host"
        host.write_text(
            "#!/bin/sh\n"
            "[ -t 0 ] && echo '--listen terminal' || echo --listen\n"
        )
        host.chmod(0o755)
        self.assertEqual(self.apply().returncode, 0)
        master, slave = os.openpty()
        try:
            checked = self.command("check", stdin=slave)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["status"], "ready")

    def test_codex_companion_is_required_and_receipt_bound(self) -> None:
        host = self.vendor / "codex-code-mode-host"
        host.unlink()
        self.assertNotEqual(self.command("plan").returncode, 0)
        host.write_text("#!/bin/sh\necho --listen\n")
        host.chmod(0o755)
        self.assertEqual(self.apply().returncode, 0)
        host.write_text("#!/bin/sh\necho changed --listen\n")
        status = json.loads(self.command("check").stdout)
        companion = next(
            item for item in status["items"]
            if item["name"] == "codex-code-mode-host"
        )
        self.assertEqual(companion["reason"], "receipt_drift")

    def test_benign_stderr_warning_does_not_break_the_version_probe(self) -> None:
        """A provider CLI may warn on stderr and still be pinnable.

        codex reports that it will not create PATH aliases because the probe
        deliberately points HOME and TMPDIR at a temporary directory. Parsing
        the merged streams made that warning indistinguishable from the version
        line and refused every codex build on the machine.
        """
        codex = self.vendor / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then\n"
            "  echo 'WARNING: refusing to create helper binaries under temporary dir' >&2\n"
            "  echo 'codex-cli 0.144.3'\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = exec ] && [ \"$2\" = --help ]; then echo '--json --model'; exit 0; fi\n"
            "exit 2\n"
        )
        codex.chmod(0o755)
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        status = json.loads(self.command("check").stdout)
        entry = next(item for item in status["items"] if item["name"] == "codex")
        self.assertEqual(entry["reason"], "exact_pin_ready")
        self.assertEqual(entry["version"], "0.144.3")

    def test_sensitive_stderr_still_refuses_the_version_probe(self) -> None:
        """The refusal scan must still cover stderr, not only stdout."""
        codex = self.vendor / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then\n"
            "  echo 'Authorization: Bearer sk-live-DO-NOT-LEAK' >&2\n"
            "  echo 'codex-cli 0.144.3'\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = exec ] && [ \"$2\" = --help ]; then echo '--json --model'; exit 0; fi\n"
            "exit 2\n"
        )
        codex.chmod(0o755)
        planned = self.command("plan")
        self.assertNotEqual(planned.returncode, 0)
        self.assertIn("version probe is invalid", planned.stderr)
        self.assertNotIn("DO-NOT-LEAK", planned.stdout + planned.stderr)

    def test_varying_stderr_keeps_the_plan_hash_stable_across_invocations(self) -> None:
        """plan -> apply must agree even when a CLI's stderr differs per run.

        codex embeds the probe's randomly named temporary directory in its
        warning. Digesting the merged streams made help_sha256 change on every
        invocation, so the approval hash never matched and apply always refused
        with 'provider CLI pin approval hash does not match'.
        """
        codex = self.vendor / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --version ]; then\n"
            "  echo \"WARNING: temporary dir $$-$(date +%N)\" >&2\n"
            "  echo 'codex-cli 0.144.3'\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = exec ] && [ \"$2\" = --help ]; then\n"
            "  echo \"WARNING: temporary dir $$-$(date +%N)\" >&2\n"
            "  echo '--json --model'\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n"
        )
        codex.chmod(0o755)
        first = self.plan()
        second = self.plan()
        self.assertEqual(first["approval_sha256"], second["approval_sha256"])
        applied = self.command("apply", approval=first["approval_sha256"])
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_never_managed_check_warns_without_creating_a_lock(self) -> None:
        lock = self.factory / ".provider-cli-pin.lock"
        checked = self.command("check")
        self.assertEqual(checked.returncode, 2, checked.stderr)
        self.assertFalse(lock.exists() or lock.is_symlink())
        self.assertEqual(
            {item["reason"] for item in json.loads(checked.stdout)["items"]},
            {"provider_cli_unmanaged"},
        )

    def test_pruned_vendor_target_is_dangling_not_absent(self) -> None:
        self.assertEqual(self.apply().returncode, 0)
        (self.vendor / "codex").unlink()
        status = json.loads(self.command("check").stdout)
        codex = next(item for item in status["items"] if item["name"] == "codex")
        self.assertEqual((codex["managed_state"], codex["reason"]),
                         ("dangling", "managed_pin_target_missing"))

    def test_binary_and_config_drift_fail_closed(self) -> None:
        self.assertEqual(self.apply().returncode, 0)
        self.tool(self.vendor / "claude", "2.1.227", "--max-budget-usd --output-format --append-system-prompt --model --effort", name="claude")
        status = json.loads(self.command("check").stdout)
        self.assertEqual(next(x for x in status["items"] if x["name"] == "claude")["reason"], "version_mismatch")
        (self.factory / "global.env").write_text((self.factory / "global.env").read_text() + "# drift\n")
        status = json.loads(self.command("check").stdout)
        self.assertTrue(all(item["reason"] == "global_config_drift" for item in status["items"]))

    def test_unsafe_source_link_and_lock_are_refused(self) -> None:
        (self.vendor / "claude").chmod(0o777)
        self.assertNotEqual(self.command("plan").returncode, 0)
        (self.vendor / "claude").chmod(0o755)
        (self.factory / "bin/codex").write_text("not a link")
        self.assertNotEqual(self.command("plan").returncode, 0)
        (self.factory / "bin/codex").unlink()
        lock = self.factory / ".provider-cli-pin.lock"
        lock.touch(mode=0o600)
        lock.chmod(0o644)
        self.assertNotEqual(self.command("check").returncode, 0)

    def test_manifest_seal_and_single_link_candidate_are_required(self) -> None:
        manifest = self.kits / "manifests" / f"{SHA}.json"
        original = manifest.read_bytes()
        value = json.loads(original)
        value["git_tree"] = "c" * 40
        manifest.write_text(json.dumps(value))
        manifest.chmod(0o600)
        self.assertNotEqual(self.command("plan").returncode, 0)
        manifest.write_bytes(original)
        manifest.chmod(0o600)
        contract = self.release / "factory-contract.json"
        contract.chmod(0o644)
        self.assertNotEqual(self.command("plan").returncode, 0)
        contract.chmod(0o444)
        alias = self.vendor / "claude-alias"
        os.link(self.vendor / "claude", alias)
        try:
            self.assertNotEqual(self.command("plan").returncode, 0)
        finally:
            alias.unlink()

    def test_explicit_local_origin_is_accepted_only_in_factory_test_mode(self) -> None:
        manifest = self.kits / "manifests" / f"{SHA}.json"
        value = json.loads(manifest.read_text())
        local_origin = self.home / "software-factory-origin"
        value["canonical_origin"] = str(local_origin)
        manifest.write_text(json.dumps(value))
        manifest.chmod(0o600)
        test_env = {
            **self.env,
            "FACTORY_KIT_TEST_MODE": "1",
            "FACTORY_KIT_CANONICAL_ORIGIN": str(local_origin) + ".git",
        }
        accepted = self.command("plan", env=test_env)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        refused = self.command(
            "plan",
            env={
                **test_env,
                "FACTORY_KIT_TEST_MODE": "0",
            },
        )
        self.assertNotEqual(refused.returncode, 0)

    def test_stale_approval_and_foreign_cursor_path_are_refused(self) -> None:
        approval = self.plan()["approval_sha256"]
        with (self.factory / "global.env").open("a") as stream:
            stream.write("# changed\n")
        result = self.command("apply", approval=approval)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.factory / "bin/claude").exists())
        (self.factory / "global.env").write_text("export CURSOR_AGENT_BIN=/tmp/foreign-agent\n")
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_safe_projects_root_artifact_is_not_a_project(self) -> None:
        artifact = self.kits / "projects/.model-migration-plan.fixture"
        artifact.write_text("{}\n")
        artifact.chmod(0o600)
        self.plan()
        artifact.unlink()
        artifact.symlink_to(self.factory / "global.env")
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_manifest_and_accounting_parent_symlinks_are_refused(self) -> None:
        manifests = self.kits / "manifests"
        real_manifests = self.kits / ".manifests-real"
        manifests.rename(real_manifests)
        manifests.symlink_to(real_manifests)
        try:
            self.assertNotEqual(self.command("plan").returncode, 0)
        finally:
            manifests.unlink()
            real_manifests.rename(manifests)
        accounting_target = self.home / "accounting-target"
        accounting_target.mkdir(mode=0o700)
        accounting = self.factory / "accounting"
        accounting.symlink_to(accounting_target)
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_active_work_and_launch_lock_interleaving_refuse_before_mutation(self) -> None:
        product = self.home / "product"
        factory = product / "factory"
        factory.mkdir(parents=True)
        (factory / "MAINTENANCE").write_text(json.dumps({
            "schema_version": 1, "project": "relay", "product_path": str(product),
        }))
        (factory / "MAINTENANCE").chmod(0o600)
        project = self.kits / "projects/relay"
        project.mkdir()
        (project / "active.json").write_text(json.dumps({
            "contract_version": "2.0.0", "kit_sha": SHA, "kit_tree": TREE,
            "product_path": str(product), "project": "relay", "release_path": str(self.release),
        }))
        (project / "active.json").chmod(0o600)
        active_runs = factory / ".active-runs"
        active_runs.mkdir()
        (active_runs / "attempt").write_text("active")
        self.assertNotEqual(self.command("plan").returncode, 0)
        shutil.rmtree(active_runs)
        launch = factory / ".launch.lock"
        launch.mkdir()
        before = (self.factory / "global.env").read_bytes()
        approval = self.plan_after_temporarily_removing(launch)["approval_sha256"]
        result = self.command("apply", approval=approval)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.factory / "global.env").read_bytes(), before)

    def plan_after_temporarily_removing(self, launch: Path) -> dict:
        launch.rmdir()
        try:
            return self.plan()
        finally:
            launch.mkdir()

    def test_broker_drain_uses_active_token_semantics(self) -> None:
        accounting = self.factory / "accounting"
        accounting.mkdir()
        path = accounting / "credential-broker.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
            PRAGMA application_id = 1314476866;
            PRAGMA user_version = 1;
            CREATE TABLE tokens(
              token_sha256 TEXT PRIMARY KEY, expires_at INTEGER,
              max_requests INTEGER, used_requests INTEGER, revoked_at INTEGER
            );
            CREATE TABLE requests(
              token_sha256 TEXT, completed_at INTEGER
            );
        """)
        connection.execute(
            "INSERT INTO tokens VALUES(?,?,?,?,NULL)",
            ("expired", int(time.time()) - 1, 1, 1),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)
        self.plan()
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO tokens VALUES(?,?,?,?,NULL)",
            ("active", int(time.time()) + 60, 1, 0),
        )
        connection.commit()
        connection.close()
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_growing_provider_ledger_remains_pinnable(self) -> None:
        accounting = self.factory / "accounting"
        accounting.mkdir()
        path = accounting / "state-v2.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
            PRAGMA application_id = 1314476867;
            PRAGMA user_version = 2;
            CREATE TABLE attempts(state TEXT);
            CREATE TABLE legacy_intervals(value TEXT);
            CREATE TABLE padding(value BLOB);
        """)
        connection.execute("INSERT INTO padding VALUES(zeroblob(1100000))")
        connection.commit()
        connection.close()
        path.chmod(0o600)
        self.assertGreater(path.stat().st_size, 1_000_000)
        self.plan()

    def test_incomplete_qualification_is_ignored_but_live_controller_blocks(self) -> None:
        root = self.qualifications / "nysa-sf-qualification.fixture"
        root.mkdir()
        marker = root / "marker.json"
        marker.write_text(json.dumps({
            "mode": "qualification",
            "schema": "nysa.software-factory.qualification-environment/v1",
        }))
        marker.chmod(0o600)
        self.plan()
        (root / "environment.json").write_text("{}")
        (root / "environment.json").chmod(0o600)
        project = root / "projects/relay"
        project.mkdir(parents=True)
        controller = self.factory / "qualification/relay/controller"
        controller.mkdir(parents=True)
        active = project / "active.json"
        active.write_text(json.dumps({
            "project": "relay", "qualification_mode": "isolated",
            "controller_state_path": str(controller),
        }))
        active.chmod(0o600)
        duplicate = self.qualifications / "nysa-sf-qualification.duplicate"
        duplicate_project = duplicate / "projects/relay"
        duplicate_project.mkdir(parents=True)
        shutil.copy2(marker, duplicate / "marker.json")
        shutil.copy2(root / "environment.json", duplicate / "environment.json")
        shutil.copy2(active, duplicate_project / "active.json")
        takeover = self.qualifications / "nysa-sf-qualification.takeover"
        takeover_project = takeover / "projects/takeover"
        takeover_project.mkdir(parents=True)
        shutil.copy2(marker, takeover / "marker.json")
        shutil.copy2(root / "environment.json", takeover / "environment.json")
        takeover_controller = self.kits / "projects/takeover/controller"
        takeover_controller.mkdir(parents=True)
        takeover_active = takeover_project / "active.json"
        takeover_active.write_text(json.dumps({
            "project": "takeover", "qualification_mode": "takeover",
            "takeover_kits_root": str(self.kits),
        }))
        takeover_active.chmod(0o600)
        self.plan()
        lock = controller / "reconcile.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.command("plan").returncode, 0)
        finally:
            os.close(descriptor)

        active.write_text(json.dumps({
            "project": "relay", "qualification_mode": "isolated",
            "controller_state_path": str(project / "controller"),
        }))
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_pin_lock_collision_is_nonblocking(self) -> None:
        lock = self.factory / ".provider-cli-pin.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.command("check")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("busy", result.stderr)
        finally:
            os.close(descriptor)

    def test_first_activation_project_lock_refuses_before_mutation(self) -> None:
        approval = self.plan()["approval_sha256"]
        before = (self.factory / "global.env").read_bytes()
        project = self.kits / "projects/first-generation"
        project.mkdir()
        activation = project / ".activation.lock"
        activation.mkdir()
        try:
            result = self.command("apply", approval=approval)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.factory / "global.env").read_bytes(), before)
        finally:
            activation.rmdir()

    def test_inactive_project_controller_is_locked_and_validated(self) -> None:
        project = self.kits / "projects/inactive-controller"
        controller = project / "controller"
        controller.mkdir(parents=True)
        lock = controller / "reconcile.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.command("plan").returncode, 0)
        finally:
            os.close(descriptor)
        lock.unlink()
        controller.rmdir()
        controller.symlink_to(project / "missing-controller")
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_receipt_authority_checks_a_distinct_allowed_release(self) -> None:
        release_b = self.kits / "releases" / SHA_B
        release_b.mkdir(mode=0o700)
        self.copy_release_files(release_b)
        self.write_manifest(release_b, SHA_B, TREE_B)
        self.seal(release_b)
        helper_b = release_b / "scripts/owner-provider-cli-pin.py"

        product = self.home / "product-authority-a"
        factory = product / "factory"
        factory.mkdir(parents=True)
        maintenance = factory / "MAINTENANCE"
        maintenance.write_text(json.dumps({
            "schema_version": 1, "project": "authority-a",
            "product_path": str(product),
        }))
        maintenance.chmod(0o600)
        project = self.kits / "projects/authority-a"
        project.mkdir()
        active = project / "active.json"
        active.write_text(json.dumps({
            "contract_version": "2.0.0", "kit_sha": SHA, "kit_tree": TREE,
            "product_path": str(product), "project": "authority-a",
            "release_path": str(self.release),
        }))
        active.chmod(0o600)

        planned = self.command(
            "plan", helper=helper_b, release=release_b, sha=SHA_B, tree=TREE_B,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        approval = json.loads(planned.stdout)["approval_sha256"]
        applied = self.command(
            "apply", helper=helper_b, release=release_b, sha=SHA_B, tree=TREE_B,
            approval=approval,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        checked = self.command("check", helper=helper_b)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        doctor = (ROOT / "scripts/factory-doctor.sh").read_text()
        self.assertIn('bash "$KIT_DIR/scripts/factory-kit.sh"', doctor)
        self.assertIn('provider-cli-pin check --sha "$KIT_SHA"', doctor)

    def test_legacy_contract_active_release_is_allowlisted(self) -> None:
        legacy = self.kits / "releases" / SHA_LEGACY
        legacy.mkdir(mode=0o700)
        self.copy_release_files(legacy)
        contract_path = legacy / "factory-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["contract_version"] = "1.5.0"
        contract_path.write_text(json.dumps(contract))
        self.write_manifest(legacy, SHA_LEGACY, TREE_LEGACY)
        self.seal(legacy)

        product = self.home / "product-legacy"
        factory = product / "factory"
        factory.mkdir(parents=True)
        maintenance = factory / "MAINTENANCE"
        maintenance.write_text(json.dumps({
            "schema_version": 1, "project": "legacy",
            "product_path": str(product),
        }))
        maintenance.chmod(0o600)
        project = self.kits / "projects/legacy"
        project.mkdir()
        active = project / "active.json"
        active.write_text(json.dumps({
            "contract_version": "1.5.0", "kit_sha": SHA_LEGACY,
            "kit_tree": TREE_LEGACY, "product_path": str(product),
            "project": "legacy", "release_path": str(legacy),
        }))
        active.chmod(0o600)

        planned = self.plan()
        self.assertIn(
            {"contract_version": "1.5.0", "factory_sha": SHA_LEGACY,
             "factory_tree": TREE_LEGACY, "release_path": str(legacy)},
            planned["compatible_releases"],
        )
        applied = self.command("apply", approval=planned["approval_sha256"])
        self.assertEqual(applied.returncode, 0, applied.stderr)
        checked = self.command("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_exception_and_crash_recovery_restore_full_unit(self) -> None:
        self.assertEqual(self.apply().returncode, 0)
        prior_config = (self.factory / "global.env").read_bytes()
        prior_receipt = (self.factory / "provider-cli-pin.json").read_bytes()
        prior_links = {
            name: os.readlink(self.factory / "bin" / name)
            for name in ("claude", "codex", "codex-code-mode-host", "agent")
        }
        v2 = self.home / "vendor/v2/bin"
        self.write_candidates(v2, "2.1.227", "0.148.0", "2026.08.02")
        plan = self.plan(v2)
        failure_env = {
            **self.env,
            "FACTORY_PROVIDER_CLI_PIN_TEST_FAIL_AFTER": "codex-code-mode-host",
        }
        failed = self.command("apply", root=v2, approval=plan["approval_sha256"], env=failure_env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.factory / "global.env").read_bytes(), prior_config)
        self.assertEqual((self.factory / "provider-cli-pin.json").read_bytes(), prior_receipt)
        self.assertEqual({name: os.readlink(self.factory / "bin" / name) for name in prior_links}, prior_links)

        plan = self.plan(v2)
        crash_env = {**self.env, "FACTORY_PROVIDER_CLI_PIN_TEST_EXIT_AFTER": "claude"}
        crashed = self.command("apply", root=v2, approval=plan["approval_sha256"], env=crash_env)
        self.assertEqual(crashed.returncode, 91)
        pending = self.command("check")
        self.assertEqual(pending.returncode, 2, pending.stderr)
        self.assertEqual(
            {item["reason"] for item in json.loads(pending.stdout)["items"]},
            {"transaction_recovery_required"},
        )
        self.assertTrue((self.factory / "provider-cli-pin.transaction.json").exists())
        self.plan(v2)
        recovered = self.command("check")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual((self.factory / "global.env").read_bytes(), prior_config)
        self.assertEqual({name: os.readlink(self.factory / "bin" / name) for name in prior_links}, prior_links)

    def test_sensitive_probe_output_is_not_echoed(self) -> None:
        self.tool(self.vendor / "codex", "token=top-secret", "--json --model", name="codex")
        result = self.command("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("top-secret", result.stderr + result.stdout)

    def test_confusable_contract_flags_are_refused(self) -> None:
        self.tool(self.vendor / "codex", "0.147.0", "--jsonl --models", name="codex")
        self.assertNotEqual(self.command("plan").returncode, 0)

    def test_factory_kit_gates_certification_and_activation_without_lock_inversion(self) -> None:
        source = (ROOT / "scripts/factory-kit.sh").read_text()
        self.assertIn("provider-cli-pin ACTION --sha FULL_SHA", source)
        certify = source[source.index("cmd_certify() {"):source.index("cmd_plan() {")]
        self.assertIn('require_provider_cli_pin_ready "$sha"', certify)
        activate = source[source.index("cmd_activate() {"):source.index("infer_product_path() {")]
        launch = activate.index('acquire_lock "$launch_lock" "product launch"')
        check = activate.index('require_provider_cli_pin_ready "$sha"')
        maintenance = activate.index('require_maintenance_after_lock "$slug" "$product_top"')
        self.assertLess(launch, check)
        self.assertLess(check, maintenance)
        reconcile = source[source.index("cmd_reconcile() {"):source.index("find_committed_journal_for_generation() {")]
        self.assertIn('require_provider_cli_pin_ready "$sha"', reconcile)
        self.assertIn('require_provider_cli_pin_ready "$rollback_sha"', reconcile)
        rollback = source[source.index("cmd_rollback() {"):source.index("cmd_recover_lease() {")]
        rollback_launch = rollback.index('acquire_lock "$launch_lock" "product launch"')
        rollback_check = rollback.index('require_provider_cli_pin_ready "$previous_sha"')
        rollback_switch = rollback.index('switch_active_from_journal "$journal" "$active" previous_record')
        self.assertLess(rollback_launch, rollback_check)
        self.assertLess(rollback_check, rollback_switch)
        self.assertIn('provider_cli_pin_authority_helper "$sha"', source)


if __name__ == "__main__":
    unittest.main()
