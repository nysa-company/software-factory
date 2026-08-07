#!/usr/bin/env python3
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = ROOT / "scripts" / "model-router.py"
SPEC = importlib.util.spec_from_file_location("model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)


class ModelRouterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, cls.routes, cls.profiles, cls.profile_map = ROUTER.load_policy()

    def readiness(self, state="READY"):
        return {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "test",
                "reported_identity": route["expected_reported_identity"],
                "state": state,
            }
            for route_id, route in self.routes.items()
            if route["enabled"]
        }

    def resolve(self, profile_id="legacy-balanced-v1", readiness=None):
        return ROUTER.resolve_policy(
            self.catalog,
            self.routes,
            self.profile_map[profile_id],
            readiness if readiness is not None else self.readiness(),
        )

    def model_policy(self):
        portfolio = self.profile_map["balanced-v2"]["portfolios"][0]
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

    def test_catalog_has_exact_current_routes_and_disabled_experimental_kimi(self):
        self.assertEqual(
            set(self.routes),
            {
                "codex-gpt-5.6-sol",
                "codex-gpt-5.6-terra",
                "claude-fable",
                "claude-sonnet",
                "cursor-gpt-5.6-sol-high",
                "cursor-claude-fable-5-thinking-medium",
                "cursor-claude-opus-5-thinking-medium",
                "cursor-claude-sonnet-5-thinking-high",
                "claude-kimi-moonshotai-kimi-k2.6",
            },
        )
        kimi = self.routes["claude-kimi-moonshotai-kimi-k2.6"]
        self.assertEqual(kimi["adapter"], "claude-kimi")
        self.assertEqual(kimi["transport"], "claude-cli")
        self.assertEqual(kimi["gateway_id"], "openrouter")
        self.assertEqual(kimi["inference_provider_id"], "moonshotai")
        self.assertEqual(kimi["provider_family"], "moonshot")
        self.assertEqual(kimi["selection_id"], "moonshotai/kimi-k2.6")
        self.assertFalse(kimi["enabled"])
        self.assertEqual(kimi["lifecycle"], "experimental")
        cursor_fable = self.routes["cursor-claude-fable-5-thinking-medium"]
        self.assertEqual(cursor_fable["selection_id"], "claude-fable-5-thinking-medium")
        self.assertEqual(
            cursor_fable["expected_reported_identity"],
            "Fable 5 300K Medium",
        )
        self.assertTrue(cursor_fable["enabled"])
        cursor_opus = self.routes["cursor-claude-opus-5-thinking-medium"]
        self.assertEqual(cursor_opus["selection_id"], "claude-opus-5-thinking-medium")
        self.assertEqual(
            cursor_opus["expected_reported_identity"],
            "Opus 5 300K Medium",
        )
        self.assertTrue(cursor_opus["enabled"])
        serialized = ROUTER.canonical_json(self.catalog)
        self.assertEqual(ROUTER.DEFAULT_CATALOG.read_text().strip(), serialized)

    def test_legacy_profile_matches_current_routes_for_all_six_roles(self):
        plan = self.resolve()
        selected = plan["selections"]
        self.assertEqual(set(selected), set(ROUTER.ROLES))
        self.assertEqual(
            {role: selected[role]["route_id"] for role in ROUTER.ROLES},
            {
                "planner": "codex-gpt-5.6-sol",
                "builder": "codex-gpt-5.6-terra",
                "narrator": "codex-gpt-5.6-terra",
                "spec-linter": "claude-fable",
                "test-author": "claude-fable",
                "reviewer": "claude-sonnet",
            },
        )
        self.assertEqual(selected["planner"]["effort"], "high")
        self.assertTrue(all(selected[role]["effort"] == "medium" for role in ROUTER.ROLES[1:]))

    def test_balanced_v2_has_requested_default_routes_and_efforts(self):
        plan = self.resolve("balanced-v2")
        selected = plan["selections"]
        self.assertEqual(
            {role: selected[role]["route_id"] for role in ROUTER.ROLES},
            {
                "planner": "codex-gpt-5.6-sol",
                "builder": "codex-gpt-5.6-terra",
                "narrator": "codex-gpt-5.6-terra",
                "spec-linter": "claude-fable",
                "test-author": "claude-fable",
                "reviewer": "claude-sonnet",
            },
        )
        self.assertEqual(
            {role: selected[role]["effort"] for role in ROUTER.ROLES},
            {
                "planner": "high",
                "builder": "high",
                "narrator": "high",
                "spec-linter": "medium",
                "test-author": "medium",
                "reviewer": "high",
            },
        )
        readiness = self.readiness()
        readiness["claude-fable"]["state"] = "UNAVAILABLE"
        fallback = self.resolve("balanced-v2", readiness)
        for role in ("spec-linter", "test-author"):
            self.assertEqual(
                fallback["selections"][role]["route_id"],
                "cursor-claude-fable-5-thinking-medium",
            )
            self.assertEqual(fallback["selections"][role]["effort"], "medium")

    def test_cursor_balanced_v2_reverses_only_candidate_order(self):
        native = self.profile_map["balanced-v2"]["portfolios"][0]
        cursor = self.profile_map["cursor-balanced-v2"]["portfolios"][0]
        for role in ROUTER.ROLES:
            self.assertEqual(cursor["roles"][role]["effort"], native["roles"][role]["effort"])
            self.assertEqual(cursor["roles"][role]["candidates"], list(reversed(native["roles"][role]["candidates"])))

        selected = self.resolve("cursor-balanced-v2")["selections"]
        self.assertTrue(all(value["adapter"].startswith("cursor-") for value in selected.values()))

        readiness = self.readiness()
        for route_id in (
            "cursor-gpt-5.6-sol-high",
            "cursor-claude-fable-5-thinking-medium",
            "cursor-claude-sonnet-5-thinking-high",
        ):
            readiness[route_id]["state"] = "UNAVAILABLE"
        fallback = self.resolve("cursor-balanced-v2", readiness)["selections"]
        self.assertEqual(
            {role: fallback[role]["route_id"] for role in ROUTER.ROLES},
            {role: self.resolve("balanced-v2")["selections"][role]["route_id"] for role in ROUTER.ROLES},
        )

    def test_cursor_opus_default_preserves_legacy_profile_and_native_fallback(self):
        legacy = self.profile_map["cursor-balanced-v2"]["portfolios"][0]
        opus = self.profile_map["cursor-opus-v1"]["portfolios"][0]
        for role in ("planner", "builder", "narrator", "reviewer"):
            self.assertEqual(opus["roles"][role], legacy["roles"][role])
        for role in ("spec-linter", "test-author"):
            self.assertEqual(
                opus["roles"][role]["candidates"],
                ["cursor-claude-opus-5-thinking-medium", "claude-fable"],
            )
            self.assertEqual(opus["roles"][role]["effort"], "medium")

        selected = self.resolve("cursor-opus-v1")["selections"]
        for role in ("spec-linter", "test-author"):
            self.assertEqual(
                selected[role]["route_id"],
                "cursor-claude-opus-5-thinking-medium",
            )

        readiness = self.readiness()
        readiness["cursor-claude-opus-5-thinking-medium"]["state"] = "UNAVAILABLE"
        fallback = self.resolve("cursor-opus-v1", readiness)["selections"]
        for role in ("spec-linter", "test-author"):
            self.assertEqual(fallback[role]["route_id"], "claude-fable")

    def test_hashes_and_resolution_are_deterministic(self):
        profile = self.profile_map["legacy-balanced-v1"]
        profile_hashes = [ROUTER.profile_hash(profile) for _ in range(5)]
        plans = [ROUTER.canonical_json(self.resolve()) for _ in range(5)]
        self.assertEqual(len(set(profile_hashes)), 1)
        self.assertEqual(len(set(plans)), 1)
        self.assertNotIn("time", plans[0].lower())

    def test_probe_list_is_unique_and_in_first_traversal_order(self):
        profile = self.profile_map["legacy-balanced-v1"]
        probes = ROUTER.probe_list(profile, self.routes)
        self.assertEqual(
            [probe["route_id"] for probe in probes],
            [
                "codex-gpt-5.6-sol",
                "cursor-gpt-5.6-sol-high",
                "codex-gpt-5.6-terra",
                "claude-fable",
                "cursor-claude-sonnet-5-thinking-high",
                "claude-sonnet",
            ],
        )
        self.assertTrue(
            all(
                set(probe)
                == {
                    "route_id", "adapter", "selection_id", "provider_family",
                    "expected_reported_identity", "account_route_id",
                }
                for probe in probes
            )
        )

    def test_profile_swaps_and_cursor_priority(self):
        claude = self.resolve("claude-priority-v1")
        self.assertEqual(claude["selections"]["planner"]["provider_family"], "anthropic")
        self.assertEqual(claude["selections"]["spec-linter"]["provider_family"], "openai")

        openai = self.resolve("openai-priority-v1")
        self.assertEqual(openai["selections"]["planner"]["provider_family"], "openai")
        self.assertEqual(openai["selections"]["reviewer"]["provider_family"], "anthropic")

        cursor = self.resolve("cursor-priority-v1")
        self.assertEqual(cursor["selections"]["planner"]["adapter"], "cursor-openai")
        self.assertEqual(cursor["selections"]["reviewer"]["adapter"], "cursor-anthropic")

    def test_unavailable_primary_uses_role_fallback(self):
        readiness = self.readiness()
        readiness["codex-gpt-5.6-sol"]["state"] = "UNAVAILABLE"
        plan = self.resolve(readiness=readiness)
        self.assertEqual(
            plan["selections"]["planner"]["route_id"], "cursor-gpt-5.6-sol-high"
        )

    def test_role_exhaustion_advances_to_next_portfolio(self):
        profile = copy.deepcopy(self.profile_map["legacy-balanced-v1"])
        first = copy.deepcopy(profile["portfolios"][0])
        first["portfolio_id"] = "cursor-planner-only"
        first["roles"]["planner"]["candidates"] = ["cursor-gpt-5.6-sol-high"]
        profile["portfolios"].insert(0, first)
        ROUTER.validate_profiles(
            {"schema": "model-routing-profiles/v1", "version": 1, "profiles": [profile]},
            self.routes,
        )
        readiness = self.readiness()
        readiness["cursor-gpt-5.6-sol-high"]["state"] = "UNAVAILABLE"
        plan = ROUTER.resolve_policy(self.catalog, self.routes, profile, readiness)
        self.assertEqual(plan["portfolio_id"], "legacy-native-first")
        self.assertEqual(plan["selections"]["planner"]["route_id"], "codex-gpt-5.6-sol")

    def test_invalid_and_unknown_hard_stop_without_fallback(self):
        for state in ("INVALID", "UNKNOWN"):
            with self.subTest(state=state):
                readiness = self.readiness()
                readiness["codex-gpt-5.6-sol"].update(
                    {"state": state, "reason": "broken"}
                )
                with self.assertRaisesRegex(ROUTER.RouterError, state):
                    self.resolve(readiness=readiness)

    def test_missing_readiness_is_unknown_and_hard_stops(self):
        readiness = self.readiness()
        del readiness["codex-gpt-5.6-sol"]
        with self.assertRaisesRegex(ROUTER.RouterError, "UNKNOWN"):
            self.resolve(readiness=readiness)

    def test_malformed_exact_schemas_fail_closed(self):
        malformed = copy.deepcopy(self.catalog)
        malformed["extra"] = True
        with self.assertRaisesRegex(ROUTER.RouterError, "keys mismatch"):
            ROUTER.validate_catalog(malformed)

        readiness = self.readiness()
        readiness["codex-gpt-5.6-sol"]["extra"] = ""
        with self.assertRaisesRegex(ROUTER.RouterError, "keys mismatch"):
            ROUTER.validate_readiness(readiness, self.routes)

        plan = self.resolve()
        plan["selections"]["planner"].pop("transport")
        with self.assertRaisesRegex(ROUTER.RouterError, "keys mismatch"):
            ROUTER.validate_plan(plan, self.catalog, self.routes, self.profile_map)

    def assert_profile_rejected(self, mutate, message):
        document = copy.deepcopy(self.profiles)
        mutate(document["profiles"][0]["portfolios"][0])
        with self.assertRaisesRegex(ROUTER.RouterError, message):
            ROUTER.validate_profiles(document, self.routes)

    def test_duplicate_auto_unknown_and_disabled_candidates_are_rejected(self):
        self.assert_profile_rejected(
            lambda p: p["roles"]["planner"]["candidates"].append(
                p["roles"]["planner"]["candidates"][0]
            ),
            "duplicate candidate",
        )
        self.assert_profile_rejected(
            lambda p: p["roles"]["planner"].update(candidates=["auto"]),
            "safe explicit identifier",
        )
        self.assert_profile_rejected(
            lambda p: p["roles"]["planner"].update(candidates=["missing-route"]),
            "unknown route",
        )
        self.assert_profile_rejected(
            lambda p: p["roles"]["planner"].update(
                candidates=["claude-kimi-moonshotai-kimi-k2.6"]
            ),
            "disabled route",
        )

        catalog = copy.deepcopy(self.catalog)
        catalog["routes"][0]["selection_id"] = "auto"
        with self.assertRaisesRegex(ROUTER.RouterError, "explicit selection ID"):
            ROUTER.validate_catalog(catalog)

    def test_family_separation_and_lane_membership_are_enforced(self):
        self.assert_profile_rejected(
            lambda p: p.update(checking_family=p["production_family"]),
            "distinct",
        )
        self.assert_profile_rejected(
            lambda p: p["roles"]["planner"].update(candidates=["claude-sonnet"]),
            "outside the openai lane",
        )

    def test_cursor_reported_identity_mismatch_is_invalid(self):
        readiness = self.readiness()
        readiness["codex-gpt-5.6-sol"]["state"] = "UNAVAILABLE"
        readiness["cursor-gpt-5.6-sol-high"]["reported_identity"] = "GPT-5.6 Sol"
        with self.assertRaisesRegex(ROUTER.RouterError, "reported_identity_mismatch"):
            self.resolve(readiness=readiness)

    def test_select_validates_plan_and_returns_exact_role_tuple(self):
        plan = self.resolve()
        ROUTER.validate_plan(plan, self.catalog, self.routes, self.profile_map)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps(plan))
            result = subprocess.run(
                [str(ROUTER_PATH), "select", str(path), "planner"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(json.loads(result.stdout), plan["selections"]["planner"])

        tampered = copy.deepcopy(plan)
        tampered["selections"]["planner"]["selection_id"] = "auto"
        with self.assertRaisesRegex(ROUTER.RouterError, "tuple mismatch"):
            ROUTER.validate_plan(tampered, self.catalog, self.routes, self.profile_map)

    def test_policy_hash_binds_every_selected_tuple_field(self):
        plan = self.resolve()
        for field, value in (
            ("adapter_version", "different-adapter-version"),
            ("reported_identity", "different-reported-identity"),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(plan)
                tampered["selections"]["planner"][field] = value
                with self.assertRaisesRegex(ROUTER.RouterError, "policy hash mismatch"):
                    ROUTER.validate_plan(
                        tampered, self.catalog, self.routes, self.profile_map
                    )

    def test_profiles_never_reference_disabled_kimi(self):
        references = {
            candidate
            for profile in self.profiles["profiles"]
            for portfolio in profile["portfolios"]
            for role in ROUTER.ROLES
            for candidate in portfolio["roles"][role]["candidates"]
        }
        self.assertNotIn("claude-kimi-moonshotai-kimi-k2.6", references)
        self.assertEqual(
            ROUTER.DEFAULT_PROFILES.read_text().strip(),
            ROUTER.canonical_json(self.profiles),
        )

    def test_project_policy_enforces_routes_lanes_family_and_effort(self):
        policy = self.model_policy()
        profile = ROUTER.model_policy_profile(policy, self.routes)
        plan = ROUTER.resolve_policy(
            self.catalog, self.routes, profile, self.readiness(), policy
        )
        self.assertEqual(plan["schema"], "model-resolution-plan/v2")
        ROUTER.validate_plan(plan, self.catalog, self.routes, self.profile_map)
        for mutate, message in (
            (
                lambda value: value["roles"]["planner"].update(
                    secondary_route_id="claude-sonnet"
                ),
                "secondary route",
            ),
            (
                lambda value: value["roles"]["reviewer"].update(
                    primary_route_id="codex-gpt-5.6-sol",
                    secondary_route_id="cursor-gpt-5.6-sol-high",
                ),
                "outside",
            ),
            (
                lambda value: value["roles"]["planner"].update(
                    primary_route_id="claude-kimi-moonshotai-kimi-k2.6"
                ),
                "enabled stable",
            ),
            (
                lambda value: value["roles"]["builder"].update(effort="auto"),
                "unsupported",
            ),
        ):
            with self.subTest(message=message):
                malformed = copy.deepcopy(policy)
                mutate(malformed)
                with self.assertRaisesRegex(ROUTER.RouterError, message):
                    ROUTER.validate_model_policy(malformed, self.routes)

    def test_project_policy_resolution_is_readiness_fail_closed(self):
        policy = self.model_policy()
        profile = ROUTER.model_policy_profile(policy, self.routes)
        readiness = self.readiness()
        del readiness[policy["roles"]["planner"]["primary_route_id"]]
        with self.assertRaisesRegex(ROUTER.RouterError, "UNKNOWN"):
            ROUTER.resolve_policy(
                self.catalog, self.routes, profile, readiness, policy
            )

    def test_project_policy_snapshot_survives_mid_ticket_fallback(self):
        policy = self.model_policy()
        profile = ROUTER.model_policy_profile(policy, self.routes)
        prior = ROUTER.resolve_policy(
            self.catalog, self.routes, profile, self.readiness(), policy
        )
        fallback = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            profile,
            self.readiness(),
            prior,
            "builder",
            prior["selections"]["builder"]["route_id"],
            ["builder", "reviewer"],
            {"P": ["openai"], "T": ["anthropic"], "B": ["openai"]},
        )
        self.assertEqual(fallback["profile_id"], "project-policy")
        self.assertEqual(fallback["model_policy"], policy)
        self.assertEqual(
            fallback["selections"]["builder"]["route_id"],
            policy["roles"]["builder"]["secondary_route_id"],
        )
        ROUTER.validate_fallback_plan(
            fallback, self.catalog, self.routes, self.profile_map
        )

    def test_history_aware_fallback_excludes_failed_route_and_resolves_future_roles(self):
        prior = self.resolve()
        readiness = self.readiness()
        result = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            self.profile_map["legacy-balanced-v1"],
            readiness,
            prior,
            "builder",
            "codex-gpt-5.6-terra",
            ["builder", "reviewer"],
            {"P": ["openai"], "T": ["anthropic"], "B": ["openai"]},
        )
        self.assertEqual(result["schema"], "model-fallback-resolution/v2")
        self.assertEqual(
            result["selections"]["builder"]["route_id"],
            "cursor-gpt-5.6-sol-high",
        )
        self.assertEqual(
            result["selections"]["reviewer"]["route_id"], "claude-sonnet"
        )
        self.assertEqual(result["selections"]["planner"], prior["selections"]["planner"])
        ROUTER.validate_fallback_plan(
            result, self.catalog, self.routes, self.profile_map
        )

    def test_historical_catalog_is_accepted_only_for_compatible_migration(self):
        historical_catalog = copy.deepcopy(self.catalog)
        historical_catalog["routes"] = [
            route for route in historical_catalog["routes"]
            if route["route_id"] != "cursor-claude-fable-5-thinking-medium"
        ]
        historical_routes = ROUTER.validate_catalog(historical_catalog)
        readiness = {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "test",
                "reported_identity": route["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, route in historical_routes.items()
            if route["enabled"]
        }
        prior = ROUTER.resolve_policy(
            historical_catalog,
            historical_routes,
            self.profile_map["legacy-balanced-v1"],
            readiness,
        )
        with self.assertRaisesRegex(ROUTER.RouterError, "catalog hash mismatch"):
            ROUTER.validate_plan(
                prior, self.catalog, self.routes, self.profile_map
            )
        ROUTER.validate_plan(
            prior,
            self.catalog,
            self.routes,
            self.profile_map,
            allow_historical_catalog=True,
        )
        current_readiness = self.readiness()
        result = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            self.profile_map["legacy-balanced-v1"],
            current_readiness,
            prior,
            "builder",
            "codex-gpt-5.6-terra",
            ["builder", "reviewer"],
            {"P": ["openai"], "T": ["anthropic"], "B": ["openai"]},
        )
        self.assertEqual(
            result["selections"]["builder"]["route_id"],
            "cursor-gpt-5.6-sol-high",
        )
        tampered = copy.deepcopy(prior)
        tampered["selections"]["builder"]["selection_id"] = "gpt-5.6-sol"
        with self.assertRaisesRegex(ROUTER.RouterError, "tuple mismatch"):
            ROUTER.validate_plan(
                tampered,
                self.catalog,
                self.routes,
                self.profile_map,
                allow_historical_catalog=True,
            )

    def test_refresh_changes_only_machine_bound_route_evidence(self):
        historical_catalog = copy.deepcopy(self.catalog)
        for route in historical_catalog["routes"]:
            if route["route_id"] == "cursor-claude-fable-5-thinking-medium":
                route["expected_reported_identity"] = "Fable 5 1M Medium Thinking"
        historical_routes = ROUTER.validate_catalog(historical_catalog)
        historical_readiness = self.readiness()
        historical_readiness["cursor-claude-fable-5-thinking-medium"].update({
            "adapter_version": "cursor-old",
            "reported_identity": "Fable 5 1M Medium Thinking",
        })
        prior = ROUTER.resolve_policy(
            historical_catalog,
            historical_routes,
            self.profile_map["cursor-balanced-v2"],
            historical_readiness,
        )
        with self.assertRaisesRegex(ROUTER.RouterError, "catalog hash mismatch"):
            ROUTER.validate_plan(prior, self.catalog, self.routes, self.profile_map)

        current_readiness = self.readiness()
        current_readiness["cursor-claude-fable-5-thinking-medium"][
            "adapter_version"
        ] = "cursor-current"
        refreshed = ROUTER.refresh_resolution(
            prior, self.catalog, self.routes, self.profile_map, current_readiness
        )
        ROUTER.validate_plan(
            refreshed, self.catalog, self.routes, self.profile_map
        )
        for role in ROUTER.ROLES:
            old = prior["selections"][role]
            new = refreshed["selections"][role]
            self.assertEqual(
                {key: value for key, value in old.items()
                 if key not in ("adapter_version", "reported_identity")},
                {key: value for key, value in new.items()
                 if key not in ("adapter_version", "reported_identity")},
            )

        tampered = copy.deepcopy(prior)
        tampered["selections"]["spec-linter"]["effort"] = "low"
        with self.assertRaisesRegex(ROUTER.RouterError, "tuple mismatch"):
            ROUTER.refresh_resolution(
                tampered, self.catalog, self.routes, self.profile_map,
                current_readiness,
            )

        current_hash_stale_identity = self.resolve("cursor-balanced-v2")
        current_hash_stale_identity["selections"]["spec-linter"][
            "reported_identity"
        ] = "Fable 5 1M Medium Thinking"
        with self.assertRaisesRegex(ROUTER.RouterError, "identity mismatch"):
            ROUTER.validate_plan(
                current_hash_stale_identity,
                self.catalog,
                self.routes,
                self.profile_map,
                allow_historical_catalog=True,
            )

    def test_fallback_advances_only_unavailable_and_hard_stops_bad_evidence(self):
        prior = self.resolve()
        for state in ("INVALID", "UNKNOWN"):
            with self.subTest(state=state):
                readiness = self.readiness()
                readiness["cursor-gpt-5.6-sol-high"].update(
                    {"state": state, "reason": "bad-evidence"}
                )
                with self.assertRaisesRegex(ROUTER.RouterError, state):
                    ROUTER.resolve_fallback_policy(
                        self.catalog,
                        self.routes,
                        self.profile_map["legacy-balanced-v1"],
                        readiness,
                        prior,
                        "planner",
                        "codex-gpt-5.6-sol",
                        ["planner"],
                        {"P": ["openai"], "T": [], "B": []},
                    )
        readiness = self.readiness()
        readiness["cursor-gpt-5.6-sol-high"].update({
            "state": "UNAVAILABLE", "reason": "cursor_cli_config_mode_0644",
        })
        with self.assertRaisesRegex(
            ROUTER.RouterError,
            "planner cursor-gpt-5.6-sol-high: cursor_cli_config_mode_0644",
        ):
            ROUTER.resolve_fallback_policy(
                self.catalog,
                self.routes,
                self.profile_map["legacy-balanced-v1"],
                readiness,
                prior,
                "planner",
                "codex-gpt-5.6-sol",
                ["planner"],
                {"P": ["openai"], "T": [], "B": []},
            )

    def test_boundary_history_requires_third_family_only_for_producer_switch(self):
        prior = self.resolve()
        with self.assertRaisesRegex(ROUTER.RouterError, "contributor-family"):
            ROUTER.resolve_fallback_policy(
                self.catalog,
                self.routes,
                self.profile_map["legacy-balanced-v1"],
                self.readiness(),
                prior,
                "planner",
                "codex-gpt-5.6-sol",
                ["planner"],
                {"P": ["openai", "anthropic"], "T": [], "B": []},
            )

        checker = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            self.profile_map["legacy-balanced-v1"],
            self.readiness(),
            prior,
            "spec-linter",
            "claude-fable",
            ["spec-linter"],
            {"P": ["openai"], "T": [], "B": []},
        )
        self.assertEqual(checker["contributor_families"]["P"], ["openai"])
        self.assertEqual(
            checker["selections"]["spec-linter"]["route_id"],
            "cursor-claude-sonnet-5-thinking-high",
        )

    def test_reviewer_same_family_requires_exact_boundary_exception(self):
        prior = self.resolve()
        contributors = {"P": ["openai"], "T": [], "B": ["anthropic"]}
        with self.assertRaisesRegex(ROUTER.RouterError, "contributor-family"):
            ROUTER.resolve_fallback_policy(
                self.catalog,
                self.routes,
                self.profile_map["legacy-balanced-v1"],
                self.readiness(),
                prior,
                "reviewer",
                "claude-sonnet",
                ["reviewer"],
                contributors,
            )
        approved = ROUTER.resolve_fallback_policy(
            self.catalog,
            self.routes,
            self.profile_map["legacy-balanced-v1"],
            self.readiness(),
            prior,
            "reviewer",
            "claude-sonnet",
            ["reviewer"],
            contributors,
            {"reviewer": "anthropic"},
        )
        self.assertEqual(
            approved["selections"]["reviewer"]["provider_family"], "anthropic"
        )
        self.assertEqual(
            approved["boundary_exceptions"], {"reviewer": "anthropic"}
        )
        ROUTER.validate_fallback_plan(
            approved, self.catalog, self.routes, self.profile_map
        )


if __name__ == "__main__":
    unittest.main()
