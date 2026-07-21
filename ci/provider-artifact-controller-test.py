#!/usr/bin/env python3
"""Tests for trusted provider artifact validation and application."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts" / "provider-artifact-controller.py"
WORKER_IMAGE = ROOT / "scripts" / "provider-worker-image.py"


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def run(*command, cwd=None, input=None):
    return subprocess.run(
        command,
        cwd=cwd,
        input=input,
        capture_output=True,
        check=True,
    ).stdout


class ArtifactControllerTest(unittest.TestCase):
    def test_release_worker_image_lock_is_digest_pinned_and_consistent(self):
        result = subprocess.run(
            [sys.executable, str(WORKER_IMAGE)],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertRegex(value["image_reference"], r"@sha256:[0-9a-f]{64}$")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        run("git", "init", "-q", "-b", "ticket/T-123", cwd=self.worktree)
        run("git", "config", "user.name", "Test", cwd=self.worktree)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.worktree)
        (self.worktree / "app.txt").write_text("before\n")
        (self.worktree / "factory").mkdir()
        (self.worktree / "factory" / "PROJECT.env").write_text("SAFE=1\n")
        run("git", "add", ".", cwd=self.worktree)
        run("git", "commit", "-qm", "base", cwd=self.worktree)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.worktree).decode().strip()
        self.attempt = self.root / "attempt"
        self.artifacts = self.attempt / "artifacts"
        self.artifacts.mkdir(parents=True)
        self.locks = self.root / "locks"
        self.locks.mkdir(mode=0o700)
        self.policy = self.root / "artifact-policy.json"
        self.policy.write_text(
            canonical(
                {
                    "schema": "nysa.software-factory.provider-artifact-policy/v1",
                    "protected_paths": [
                        ".git",
                        ".github/workflows",
                        "factory/PROJECT.env",
                        "factory/.dispatch-leases",
                    ],
                }
            ) + "\n"
        )
        os.chmod(self.policy, 0o600)
        self.identity = {
            "attempt_id": "attempt-1",
            "base_sha": self.base,
            "binding_sha256": "b" * 64,
            "command": ["provider"],
            "container_name": "sf-attempt-1",
            "image": "worker@sha256:" + "c" * 64,
            "image_digest": "c" * 64,
            "input_sha256": "d" * 64,
            "policy_sha256": "e" * 64,
            "role": "builder",
            "route_id": "route-1",
            "schema": "nysa.software-factory.provider-container-identity/v2",
            "source_sha256": "f" * 64,
            "ticket": "T-123",
            "worker_sha256": "9" * 64,
        }
        (self.attempt / "identity.json").write_text(canonical(self.identity) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def make_patch(self, path="app.txt", content="after\n"):
        destination = self.worktree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        patch = run("git", "diff", "--binary", "--", path, cwd=self.worktree)
        run("git", "checkout", "--", path, cwd=self.worktree)
        return patch

    def write_bundle(self, patch, files=None, telemetry=None):
        files = files or ["app.txt"]
        (self.artifacts / "changes.patch").write_bytes(patch)
        artifact = {
            "attempt_id": self.identity["attempt_id"],
            "base_sha": self.identity["base_sha"],
            "binding_sha256": self.identity["binding_sha256"],
            "files": files,
            "image_digest": self.identity["image_digest"],
            "input_sha256": self.identity["input_sha256"],
            "patch_path": "changes.patch",
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "policy_sha256": self.identity["policy_sha256"],
            "role": self.identity["role"],
            "route_id": self.identity["route_id"],
            "schema": "nysa.software-factory.provider-patch-artifact/v1",
            "source_sha256": self.identity["source_sha256"],
            "telemetry": telemetry
            or {
                "charge_micro_usd": 4000,
                "duration_ms": 100,
                "input_tokens": 10,
                "output_tokens": 20,
                "provider_request_id": "request-1",
            },
            "ticket": self.identity["ticket"],
            "worker_sha256": self.identity["worker_sha256"],
        }
        (self.artifacts / "artifact.json").write_text(canonical(artifact) + "\n")
        hashed = []
        total = 0
        for name in ("artifact.json", "changes.patch"):
            raw = (self.artifacts / name).read_bytes()
            total += len(raw)
            encoded = name.encode()
            hashed.append(
                len(encoded).to_bytes(8, "big")
                + encoded
                + len(raw).to_bytes(8, "big")
                + raw
            )
        result = {
            "artifact_bytes": total,
            "artifact_sha256": hashlib.sha256(b"".join(hashed)).hexdigest(),
            "attempt_id": self.identity["attempt_id"],
            "base_sha": self.identity["base_sha"],
            "binding_sha256": self.identity["binding_sha256"],
            "container_name": self.identity["container_name"],
            "image_digest": self.identity["image_digest"],
            "input_sha256": self.identity["input_sha256"],
            "mode": "isolated-v1",
            "policy_sha256": self.identity["policy_sha256"],
            "return_code": 0,
            "role": self.identity["role"],
            "route_id": self.identity["route_id"],
            "schema": "nysa.software-factory.provider-execution-result/v2",
            "source_sha256": self.identity["source_sha256"],
            "stderr": "",
            "stderr_truncated": False,
            "stdout": "",
            "stdout_truncated": False,
            "ticket": self.identity["ticket"],
            "worker_sha256": self.identity["worker_sha256"],
        }
        (self.attempt / "result.json").write_text(canonical(result) + "\n")

    def command(self, action, *, expected=0, base=None):
        result = subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--attempt",
                str(self.attempt),
                "--worktree",
                str(self.worktree),
                "--policy",
                str(self.policy),
                "--lock",
                str(self.locks / "T-123.lock"),
                "--expected-branch",
                "ticket/T-123",
                "--base-sha",
                base or self.base,
                "--reserve-micro-usd",
                "5000",
                action,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_validates_then_applies_once_under_bound_identity(self):
        self.write_bundle(self.make_patch())
        validated = self.command("validate")
        self.assertEqual(validated["status"], "valid")
        self.assertEqual((self.worktree / "app.txt").read_text(), "before\n")
        applied = self.command("apply")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual((self.worktree / "app.txt").read_text(), "after\n")
        self.assertRegex(applied["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            run("git", "show", "--format=", "--name-only", "HEAD", cwd=self.worktree)
            .decode()
            .strip(),
            "app.txt",
        )
        self.assertEqual(run("git", "status", "--porcelain", cwd=self.worktree), b"")
        replay = self.command("apply", expected=2)
        self.assertIn("already applied", replay["error"])

    def test_rejects_base_drift_and_manifest_path_mismatch(self):
        self.write_bundle(self.make_patch(), files=["other.txt"])
        mismatch = self.command("validate", expected=2)
        self.assertIn("patch paths", mismatch["error"])
        self.write_bundle(self.make_patch(), files=["app.txt"])
        drift = self.command("validate", expected=2, base="a" * 40)
        self.assertIn("expected base SHA", drift["error"])

    def test_rejects_protected_path_and_symlink_output(self):
        patch = self.make_patch("factory/PROJECT.env", "UNSAFE=1\n")
        self.write_bundle(patch, files=["factory/PROJECT.env"])
        protected = self.command("validate", expected=2)
        self.assertIn("protected path", protected["error"])
        (self.artifacts / "changes.patch").unlink()
        (self.artifacts / "changes.patch").symlink_to("/etc/passwd")
        unsafe = self.command("validate", expected=2)
        self.assertIn("unsafe", unsafe["error"])

    def test_rejects_overspend_malformed_telemetry_and_tampering(self):
        telemetry = {
            "charge_micro_usd": 5001,
            "duration_ms": 100,
            "input_tokens": 10,
            "output_tokens": 20,
            "provider_request_id": "request-1",
        }
        self.write_bundle(self.make_patch(), telemetry=telemetry)
        overspend = self.command("validate", expected=2)
        self.assertIn("exceeds its reservation", overspend["error"])
        (self.artifacts / "changes.patch").write_text("tampered\n")
        tampered = self.command("validate", expected=2)
        self.assertTrue(
            "digest mismatch" in tampered["error"]
            or "does not bind" in tampered["error"]
        )


if __name__ == "__main__":
    unittest.main()
