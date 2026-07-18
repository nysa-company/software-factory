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

    def test_catalog_has_exact_current_routes_and_disabled_experimental_kimi(self):
        self.assertEqual(
            set(self.routes),
            {
                "codex-gpt-5.6-sol",
                "codex-gpt-5.6-terra",
                "claude-fable",
                "claude-sonnet",
                "cursor-gpt-5.6-sol-high",
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


if __name__ == "__main__":
    unittest.main()
