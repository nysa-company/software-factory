#!/usr/bin/env python3
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ledger-view.py"
INTEGRITY_HELPER = ROOT / "scripts" / "lib" / "runs-integrity.py"
DURABLE_HELPER = ROOT / "scripts" / "lib" / "durable-file.py"
SPEND_ROLLUP = ROOT / "scripts" / "spend-rollup.sh"
SPEC = importlib.util.spec_from_file_location("ledger_view", HELPER)
LEDGER_VIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEDGER_VIEW)
DURABLE_SPEC = importlib.util.spec_from_file_location("durable_file", DURABLE_HELPER)
DURABLE_FILE = importlib.util.module_from_spec(DURABLE_SPEC)
DURABLE_SPEC.loader.exec_module(DURABLE_FILE)
HEADER = "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"


def run(*args, check=True):
    return subprocess.run(
        [str(HELPER), *map(str, args)], check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, stdout=subprocess.DEVNULL)


def manifest(path, *, state="reserved", phase=None, go="0", cost="", status="", terminal="", ticket="T-123"):
    values = {
        "run_id": path.stem,
        "phase": phase or ("reserved" if state == "reserved" else state),
        "accounting_schema": "1",
        "accounting_state": state,
        "reserved_usd": "2.00",
        "go_issued": go,
        "started_at": "2026-07-15T12:00:00Z",
        "terminal_at": terminal,
        "prompt_version": "3",
        "turns": "2",
        "effective_cost": cost,
        "exit_status": status,
        "cost_basis": "provider_reported" if cost else "",
        "ticket": ticket,
        "role": "planner",
        "adapter": "codex",
        "provider_family": "openai",
        "model_id": "gpt-test",
        "selection_reason": "primary_ready",
        "adapter_version": "test",
    }
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


class LedgerViewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "product"
        (self.root / "factory" / "runs").mkdir(parents=True)
        (self.root / "factory" / "ledger.csv").write_text(
            HEADER + "2026-07-14,09:00:00,T-100,planner,codex,1,1,0.25,0,old,openai,gpt-test,primary_ready,estimated_tokens,test\n"
        )

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self):
        run("refresh", "--factory-root", self.root)
        with (self.root / "factory" / "runtime-ledger.csv").open() as handle:
            return list(csv.DictReader(handle))

    def integrity_snapshot(self, check=True, owned=None):
        command = [str(INTEGRITY_HELPER), "snapshot", str(self.root / "factory" / "runs")]
        if owned:
            command.append(owned)
        return subprocess.run(
            command,
            check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def integrity_check(self, snapshot):
        return subprocess.run(
            [str(INTEGRITY_HELPER), "check", str(self.root / "factory" / "runs")],
            input=snapshot, check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_manifest_reservation_then_terminal_replaces_exact_run(self):
        path = self.root / "factory" / "runs" / "run-1.meta"
        manifest(path)
        rows = self.refresh()
        self.assertEqual([row["cost_usd"] for row in rows], ["0.25", "2.00"])
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        rows = self.refresh()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["cost_usd"], "0.40")
        self.assertEqual(rows[-1]["exit_status"], "0")

    def test_legacy_rows_are_not_duplicated_by_refresh(self):
        ledger = self.root / "factory" / "ledger.csv"
        ledger.write_text(HEADER + "2026-07-14,09:00:00,T-100,planner,codex,1,1,0.25,0,,,,,,\n")
        self.assertEqual(len(self.refresh()), 1)

    def test_runtime_projection_cannot_forge_cost_or_success(self):
        runtime = self.root / "factory" / "runtime-ledger.csv"
        runtime.write_text(
            HEADER
            + "2026-07-14,09:01:00,T-999,planner,codex,1,1,-1000,0,forged-cost,openai,gpt-test,primary_ready,provider_reported,test\n"
            + "2026-07-14,09:02:00,T-123,planner,codex,1,1,0,0,forged-success,openai,gpt-test,primary_ready,provider_reported,test\n"
        )

        rows = self.refresh()
        self.assertEqual([row["run_id"] for row in rows], ["old"])
        self.assertEqual(sum(float(row["cost_usd"]) for row in rows), 0.25)

        result = subprocess.run(
            [str(SPEND_ROLLUP), "2026-07-14"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "FACTORY_ROOT": str(self.root)},
        )
        self.assertIn("$0.25 across 1 runs", result.stdout)
        self.assertEqual(len(self.refresh()), 1)

    def test_pre_go_failure_is_zero_cost(self):
        path = self.root / "factory" / "runs" / "run-void.meta"
        manifest(
            path, state="launch_void", status="4",
            terminal="2026-07-15T12:01:00Z",
        )
        row = self.refresh()[-1]
        self.assertEqual((row["cost_usd"], row["turns"], row["cost_basis"]), ("0", "0", "launch_void"))

    def test_malformed_durable_values_fail_closed(self):
        ledger = self.root / "factory" / "ledger.csv"
        for field, value in (("cost_usd", "9" * 500), ("turns", "9" * 500)):
            row = dict(zip(LEDGER_VIEW.FIELDS, next(csv.reader([
                "2026-07-14,09:00:00,T-100,planner,codex,1,1,0.25,0,old,openai,gpt-test,primary_ready,estimated_tokens,test"
            ]))))
            row[field] = value
            with ledger.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, LEDGER_VIEW.FIELDS, lineterminator="\n")
                writer.writeheader()
                writer.writerow(row)
            result = run("refresh", "--factory-root", self.root, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"invalid durable ledger {field.split('_')[0]}", result.stderr)

    def test_unresolved_durable_reservation_is_retained(self):
        (self.root / "factory" / "ledger.csv").write_text(
            HEADER
            + "2026-07-14,09:00:00,T-100,planner,codex,reserved,0,1.00,reserved-live-1,live-1,openai,gpt-test,primary_ready,conservative_reservation,test\n"
        )
        row = self.refresh()[0]
        self.assertEqual((row["run_id"], row["exit_status"]), ("live-1", "reserved-live-1"))

    def test_symlink_manifest_fails_closed(self):
        path = self.root / "factory" / "runs" / "linked.meta"
        path.symlink_to(self.root / "factory" / "ledger.csv")
        result = run("refresh", "--factory-root", self.root, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular single-link", result.stderr)

    def test_runs_root_must_be_a_real_directory(self):
        runs = self.root / "factory" / "runs"
        outside = Path(self.temp.name) / "outside-runs"
        outside.mkdir()
        for replacement in ("missing", "file", "symlink"):
            if runs.is_symlink() or runs.is_file():
                runs.unlink()
            elif runs.exists():
                shutil.rmtree(runs)
            if replacement == "file":
                runs.write_text("not a directory")
            elif replacement == "symlink":
                runs.symlink_to(outside, target_is_directory=True)

            reduced = run("refresh", "--factory-root", self.root, check=False)
            snapped = self.integrity_snapshot(check=False)
            self.assertNotEqual(reduced.returncode, 0, replacement)
            self.assertNotEqual(snapped.returncode, 0, replacement)
            self.assertIn("runs root", reduced.stderr)
            self.assertIn("runs root", snapped.stderr)

    def test_multi_link_manifest_fails_closed(self):
        path = self.root / "factory" / "runs" / "linked.meta"
        manifest(path)
        os.link(path, self.root / "factory" / "linked-copy")
        reduced = run("refresh", "--factory-root", self.root, check=False)
        snapped = self.integrity_snapshot(check=False)
        self.assertNotEqual(reduced.returncode, 0)
        self.assertNotEqual(snapped.returncode, 0)
        self.assertIn("single-link", reduced.stderr)
        self.assertIn("multi-link", snapped.stderr)

    def test_integrity_recovers_renamed_runs_without_following_replacement_symlink(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        owned.write_bytes(b"launcher-owned\n")
        snapshot = self.integrity_snapshot().stdout
        original = runs.stat()

        hidden = self.root / "factory" / "provider-renamed-runs"
        runs.rename(hidden)
        outside = Path(self.temp.name) / "outside-control-plane"
        outside.mkdir()
        (outside / "owned.meta").write_bytes(b"launcher-owned\n")
        (outside / "sentinel").write_bytes(b"do-not-touch\n")
        runs.symlink_to(outside, target_is_directory=True)

        result = self.integrity_check(snapshot)
        restored = runs.stat()
        self.assertEqual(result.returncode, 1)
        self.assertIn("control_plane_mutation", result.stderr)
        self.assertFalse(runs.is_symlink())
        self.assertEqual((restored.st_dev, restored.st_ino), (original.st_dev, original.st_ino))
        self.assertEqual((runs / "owned.meta").read_bytes(), b"launcher-owned\n")
        self.assertEqual((outside / "sentinel").read_bytes(), b"do-not-touch\n")
        self.assertEqual((outside / "owned.meta").read_bytes(), b"launcher-owned\n")
        quarantined = list((self.root / "factory").glob("runs.rejected-role-mutation-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0].is_symlink())

    def test_integrity_recreates_deleted_runs_and_restores_manifests(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        owned.write_bytes(b"launcher-owned\n")
        snapshot = self.integrity_snapshot().stdout
        shutil.rmtree(runs)

        result = self.integrity_check(snapshot)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(runs.is_dir())
        self.assertFalse(runs.is_symlink())
        self.assertEqual((runs / "owned.meta").read_bytes(), b"launcher-owned\n")

    def test_integrity_quarantines_replacement_directory_before_restore(self):
        runs = self.root / "factory" / "runs"
        (runs / "owned.meta").write_bytes(b"launcher-owned\n")
        snapshot = self.integrity_snapshot().stdout
        removed = Path(self.temp.name) / "removed-original"
        runs.rename(removed)
        runs.mkdir()
        (runs / "forged.meta").write_bytes(b"forged\n")

        result = self.integrity_check(snapshot)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((runs / "owned.meta").read_bytes(), b"launcher-owned\n")
        self.assertFalse((runs / "forged.meta").exists())
        quarantined = list((self.root / "factory").glob("runs.rejected-role-mutation-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual((quarantined[0] / "forged.meta").read_bytes(), b"forged\n")

    def test_integrity_allows_valid_new_terminal_sibling(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        sibling = runs / "sibling.meta"
        manifest(owned)
        owned.write_text(owned.read_text().replace("phase=reserved", "phase=spawned").replace("go_issued=0", "go_issued=1"))
        snapshot = self.integrity_snapshot(owned=owned.name).stdout

        manifest(
            sibling, state="completed", phase="completed", go="1", cost="0.40",
            status="0", terminal="2026-07-15T12:01:00Z",
        )
        result = self.integrity_check(snapshot)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(sibling.exists())

    def test_integrity_quarantines_malformed_new_sibling(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        manifest(owned, phase="spawned", go="1")
        snapshot = self.integrity_snapshot(owned=owned.name).stdout
        (runs / "malformed.meta").write_bytes(b"not-a-manifest\n")

        result = self.integrity_check(snapshot)

        self.assertEqual(result.returncode, 1)
        self.assertFalse((runs / "malformed.meta").exists())
        self.assertEqual(len(list(runs.glob("malformed.meta.rejected-role-mutation-*"))), 1)

    def test_integrity_restores_corrupted_existing_sibling_reservation(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        sibling = runs / "sibling.meta"
        manifest(owned, phase="spawned", go="1")
        manifest(sibling)
        original = sibling.read_bytes()
        snapshot = self.integrity_snapshot(owned=owned.name).stdout
        sibling.write_bytes(b"not-a-manifest\n")

        result = self.integrity_check(snapshot)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(sibling.read_bytes(), original)
        rejected = list(runs.glob("sibling.meta.rejected-role-mutation-*"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].read_bytes(), b"not-a-manifest\n")

    def test_integrity_restores_owned_manifest_without_reverting_valid_sibling(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        sibling = runs / "sibling.meta"
        manifest(owned, phase="spawned", go="1")
        owned_original = owned.read_bytes()
        manifest(sibling, phase="spawned", go="1")
        snapshot = self.integrity_snapshot(owned=owned.name).stdout
        owned.write_bytes(b"provider mutation\n")
        manifest(
            sibling, state="completed", phase="completed", go="1", cost="0.40",
            status="0", terminal="2026-07-15T12:01:00Z",
        )
        sibling_terminal = sibling.read_bytes()

        result = self.integrity_check(snapshot)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(owned.read_bytes(), owned_original)
        self.assertEqual(sibling.read_bytes(), sibling_terminal)
        self.assertEqual(len(list(runs.glob("owned.meta.rejected-role-mutation-*"))), 1)

    def test_integrity_allows_new_inflight_sibling_phases(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        manifest(owned, phase="spawned", go="1")
        snapshot = self.integrity_snapshot(owned=owned.name).stdout

        resolved = runs / "new-resolved.meta"
        manifest(resolved)
        resolved.write_text(
            resolved.read_text()
            .replace("phase=reserved", "phase=resolved")
            .replace("accounting_schema=1", "accounting_schema=")
            .replace("accounting_state=reserved", "accounting_state=")
        )
        manifest(runs / "new-reserved.meta")
        manifest(runs / "new-prepared.meta", phase="prepared")
        manifest(runs / "new-spawned.meta", phase="spawned", go="1")

        result = self.integrity_check(snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_integrity_allows_existing_sibling_lifecycle_progress(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        manifest(owned, phase="spawned", go="1")
        resolved = runs / "from-resolved.meta"
        manifest(resolved)
        resolved.write_text(
            resolved.read_text()
            .replace("phase=reserved", "phase=resolved")
            .replace("accounting_schema=1", "accounting_schema=")
            .replace("accounting_state=reserved", "accounting_state=")
        )
        manifest(runs / "from-reserved.meta")
        manifest(runs / "from-prepared.meta", phase="prepared")
        manifest(runs / "from-spawned.meta", phase="spawned", go="1")
        snapshot = self.integrity_snapshot(owned=owned.name).stdout

        manifest(resolved, phase="prepared")
        manifest(runs / "from-reserved.meta", phase="spawned", go="1")
        manifest(
            runs / "from-prepared.meta", state="completed", phase="completed", go="1",
            cost="0.40", status="0", terminal="2026-07-15T12:01:00Z",
        )
        manifest(
            runs / "from-spawned.meta", state="abandoned_conservative",
            phase="abandoned", go="1", status="11",
            terminal="2026-07-15T12:01:00Z",
        )

        result = self.integrity_check(snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_integrity_rejects_regressive_or_identity_changing_siblings(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        manifest(owned, phase="spawned", go="1")
        regressive = runs / "sibling-regressive.meta"
        manifest(regressive, phase="prepared", go="0")
        regressive_original = regressive.read_bytes()
        snapshot = self.integrity_snapshot(owned=owned.name).stdout
        regressive.write_text(regressive.read_text().replace("phase=prepared", "phase=reserved"))
        result = self.integrity_check(snapshot)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(regressive.read_bytes(), regressive_original)

        cases = {
            "go-regression": lambda text: text.replace("go_issued=1", "go_issued=0"),
            "pid-change": lambda text: text.replace("pid=100", "pid=200"),
            "ticket-change": lambda text: text.replace("ticket=T-123", "ticket=T-999"),
            "role-change": lambda text: text.replace("role=planner", "role=builder"),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            with self.subTest(label=label):
                sibling = runs / f"sibling-{index}.meta"
                manifest(sibling, phase="spawned", go="1")
                sibling.write_text(sibling.read_text() + "pid=100\n")
                original = sibling.read_bytes()
                snapshot = self.integrity_snapshot(owned=owned.name).stdout
                sibling.write_text(mutate(sibling.read_text()))

                result = self.integrity_check(snapshot)

                self.assertEqual(result.returncode, 1)
                self.assertEqual(sibling.read_bytes(), original)
                self.assertEqual(
                    len(list(runs.glob(f"{sibling.name}.rejected-role-mutation-*"))), 1,
                )

    def test_integrity_allows_sibling_resolved_to_launch_void(self):
        runs = self.root / "factory" / "runs"
        owned = runs / "owned.meta"
        sibling = runs / "sibling.meta"
        manifest(owned)
        manifest(sibling)
        sibling.write_text(
            sibling.read_text()
            .replace("phase=reserved", "phase=resolved")
            .replace("accounting_schema=1", "accounting_schema=")
            .replace("accounting_state=reserved", "accounting_state=")
        )
        snapshot = self.integrity_snapshot(owned=owned.name).stdout

        manifest(sibling, state="launch_void", status="5", terminal="2026-07-15T12:01:00Z")
        sibling.write_text(sibling.read_text().replace("phase=launch_void", "phase=abandoned"))
        result = self.integrity_check(snapshot)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_integrity_refuses_replaced_parent_without_writing_through_symlink(self):
        runs = self.root / "factory" / "runs"
        (runs / "owned.meta").write_bytes(b"launcher-owned\n")
        snapshot = self.integrity_snapshot().stdout
        original_factory = self.root / "original-factory"
        (self.root / "factory").rename(original_factory)
        outside = Path(self.temp.name) / "outside-parent"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"do-not-touch\n")
        (self.root / "factory").symlink_to(outside, target_is_directory=True)

        result = self.integrity_check(snapshot)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runs parent must be a real directory", result.stderr)
        self.assertEqual((outside / "sentinel").read_bytes(), b"do-not-touch\n")
        self.assertFalse((outside / "runs").exists())

    def test_durable_publish_fsyncs_runs_parent(self):
        runs = self.root / "factory" / "runs"
        calls = []
        original = DURABLE_FILE.fsync_directory

        def record(path):
            calls.append(Path(path))
            return original(path)

        with mock.patch.object(DURABLE_FILE, "fsync_directory", side_effect=record):
            DURABLE_FILE.publish(runs / "durable.meta", b"durable\n")
        self.assertIn(self.root / "factory", calls)

    def test_durable_publish_does_not_follow_runs_symlink(self):
        runs = self.root / "factory" / "runs"
        runs.rmdir()
        outside = Path(self.temp.name) / "outside-durable"
        outside.mkdir()
        runs.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            DURABLE_FILE.publish(runs / "escaped.meta", b"must-not-escape\n")
        self.assertFalse((outside / "escaped.meta").exists())

    def test_oversized_manifest_numbers_fail_closed(self):
        path = self.root / "factory" / "runs" / "huge.meta"
        manifest(path)
        text = path.read_text().replace("reserved_usd=2.00", f"reserved_usd={'9' * 500}")
        path.write_text(text)
        result = run("refresh", "--factory-root", self.root, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid reserved cost", result.stderr)

    def test_terminal_run_stays_in_its_utc_start_day(self):
        path = self.root / "factory" / "runs" / "midnight.meta"
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-16T00:01:00Z",
        )
        row = self.refresh()[-1]
        self.assertEqual((row["date"], row["time"]), ("2026-07-15", "12:00:00"))

    def test_projection_uses_existing_launch_and_ledger_locks(self):
        with LEDGER_VIEW.projection_locks(self.root):
            self.assertTrue((self.root / "factory" / ".launch.lock").is_dir())
            self.assertTrue((self.root / "factory" / ".ledger.lock").is_dir())
        self.assertFalse((self.root / "factory" / ".launch.lock").exists())
        self.assertFalse((self.root / "factory" / ".ledger.lock").exists())

    def test_conflicting_terminal_rows_fail_closed(self):
        path = self.root / "factory" / "runs" / "old.meta"
        manifest(
            path, state="completed", go="1", cost="0.50", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        result = run("refresh", "--factory-root", self.root, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting manifest records", result.stderr)

    def test_manifest_reservation_cannot_be_hidden_by_durable_terminal(self):
        path = self.root / "factory" / "runs" / "old.meta"
        manifest(path)
        result = run("refresh", "--factory-root", self.root, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting manifest records for run_id old", result.stderr)

    def test_terminal_manifest_replaces_durable_reservation(self):
        (self.root / "factory" / "ledger.csv").write_text(
            HEADER
            + "2026-07-14,09:00:00,T-123,planner,codex,reserved,0,2.00,reserved-run-1,run-1,openai,gpt-test,primary_ready,conservative_reservation,test\n"
        )
        path = self.root / "factory" / "runs" / "run-1.meta"
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        row = self.refresh()[0]
        self.assertEqual((row["cost_usd"], row["exit_status"]), ("0.40", "0"))

    def test_projection_is_deterministic_and_refuses_unsettled_runs(self):
        origin = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / ".gitignore").write_text("factory/runtime-ledger.csv\nfactory/runs/\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")
        git(self.root, "remote", "add", "origin", str(origin))
        git(self.root, "push", "-qu", "origin", "main")
        worktree = Path(self.temp.name) / "closeout"
        git(self.root, "worktree", "add", "-q", "-b", "chore/t123-closeout", str(worktree), "origin/main")

        path = self.root / "factory" / "runs" / "run-1.meta"
        manifest(
            path, state="completed", go="1", cost="0.40", status="0",
            terminal="2026-07-15T12:05:00Z",
        )
        first = run("project", "--factory-root", self.root, "--workdir", worktree, "--ticket", "T-123")
        payload = json.loads(first.stdout)
        content = (worktree / "factory" / "ledger.csv").read_bytes()
        self.assertEqual(payload["row_count"], 2)
        self.assertEqual(payload["ticket_cost_usd"], 0.4)

        git(worktree, "checkout", "--", "factory/ledger.csv")
        second = run("project", "--factory-root", self.root, "--workdir", worktree, "--ticket", "T-123")
        self.assertEqual(json.loads(second.stdout)["sha256"], payload["sha256"])
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), content)

        git(worktree, "checkout", "--", "factory/ledger.csv")
        before = (worktree / "factory" / "ledger.csv").read_bytes()
        claim = self.root / "factory" / ".active-runs" / "T-123.planner.lock"
        claim.mkdir(parents=True)
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("factory/.active-runs/T-123.planner.lock", refused.stderr)
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), before)
        claim.rmdir()

        pid = self.root / "factory" / "runs" / "orphan.pid"
        pid.write_text("999999\n")
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("factory/runs/orphan.pid", refused.stderr)
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), before)
        pid.unlink()

        git(worktree, "checkout", "--", "factory/ledger.csv")
        other = self.root / "factory" / "runs" / "other-live.meta"
        manifest(other, ticket="T-999")
        before = (worktree / "factory" / "ledger.csv").read_bytes()
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("T-999: other-live.meta", refused.stderr)
        self.assertEqual((worktree / "factory" / "ledger.csv").read_bytes(), before)
        other.unlink()

        git(worktree, "checkout", "--", "factory/ledger.csv")
        manifest(path)
        refused = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("T-123: run-1.meta", refused.stderr)

        path.write_text(
            "run_id=run-1\nphase=resolved\naccounting_schema=\nticket=T-123\n"
        )
        unresolved = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(unresolved.returncode, 0)
        self.assertIn("T-123: run-1.meta", unresolved.stderr)

        path.unlink()
        legacy = self.root / "factory" / "runs" / "legacy-1.meta"
        legacy.write_text(
            "run_id=legacy-1\nphase=completed\naccounting_schema=\nticket=T-123\n"
        )
        unaccounted = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(unaccounted.returncode, 0)
        self.assertIn("T-123: legacy-1.meta", unaccounted.stderr)

        legacy.write_text(
            "run_id=old\nphase=completed\naccounting_schema=\nticket=T-999\n"
        )
        cross_ticket = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(cross_ticket.returncode, 0)
        self.assertIn("T-999: legacy-1.meta", cross_ticket.stderr)

        legacy.unlink()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        git(worktree, "rm", "-qr", "factory")
        (worktree / "factory").symlink_to(outside, target_is_directory=True)
        git(worktree, "add", "factory")
        git(worktree, "commit", "-qm", "replace factory with symlink")
        escaped = run(
            "project", "--factory-root", self.root, "--workdir", worktree,
            "--ticket", "T-123", check=False,
        )
        self.assertNotEqual(escaped.returncode, 0)
        self.assertIn("factory directory must be a real directory", escaped.stderr)
        self.assertFalse((outside / "ledger.csv").exists())


if __name__ == "__main__":
    unittest.main()
