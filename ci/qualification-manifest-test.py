#!/usr/bin/env python3
"""Regression coverage for the shared committed qualification-manifest gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/lib/qualification_manifest.py"
KIT_SHA = "a" * 40
SOURCE_SHA = "b" * 40


def run(
    root: Path, *args: str, check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=root, text=True, capture_output=True, check=check, env=env,
    )


def commit(root: Path, message: str) -> str:
    run(root, "git", "add", "-A")
    run(root, "git", "commit", "-qm", message)
    return run(root, "git", "rev-parse", "HEAD").stdout.strip()


def ordinary(factory_sha: str = KIT_SHA) -> dict:
    return {
        "budget_usd": "100.000000",
        "capacity": 3,
        "contract_version": "1.8.0",
        "factory_sha": factory_sha,
        "generation": 1,
        "per_run_budget_usd": "2.000000",
        "per_ticket_budget_usd": "25.000000",
        "schema": "nysa.software-factory.qualification/v2",
        "target_done": 3,
        "tickets": ["T-101", "T-102", "T-103"],
    }


def successor() -> dict:
    value = ordinary()
    value.update({
        "budget_usd": "300.000000",
        "mode": "successor",
        "per_run_budget_usd": "10.000000",
        "per_ticket_budget_usd": "100.000000",
        "source_factory_sha": SOURCE_SHA,
    })
    return value


class QualificationManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="qualification-manifest-test."))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace)

    def repository(
        self, name: str = "product", with_pin: bool = True,
        kit_sha: str = KIT_SHA,
    ) -> tuple[Path, str]:
        root = self.workspace / name
        root.mkdir()
        run(root, "git", "init", "-q", "-b", "main")
        run(root, "git", "config", "user.name", "Manifest Test")
        run(root, "git", "config", "user.email", "manifest@example.invalid")
        (root / "app.txt").write_text("base\n", encoding="utf-8")
        if with_pin:
            (root / "factory").mkdir()
            (root / "factory/KIT_PIN").write_text(kit_sha + "\n", encoding="ascii")
        return root, commit(root, "base")

    def installed_parser(self, source: Path = CHECK, pin: str = KIT_SHA) -> dict[str, str]:
        home = self.workspace / f"home-{pin[:8]}"
        parser = home / ".factory/kits/releases" / pin / "scripts/lib/qualification_manifest.py"
        parser.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, parser)
        return {**os.environ, "HOME": str(home)}

    def invoke(
        self, root: Path, base: str, head: str,
    ) -> subprocess.CompletedProcess[str]:
        return run(
            root, "python3", str(CHECK), "--product-root", str(root),
            "--base", base, "--head", head, check=False,
        )

    def write_manifest(self, root: Path, value: object, message: str) -> str:
        (root / "factory/QUALIFICATION.json").write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8",
        )
        return commit(root, message)

    def test_exact_ordinary_and_successor_manifests_pass(self) -> None:
        root, base = self.repository()
        ordinary_head = self.write_manifest(root, ordinary(), "ordinary")
        result = self.invoke(root, base, ordinary_head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "QUALIFICATION MANIFEST VALIDATED\n")

        high_budget = ordinary()
        high_budget.update({
            "budget_usd": "300.000000",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
        })
        high_budget_head = self.write_manifest(root, high_budget, "high-budget ordinary")
        result = self.invoke(root, ordinary_head, high_budget_head)
        self.assertEqual(result.returncode, 0, result.stderr)

        successor_head = self.write_manifest(root, successor(), "successor")
        result = self.invoke(root, high_budget_head, successor_head)
        self.assertEqual(result.returncode, 0, result.stderr)

        four = ordinary()
        four.update({
            "capacity": 4,
            "target_done": 4,
            "tickets": ["T-101", "T-102", "T-103", "T-104"],
        })
        four_head = self.write_manifest(root, four, "ordinary capacity four")
        result = self.invoke(root, successor_head, four_head)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_contract_shapes_refuse(self) -> None:
        cases: dict[str, dict] = {}
        value = ordinary(); value.update({
            "budget_usd": "300.000000",
            "per_run_budget_usd": "10.000000",
        }); cases["mixed budget profile"] = value
        value = ordinary(); value.pop("generation"); cases["missing key"] = value
        value = ordinary(); value["extra"] = True; cases["extra key"] = value
        value = ordinary(); value["tickets"][0] = "ticket-101"; cases["malformed ticket"] = value
        value = ordinary(); value["tickets"][1] = "T-101"; cases["duplicate ticket"] = value
        value = ordinary(); value["factory_sha"] = SOURCE_SHA; cases["wrong Factory SHA"] = value
        value = ordinary(); value["capacity"] = 2; cases["invalid capacity"] = value
        value = ordinary(); value["target_done"] = 4; cases["invalid target"] = value
        value = ordinary(); value.update({
            "budget_usd": "300.000000",
            "capacity": 4,
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "target_done": 4,
            "tickets": ["T-101", "T-102", "T-103", "T-104"],
        }); cases["extended capacity four"] = value
        value = successor(); value["capacity"] = 4; cases["invalid successor capacity"] = value
        value = successor(); value["source_factory_sha"] = "invalid"; cases["invalid source SHA"] = value
        value = successor(); value["source_factory_sha"] = KIT_SHA; cases["same source SHA"] = value

        for index, (label, value) in enumerate(cases.items()):
            with self.subTest(label=label):
                root, base = self.repository(f"invalid-{index}")
                head = self.write_manifest(root, value, label)
                result = self.invoke(root, base, head)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("QUALIFICATION MANIFEST FAIL:", result.stderr)

    def test_malformed_ticket_variants_refuse_deterministically(self) -> None:
        root, base = self.repository("malformed-tickets")
        for label, malformed in (
            ("path traversal", "../T-101"),
            ("trailing whitespace", "T-101 "),
            ("leading whitespace", " T-101"),
            ("Unicode hyphen", "T‐101"),
            ("Unicode digits", "T-١٠١"),
            ("embedded newline", "T-10\n1"),
        ):
            with self.subTest(label=label):
                value = ordinary()
                value["tickets"][0] = malformed
                head = self.write_manifest(root, value, label)
                before = run(
                    root, "git", "status", "--porcelain=v1", "-z",
                ).stdout
                result = self.invoke(root, base, head)
                replay = self.invoke(root, base, head)
                self.assertEqual(
                    (replay.returncode, replay.stdout, replay.stderr),
                    (result.returncode, result.stdout, result.stderr),
                )
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("QUALIFICATION MANIFEST FAIL:", result.stderr)
                self.assertEqual(
                    run(root, "git", "status", "--porcelain=v1", "-z").stdout,
                    before,
                )

    def test_duplicate_raw_fields_refuse_before_last_value_wins(self) -> None:
        root, base = self.repository()
        raw = json.dumps(ordinary(), sort_keys=True)[:-1] + ',"generation":2}\n'
        (root / "factory/QUALIFICATION.json").write_text(raw, encoding="utf-8")
        head = commit(root, "duplicate manifest field")
        result = self.invoke(root, base, head)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("qualification manifest has duplicate fields", result.stderr)

    def test_committed_blobs_are_authoritative_and_deletion_is_inert(self) -> None:
        root, base = self.repository()
        head = self.write_manifest(root, ordinary(), "valid")
        dirty = successor()
        dirty["source_factory_sha"] = "invalid"
        (root / "factory/QUALIFICATION.json").write_text(json.dumps(dirty) + "\n")
        result = self.invoke(root, base, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        run(root, "git", "restore", "factory/QUALIFICATION.json")

        (root / "factory/QUALIFICATION.json").unlink()
        deleted = commit(root, "remove qualification authority")
        result = self.invoke(root, head, deleted)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "QUALIFICATION MANIFEST ABSENT\n")

    def test_unrelated_change_skips_without_pin_or_parser_input(self) -> None:
        root, base = self.repository(with_pin=False)
        (root / "app.txt").write_text("changed\n", encoding="utf-8")
        head = commit(root, "application change")
        result = self.invoke(root, base, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "QUALIFICATION MANIFEST UNCHANGED\n")

    def test_committed_control_files_must_be_safe_regular_blobs(self) -> None:
        cases = (
            "executable manifest", "symlink manifest", "executable pin",
            "symlink pin", "malformed pin", "oversized pin",
        )
        for index, label in enumerate(cases):
            with self.subTest(label=label):
                root, base = self.repository(f"unsafe-{index}")
                manifest = root / "factory/QUALIFICATION.json"
                manifest.write_text(json.dumps(ordinary()) + "\n")
                pin = root / "factory/KIT_PIN"
                if label == "executable manifest":
                    manifest.chmod(0o755)
                elif label == "symlink manifest":
                    manifest.unlink()
                    os.symlink("../app.txt", manifest)
                elif label == "executable pin":
                    pin.chmod(0o755)
                elif label == "symlink pin":
                    pin.unlink()
                    os.symlink("../app.txt", pin)
                elif label == "malformed pin":
                    pin.write_text("A" * 40 + "\n", encoding="ascii")
                elif label == "oversized pin":
                    pin.write_text("a" * 65, encoding="ascii")
                head = commit(root, label)
                result = self.invoke(root, base, head)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("QUALIFICATION MANIFEST FAIL:", result.stderr)

    def test_invalid_revisions_and_nested_product_root_fail_closed(self) -> None:
        root, base = self.repository()
        head = self.write_manifest(root, ordinary(), "manifest")
        self.assertEqual(self.invoke(root, "missing", head).returncode, 1)
        self.assertEqual(self.invoke(root, base, "missing").returncode, 1)
        nested = root / "nested"
        nested.mkdir()
        result = run(
            root, "python3", str(CHECK), "--product-root", str(nested),
            "--base", base, "--head", head, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("product is not a Git checkout", result.stderr)

    def test_zero_base_validates_present_manifest_and_local_readiness_is_full(self) -> None:
        root = self.workspace / "initial"
        root.mkdir()
        run(root, "git", "init", "-q", "-b", "main")
        run(root, "git", "config", "user.name", "Manifest Test")
        run(root, "git", "config", "user.email", "manifest@example.invalid")
        (root / "factory").mkdir()
        (root / "factory/KIT_PIN").write_text(KIT_SHA + "\n", encoding="ascii")
        (root / "factory/QUALIFICATION.json").write_text(
            json.dumps(ordinary()) + "\n", encoding="utf-8",
        )
        head = commit(root, "initial qualification")
        result = self.invoke(root, "0" * 40, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "QUALIFICATION MANIFEST VALIDATED\n")
        readiness = run(
            root, "bash", str(ROOT / "ci/lightweight-change.sh"), "0" * 40, head,
            check=False, env=self.installed_parser(),
        )
        self.assertEqual(readiness.returncode, 1, readiness.stderr)

    def test_noop_pinned_parser_fails_local_readiness_closed(self) -> None:
        factory = self.workspace / "old-factory"
        parser = factory / "scripts/lib/qualification_manifest.py"
        parser.parent.mkdir(parents=True)
        run(factory, "git", "init", "-q", "-b", "main")
        run(factory, "git", "config", "user.name", "Manifest Test")
        run(factory, "git", "config", "user.email", "manifest@example.invalid")
        parser.write_text("# old parser had no CLI\n", encoding="utf-8")
        factory_sha = commit(factory, "old parser")
        root, base = self.repository("noop", kit_sha=factory_sha)
        head = self.write_manifest(root, ordinary(factory_sha), "manifest")
        result = run(
            root, "bash", str(ROOT / "ci/lightweight-change.sh"), base, head,
            check=False,
            env={**os.environ, "FACTORY_QUALIFICATION_PARSER": str(parser)},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("qualification manifest validation failed", result.stderr)

        for script in (
            "test-all.sh", "changed-test-suites.sh", "lightweight-change.sh",
            "suite-registry.sh", "suite-groups.sh",
        ):
            destination = root / "ci" / script
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "ci" / script, destination)
        readiness_base = commit(root, "local readiness")
        changed = ordinary(factory_sha)
        changed["generation"] = 2
        readiness_head = self.write_manifest(root, changed, "changed manifest")
        result = run(
            root, "bash", "ci/test-all.sh", "--changed-or-defer",
            readiness_base, readiness_head, check=False,
            env={
                **os.environ,
                "CI_FORCE_FULL": "0",
                "FACTORY_QUALIFICATION_PARSER": str(parser),
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("CI selection refused", result.stderr)

    def test_product_template_and_local_readiness_use_the_shared_parser(self) -> None:
        workflow = (ROOT / "ci/github-actions-ci.template.yml").read_text()
        setup = (ROOT / "docs/factory-setup.md").read_text()
        lightweight = (ROOT / "ci/lightweight-change.sh").read_text()
        parser_path = "scripts/lib/qualification_manifest.py"
        self.assertIn(f".qualification-kit/{parser_path}", workflow)
        self.assertIn('expected_release="$HOME/.factory/kits/releases/$pin"', lightweight)
        self.assertIn(f'parser="$expected_release/{parser_path}"', lightweight)
        self.assertIn("copied local readiness classifier", setup)
        self.assertEqual(
            workflow.count("if: steps.qualification.outputs.changed == 'true'"), 1,
        )
        self.assertEqual(workflow.count("FACTORY_REPOSITORY_TOKEN"), 1)
        self.assertIn("ref: ${{ steps.qualification.outputs.pin }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('test ! -e "$GITHUB_WORKSPACE/.qualification-kit"', workflow)
        self.assertIn('test ! -L "$GITHUB_WORKSPACE/.qualification-kit"', workflow)
        self.assertIn("QUALIFICATION MANIFEST VALIDATED", lightweight)
        self.assertIn('*) exit "$status"', workflow)
        self.assertIn('*) exit "$LIGHTWEIGHT_STATUS"', (ROOT / "ci/changed-test-suites.sh").read_text())
        self.assertIn(
            "if test \"$BASE_SHA\" = 0000000000000000000000000000000000000000",
            workflow,
        )
        self.assertIn('test "${#pin}" = 40', workflow)
        self.assertIn('test -z "${pin//[0-9a-f]/}"', workflow)
        self.assertIn(
            'if git cat-file -e "$GITHUB_SHA:factory/QUALIFICATION.json"; then',
            workflow,
        )
        self.assertLess(
            workflow.index('test -z "${pin//[0-9a-f]/}"'),
            workflow.index("repository: nysa-company/software-factory"),
        )
        inspect = workflow.split("- name: inspect qualification control", 1)[1]
        inspect = inspect.split("- uses: actions/checkout@v5", 1)[0]
        self.assertIn('else\n                echo "changed=false"', inspect)

        target = self.workspace / ".qualification-kit"
        target.mkdir()
        collision = run(
            self.workspace, "bash", "-c", 'test ! -e "$1" && test ! -L "$1"',
            "collision-check", str(target), check=False,
        )
        self.assertNotEqual(collision.returncode, 0)
        target.rmdir()
        os.symlink("missing", target)
        collision = run(
            self.workspace, "bash", "-c", 'test ! -e "$1" && test ! -L "$1"',
            "collision-check", str(target), check=False,
        )
        self.assertNotEqual(collision.returncode, 0)

    def test_manifest_only_change_stays_lightweight_but_application_does_not(self) -> None:
        root, base = self.repository()
        head = self.write_manifest(root, ordinary(), "manifest")
        lightweight = run(
            root, "bash", str(ROOT / "ci/lightweight-change.sh"), base, head,
            check=False, env=self.installed_parser(),
        )
        self.assertEqual(lightweight.returncode, 0)
        mixed_root, mixed_base = self.repository("mixed")
        (mixed_root / "factory/QUALIFICATION.json").write_text(
            json.dumps(ordinary()) + "\n", encoding="utf-8",
        )
        (mixed_root / "app.txt").write_text("changed\n", encoding="utf-8")
        application = commit(mixed_root, "manifest and application")
        broad = run(
            mixed_root, "bash", str(ROOT / "ci/lightweight-change.sh"),
            mixed_base, application,
            check=False, env=self.installed_parser(),
        )
        self.assertEqual(broad.returncode, 1)


if __name__ == "__main__":
    unittest.main()
