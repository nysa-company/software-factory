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
ATTEST_SPEC = importlib.util.spec_from_file_location(
    "ticket_attest", ROOT / "scripts" / "ticket-attest.py"
)
ATTEST_MODULE = importlib.util.module_from_spec(ATTEST_SPEC)
ATTEST_SPEC.loader.exec_module(ATTEST_MODULE)


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

    def model_policy(self):
        portfolio = self.profiles["balanced-v2"]["portfolios"][0]
        return {
            "checking_family": portfolio["checking_family"],
            "production_family": portfolio["production_family"],
            "roles": {
                role: {
                    "effort": value["effort"],
                    "primary_route_id": value["candidates"][0],
                    "secondary_route_id": value["candidates"][1],
                }
                for role, value in portfolio["roles"].items()
            },
            "schema": "factory-model-policy/v1",
            "version": 1,
        }

    def activate(self, profile_id="legacy-balanced-v1", project=None):
        return self.output(
            "activate",
            "--profile", profile_id,
            "--approve-hash", self.profile_hash(profile_id),
            "--approved-by", "operator-1",
            project=project,
        )

    def test_route_revision_hash_matches_attestation_for_unicode(self):
        body = {
            "kind": "fallback",
            "operator_note": "crédit épuisé",
        }
        self.assertEqual(
            MANAGER_MODULE._revision_hash(3, "a" * 64, body),
            ATTEST_MODULE.route_revision_hash(3, "a" * 64, body),
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

    def test_runtime_isolation_failure_can_circuit_break_one_route(self):
        self.output(
            "disable",
            "--scope-type", "route",
            "--scope-id", "claude-fable",
            "--reason", "runtime_isolation_failure",
            "--ttl-seconds", "60",
            "--operator-id", "factory-dev-lane",
        )
        status = self.output("status")
        self.assertEqual(status["overrides"][0]["reason"], "runtime_isolation_failure")
        self.assertEqual(status["overrides"][0]["scope_id"], "claude-fable")

    def test_probe_context_applies_scopes_and_uses_active_or_default_profile(self):
        initial = self.output("probe-context")
        self.assertEqual(initial["profile_id"], "cursor-opus-v2")
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

    def test_project_policy_preview_cas_apply_drives_plan_and_preserves_old_pin(self):
        policy_path = self.base / "model-policy.json"
        policy = self.model_policy()
        preview = self.output(
            "policy-preview",
            "--policy-file", str(policy_path),
            "--policy", json.dumps(policy),
        )
        self.assertEqual(preview["current_policy_hash"], ROUTER.content_hash(None))
        rejected = self.command(
            "policy-apply",
            "--policy-file", str(policy_path),
            "--policy", json.dumps(policy),
            "--expected-current-hash", "0" * 64,
            "--approve-hash", preview["preview_hash"],
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        applied = self.output(
            "policy-apply",
            "--policy-file", str(policy_path),
            "--policy", json.dumps(policy),
            "--expected-current-hash", preview["current_policy_hash"],
            "--approve-hash", preview["preview_hash"],
        )
        self.assertEqual(applied["policy_hash"], ROUTER.content_hash(policy))
        self.assertEqual(policy_path.stat().st_mode & 0o777, 0o644)

        plan = self.output(
            "plan",
            "--policy-file", str(policy_path),
            "--readiness", json.dumps(self.readiness),
        )
        self.assertEqual(plan["schema"], "model-resolution-plan/v2")
        self.assertEqual(plan["model_policy"], policy)
        pin_path = self.base / "project-pin.json"
        pin = self.output(
            "pin",
            "--policy-file", str(policy_path),
            "--ticket", "T-777",
            "--kit-sha", "a" * 40,
            "--readiness", json.dumps(self.readiness),
            "--output", str(pin_path),
        )

        changed = copy.deepcopy(policy)
        changed["roles"]["planner"]["effort"] = "medium"
        second_preview = self.output(
            "policy-preview",
            "--policy-file", str(policy_path),
            "--policy", json.dumps(changed),
        )
        self.output(
            "policy-apply",
            "--policy-file", str(policy_path),
            "--policy", json.dumps(changed),
            "--expected-current-hash", second_preview["current_policy_hash"],
            "--approve-hash", second_preview["preview_hash"],
        )
        selected = self.output(
            "select",
            "--policy-file", str(policy_path),
            "--ticket-plan", str(pin_path),
            "--ticket", "T-777",
            "--kit-sha", "a" * 40,
            "--role", "planner",
        )
        self.assertEqual(selected, pin["resolution"]["selections"]["planner"])

    def test_policy_candidates_and_ticket_scoped_reviewer_exception_contract(self):
        values = self.output("policy-candidates")
        self.assertEqual(values["efforts"], ["low", "medium", "high"])
        self.assertTrue(values["routes"])
        self.assertTrue(all(
            self.routes[value["route_id"]]["enabled"]
            and self.routes[value["route_id"]]["lifecycle"] == "stable"
            for value in values["routes"]
        ))
        contract = self.output("reviewer-exception-contract")
        self.assertTrue(contract["supported"])
        self.assertTrue(contract["ticket_scoped"])
        self.assertTrue(contract["one_use"])
        self.assertEqual(
            contract["approval"], "exact one-use operator fallback approval receipt"
        )

        policy = self.model_policy()
        policy["roles"]["reviewer"]["primary_route_id"] = "codex-gpt-5.6-sol"
        policy["roles"]["reviewer"]["secondary_route_id"] = "cursor-gpt-5.6-sol-high"
        rejected = self.command(
            "policy-preview",
            "--policy-file", str(self.base / "model-policy.json"),
            "--policy", json.dumps(policy),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("outside", rejected.stderr)

    def test_ticket_status_reads_state_and_validates_optional_pin(self):
        ticket = self.base / "T-123.md"
        ticket.write_text("# T-123\n\nState: Ready\n\nKit-SHA: %s\n" % ("a" * 40))
        os.chmod(ticket, 0o644)
        plan_path = self.base / "ticket-status-plan.json"
        self.pin(plan_path)
        status = self.output(
            "ticket-status",
            "--ticket", "T-123",
            "--ticket-file", str(ticket),
            "--ticket-plan", str(plan_path),
        )
        self.assertEqual(status["state"], "Ready")
        self.assertEqual(status["route_plan_status"], "pinned")
        self.assertRegex(status["route_plan_hash"], r"^[0-9a-f]{64}$")

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
        refreshed = MANAGER_MODULE.migrate_route_document(
            original,
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
            self.readiness,
        )
        self.assertEqual(len(refreshed["revisions"]), 2)
        self.assertEqual(
            refreshed["revisions"][0]["body"]["new_kit_sha"], "a" * 40
        )
        self.assertEqual(
            refreshed["revisions"][1]["body"]["new_kit_sha"], "c" * 40
        )
        refresh_body = refreshed["revisions"][1]["body"]
        self.assertNotIn("new_resolution", refresh_body)
        self.assertEqual(
            refresh_body["prior_resolution_sha256"],
            ROUTER.content_hash(legacy["resolution"]),
        )
        self.assertEqual(
            MANAGER_MODULE.active_resolution(refreshed), legacy["resolution"]
        )
        attest_root = self.base / "attest-product"
        (attest_root / "factory/route-plans").mkdir(parents=True)
        (attest_root / "factory/route-plans/T-123.json").write_text(
            ROUTER.canonical_json(refreshed) + "\n"
        )
        evidence = ATTEST_MODULE.route_plan_evidence(
            attest_root, attest_root, "T-123", "c" * 40, []
        )
        self.assertEqual(evidence["policy_hash"], legacy["resolution"]["policy_hash"])

        rejected = self.command(
            "migrate-plan",
            "--ticket-plan", str(legacy_path),
            "--pin-commit", "b" * 40,
            "--kit-sha", "c" * 40,
            "--migrated-at", "2026-07-18T12:00:00Z",
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)

    def test_migration_accepts_only_tuple_compatible_historical_catalog(self):
        historical_catalog = copy.deepcopy(self.catalog)
        historical_catalog["routes"] = [
            route for route in historical_catalog["routes"]
            if route["route_id"] != "cursor-claude-fable-5-thinking-medium"
        ]
        historical_routes = ROUTER.validate_catalog(historical_catalog)
        historical_readiness = {
            route_id: dict(value)
            for route_id, value in self.readiness.items()
            if route_id in historical_routes
        }
        resolution = ROUTER.resolve_policy(
            historical_catalog,
            historical_routes,
            self.profiles["legacy-balanced-v1"],
            historical_readiness,
        )
        legacy = {
            "created_at": "2026-07-18T11:00:00Z",
            "kit_sha": "a" * 40,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-123",
        }
        raw = (ROUTER.canonical_json(legacy) + "\n").encode()
        journal = MANAGER_MODULE.migrate_v1_plan(
            raw,
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        MANAGER_MODULE.validate_journal(
            journal, self.catalog, self.routes, self.profiles,
            allow_historical_active=True,
        )
        tampered = copy.deepcopy(legacy)
        tampered["resolution"]["selections"]["planner"]["selection_id"] = "auto"
        tampered_raw = (ROUTER.canonical_json(tampered) + "\n").encode()
        with self.assertRaisesRegex(MANAGER_MODULE.ManagerError, "tuple mismatch"):
            MANAGER_MODULE.migrate_v1_plan(
                tampered_raw,
                "b" * 40,
                "c" * 40,
                "2026-07-18T12:00:00Z",
                self.catalog,
                self.routes,
                self.profiles,
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
            legacy["resolution"]["selections"]["builder"]["route_id"],
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

    def test_release_migration_preserves_v2_history_and_active_resolution(self):
        legacy_path = self.base / "legacy-plan.json"
        legacy = self.pin(legacy_path)
        journal = MANAGER_MODULE.migrate_v1_plan(
            legacy_path.read_bytes(), "b" * 40, "c" * 40,
            "2026-07-18T12:00:00Z", self.catalog, self.routes, self.profiles,
        )
        fallback = ROUTER.resolve_fallback_policy(
            self.catalog, self.routes,
            self.profiles[legacy["resolution"]["profile_id"]],
            self.readiness, legacy["resolution"], "builder",
            legacy["resolution"]["selections"]["builder"]["route_id"],
            ["builder", "reviewer"],
            {"P": ["openai"], "T": ["anthropic"], "B": ["openai"]},
        )
        journal = MANAGER_MODULE.append_fallback_revision(
            journal, fallback, "d" * 64, "e" * 64,
            "provider_unavailable", {"receipt_id": "receipt-1"},
            "2026-07-18T12:01:00Z", self.catalog, self.routes, self.profiles,
        )
        before = copy.deepcopy(journal)
        resolution = MANAGER_MODULE.active_resolution(journal)
        self.assertEqual(
            MANAGER_MODULE.migrate_v2_journal(
                journal,
                "f" * 40,
                journal["kit_sha"],
                "2026-07-18T12:02:00Z",
                self.catalog,
                self.routes,
                self.profiles,
                self.readiness,
            ),
            journal,
        )
        migrated = MANAGER_MODULE.migrate_v2_journal(
            journal, "f" * 40, "1" * 40, "2026-07-18T12:02:00Z",
            self.catalog, self.routes, self.profiles,
        )
        self.assertEqual(journal, before)
        self.assertEqual(migrated["revisions"][:-1], before["revisions"])
        self.assertEqual(migrated["kit_sha"], "1" * 40)
        self.assertEqual(MANAGER_MODULE.active_resolution(migrated), resolution)
        MANAGER_MODULE.validate_journal(
            migrated, self.catalog, self.routes, self.profiles
        )
        legacy_migration = copy.deepcopy(migrated)
        legacy_body = legacy_migration["revisions"][-1]["body"]
        legacy_body["prior_resolution"] = resolution
        del legacy_body["prior_resolution_sha256"]
        legacy_migration["revisions"][-1]["revision_hash"] = (
            MANAGER_MODULE._revision_hash(
                legacy_migration["revisions"][-1]["revision"],
                legacy_migration["revisions"][-1]["parent_hash"],
                legacy_body,
            )
        )
        MANAGER_MODULE.validate_journal(
            legacy_migration, self.catalog, self.routes, self.profiles
        )
        self.assertEqual(
            MANAGER_MODULE.migrate_v2_journal(
                migrated, "f" * 40, "1" * 40, "2026-07-18T12:03:00Z",
                self.catalog, self.routes, self.profiles,
            ),
            migrated,
        )
        tampered = copy.deepcopy(migrated)
        tampered["revisions"][-1]["body"]["old_kit_sha"] = "2" * 40
        with self.assertRaises(MANAGER_MODULE.ManagerError):
            MANAGER_MODULE.validate_journal(
                tampered, self.catalog, self.routes, self.profiles
            )

    def test_release_migration_refreshes_identity_without_rewriting_history(self):
        historical_catalog = copy.deepcopy(self.catalog)
        for route in historical_catalog["routes"]:
            if route["route_id"] == "cursor-claude-fable-5-thinking-medium":
                route["expected_reported_identity"] = "Fable 5 1M Medium Thinking"
        historical_routes = ROUTER.validate_catalog(historical_catalog)
        historical_readiness = copy.deepcopy(self.readiness)
        historical_readiness["cursor-claude-fable-5-thinking-medium"].update({
            "adapter_version": "cursor-old",
            "reported_identity": "Fable 5 1M Medium Thinking",
        })
        resolution = ROUTER.resolve_policy(
            historical_catalog,
            historical_routes,
            self.profiles["cursor-balanced-v2"],
            historical_readiness,
        )
        legacy = {
            "created_at": "2026-07-18T11:00:00Z",
            "kit_sha": "a" * 40,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-123",
        }
        journal = MANAGER_MODULE.migrate_v1_plan(
            (ROUTER.canonical_json(legacy) + "\n").encode(),
            "b" * 40,
            "c" * 40,
            "2026-07-18T12:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        before = copy.deepcopy(journal)
        affinity_only = MANAGER_MODULE.migrate_v2_journal(
            journal,
            "d" * 40,
            "f" * 40,
            "2026-07-18T12:01:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        self.assertNotIn(
            "new_resolution", affinity_only["revisions"][-1]["body"]
        )
        self.assertEqual(
            MANAGER_MODULE.active_resolution(affinity_only), resolution
        )
        with self.assertRaises(MANAGER_MODULE.ManagerError):
            MANAGER_MODULE.migrate_v2_journal(
                journal,
                "d" * 40,
                journal["kit_sha"],
                "2026-07-18T12:01:00Z",
                self.catalog,
                self.routes,
                self.profiles,
                self.readiness,
            )
        current_readiness = copy.deepcopy(self.readiness)
        current_readiness["cursor-claude-fable-5-thinking-medium"][
            "adapter_version"
        ] = "cursor-current"
        migrated = MANAGER_MODULE.migrate_v2_journal(
            journal,
            "d" * 40,
            "e" * 40,
            "2026-07-18T12:01:00Z",
            self.catalog,
            self.routes,
            self.profiles,
            current_readiness,
        )
        self.assertEqual(migrated["revisions"][:-1], before["revisions"])
        body = migrated["revisions"][-1]["body"]
        self.assertEqual(
            body["prior_resolution_sha256"], ROUTER.content_hash(resolution)
        )
        self.assertEqual(
            MANAGER_MODULE.active_resolution(migrated), body["new_resolution"]
        )
        MANAGER_MODULE.validate_journal(
            migrated,
            self.catalog,
            self.routes,
            self.profiles,
            allow_historical_active=False,
        )

        intermediate_catalog = copy.deepcopy(self.catalog)
        for route in intermediate_catalog["routes"]:
            if route["route_id"] == "cursor-claude-fable-5-thinking-medium":
                route["expected_reported_identity"] = "Fable 5 500K Medium"
        intermediate_routes = ROUTER.validate_catalog(intermediate_catalog)
        intermediate_readiness = copy.deepcopy(self.readiness)
        intermediate_readiness["cursor-claude-fable-5-thinking-medium"].update({
            "adapter_version": "cursor-middle",
            "reported_identity": "Fable 5 500K Medium",
        })
        first_refresh = MANAGER_MODULE.migrate_v2_journal(
            journal,
            "d" * 40,
            "1" * 40,
            "2026-07-18T12:01:00Z",
            intermediate_catalog,
            intermediate_routes,
            self.profiles,
            intermediate_readiness,
        )
        second_refresh = MANAGER_MODULE.migrate_v2_journal(
            first_refresh,
            "2" * 40,
            "3" * 40,
            "2026-07-18T12:02:00Z",
            self.catalog,
            self.routes,
            self.profiles,
            current_readiness,
        )
        self.assertEqual(
            second_refresh["revisions"][:-1], first_refresh["revisions"]
        )
        MANAGER_MODULE.validate_journal(
            second_refresh, self.catalog, self.routes, self.profiles
        )

        journal_path = self.base / "historical-journal.json"
        journal_path.write_text(ROUTER.canonical_json(journal) + "\n")
        os.chmod(journal_path, 0o644)
        preview = self.output(
            "migrate-plan",
            "--ticket-plan", str(journal_path),
            "--pin-commit", "d" * 40,
            "--kit-sha", "f" * 40,
            "--migrated-at", "2026-07-18T12:01:00Z",
            "--readiness", json.dumps(current_readiness),
            "--include-journal",
        )
        self.assertIn(
            "new_resolution", preview["journal"]["revisions"][-1]["body"]
        )
        elsewhere = self.base / "elsewhere.json"
        rejected = self.command(
            "migrate",
            "--ticket-plan", str(journal_path),
            "--pin-commit", "d" * 40,
            "--kit-sha", "f" * 40,
            "--migrated-at", "2026-07-18T12:01:00Z",
            "--readiness", json.dumps(current_readiness),
            "--approve-hash", preview["preview_hash"],
            "--output", str(elsewhere),
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertFalse(elsewhere.exists())
        applied = self.output(
            "migrate",
            "--ticket-plan", str(journal_path),
            "--pin-commit", "d" * 40,
            "--kit-sha", "f" * 40,
            "--migrated-at", "2026-07-18T12:01:00Z",
            "--readiness", json.dumps(current_readiness),
            "--approve-hash", preview["preview_hash"],
            "--output", str(journal_path),
        )
        self.assertEqual(applied, preview["journal"])
        self.assertEqual(applied["revisions"][:-1], journal["revisions"])
        self.assertEqual(
            self.output(
                "migrate",
                "--ticket-plan", str(journal_path),
                "--pin-commit", "d" * 40,
                "--kit-sha", "f" * 40,
                "--migrated-at", "2026-07-18T12:01:00Z",
                "--readiness", json.dumps(current_readiness),
                "--approve-hash", preview["preview_hash"],
                "--output", str(journal_path),
            ),
            applied,
        )

        tampered = copy.deepcopy(migrated)
        tampered["revisions"][-1]["body"]["new_resolution"]["selections"][
            "spec-linter"
        ]["route_id"] = "claude-fable"
        tampered["revisions"][-1]["revision_hash"] = MANAGER_MODULE._revision_hash(
            tampered["revisions"][-1]["revision"],
            tampered["revisions"][-1]["parent_hash"],
            tampered["revisions"][-1]["body"],
        )
        with self.assertRaises(MANAGER_MODULE.ManagerError):
            MANAGER_MODULE.validate_journal(
                tampered, self.catalog, self.routes, self.profiles
            )

    def test_long_migration_preview_is_compact_and_diagnostic_is_equivalent(self):
        legacy_path = self.base / "legacy-plan.json"
        self.pin(legacy_path, ticket="T-181")
        journal = MANAGER_MODULE.migrate_v1_plan(
            legacy_path.read_bytes(),
            "1" * 40,
            "b" * 40,
            "2026-01-01T00:00:00Z",
            self.catalog,
            self.routes,
            self.profiles,
        )
        for index in range(1, 71):
            new_kit = ("c" if journal["kit_sha"] != "c" * 40 else "d") * 40
            journal = MANAGER_MODULE.migrate_v2_journal(
                journal,
                format(index + 1, "040x"),
                new_kit,
                "2026-01-01T00:%02d:%02dZ" % (index // 60, index % 60),
                self.catalog,
                self.routes,
                self.profiles,
                self.readiness,
            )
        journal_path = self.base / "long-journal.json"
        journal_path.write_text(ROUTER.canonical_json(journal) + "\n")
        self.assertLess(journal_path.stat().st_size, 100_000)
        self.assertTrue(all(
            "prior_resolution_sha256" in revision["body"]
            and "prior_resolution" not in revision["body"]
            for revision in journal["revisions"][1:]
        ))
        os.chmod(journal_path, 0o644)
        arguments = (
            "--ticket-plan", str(journal_path),
            "--pin-commit", "e" * 40,
            "--kit-sha", "f" * 40,
            "--migrated-at", "2026-01-01T02:00:00Z",
            "--readiness", json.dumps(self.readiness),
        )
        compact_result = self.command("migrate-plan", *arguments)
        diagnostic_result = self.command(
            "migrate-plan", *arguments, "--include-journal"
        )
        compact = json.loads(compact_result.stdout)
        diagnostic = json.loads(diagnostic_result.stdout)
        self.assertNotIn("journal", compact)
        self.assertEqual(
            diagnostic["journal"]["revisions"][:-1], journal["revisions"]
        )
        self.assertEqual(
            {key: value for key, value in diagnostic.items() if key != "journal"},
            compact,
        )
        self.assertEqual(
            ROUTER.content_hash(diagnostic["journal"]), compact["preview_hash"]
        )
        self.assertLess(len(compact_result.stdout), 2048)
        self.assertGreater(
            len(diagnostic_result.stdout), len(compact_result.stdout) * 50
        )

        tampered = copy.deepcopy(journal)
        tampered["revisions"][-1]["parent_hash"] = "0" * 64
        journal_path.write_text(ROUTER.canonical_json(tampered) + "\n")
        compact_refusal = self.command("migrate-plan", *arguments, check=False)
        diagnostic_refusal = self.command(
            "migrate-plan", *arguments, "--include-journal", check=False
        )
        self.assertEqual(compact_refusal.returncode, 2)
        self.assertEqual(diagnostic_refusal.returncode, 2)
        self.assertEqual(compact_refusal.stderr, diagnostic_refusal.stderr)

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
