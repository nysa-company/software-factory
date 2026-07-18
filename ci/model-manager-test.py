#!/usr/bin/env python3
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "scripts" / "model-manager.py"
ROUTER_PATH = ROOT / "scripts" / "model-router.py"
SPEC = importlib.util.spec_from_file_location("model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)
MANAGER_SPEC = importlib.util.spec_from_file_location("model_manager", MANAGER)
MANAGER_MODULE = importlib.util.module_from_spec(MANAGER_SPEC)
MANAGER_SPEC.loader.exec_module(MANAGER_MODULE)


class ModelManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.project = "factory-test"
        self.catalog, self.routes, _, self.profiles = ROUTER.load_policy()
        self.readiness = {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "test",
                "reported_identity": route["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, route in self.routes.items()
            if route["enabled"]
        }

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, operation, *arguments, project=None, check=True):
        command = [
            str(MANAGER),
            operation,
            "--state-root",
            str(self.state),
            "--project",
            project or self.project,
            *arguments,
        ]
        result = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        if check and result.returncode:
            self.fail("command failed: %s" % result.stderr)
        return result

    def output(self, operation, *arguments, project=None):
        return json.loads(self.command(operation, *arguments, project=project).stdout)

    def profile_hash(self, profile_id):
        return ROUTER.profile_hash(self.profiles[profile_id])

    def activate(self, profile_id="legacy-balanced-v1", project=None):
        return self.output(
            "activate",
            "--profile", profile_id,
            "--approve-hash", self.profile_hash(profile_id),
            "--approved-by", "operator-1",
            project=project,
        )

    def pin(self, output, ticket="T-123", kit_sha="a" * 40, readiness=None):
        return self.output(
            "pin",
            "--ticket", ticket,
            "--kit-sha", kit_sha,
            "--readiness", json.dumps(readiness or self.readiness),
            "--output", str(output),
        )

    def test_profiles_activation_hash_approval_and_default_legacy_plan(self):
        profiles = self.output("profiles")
        self.assertIsNone(profiles["active_profile"])
        self.assertEqual(
            {value["profile_id"] for value in profiles["profiles"]},
            set(self.profiles),
        )
        rejected = self.command(
            "activate",
            "--profile", "legacy-balanced-v1",
            "--approve-hash", "0" * 64,
            "--approved-by", "operator-1",
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        active = self.activate()
        self.assertEqual(active["profile_hash"], self.profile_hash("legacy-balanced-v1"))
        active_path = self.state / self.project / "routing" / "active.json"
        self.assertEqual(active_path.stat().st_mode & 0o777, 0o600)

        plan = self.output("plan", "--readiness", json.dumps(self.readiness))
        self.assertEqual(plan["profile_id"], "legacy-balanced-v1")

    def test_per_project_isolation(self):
        self.activate("claude-priority-v1", project="project-one")
        one = self.output("profiles", project="project-one")
        two = self.output("profiles", project="project-two")
        self.assertEqual(one["active_profile"]["profile_id"], "claude-priority-v1")
        self.assertIsNone(two["active_profile"])

    def test_existing_pin_is_exact_and_profile_change_does_not_change_it(self):
        output = self.base / "ticket-plan.json"
        first = self.pin(output)
        original = output.read_bytes()
        self.activate("claude-priority-v1")
        second = self.pin(output, readiness={"not": "consulted"})
        self.assertEqual(first, second)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_pin_accepts_fully_validated_absolute_resolution_file(self):
        resolution = self.base / "resolution.json"
        plan = self.output("plan", "--readiness", json.dumps(self.readiness))
        resolution.write_text(ROUTER.canonical_json(plan) + "\n")
        os.chmod(resolution, 0o600)
        output = self.base / "ticket-plan.json"
        pin = self.output(
            "pin",
            "--ticket", "T-123",
            "--kit-sha", "a" * 40,
            "--resolution-file", str(resolution),
            "--output", str(output),
        )
        self.assertEqual(pin["resolution"], plan)

        plan["selections"]["planner"]["selection_id"] = "tampered"
        resolution.write_text(ROUTER.canonical_json(plan) + "\n")
        os.chmod(resolution, 0o600)
        rejected = self.command(
            "pin",
            "--ticket", "T-124",
            "--kit-sha", "a" * 40,
            "--resolution-file", str(resolution),
            "--output", str(self.base / "bad-plan.json"),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)

    def test_pin_requires_exactly_one_resolution_source(self):
        common = (
            "--ticket", "T-123",
            "--kit-sha", "a" * 40,
            "--output", str(self.base / "ticket-plan.json"),
        )
        self.assertEqual(self.command("pin", *common, check=False).returncode, 2)
        resolution = self.base / "resolution.json"
        resolution.write_text("{}\n")
        os.chmod(resolution, 0o600)
        self.assertEqual(
            self.command(
                "pin", *common,
                "--readiness", json.dumps(self.readiness),
                "--resolution-file", str(resolution),
                check=False,
            ).returncode,
            2,
        )

    def test_select_validates_tampering_ticket_and_kit(self):
        output = self.base / "ticket-plan.json"
        pin = self.pin(output)
        selected = self.output(
            "select",
            "--ticket-plan", str(output),
            "--ticket", "T-123",
            "--kit-sha", "a" * 40,
            "--role", "planner",
        )
        self.assertEqual(selected, pin["resolution"]["selections"]["planner"])
        for arguments in (
            ("--ticket", "T-999", "--kit-sha", "a" * 40),
            ("--ticket", "T-123", "--kit-sha", "b" * 40),
        ):
            result = self.command(
                "select",
                "--ticket-plan", str(output),
                *arguments,
                "--role", "planner",
                check=False,
            )
            self.assertEqual(result.returncode, 2)

        pin["resolution"]["selections"]["planner"]["selection_id"] = "tampered"
        output.write_text(ROUTER.canonical_json(pin) + "\n")
        os.chmod(output, 0o644)
        result = self.command(
            "select",
            "--ticket-plan", str(output),
            "--ticket", "T-123",
            "--kit-sha", "a" * 40,
            "--role", "planner",
            check=False,
        )
        self.assertEqual(result.returncode, 2)

        output.write_text(ROUTER.canonical_json(pin) + "\n")
        os.chmod(output, 0o600)
        self.assertEqual(
            self.command(
                "select",
                "--ticket-plan", str(output),
                "--ticket", "T-123",
                "--kit-sha", "a" * 40,
                "--role", "planner",
                check=False,
            ).returncode,
            2,
        )

    def test_strict_modes_and_symlinks_are_rejected(self):
        self.activate()
        active = self.state / self.project / "routing" / "active.json"
        os.chmod(active, 0o644)
        self.assertEqual(self.command("status", check=False).returncode, 2)
        os.chmod(active, 0o600)

        other_state = self.base / "other-state"
        other_state.mkdir()
        (other_state / "linked").symlink_to(self.state)
        result = subprocess.run(
            [
                str(MANAGER), "status", "--state-root", str(other_state / "linked"),
                "--project", self.project,
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 2)

    def test_ttl_expiry_disable_enable_and_scope_types(self):
        for ttl in ("0", str(7 * 24 * 60 * 60 + 1)):
            result = self.command(
                "disable",
                "--scope-type", "route",
                "--scope-id", "codex-gpt-5.6-sol",
                "--reason", "credits_exhausted",
                "--ttl-seconds", ttl,
                "--operator-id", "operator-1",
                check=False,
            )
            self.assertEqual(result.returncode, 2)

        scopes = (
            ("route", "codex-gpt-5.6-sol"),
            ("account-route", "codex-native"),
            ("provider-family", "openai"),
            ("model", "gpt-5.6-sol"),
        )
        for scope_type, scope_id in scopes:
            self.output(
                "disable",
                "--scope-type", scope_type,
                "--scope-id", scope_id,
                "--reason", "credits_exhausted",
                "--ttl-seconds", "60",
                "--operator-id", "operator-1",
            )
        status = self.output("status")
        self.assertEqual(len(status["overrides"]), 4)
        self.output(
            "enable",
            "--scope-type", "route",
            "--scope-id", "codex-gpt-5.6-sol",
        )
        self.assertEqual(len(self.output("status")["overrides"]), 3)

        overrides = self.state / self.project / "routing" / "overrides.json"
        value = json.loads(overrides.read_text())
        value["overrides"][0]["expires_at"] = "2000-01-01T00:00:01Z"
        value["overrides"][0]["created_at"] = "2000-01-01T00:00:00Z"
        overrides.write_text(ROUTER.canonical_json(value) + "\n")
        os.chmod(overrides, 0o600)
        before = overrides.read_bytes()
        self.assertEqual(len(self.output("status")["overrides"]), 2)
        self.assertEqual(overrides.read_bytes(), before)

    def test_probe_context_applies_scopes_and_uses_active_or_default_profile(self):
        initial = self.output("probe-context")
        self.assertEqual(initial["profile_id"], "legacy-balanced-v1")
        self.assertEqual(initial["disabled_route_ids"], [])

        self.activate("claude-priority-v1")
        for scope_type, scope_id in (
            ("account-route", "codex-native"),
            ("provider-family", "anthropic"),
            ("model", "gpt-5.6-sol-high"),
            ("route", "codex-gpt-5.6-sol"),
        ):
            self.output(
                "disable",
                "--scope-type", scope_type,
                "--scope-id", scope_id,
                "--reason", "credits_exhausted",
                "--ttl-seconds", "60",
                "--operator-id", "operator-1",
            )
        context = self.output("probe-context")
        expected = sorted(
            route_id
            for route_id, route in self.routes.items()
            if (
                route["account_route_id"] == "codex-native"
                or route["provider_family"] == "anthropic"
                or route["selection_id"] == "gpt-5.6-sol-high"
                or route_id == "codex-gpt-5.6-sol"
            )
        )
        self.assertEqual(context["profile_id"], "claude-priority-v1")
        self.assertEqual(context["disabled_route_ids"], expected)

    def test_ticket_convention_accepts_variable_length_numeric_ids(self):
        output = self.base / "ticket-plan.json"
        pin = self.pin(output, ticket="T-7")
        self.assertEqual(pin["ticket"], "T-7")
        rejected = self.command(
            "pin",
            "--ticket", "T-ABC",
            "--kit-sha", "a" * 40,
            "--readiness", json.dumps(self.readiness),
            "--output", str(self.base / "bad.json"),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)

    def test_duplicate_keys_and_credential_like_fields_are_absent(self):
        self.activate()
        active = self.state / self.project / "routing" / "active.json"
        active.write_text(
            '{"schema":"model-routing-active/v1","schema":"duplicate"}\n'
        )
        os.chmod(active, 0o600)
        self.assertEqual(self.command("status", check=False).returncode, 2)
        active.unlink()

        output = self.base / "credential-check.json"
        pin = self.pin(output)

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        forbidden = {"token", "password", "secret", "api_key", "credential"}
        self.assertFalse({key.lower() for key in keys(pin)} & forbidden)

    def test_explicit_v1_migration_preserves_exact_blob_and_provenance(self):
        legacy_path = self.base / "legacy-plan.json"
        legacy = self.pin(legacy_path, kit_sha="a" * 40)
        original = legacy_path.read_bytes()
        journal = MANAGER_MODULE.migrate_v1_plan(
            original,
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        MANAGER_MODULE.validate_journal(
            journal, self.catalog, self.routes, self.profiles
        )
        body = journal["revisions"][0]["body"]
        self.assertEqual(
            __import__("base64").b64decode(body["legacy_plan_b64"]), original
        )
        self.assertEqual(body["pin_commit"], "b" * 40)
        self.assertEqual(body["old_kit_sha"], "a" * 40)
        self.assertEqual(body["new_kit_sha"], "c" * 40)
        self.assertEqual(body["policy_hash"], legacy["resolution"]["policy_hash"])
        self.assertEqual(
            body["historical_selections"], legacy["resolution"]["selections"]
        )

    def test_append_only_fallback_revision_is_parent_hashed_and_selectable(self):
        legacy_path = self.base / "legacy-plan.json"
        legacy = self.pin(legacy_path)
        journal = MANAGER_MODULE.migrate_v1_plan(
            legacy_path.read_bytes(),
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        before = copy.deepcopy(journal)
        fallback = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            self.profiles[legacy["resolution"]["profile_id"]],
            self.readiness,
            legacy["resolution"],
            "builder",
            "codex-gpt-5.6-terra",
            ["builder", "reviewer"],
            {"P": ["openai"], "T": ["anthropic"], "B": ["openai"]},
        )
        updated = MANAGER_MODULE.append_fallback_revision(
            journal,
            fallback,
            "d" * 64,
            "e" * 64,
            "provider_unavailable",
            {"operator_id": "operator-1", "receipt_id": "receipt-1"},
            "2026-07-18T12:01:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        self.assertEqual(journal, before)
        self.assertEqual(updated["revisions"][:1], journal["revisions"])
        self.assertEqual(updated["revisions"][1]["revision"], 1)
        self.assertEqual(
            updated["revisions"][1]["parent_hash"],
            updated["revisions"][0]["revision_hash"],
        )
        MANAGER_MODULE.validate_journal(
            updated, self.catalog, self.routes, self.profiles
        )

        journal_path = self.base / "journal.json"
        journal_path.write_text(ROUTER.canonical_json(updated) + "\n")
        os.chmod(journal_path, 0o644)
        selected = self.output(
            "select",
            "--ticket-plan", str(journal_path),
            "--ticket", "T-123",
            "--kit-sha", "c" * 40,
            "--role", "builder",
        )
        self.assertEqual(selected, fallback["selections"]["builder"])

    def test_journal_tampering_non_monotonic_revision_and_ineligible_reason_fail(self):
        legacy_path = self.base / "legacy-plan.json"
        self.pin(legacy_path)
        journal = MANAGER_MODULE.migrate_v1_plan(
            legacy_path.read_bytes(),
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        for mutate in (
            lambda value: value["revisions"][0].update(revision=1),
            lambda value: value["revisions"][0]["body"].update(
                historical_selections={}
            ),
        ):
            tampered = copy.deepcopy(journal)
            mutate(tampered)
            with self.assertRaises(MANAGER_MODULE.ManagerError):
                MANAGER_MODULE.validate_journal(
                    tampered, self.catalog, self.routes, self.profiles
                )
        with self.assertRaisesRegex(MANAGER_MODULE.ManagerError, "not eligible"):
            MANAGER_MODULE.append_fallback_revision(
                journal,
                {},
                "d" * 64,
                "e" * 64,
                "model_error",
                {},
                "2026-07-18T12:01:00Z",
                self.catalog,
                self.routes,
                self.profiles,
            )


if __name__ == "__main__":
    unittest.main()
