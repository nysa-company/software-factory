#!/usr/bin/env python3
"""Focused sealed qualification-environment test."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_environment", ROOT / "scripts/qualification-environment.py"
)
assert SPEC and SPEC.loader
ENVIRONMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENVIRONMENT)


def run(root: Path, *arguments: str) -> str:
    return subprocess.run(
        arguments, cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


class QualificationEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="qualification-test."))
        self.root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.test-", dir="/private/tmp",
        )).resolve()
        os.chmod(self.root, 0o700)
        self.factory = self.workspace / "factory"
        (self.factory / "integrations/hermes/bin").mkdir(parents=True)
        (self.factory / "integrations/hermes/contract.json").write_text(
            '{"contract_version":"1.8.0"}\n', encoding="utf-8",
        )
        launcher = self.factory / "integrations/hermes/bin/factory-launch"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        run(self.factory, "git", "init", "-q", "-b", "main")
        run(self.factory, "git", "config", "user.name", "Test")
        run(self.factory, "git", "config", "user.email", "test@example.invalid")
        run(self.factory, "git", "add", ".")
        run(self.factory, "git", "commit", "-qm", "candidate")
        self.sha = run(self.factory, "git", "rev-parse", "HEAD")

        self.product = self.workspace / "product"
        (self.product / "factory").mkdir(parents=True)
        (self.product / "factory/KIT_PIN").write_text(
            self.sha + "\n", encoding="utf-8",
        )
        run(self.product, "git", "init", "-q", "-b", "main")
        run(self.product, "git", "config", "user.name", "Test")
        run(self.product, "git", "config", "user.email", "test@example.invalid")
        run(self.product, "git", "remote", "add", "origin", "git@example.invalid")
        run(self.product, "git", "add", ".")
        run(self.product, "git", "commit", "-qm", "product")

    def tearDown(self) -> None:
        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        shutil.rmtree(self.workspace)

    def test_prepares_exact_read_only_candidate_once(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        release = Path(value["launcher"]).parents[3]
        self.assertEqual(value["factory_sha"], self.sha)
        self.assertEqual(ENVIRONMENT.git_tree(release), value["factory_tree"])
        self.assertFalse(release.stat().st_mode & 0o222)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "already exists",
        ):
            ENVIRONMENT.prepare(args)


if __name__ == "__main__":
    unittest.main()
