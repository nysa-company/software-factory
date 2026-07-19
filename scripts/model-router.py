#!/usr/bin/env python3
"""Strict, deterministic model portfolio resolver."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "model-routing" / "catalog-v1.json"
DEFAULT_PROFILES = ROOT / "model-routing" / "profiles-v1.json"

ROLES = ("planner", "builder", "narrator", "spec-linter", "test-author", "reviewer")
PRODUCTION_ROLES = frozenset(("planner", "builder", "narrator"))
CHECKING_ROLES = frozenset(("spec-linter", "test-author", "reviewer"))
STATES = frozenset(("READY", "UNAVAILABLE", "INVALID", "UNKNOWN"))
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
SAFE_SELECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
SAFE_TEXT = re.compile(r"[^\x00-\x1f\x7f]{0,500}\Z")

CATALOG_KEYS = frozenset(("schema", "version", "routes"))
ROUTE_KEYS = frozenset((
    "route_id", "adapter", "transport", "gateway_id",
    "inference_provider_id", "provider_family", "account_route_id",
    "selection_id", "expected_reported_identity", "enabled", "lifecycle",
))
PROFILES_KEYS = frozenset(("schema", "version", "profiles"))
PROFILE_KEYS = frozenset(("profile_id", "version", "portfolios"))
PORTFOLIO_KEYS = frozenset((
    "portfolio_id", "production_family", "checking_family", "roles",
))
ROLE_KEYS = frozenset(("candidates", "effort"))
MODEL_POLICY_KEYS = frozenset((
    "schema", "version", "production_family", "checking_family", "roles",
))
MODEL_POLICY_ROLE_KEYS = frozenset((
    "primary_route_id", "secondary_route_id", "effort",
))
EFFORTS = ("low", "medium", "high")
READINESS_KEYS = frozenset((
    "state", "reason", "adapter_version", "reported_identity",
))
PLAN_KEYS = frozenset((
    "schema", "profile_id", "profile_version", "profile_hash",
    "portfolio_id", "catalog_hash", "selections", "policy_hash",
))
PLAN_V2_KEYS = PLAN_KEYS | frozenset(("model_policy",))
SELECTION_KEYS = frozenset((
    "role", "route_id", "adapter", "transport", "gateway_id",
    "inference_provider_id", "provider_family", "account_route_id",
    "selection_id", "effort", "adapter_version", "reported_identity",
))
CONTRIBUTOR_BOUNDARIES = ("P", "T", "B")
BOUNDARY_PRODUCER = {
    "planner": "P",
    "test-author": "T",
    "builder": "B",
}
BOUNDARY_CHECKER = {
    "spec-linter": "P",
    "builder": "T",
    "reviewer": "B",
}
FALLBACK_PLAN_KEYS = frozenset((
    "schema", "profile_id", "profile_version", "profile_hash",
    "catalog_hash", "prior_policy_hash", "policy_hash", "failed_role",
    "failed_route_id", "future_roles", "contributor_families", "selections",
))
FALLBACK_PROJECT_PLAN_KEYS = FALLBACK_PLAN_KEYS | frozenset(("model_policy",))
FALLBACK_EXCEPTION_PLAN_KEYS = FALLBACK_PLAN_KEYS | frozenset(
    ("boundary_exceptions",)
)
FALLBACK_PROJECT_EXCEPTION_PLAN_KEYS = (
    FALLBACK_PROJECT_PLAN_KEYS | frozenset(("boundary_exceptions",))
)


class RouterError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RouterError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_object_no_duplicates)
    except RouterError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RouterError("cannot load JSON %s: %s" % (path, exc))


def load_json_stream(handle, location):
    try:
        return json.load(handle, object_pairs_hook=_object_no_duplicates)
    except RouterError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RouterError("cannot load JSON %s: %s" % (location, exc))


def _exact_keys(value, expected, location):
    if not isinstance(value, dict):
        raise RouterError("%s must be an object" % location)
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RouterError("%s keys mismatch (missing=%s extra=%s)" % (location, missing, extra))


def _safe_id(value, location):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or value == "auto":
        raise RouterError("%s is not a safe explicit identifier" % location)


def _safe_selection(value, location):
    if not isinstance(value, str) or not SAFE_SELECTION.fullmatch(value) or value == "auto":
        raise RouterError("%s is not a safe explicit selection ID" % location)


def _safe_text(value, location):
    if not isinstance(value, str) or not SAFE_TEXT.fullmatch(value):
        raise RouterError("%s is not safe text" % location)


def _positive_version(value, location):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RouterError("%s must be a positive integer" % location)


def validate_catalog(catalog):
    _exact_keys(catalog, CATALOG_KEYS, "catalog")
    if catalog["schema"] != "model-route-catalog/v1":
        raise RouterError("unsupported catalog schema")
    if catalog["version"] != 1:
        raise RouterError("unsupported catalog version")
    routes = catalog["routes"]
    if not isinstance(routes, list) or not routes:
        raise RouterError("catalog routes must be a non-empty array")
    by_id = {}
    for index, route in enumerate(routes):
        location = "catalog.routes[%d]" % index
        _exact_keys(route, ROUTE_KEYS, location)
        for key in (
            "route_id", "adapter", "transport", "gateway_id",
            "inference_provider_id", "provider_family", "account_route_id",
            "lifecycle",
        ):
            _safe_id(route[key], "%s.%s" % (location, key))
        _safe_selection(route["selection_id"], "%s.selection_id" % location)
        _safe_text(route["expected_reported_identity"], "%s.expected_reported_identity" % location)
        if type(route["enabled"]) is not bool:
            raise RouterError("%s.enabled must be boolean" % location)
        if route["lifecycle"] not in ("stable", "experimental"):
            raise RouterError("%s.lifecycle is invalid" % location)
        if route["enabled"] and route["lifecycle"] != "stable":
            raise RouterError("%s enabled routes must be stable" % location)
        route_id = route["route_id"]
        if route_id in by_id:
            raise RouterError("duplicate route_id: %s" % route_id)
        by_id[route_id] = route
    return by_id


def validate_profiles(profiles, routes):
    _exact_keys(profiles, PROFILES_KEYS, "profiles document")
    if profiles["schema"] != "model-routing-profiles/v1":
        raise RouterError("unsupported profiles schema")
    if profiles["version"] != 1:
        raise RouterError("unsupported profiles version")
    values = profiles["profiles"]
    if not isinstance(values, list) or not values:
        raise RouterError("profiles must be a non-empty array")
    by_id = {}
    for profile_index, profile in enumerate(values):
        profile_location = "profiles[%d]" % profile_index
        _exact_keys(profile, PROFILE_KEYS, profile_location)
        _safe_id(profile["profile_id"], "%s.profile_id" % profile_location)
        _positive_version(profile["version"], "%s.version" % profile_location)
        profile_id = profile["profile_id"]
        if profile_id in by_id:
            raise RouterError("duplicate profile_id: %s" % profile_id)
        portfolios = profile["portfolios"]
        if not isinstance(portfolios, list) or not portfolios:
            raise RouterError("%s.portfolios must be a non-empty array" % profile_location)
        portfolio_ids = set()
        for portfolio_index, portfolio in enumerate(portfolios):
            location = "%s.portfolios[%d]" % (profile_location, portfolio_index)
            _exact_keys(portfolio, PORTFOLIO_KEYS, location)
            for key in ("portfolio_id", "production_family", "checking_family"):
                _safe_id(portfolio[key], "%s.%s" % (location, key))
            if portfolio["portfolio_id"] in portfolio_ids:
                raise RouterError("duplicate portfolio_id in %s" % profile_id)
            portfolio_ids.add(portfolio["portfolio_id"])
            if portfolio["production_family"] == portfolio["checking_family"]:
                raise RouterError("%s lane families must be distinct" % location)
            role_values = portfolio["roles"]
            _exact_keys(role_values, frozenset(ROLES), "%s.roles" % location)
            for role in ROLES:
                role_location = "%s.roles.%s" % (location, role)
                role_value = role_values[role]
                _exact_keys(role_value, ROLE_KEYS, role_location)
                _safe_id(role_value["effort"], "%s.effort" % role_location)
                candidates = role_value["candidates"]
                if not isinstance(candidates, list) or not candidates:
                    raise RouterError("%s.candidates must be a non-empty array" % role_location)
                lane_family = (
                    portfolio["production_family"]
                    if role in PRODUCTION_ROLES
                    else portfolio["checking_family"]
                )
                seen_candidates = set()
                for candidate in candidates:
                    _safe_id(candidate, "%s candidate" % role_location)
                    if candidate in seen_candidates:
                        raise RouterError("%s has a duplicate candidate" % role_location)
                    seen_candidates.add(candidate)
                    if candidate not in routes:
                        raise RouterError("%s references unknown route %s" % (role_location, candidate))
                    route = routes[candidate]
                    if not route["enabled"]:
                        raise RouterError("%s references disabled route %s" % (role_location, candidate))
                    if route["provider_family"] != lane_family:
                        raise RouterError(
                            "%s route %s is outside the %s lane"
                            % (role_location, candidate, lane_family)
                        )
        by_id[profile_id] = profile
    return by_id


def validate_model_policy(policy, routes):
    """Validate a product-owned, single-portfolio policy."""
    _exact_keys(policy, MODEL_POLICY_KEYS, "model policy")
    if policy["schema"] != "factory-model-policy/v1":
        raise RouterError("unsupported model policy schema")
    if policy["version"] != 1:
        raise RouterError("unsupported model policy version")
    for key in ("production_family", "checking_family"):
        _safe_id(policy[key], "model policy.%s" % key)
    if policy["production_family"] == policy["checking_family"]:
        raise RouterError("model policy lane families must be distinct")
    _exact_keys(policy["roles"], frozenset(ROLES), "model policy.roles")
    for role in ROLES:
        location = "model policy.roles.%s" % role
        role_policy = policy["roles"][role]
        _exact_keys(role_policy, MODEL_POLICY_ROLE_KEYS, location)
        if role_policy["effort"] not in EFFORTS:
            raise RouterError("%s.effort is unsupported" % location)
        primary_id = role_policy["primary_route_id"]
        secondary_id = role_policy["secondary_route_id"]
        for key, route_id in (
            ("primary_route_id", primary_id),
            ("secondary_route_id", secondary_id),
        ):
            _safe_id(route_id, "%s.%s" % (location, key))
            route = routes.get(route_id)
            if route is None:
                raise RouterError("%s.%s references an unknown route" % (location, key))
            if not route["enabled"] or route["lifecycle"] != "stable":
                raise RouterError("%s.%s must select an enabled stable route" % (location, key))
        if primary_id == secondary_id:
            raise RouterError("%s primary and secondary routes must be distinct" % location)
        primary_family = routes[primary_id]["provider_family"]
        if routes[secondary_id]["provider_family"] != primary_family:
            raise RouterError("%s secondary route must be in the primary route family" % location)
        lane_family = (
            policy["production_family"] if role in PRODUCTION_ROLES
            else policy["checking_family"]
        )
        if primary_family != lane_family:
            raise RouterError("%s routes are outside the %s lane" % (location, lane_family))
    return policy


def model_policy_profile(policy, routes):
    validate_model_policy(policy, routes)
    return {
        "portfolios": [{
            "checking_family": policy["checking_family"],
            "portfolio_id": "project-policy",
            "production_family": policy["production_family"],
            "roles": {
                role: {
                    "candidates": [
                        policy["roles"][role]["primary_route_id"],
                        policy["roles"][role]["secondary_route_id"],
                    ],
                    "effort": policy["roles"][role]["effort"],
                }
                for role in ROLES
            },
        }],
        "profile_id": "project-policy",
        "version": 1,
    }


def model_policy_candidates(routes):
    """Return only catalog-authorized values; clients never invent routes."""
    values = []
    for route_id in sorted(routes):
        route = routes[route_id]
        if not route["enabled"] or route["lifecycle"] != "stable":
            continue
        values.append({
            "adapter": route["adapter"],
            "provider_family": route["provider_family"],
            "route_id": route_id,
            "selection_id": route["selection_id"],
        })
    return {
        "efforts": list(EFFORTS),
        "reviewer_exception": {
            "normal_policy_allowed": False,
            "supported": False,
            "ticket_scoped_one_use_required": True,
        },
        "roles": [
            {
                "lane": "production" if role in PRODUCTION_ROLES else "checking",
                "role": role,
            }
            for role in ROLES
        ],
        "routes": values,
        "schema": "factory-model-policy-candidates/v1",
    }


def load_policy(catalog_path=DEFAULT_CATALOG, profiles_path=DEFAULT_PROFILES):
    catalog = load_json(catalog_path)
    routes = validate_catalog(catalog)
    profiles = load_json(profiles_path)
    profile_map = validate_profiles(profiles, routes)
    return catalog, routes, profiles, profile_map


def validate_readiness(readiness, routes):
    if not isinstance(readiness, dict):
        raise RouterError("readiness must be an object mapping route IDs")
    result = {}
    for route_id, value in readiness.items():
        _safe_id(route_id, "readiness route_id")
        if route_id not in routes:
            raise RouterError("readiness references unknown route %s" % route_id)
        _exact_keys(value, READINESS_KEYS, "readiness.%s" % route_id)
        if not isinstance(value["state"], str) or value["state"] not in STATES:
            raise RouterError("readiness.%s.state is invalid" % route_id)
        for key in ("reason", "adapter_version", "reported_identity"):
            _safe_text(value[key], "readiness.%s.%s" % (route_id, key))
        normalized = dict(value)
        expected = routes[route_id]["expected_reported_identity"]
        reported = normalized["reported_identity"]
        if expected and (
            (reported and reported != expected)
            or (normalized["state"] == "READY" and reported != expected)
        ):
            normalized["state"] = "INVALID"
            normalized["reason"] = "reported_identity_mismatch"
        result[route_id] = normalized
    return result


def _profile(profile_map, profile_id):
    if profile_id not in profile_map:
        raise RouterError("unknown profile: %s" % profile_id)
    return profile_map[profile_id]


def _policy_hash(catalog_hash, profile_hash, portfolio, selections):
    policy = {
        "catalog_hash": catalog_hash,
        "portfolio_id": portfolio["portfolio_id"],
        "profile_hash": profile_hash,
        "selections": {role: selections[role] for role in ROLES},
        "schema": "model-resolution-policy/v1",
    }
    return content_hash(policy)


def profile_hash(profile):
    return content_hash(profile)


def candidate_view(profile):
    return {
        "portfolios": [
            {
                "checking_family": portfolio["checking_family"],
                "portfolio_id": portfolio["portfolio_id"],
                "production_family": portfolio["production_family"],
                "roles": portfolio["roles"],
            }
            for portfolio in profile["portfolios"]
        ],
        "profile_hash": profile_hash(profile),
        "profile_id": profile["profile_id"],
        "profile_version": profile["version"],
        "schema": "model-candidates/v1",
    }


def probe_list(profile, routes):
    """Return unique profile routes in deterministic first-traversal order."""
    seen = set()
    probes = []
    for portfolio in profile["portfolios"]:
        for role in ROLES:
            for route_id in portfolio["roles"][role]["candidates"]:
                if route_id in seen:
                    continue
                seen.add(route_id)
                route = routes[route_id]
                probes.append({
                    "account_route_id": route["account_route_id"],
                    "adapter": route["adapter"],
                    "expected_reported_identity": route["expected_reported_identity"],
                    "provider_family": route["provider_family"],
                    "route_id": route_id,
                    "selection_id": route["selection_id"],
                })
    return probes


def _selected_tuple(role, route, role_policy, readiness):
    return {
        "account_route_id": route["account_route_id"],
        "adapter": route["adapter"],
        "adapter_version": readiness["adapter_version"],
        "effort": role_policy["effort"],
        "gateway_id": route["gateway_id"],
        "inference_provider_id": route["inference_provider_id"],
        "provider_family": route["provider_family"],
        "reported_identity": readiness["reported_identity"],
        "role": role,
        "route_id": route["route_id"],
        "selection_id": route["selection_id"],
        "transport": route["transport"],
    }


def resolve_policy(catalog, routes, profile, readiness, model_policy=None):
    if model_policy is not None:
        validate_model_policy(model_policy, routes)
        if profile != model_policy_profile(model_policy, routes):
            raise RouterError("model policy profile mismatch")
    readiness = validate_readiness(readiness, routes)
    catalog_digest = content_hash(catalog)
    profile_digest = profile_hash(profile)
    for portfolio in profile["portfolios"]:
        selections = {}
        portfolio_unavailable = False
        for role in ROLES:
            role_policy = portfolio["roles"][role]
            selected = None
            for route_id in role_policy["candidates"]:
                state = readiness.get(route_id, {
                    "state": "UNKNOWN",
                    "reason": "readiness_missing",
                    "adapter_version": "",
                    "reported_identity": "",
                })
                if state["state"] == "UNAVAILABLE":
                    continue
                if state["state"] in ("INVALID", "UNKNOWN"):
                    raise RouterError(
                        "%s route %s is %s: %s"
                        % (role, route_id, state["state"], state["reason"])
                    )
                if state["state"] != "READY":
                    raise RouterError("%s route %s has invalid state" % (role, route_id))
                selected = _selected_tuple(role, routes[route_id], role_policy, state)
                break
            if selected is None:
                portfolio_unavailable = True
                break
            selections[role] = selected
        if portfolio_unavailable:
            continue
        policy_digest = _policy_hash(catalog_digest, profile_digest, portfolio, selections)
        result = {
            "catalog_hash": catalog_digest,
            "policy_hash": policy_digest,
            "portfolio_id": portfolio["portfolio_id"],
            "profile_hash": profile_digest,
            "profile_id": profile["profile_id"],
            "profile_version": profile["version"],
            "schema": (
                "model-resolution-plan/v2"
                if model_policy is not None else "model-resolution-plan/v1"
            ),
            "selections": selections,
        }
        if model_policy is not None:
            result["model_policy"] = model_policy
        return result
    raise RouterError("no portfolio has ready candidates for every role")


def validate_contributor_families(value):
    """Validate and normalize the boundary-specific P/T/B family history."""
    _exact_keys(value, frozenset(CONTRIBUTOR_BOUNDARIES), "contributor_families")
    result = {}
    for boundary in CONTRIBUTOR_BOUNDARIES:
        families = value[boundary]
        if not isinstance(families, list):
            raise RouterError("contributor_families.%s must be an array" % boundary)
        seen = set()
        normalized = []
        for index, family in enumerate(families):
            _safe_id(
                family,
                "contributor_families.%s[%d]" % (boundary, index),
            )
            if family in seen:
                raise RouterError(
                    "contributor_families.%s has a duplicate family" % boundary
                )
            seen.add(family)
            normalized.append(family)
        result[boundary] = normalized
    return result


def _fallback_role_policies(profile, role):
    """Return profile-authorized candidates in deterministic profile order."""
    seen = set()
    result = []
    for portfolio in profile["portfolios"]:
        role_policy = portfolio["roles"][role]
        for route_id in role_policy["candidates"]:
            if route_id in seen:
                continue
            seen.add(route_id)
            result.append((route_id, role_policy))
    return result


def validate_boundary_exceptions(value, contributors, future_roles):
    if value is None:
        return {}
    _exact_keys(value, frozenset(("reviewer",)), "boundary_exceptions")
    family = value["reviewer"]
    _safe_id(family, "boundary_exceptions.reviewer")
    if "reviewer" not in future_roles:
        raise RouterError("Reviewer exception requires a future Reviewer role")
    if family not in contributors["B"]:
        raise RouterError(
            "Reviewer exception must name a Builder contributor family"
        )
    return {"reviewer": family}


def _validate_fallback_assignment(selections, contributors, exceptions=None):
    effective = dict((key, list(value)) for key, value in contributors.items())
    exceptions = exceptions or {}
    for role, boundary in BOUNDARY_PRODUCER.items():
        family = selections[role]["provider_family"]
        if family not in effective[boundary]:
            effective[boundary].append(family)
    for role, boundary in BOUNDARY_CHECKER.items():
        family = selections[role]["provider_family"]
        if family in effective[boundary] and exceptions.get(role) != family:
            return None
    return effective


def validate_fallback_plan(plan, catalog, routes, profile_map):
    if not isinstance(plan, dict):
        raise RouterError("fallback plan must be an object")
    project_policy = plan.get("profile_id") == "project-policy"
    has_exception = "boundary_exceptions" in plan
    if project_policy:
        keys = (
            FALLBACK_PROJECT_EXCEPTION_PLAN_KEYS
            if has_exception else FALLBACK_PROJECT_PLAN_KEYS
        )
    else:
        keys = FALLBACK_EXCEPTION_PLAN_KEYS if has_exception else FALLBACK_PLAN_KEYS
    _exact_keys(plan, keys, "fallback plan")
    if plan["schema"] != "model-fallback-resolution/v2":
        raise RouterError("unsupported fallback plan schema")
    for key in ("catalog_hash", "profile_hash", "prior_policy_hash", "policy_hash"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]):
            raise RouterError("fallback plan.%s is not a SHA-256 hash" % key)
    if plan["catalog_hash"] != content_hash(catalog):
        raise RouterError("fallback plan catalog hash mismatch")
    if project_policy:
        if plan["profile_version"] != 1:
            raise RouterError("project policy fallback profile identity mismatch")
        profile = model_policy_profile(plan["model_policy"], routes)
    else:
        profile = _profile(profile_map, plan["profile_id"])
    if plan["profile_version"] != profile["version"]:
        raise RouterError("fallback plan profile version mismatch")
    if plan["profile_hash"] != profile_hash(profile):
        raise RouterError("fallback plan profile hash mismatch")
    if plan["failed_role"] not in ROLES:
        raise RouterError("fallback plan failed role is invalid")
    if plan["failed_route_id"] not in routes:
        raise RouterError("fallback plan failed route is invalid")
    future_roles = plan["future_roles"]
    if (
        not isinstance(future_roles, list)
        or plan["failed_role"] not in future_roles
        or len(future_roles) != len(set(future_roles))
        or any(role not in ROLES for role in future_roles)
    ):
        raise RouterError("fallback plan future roles are invalid")
    contributors = validate_contributor_families(plan["contributor_families"])
    exceptions = validate_boundary_exceptions(
        plan.get("boundary_exceptions"), contributors, future_roles
    )
    _exact_keys(plan["selections"], frozenset(ROLES), "fallback plan selections")
    authorized = {
        role: dict(_fallback_role_policies(profile, role)) for role in ROLES
    }
    for role in ROLES:
        selection = plan["selections"][role]
        _exact_keys(selection, SELECTION_KEYS, "fallback plan selections.%s" % role)
        route_id = selection["route_id"]
        if route_id not in authorized[role]:
            raise RouterError("fallback route is not profile-authorized for %s" % role)
        expected = _selected_tuple(
            role,
            routes[route_id],
            authorized[role][route_id],
            {
                "adapter_version": selection["adapter_version"],
                "reported_identity": selection["reported_identity"],
            },
        )
        if selection != expected:
            raise RouterError("fallback plan route tuple mismatch for %s" % role)
        if role in future_roles and route_id == plan["failed_route_id"]:
            raise RouterError("fallback plan reuses the failed route")
    if (
        _validate_fallback_assignment(
            plan["selections"], contributors, exceptions
        )
        != contributors
    ):
        raise RouterError("fallback contributor-family constraints are invalid")
    without_hash = dict(plan)
    without_hash.pop("policy_hash")
    if plan["policy_hash"] != content_hash(without_hash):
        raise RouterError("fallback plan policy hash mismatch")
    return plan


def resolve_fallback_policy(
    catalog,
    routes,
    profile,
    readiness,
    prior_plan,
    failed_role,
    failed_route_id,
    future_roles,
    contributor_families,
    boundary_exceptions=None,
):
    """Resolve all remaining roles while preserving immutable role history.

    Only future roles are readiness-gated. The exact failed route is excluded
    from every future role, and candidate traversal advances only through
    UNAVAILABLE. INVALID and UNKNOWN are terminal evidence failures.
    """
    profile_map = {profile["profile_id"]: profile}
    if prior_plan.get("schema") in (
        "model-resolution-plan/v1",
        "model-resolution-plan/v2",
    ):
        validate_plan(
            prior_plan,
            catalog,
            routes,
            profile_map,
            allow_historical_catalog=True,
        )
    elif prior_plan.get("schema") == "model-fallback-resolution/v2":
        validate_fallback_plan(prior_plan, catalog, routes, profile_map)
    else:
        raise RouterError("unsupported prior resolution schema")
    if failed_role not in ROLES:
        raise RouterError("unknown failed role: %s" % failed_role)
    if failed_route_id not in routes:
        raise RouterError("unknown failed route: %s" % failed_route_id)
    if prior_plan["selections"][failed_role]["route_id"] != failed_route_id:
        raise RouterError("failed route does not match the prior role selection")
    if not isinstance(future_roles, list) or not future_roles:
        raise RouterError("future_roles must be a non-empty role array")
    if len(set(future_roles)) != len(future_roles):
        raise RouterError("future_roles contains a duplicate role")
    if any(role not in ROLES for role in future_roles):
        raise RouterError("future_roles contains an unknown role")
    if failed_role not in future_roles:
        raise RouterError("future_roles must include the failed role")

    normalized_readiness = validate_readiness(readiness, routes)
    contributors = validate_contributor_families(contributor_families)
    exceptions = validate_boundary_exceptions(
        boundary_exceptions, contributors, future_roles
    )
    failed_boundary = BOUNDARY_PRODUCER.get(failed_role)
    failed_family = routes[failed_route_id]["provider_family"]
    if failed_boundary is not None and failed_family not in contributors[failed_boundary]:
        contributors[failed_boundary].append(failed_family)

    future = frozenset(future_roles)
    selections = dict(
        (role, dict(prior_plan["selections"][role])) for role in ROLES
    )
    choices = {}
    for role in ROLES:
        if role not in future:
            continue
        policies = _fallback_role_policies(profile, role)
        prior_route = prior_plan["selections"][role]["route_id"]
        if prior_route != failed_route_id:
            policies.sort(key=lambda value: value[0] != prior_route)
        choices[role] = [
            (route_id, role_policy)
            for route_id, role_policy in policies
            if route_id != failed_route_id
        ]
        if not choices[role]:
            raise RouterError("no ready fallback candidate for %s" % role)

    ordered_future = [role for role in ROLES if role in future]

    def assign(index):
        if index == len(ordered_future):
            return _validate_fallback_assignment(
                selections, contributors, exceptions
            )
        role = ordered_future[index]
        for route_id, role_policy in choices[role]:
            state = normalized_readiness.get(route_id, {
                "state": "UNKNOWN",
                "reason": "readiness_missing",
                "adapter_version": "",
                "reported_identity": "",
            })
            if state["state"] == "UNAVAILABLE":
                continue
            if state["state"] in ("INVALID", "UNKNOWN"):
                raise RouterError(
                    "%s route %s is %s: %s"
                    % (role, route_id, state["state"], state["reason"])
                )
            if state["state"] != "READY":
                raise RouterError("%s route %s has invalid state" % (role, route_id))
            selection = _selected_tuple(
                role, routes[route_id], role_policy, state
            )
            selections[role] = selection
            effective = assign(index + 1)
            if effective is not None:
                return effective
        return None

    effective_contributors = assign(0)
    if effective_contributors is None:
        raise RouterError(
            "no complete fallback assignment satisfies contributor-family boundaries"
        )

    catalog_digest = content_hash(catalog)
    profile_digest = profile_hash(profile)
    fallback_policy = {
        "catalog_hash": catalog_digest,
        "contributor_families": effective_contributors,
        "failed_role": failed_role,
        "failed_route_id": failed_route_id,
        "future_roles": list(future_roles),
        "prior_policy_hash": prior_plan["policy_hash"],
        "profile_hash": profile_digest,
        "profile_id": profile["profile_id"],
        "profile_version": profile["version"],
        "schema": "model-fallback-resolution/v2",
        "selections": selections,
    }
    if exceptions:
        fallback_policy["boundary_exceptions"] = exceptions
    if profile["profile_id"] == "project-policy":
        model_policy = prior_plan.get("model_policy")
        if model_policy is None:
            raise RouterError("project policy fallback is missing its policy snapshot")
        if profile != model_policy_profile(model_policy, routes):
            raise RouterError("project policy fallback profile mismatch")
        fallback_policy["model_policy"] = model_policy
    fallback_policy["policy_hash"] = content_hash(fallback_policy)
    return fallback_policy


def validate_plan(plan, catalog, routes, profile_map, allow_historical_catalog=False):
    if not isinstance(plan, dict):
        raise RouterError("plan must be an object")
    schema = plan.get("schema")
    if schema == "model-resolution-plan/v1":
        _exact_keys(plan, PLAN_KEYS, "plan")
    elif schema == "model-resolution-plan/v2":
        _exact_keys(plan, PLAN_V2_KEYS, "plan")
    else:
        raise RouterError("unsupported plan schema")
    for key in ("profile_id", "portfolio_id"):
        _safe_id(plan[key], "plan.%s" % key)
    _positive_version(plan["profile_version"], "plan.profile_version")
    for key in ("profile_hash", "catalog_hash", "policy_hash"):
        value = plan[key]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RouterError("plan.%s is not a SHA-256 hash" % key)
    if (
        not allow_historical_catalog
        and plan["catalog_hash"] != content_hash(catalog)
    ):
        raise RouterError("plan catalog hash mismatch")
    if schema == "model-resolution-plan/v2":
        if plan["profile_id"] != "project-policy" or plan["profile_version"] != 1:
            raise RouterError("project policy plan profile identity mismatch")
        profile = model_policy_profile(plan["model_policy"], routes)
    else:
        profile = _profile(profile_map, plan["profile_id"])
    if plan["profile_version"] != profile["version"]:
        raise RouterError("plan profile version mismatch")
    if plan["profile_hash"] != profile_hash(profile):
        raise RouterError("plan profile hash mismatch")
    portfolios = dict((value["portfolio_id"], value) for value in profile["portfolios"])
    if plan["portfolio_id"] not in portfolios:
        raise RouterError("plan portfolio is not in profile")
    portfolio = portfolios[plan["portfolio_id"]]
    selections = plan["selections"]
    _exact_keys(selections, frozenset(ROLES), "plan.selections")
    for role in ROLES:
        selection = selections[role]
        _exact_keys(selection, SELECTION_KEYS, "plan.selections.%s" % role)
        if selection["role"] != role:
            raise RouterError("plan role tuple mismatch for %s" % role)
        route_id = selection["route_id"]
        if not isinstance(route_id, str) or route_id not in routes or not routes[route_id]["enabled"]:
            raise RouterError("plan selects unknown or disabled route %s" % route_id)
        role_policy = portfolio["roles"][role]
        if route_id not in role_policy["candidates"]:
            raise RouterError("plan route %s is not a candidate for %s" % (route_id, role))
        expected = _selected_tuple(
            role,
            routes[route_id],
            role_policy,
            {
                "adapter_version": selection["adapter_version"],
                "reported_identity": selection["reported_identity"],
            },
        )
        if selection != expected:
            raise RouterError("plan route tuple mismatch for %s" % role)
        identity = routes[route_id]["expected_reported_identity"]
        if identity and selection["reported_identity"] != identity:
            raise RouterError("plan reported identity mismatch for %s" % role)
        _safe_text(selection["adapter_version"], "plan adapter_version")
        _safe_text(selection["reported_identity"], "plan reported_identity")
    expected_policy_hash = _policy_hash(
        plan["catalog_hash"], plan["profile_hash"], portfolio, selections
    )
    if plan["policy_hash"] != expected_policy_hash:
        raise RouterError("plan policy hash mismatch")
    return plan


def _add_policy_paths(parser):
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--profiles", default=str(DEFAULT_PROFILES))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    _add_policy_paths(validate)

    hash_parser = commands.add_parser("profile-hash")
    hash_parser.add_argument("profile_id")
    _add_policy_paths(hash_parser)

    candidates = commands.add_parser("candidates")
    candidates.add_argument("profile_id")
    candidates.add_argument("role", nargs="?")
    _add_policy_paths(candidates)

    probes = commands.add_parser("probe-list")
    probes.add_argument("profile_id")
    _add_policy_paths(probes)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("profile_id")
    resolve.add_argument("readiness", nargs="?", default="-")
    _add_policy_paths(resolve)

    select = commands.add_parser("select")
    select.add_argument("plan", nargs="?", default="-")
    select.add_argument("role")
    _add_policy_paths(select)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        catalog, routes, profiles, profile_map = load_policy(args.catalog, args.profiles)
        if args.command == "validate":
            output = {
                "catalog_hash": content_hash(catalog),
                "profiles": dict(
                    (key, profile_hash(value)) for key, value in sorted(profile_map.items())
                ),
                "schema": "model-routing-validation/v1",
                "valid": True,
            }
        elif args.command == "profile-hash":
            output = {
                "profile_hash": profile_hash(_profile(profile_map, args.profile_id)),
                "profile_id": args.profile_id,
                "schema": "model-profile-hash/v1",
            }
        elif args.command == "candidates":
            profile = _profile(profile_map, args.profile_id)
            output = candidate_view(profile)
            if args.role is not None:
                if args.role not in ROLES:
                    raise RouterError("unknown role: %s" % args.role)
                output["role"] = args.role
                output["portfolios"] = [
                    {
                        "candidates": value["roles"][args.role]["candidates"],
                        "effort": value["roles"][args.role]["effort"],
                        "portfolio_id": value["portfolio_id"],
                    }
                    for value in profile["portfolios"]
                ]
        elif args.command == "probe-list":
            output = probe_list(_profile(profile_map, args.profile_id), routes)
        elif args.command == "resolve":
            readiness = (
                load_json(args.readiness)
                if args.readiness != "-"
                else load_json_stream(sys.stdin, "stdin")
            )
            output = resolve_policy(
                catalog, routes, _profile(profile_map, args.profile_id), readiness
            )
        else:
            if args.role not in ROLES:
                raise RouterError("unknown role: %s" % args.role)
            plan = (
                load_json(args.plan)
                if args.plan != "-"
                else load_json_stream(sys.stdin, "stdin")
            )
            validate_plan(plan, catalog, routes, profile_map)
            output = plan["selections"][args.role]
        sys.stdout.write(canonical_json(output) + "\n")
        return 0
    except (RouterError, json.JSONDecodeError) as exc:
        sys.stderr.write("model-router: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
