#!/usr/bin/env python3
"""Focused fail-closed publication conflict policy tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/publication-conflict-policy.py"


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class PublicationConflictPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name).resolve() / "repo"
        self.repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.repo)
        (self.repo / "app").mkdir()
        (self.repo / "factory").mkdir()
        (self.repo / "app/shared").write_text("base\n")
        (self.repo / "package.json").write_text('{"base":true}\n')
        (self.repo / "factory/PROJECT.env").write_text("TEST_PATHS=tests\n")
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "base", cwd=self.repo)
        self.base = run("git", "rev-parse", "HEAD", cwd=self.repo)
        run("git", "switch", "-qc", "sealed", cwd=self.repo)
        (self.repo / "app/shared").write_text("sealed\n")
        (self.repo / "package.json").write_text('{"sealed":true}\n')
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "sealed", cwd=self.repo)
        run("git", "update-ref", "refs/retry/T-110", "HEAD", cwd=self.repo)
        run("git", "switch", "-q", "main", cwd=self.repo)
        (self.repo / "app/shared").write_text("protected\n")
        (self.repo / "package.json").write_text('{"protected":true}\n')
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "protected", cwd=self.repo)
        self.protected = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.receipt = self.repo / "receipt.json"
        self.receipt.write_text(json.dumps({
            "checkpoint_base_sha": self.base,
            "new_protected_base_sha": self.protected,
            "old_protected_base_sha": self.base,
            "repair_owner": "builder",
            "ticket": "T-110",
        }))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "python3", str(HELPER), "--receipt", str(self.receipt),
                "--project", str(self.repo / "factory/PROJECT.env"),
                "--workdir", str(self.repo), "--ticket", "T-110", path,
            ],
            text=True, capture_output=True, check=False,
        )

    def test_product_conflict_preserves_protected_main_but_config_fails_closed(self) -> None:
        allowed = self.command("app/shared")
        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(allowed.stdout.strip(), "ours\tapp/shared")
        self.assertNotEqual(self.command("package.json").returncode, 0)
        self.assertNotEqual(self.command("factory/PROJECT.env").returncode, 0)


if __name__ == "__main__":
    unittest.main()
