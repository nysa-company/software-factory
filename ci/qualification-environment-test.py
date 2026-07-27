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
import json


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
            prefix="nysa-sf-qualification.q-", dir="/private/tmp",
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
        (self.factory / "scripts/model-routing").mkdir(parents=True)
        shutil.copy2(
            ROOT / "scripts/provider-activation.py",
            self.factory / "scripts/provider-activation.py",
        )
        (self.factory / "scripts/model-routing/catalog-v1.json").write_text(
            json.dumps({
                "routes": [{
                    "account_route_id": "cursor",
                    "adapter": "cursor-openai",
                    "enabled": True,
                    "provider_family": "openai",
                    "route_id": "cursor-test",
                    "selection_id": "test-model",
                }],
            }) + "\n",
            encoding="utf-8",
        )
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
        self.assertEqual(
            (self.root / "profile/projects/relay.env").read_text(),
            f"PRODUCT_ROOT={self.product.resolve()}\n",
        )
        self.assertEqual(
            json.loads((self.root / "marker.json").read_text()),
            {
                "mode": "qualification",
                "schema": "nysa.software-factory.qualification-environment/v1",
            },
        )
        status = json.loads(run(
            self.root,
            "/usr/bin/python3",
            str(release / "scripts/provider-activation.py"),
            "--config", str(self.root / "provider/provider-activation.json"),
            "--policy", str(self.root / "provider/provider-policy.json"),
            "--contract-version", "1.8.0",
            "--status",
        ))
        self.assertEqual(status["execution_mode"], "cli-concurrent-v1")
        launcher_text = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PROVIDER_STATE_ROOT="$QUALIFICATION_ROOT/provider"', launcher_text
        )
        self.assertIn(
            'HELPER_ENV+=("FACTORY_CLI_LANE_ROOT=$QUALIFICATION_ROOT")',
            launcher_text,
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "already exists",
        ):
            ENVIRONMENT.prepare(args)

    def test_rejects_root_too_long_for_cursor_scratch(self) -> None:
        root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.too-long-", dir="/private/tmp",
        )).resolve()
        os.chmod(root, 0o700)
        try:
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "too long for isolated Cursor",
            ):
                ENVIRONMENT.prepare(argparse.Namespace(
                    factory_root=self.factory,
                    product_root=self.product,
                    project="relay",
                    root=root,
                ))
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
