#!/usr/bin/env python3
"""Strict per-project operator state for deterministic model routing."""

import argparse
import base64
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROUTER_PATH = SCRIPT_DIR / "model-router.py"
SPEC = importlib.util.spec_from_file_location("model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)

DEFAULT_PROFILE = "balanced-v2"
MAX_FILE_SIZE = 1024 * 1024
MAX_TTL_SECONDS = 7 * 24 * 60 * 60
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
KIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")

ACTIVE_KEYS = frozenset((
    "schema", "project", "profile_id", "profile_version", "profile_hash",
    "approved_by", "approved_at",
))
OVERRIDES_KEYS = frozenset(("schema", "project", "overrides"))
OVERRIDE_KEYS = frozenset((
    "scope_type", "scope_id", "reason", "created_at", "expires_at",
    "operator_id",
))
PIN_KEYS = frozenset((
    "schema", "ticket", "kit_sha", "created_at", "resolution",
))
JOURNAL_KEYS = frozenset(("schema", "ticket", "kit_sha", "revisions"))
JOURNAL_REVISION_KEYS = frozenset((
    "revision", "parent_hash", "body", "revision_hash",
))
MIGRATION_BODY_KEYS = frozenset((
    "kind", "migrated_at", "legacy_plan_b64", "legacy_plan_sha256",
    "pin_commit", "old_kit_sha", "new_kit_sha", "policy_hash",
    "historical_selections",
))
FALLBACK_BODY_KEYS = frozenset((
    "kind", "created_at", "failed_manifest_digest",
    "approved_snapshot_digest", "reason", "approval_receipt",
    "prior_resolution", "new_resolution", "contributor_families",
))
FALLBACK_REASONS = frozenset(("credits_exhausted", "provider_unavailable"))
SCOPE_TYPES = frozenset(("account-route", "provider-family", "model", "route"))
ABSENT_POLICY_HASH = ROUTER.content_hash(None)


class ManagerError(ValueError):
    pass


def canonical_json(value):
    return ROUTER.canonical_json(value)


def _exact_keys(value, expected, location):
    if not isinstance(value, dict):
        raise ManagerError("%s must be an object" % location)
    actual = frozenset(value)
    if actual != expected:
        raise ManagerError(
            "%s keys mismatch (missing=%s extra=%s)"
            % (location, sorted(expected - actual), sorted(actual - expected))
        )


def _safe_id(value, location):
    try:
        ROUTER._safe_id(value, location)
    except ROUTER.RouterError as exc:
        raise ManagerError(str(exc))


def _timestamp(value, location):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        raise ManagerError("%s must be a UTC timestamp" % location)
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError as exc:
        raise ManagerError("%s is invalid: %s" % (location, exc))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _format_time(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_no_symlink_components(path):
    path = Path(path)
    if not path.is_absolute():
        raise ManagerError("path must be absolute: %s" % path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ManagerError("cannot inspect path %s: %s" % (current, exc))
        if stat.S_ISLNK(info.st_mode):
            raise ManagerError("symlink path component is forbidden: %s" % current)


def _ensure_directory(path):
    path = Path(path)
    _check_no_symlink_components(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise ManagerError("path component is not a directory: %s" % current)
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except OSError as exc:
            raise ManagerError("cannot create directory %s: %s" % (directory, exc))
    _check_no_symlink_components(path)


def _secure_file(path, must_exist=True, expected_mode=0o600):
    path = Path(path)
    _check_no_symlink_components(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ManagerError("file does not exist: %s" % path)
        return None
    except OSError as exc:
        raise ManagerError("cannot inspect file %s: %s" % (path, exc))
    if not stat.S_ISREG(info.st_mode):
        raise ManagerError("file must be regular: %s" % path)
    if info.st_uid != os.getuid():
        raise ManagerError("file must be owned by the current UID: %s" % path)
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise ManagerError("file mode must be %04o: %s" % (expected_mode, path))
    if info.st_size > MAX_FILE_SIZE:
        raise ManagerError("file exceeds size limit: %s" % path)
    return info


def _load_secure_json(path, required=True, expected_mode=0o600):
    info = _secure_file(path, must_exist=required, expected_mode=expected_mode)
    if info is None:
        return None
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=ROUTER._object_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, ROUTER.RouterError) as exc:
        raise ManagerError("cannot load JSON %s: %s" % (path, exc))


def _load_model_policy(path, routes, required=False):
    if path is None:
        if required:
            raise ManagerError("--policy-file is required")
        return None
    policy = _load_secure_json(Path(path), required=required, expected_mode=0o644)
    if policy is None:
        return None
    try:
        return ROUTER.validate_model_policy(policy, routes)
    except ROUTER.RouterError as exc:
        raise ManagerError("invalid project model policy: %s" % exc)


def _policy_hash(policy):
    return ABSENT_POLICY_HASH if policy is None else ROUTER.content_hash(policy)


def _policy_preview(current, proposed):
    body = {
        "current_policy_hash": _policy_hash(current),
        "policy": proposed,
    }
    return {
        **body,
        "preview_hash": ROUTER.content_hash(body),
        "schema": "factory-model-policy-preview/v1",
    }


def _atomic_write(path, value, mode=0o600):
    path = Path(path)
    _ensure_directory(path.parent)
    _secure_file(path, must_exist=False, expected_mode=mode)
    data = (canonical_json(value) + "\n").encode("utf-8")
    if len(data) > MAX_FILE_SIZE:
        raise ManagerError("output exceeds size limit")
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".%s." % path.name, dir=str(path.parent)
        )
        temporary = Path(name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _check_no_symlink_components(path)
        _secure_file(path, must_exist=False, expected_mode=mode)
        os.replace(str(temporary), str(path))
        temporary = None
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise ManagerError("cannot atomically write %s: %s" % (path, exc))
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


@contextmanager
def _exclusive_lock(path):
    path = Path(path)
    _ensure_directory(path.parent)
    _check_no_symlink_components(path)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(str(path), flags, 0o600)
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ManagerError("lock file is unsafe: %s" % path)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ManagerError("cannot lock %s: %s" % (path, exc))
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _state_paths(state_root, project):
    root = Path(state_root)
    if not root.is_absolute():
        raise ManagerError("--state-root must be an absolute path")
    _safe_id(project, "--project")
    _check_no_symlink_components(root)
    routing = root / project / "routing"
    return routing / "active.json", routing / "overrides.json"


def _validate_active(value, project, profile_map):
    _exact_keys(value, ACTIVE_KEYS, "active state")
    if value["schema"] != "model-routing-active/v1":
        raise ManagerError("unsupported active state schema")
    if value["project"] != project:
        raise ManagerError("active state project mismatch")
    for key in ("project", "profile_id", "approved_by"):
        _safe_id(value[key], "active.%s" % key)
    profile = profile_map.get(value["profile_id"])
    if profile is None:
        raise ManagerError("active state references unknown profile")
    if value["profile_version"] != profile["version"]:
        raise ManagerError("active profile version mismatch")
    if value["profile_hash"] != ROUTER.profile_hash(profile):
        raise ManagerError("active profile hash mismatch")
    _timestamp(value["approved_at"], "active.approved_at")
    return value


def _scope_id(value, scope_type, routes):
    if scope_type == "model":
        try:
            ROUTER._safe_selection(value, "scope ID")
        except ROUTER.RouterError as exc:
            raise ManagerError(str(exc))
        valid = {route["selection_id"] for route in routes.values()}
    else:
        _safe_id(value, "scope ID")
        field = {
            "account-route": "account_route_id",
            "provider-family": "provider_family",
            "route": "route_id",
        }[scope_type]
        valid = {route[field] for route in routes.values()}
    if value not in valid:
        raise ManagerError("unknown %s scope ID: %s" % (scope_type, value))


def _validate_overrides(value, project, routes):
    _exact_keys(value, OVERRIDES_KEYS, "overrides state")
    if value["schema"] != "model-routing-overrides/v1":
        raise ManagerError("unsupported overrides state schema")
    if value["project"] != project:
        raise ManagerError("overrides state project mismatch")
    _safe_id(value["project"], "overrides.project")
    if not isinstance(value["overrides"], list):
        raise ManagerError("overrides must be an array")
    seen = set()
    for index, override in enumerate(value["overrides"]):
        location = "overrides[%d]" % index
        _exact_keys(override, OVERRIDE_KEYS, location)
        if override["scope_type"] not in SCOPE_TYPES:
            raise ManagerError("%s.scope_type is invalid" % location)
        _scope_id(override["scope_id"], override["scope_type"], routes)
        if override["reason"] != "credits_exhausted":
            raise ManagerError("%s.reason is invalid" % location)
        _safe_id(override["operator_id"], "%s.operator_id" % location)
        created = _timestamp(override["created_at"], "%s.created_at" % location)
        expires = _timestamp(override["expires_at"], "%s.expires_at" % location)
        if expires <= created:
            raise ManagerError("%s expiry must follow creation" % location)
        key = (override["scope_type"], override["scope_id"])
        if key in seen:
            raise ManagerError("duplicate override scope")
        seen.add(key)
    return value


def _load_active(path, project, profile_map, required=False):
    value = _load_secure_json(path, required=required)
    return None if value is None else _validate_active(value, project, profile_map)


def _load_overrides(path, project, routes):
    value = _load_secure_json(path, required=False)
    if value is None:
        return {
            "schema": "model-routing-overrides/v1",
            "project": project,
            "overrides": [],
        }
    return _validate_overrides(value, project, routes)


def _profile_for_plan(
    active_path, project, profile_map, routes, requested=None, model_policy=None
):
    if requested is not None:
        _safe_id(requested, "--profile")
        if requested == "project-policy":
            if model_policy is None:
                raise ManagerError("project model policy is absent")
            return ROUTER.model_policy_profile(model_policy, routes), model_policy
        if requested not in profile_map:
            raise ManagerError("unknown profile: %s" % requested)
        return profile_map[requested], None
    if model_policy is not None:
        return ROUTER.model_policy_profile(model_policy, routes), model_policy
    active = _load_active(active_path, project, profile_map)
    profile_id = active["profile_id"] if active else DEFAULT_PROFILE
    if profile_id not in profile_map:
        raise ManagerError("default profile is absent")
    return profile_map[profile_id], None


def _parse_json_argument(value, location):
    try:
        return json.loads(value, object_pairs_hook=ROUTER._object_no_duplicates)
    except (json.JSONDecodeError, ROUTER.RouterError) as exc:
        raise ManagerError("cannot parse %s JSON: %s" % (location, exc))


def _validate_pin(
    value, catalog, routes, profile_map, allow_historical_catalog=False
):
    _exact_keys(value, PIN_KEYS, "ticket plan")
    if value["schema"] != "ticket-model-route-plan/v1":
        raise ManagerError("unsupported ticket plan schema")
    if not isinstance(value["ticket"], str) or not TICKET.fullmatch(value["ticket"]):
        raise ManagerError("ticket plan has invalid ticket")
    if not isinstance(value["kit_sha"], str) or not KIT_SHA.fullmatch(value["kit_sha"]):
        raise ManagerError("ticket plan has invalid kit SHA")
    _timestamp(value["created_at"], "ticket plan created_at")
    try:
        ROUTER.validate_plan(
            value["resolution"],
            catalog,
            routes,
            profile_map,
            allow_historical_catalog=allow_historical_catalog,
        )
    except ROUTER.RouterError as exc:
        raise ManagerError("invalid embedded resolution: %s" % exc)
    return value


def _digest(value, location):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ManagerError("%s must be a SHA-256 hash" % location)


def _revision_hash(revision, parent_hash, body):
    return ROUTER.content_hash({
        "body": body,
        "parent_hash": parent_hash,
        "revision": revision,
    })


def migrate_v1_plan(plan_blob, pin_commit, new_kit_sha, migrated_at,
                    catalog, routes, profile_map):
    """Create revision zero without changing one byte of v1 provenance."""
    if not isinstance(plan_blob, bytes):
        raise ManagerError("legacy plan blob must be bytes")
    try:
        value = json.loads(
            plan_blob.decode("utf-8"),
            object_pairs_hook=ROUTER._object_no_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError, ROUTER.RouterError) as exc:
        raise ManagerError("cannot parse legacy plan: %s" % exc)
    _validate_pin(
        value, catalog, routes, profile_map, allow_historical_catalog=True
    )
    if not isinstance(pin_commit, str) or not KIT_SHA.fullmatch(pin_commit):
        raise ManagerError("pin commit must be 40 lowercase hex characters")
    if not isinstance(new_kit_sha, str) or not KIT_SHA.fullmatch(new_kit_sha):
        raise ManagerError("new kit SHA must be 40 lowercase hex characters")
    _timestamp(migrated_at, "migration migrated_at")
    body = {
        "historical_selections": value["resolution"]["selections"],
        "kind": "migration",
        "legacy_plan_b64": base64.b64encode(plan_blob).decode("ascii"),
        "legacy_plan_sha256": hashlib.sha256(plan_blob).hexdigest(),
        "migrated_at": migrated_at,
        "new_kit_sha": new_kit_sha,
        "old_kit_sha": value["kit_sha"],
        "pin_commit": pin_commit,
        "policy_hash": value["resolution"]["policy_hash"],
    }
    revision = {
        "body": body,
        "parent_hash": None,
        "revision": 0,
    }
    revision["revision_hash"] = _revision_hash(0, None, body)
    return {
        "kit_sha": new_kit_sha,
        "revisions": [revision],
        "schema": "ticket-model-route-journal/v2",
        "ticket": value["ticket"],
    }


def active_resolution(journal):
    body = journal["revisions"][-1]["body"]
    if body["kind"] == "migration":
        plan_blob = base64.b64decode(body["legacy_plan_b64"], validate=True)
        return json.loads(
            plan_blob.decode("utf-8"),
            object_pairs_hook=ROUTER._object_no_duplicates,
        )["resolution"]
    return body["new_resolution"]


def validate_journal(value, catalog, routes, profile_map):
    _exact_keys(value, JOURNAL_KEYS, "route journal")
    if value["schema"] != "ticket-model-route-journal/v2":
        raise ManagerError("unsupported route journal schema")
    if not isinstance(value["ticket"], str) or not TICKET.fullmatch(value["ticket"]):
        raise ManagerError("route journal has invalid ticket")
    if not isinstance(value["kit_sha"], str) or not KIT_SHA.fullmatch(value["kit_sha"]):
        raise ManagerError("route journal has invalid kit SHA")
    revisions = value["revisions"]
    if not isinstance(revisions, list) or not revisions:
        raise ManagerError("route journal revisions must be a non-empty array")
    parent_hash = None
    prior_resolution = None
    for index, revision in enumerate(revisions):
        location = "route journal revisions[%d]" % index
        _exact_keys(revision, JOURNAL_REVISION_KEYS, location)
        if revision["revision"] != index:
            raise ManagerError("%s is not monotonic" % location)
        if revision["parent_hash"] != parent_hash:
            raise ManagerError("%s parent hash mismatch" % location)
        expected_hash = _revision_hash(index, parent_hash, revision["body"])
        if revision["revision_hash"] != expected_hash:
            raise ManagerError("%s hash mismatch" % location)
        body = revision["body"]
        if index == 0:
            _exact_keys(body, MIGRATION_BODY_KEYS, "%s.body" % location)
            if body["kind"] != "migration":
                raise ManagerError("route journal revision zero must be migration")
            for key in ("pin_commit", "old_kit_sha", "new_kit_sha"):
                if not isinstance(body[key], str) or not KIT_SHA.fullmatch(body[key]):
                    raise ManagerError("migration %s is invalid" % key)
            _timestamp(body["migrated_at"], "migration migrated_at")
            _digest(body["legacy_plan_sha256"], "migration legacy plan digest")
            _digest(body["policy_hash"], "migration policy hash")
            try:
                plan_blob = base64.b64decode(body["legacy_plan_b64"], validate=True)
                legacy = json.loads(
                    plan_blob.decode("utf-8"),
                    object_pairs_hook=ROUTER._object_no_duplicates,
                )
            except (ValueError, UnicodeError, json.JSONDecodeError,
                    ROUTER.RouterError) as exc:
                raise ManagerError("invalid migration legacy plan: %s" % exc)
            _validate_pin(
                legacy,
                catalog,
                routes,
                profile_map,
                allow_historical_catalog=True,
            )
            if hashlib.sha256(plan_blob).hexdigest() != body["legacy_plan_sha256"]:
                raise ManagerError("migration legacy plan digest mismatch")
            if legacy["ticket"] != value["ticket"]:
                raise ManagerError("migration ticket mismatch")
            if legacy["kit_sha"] != body["old_kit_sha"]:
                raise ManagerError("migration old kit SHA mismatch")
            if body["new_kit_sha"] != value["kit_sha"]:
                raise ManagerError("migration new kit SHA mismatch")
            if legacy["resolution"]["policy_hash"] != body["policy_hash"]:
                raise ManagerError("migration policy hash mismatch")
            if legacy["resolution"]["selections"] != body["historical_selections"]:
                raise ManagerError("migration historical selections mismatch")
            prior_resolution = legacy["resolution"]
        else:
            _exact_keys(body, FALLBACK_BODY_KEYS, "%s.body" % location)
            if body["kind"] != "fallback":
                raise ManagerError("later route journal revisions must be fallback")
            _timestamp(body["created_at"], "fallback created_at")
            _digest(body["failed_manifest_digest"], "failed manifest digest")
            _digest(body["approved_snapshot_digest"], "approved snapshot digest")
            if body["reason"] not in FALLBACK_REASONS:
                raise ManagerError("fallback reason is not eligible")
            if not isinstance(body["approval_receipt"], dict):
                raise ManagerError("fallback approval receipt must be an object")
            if body["prior_resolution"] != prior_resolution:
                raise ManagerError("fallback prior resolution mismatch")
            try:
                contributors = ROUTER.validate_contributor_families(
                    body["contributor_families"]
                )
            except ROUTER.RouterError as exc:
                raise ManagerError(str(exc))
            new_resolution = body["new_resolution"]
            if (
                not isinstance(new_resolution, dict)
                or new_resolution.get("schema") != "model-fallback-resolution/v2"
                or new_resolution.get("contributor_families") != contributors
            ):
                raise ManagerError("fallback new resolution is invalid")
            if new_resolution.get("prior_policy_hash") != prior_resolution["policy_hash"]:
                raise ManagerError("fallback prior policy hash mismatch")
            try:
                ROUTER.validate_fallback_plan(
                    new_resolution, catalog, routes, profile_map
                )
            except ROUTER.RouterError as exc:
                raise ManagerError("invalid fallback resolution: %s" % exc)
            prior_resolution = new_resolution
        parent_hash = revision["revision_hash"]
    return value


def append_fallback_revision(journal, new_resolution, failed_manifest_digest,
                             approved_snapshot_digest, reason, approval_receipt,
                             created_at, catalog, routes, profile_map):
    """Return a new journal value; never mutate or rewrite prior revisions."""
    validate_journal(journal, catalog, routes, profile_map)
    _timestamp(created_at, "fallback created_at")
    _digest(failed_manifest_digest, "failed manifest digest")
    _digest(approved_snapshot_digest, "approved snapshot digest")
    if reason not in FALLBACK_REASONS:
        raise ManagerError("fallback reason is not eligible")
    if not isinstance(approval_receipt, dict):
        raise ManagerError("fallback approval receipt must be an object")
    if (
        not isinstance(new_resolution, dict)
        or new_resolution.get("schema") != "model-fallback-resolution/v2"
    ):
        raise ManagerError("new resolution must be a v2 fallback resolution")
    try:
        ROUTER.validate_fallback_plan(
            new_resolution, catalog, routes, profile_map
        )
    except ROUTER.RouterError as exc:
        raise ManagerError("invalid fallback resolution: %s" % exc)
    if new_resolution["prior_policy_hash"] != active_resolution(journal)["policy_hash"]:
        raise ManagerError("fallback resolution does not extend the journal head")
    body = {
        "approval_receipt": approval_receipt,
        "approved_snapshot_digest": approved_snapshot_digest,
        "contributor_families": new_resolution["contributor_families"],
        "created_at": created_at,
        "failed_manifest_digest": failed_manifest_digest,
        "kind": "fallback",
        "new_resolution": new_resolution,
        "prior_resolution": active_resolution(journal),
        "reason": reason,
    }
    result = dict(journal)
    result["revisions"] = list(journal["revisions"])
    number = len(result["revisions"])
    parent_hash = result["revisions"][-1]["revision_hash"]
    revision = {
        "body": body,
        "parent_hash": parent_hash,
        "revision": number,
    }
    revision["revision_hash"] = _revision_hash(number, parent_hash, body)
    result["revisions"].append(revision)
    validate_journal(result, catalog, routes, profile_map)
    return result


def _load_plan_blob(path):
    path = Path(path)
    if not path.is_absolute():
        raise ManagerError("legacy ticket plan path must be absolute")
    _secure_file(path, expected_mode=0o644)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ManagerError("cannot read legacy ticket plan: %s" % exc)


def _ticket_status(ticket, ticket_file, ticket_plan, catalog, routes, profile_map):
    if not isinstance(ticket, str) or not TICKET.fullmatch(ticket):
        raise ManagerError("--ticket must match T-[0-9]+")
    path = Path(ticket_file)
    if not path.is_absolute():
        raise ManagerError("--ticket-file must be absolute")
    _secure_file(path, expected_mode=0o644)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManagerError("cannot read ticket file: %s" % exc)
    states = re.findall(r"^[ \t]*(?:State|Status):[ \t]*([^\r\n]+?)[ \t]*$", text, re.M)
    if len(states) != 1:
        raise ManagerError("ticket must contain exactly one State or Status field")
    kit_shas = re.findall(r"^[ \t]*Kit-SHA:[ \t]*([0-9a-f]{40})[ \t]*$", text, re.M)
    if len(kit_shas) > 1:
        raise ManagerError("ticket contains duplicate Kit-SHA fields")
    plan_status = "absent"
    plan_hash = None
    if ticket_plan is not None:
        plan_path = Path(ticket_plan)
        if not plan_path.is_absolute():
            raise ManagerError("--ticket-plan must be absolute")
        value = _load_secure_json(plan_path, required=False, expected_mode=0o644)
        if value is not None:
            if value.get("schema") == "ticket-model-route-plan/v1":
                _validate_pin(value, catalog, routes, profile_map)
                plan_ticket, plan_kit = value["ticket"], value["kit_sha"]
            elif value.get("schema") == "ticket-model-route-journal/v2":
                validate_journal(value, catalog, routes, profile_map)
                plan_ticket, plan_kit = value["ticket"], value["kit_sha"]
            else:
                raise ManagerError("unsupported ticket route document schema")
            if plan_ticket != ticket:
                raise ManagerError("ticket plan ticket mismatch")
            if not kit_shas or plan_kit != kit_shas[0]:
                raise ManagerError("ticket plan Kit-SHA mismatch")
            plan_status = "pinned"
            try:
                plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ManagerError("cannot hash ticket plan: %s" % exc)
    return {
        "kit_sha": kit_shas[0] if kit_shas else None,
        "route_plan_hash": plan_hash,
        "route_plan_status": plan_status,
        "schema": "factory-ticket-status/v1",
        "state": states[0].strip(),
        "ticket": ticket,
    }


def _common(parser):
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--catalog", default=str(ROUTER.DEFAULT_CATALOG))
    parser.add_argument("--profiles-file", default=str(ROUTER.DEFAULT_PROFILES))
    parser.add_argument("--policy-file")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profiles = commands.add_parser("profiles")
    _common(profiles)
    candidates = commands.add_parser("policy-candidates")
    _common(candidates)
    policy_preview = commands.add_parser("policy-preview")
    _common(policy_preview)
    policy_preview.add_argument("--policy", required=True)
    policy_apply = commands.add_parser("policy-apply")
    _common(policy_apply)
    policy_apply.add_argument("--policy", required=True)
    policy_apply.add_argument("--expected-current-hash", required=True)
    policy_apply.add_argument("--approve-hash", required=True)
    reviewer_contract = commands.add_parser("reviewer-exception-contract")
    _common(reviewer_contract)
    status = commands.add_parser("status")
    _common(status)
    ticket_status = commands.add_parser("ticket-status")
    _common(ticket_status)
    ticket_status.add_argument("--ticket", required=True)
    ticket_status.add_argument("--ticket-file", required=True)
    ticket_status.add_argument("--ticket-plan")
    probe_context = commands.add_parser("probe-context")
    _common(probe_context)
    probe_list = commands.add_parser("probe-list")
    _common(probe_list)
    probe_list.add_argument("--profile")
    plan = commands.add_parser("plan")
    _common(plan)
    plan.add_argument("--readiness", required=True)
    plan.add_argument("--profile")
    activate = commands.add_parser("activate")
    _common(activate)
    activate.add_argument("--profile", required=True)
    activate.add_argument("--approve-hash", required=True)
    activate.add_argument("--approved-by", required=True)
    disable = commands.add_parser("disable")
    _common(disable)
    disable.add_argument("--scope-type", required=True, choices=sorted(SCOPE_TYPES))
    disable.add_argument("--scope-id", required=True)
    disable.add_argument("--reason", required=True, choices=("credits_exhausted",))
    disable.add_argument("--ttl-seconds", required=True, type=int)
    disable.add_argument("--operator-id", required=True)
    enable = commands.add_parser("enable")
    _common(enable)
    enable.add_argument("--scope-type", required=True, choices=sorted(SCOPE_TYPES))
    enable.add_argument("--scope-id", required=True)
    pin = commands.add_parser("pin")
    _common(pin)
    pin.add_argument("--ticket", required=True)
    pin.add_argument("--kit-sha", required=True)
    pin_source = pin.add_mutually_exclusive_group(required=True)
    pin_source.add_argument("--readiness")
    pin_source.add_argument("--resolution-file")
    pin.add_argument("--output", required=True)
    select = commands.add_parser("select")
    _common(select)
    select.add_argument("--ticket-plan", required=True)
    select.add_argument("--ticket", required=True)
    select.add_argument("--kit-sha", required=True)
    select.add_argument("--role", required=True)
    for name in ("migrate-plan", "migrate"):
        migrate = commands.add_parser(name)
        _common(migrate)
        migrate.add_argument("--ticket-plan", required=True)
        migrate.add_argument("--pin-commit", required=True)
        migrate.add_argument("--kit-sha", required=True)
        migrate.add_argument("--migrated-at", required=True)
        if name == "migrate":
            migrate.add_argument("--approve-hash", required=True)
            migrate.add_argument("--output", required=True)
    fallback_plan = commands.add_parser("fallback-plan")
    _common(fallback_plan)
    fallback_plan.add_argument("--ticket-plan", required=True)
    fallback_plan.add_argument("--readiness", required=True)
    fallback_plan.add_argument("--failed-role", required=True)
    fallback_plan.add_argument("--failed-route", required=True)
    fallback_plan.add_argument("--future-roles", required=True)
    fallback_plan.add_argument("--contributors", required=True)
    fallback = commands.add_parser("fallback")
    _common(fallback)
    fallback.add_argument("--ticket-plan", required=True)
    fallback.add_argument("--resolution-file", required=True)
    fallback.add_argument("--failed-manifest-digest", required=True)
    fallback.add_argument("--snapshot-digest", required=True)
    fallback.add_argument("--reason", required=True, choices=sorted(FALLBACK_REASONS))
    fallback.add_argument("--approval-receipt", required=True)
    fallback.add_argument("--created-at", required=True)
    fallback.add_argument("--output", required=True)
    return parser


def run(args):
    active_path, overrides_path = _state_paths(args.state_root, args.project)
    try:
        catalog, routes, profiles_doc, profile_map = ROUTER.load_policy(
            args.catalog, args.profiles_file
        )
    except ROUTER.RouterError as exc:
        raise ManagerError(str(exc))
    model_policy = _load_model_policy(args.policy_file, routes)

    if args.command == "profiles":
        active = _load_active(active_path, args.project, profile_map)
        return {
            "active_profile": active,
            "profiles": [
                {
                    "profile_hash": ROUTER.profile_hash(profile),
                    "profile_id": profile["profile_id"],
                    "profile_version": profile["version"],
                }
                for profile in profiles_doc["profiles"]
            ],
            "schema": "model-manager-profiles/v1",
        }
    if args.command == "policy-candidates":
        result = ROUTER.model_policy_candidates(routes)
        result["current_policy"] = model_policy
        result["current_policy_hash"] = _policy_hash(model_policy)
        return result
    if args.command in ("policy-preview", "policy-apply"):
        if args.policy_file is None:
            raise ManagerError("--policy-file is required")
        proposed = _parse_json_argument(args.policy, "policy")
        try:
            ROUTER.validate_model_policy(proposed, routes)
        except ROUTER.RouterError as exc:
            raise ManagerError("invalid proposed model policy: %s" % exc)
        if args.command == "policy-preview":
            return _policy_preview(model_policy, proposed)
        for value, location in (
            (args.expected_current_hash, "--expected-current-hash"),
            (args.approve_hash, "--approve-hash"),
        ):
            if not isinstance(value, str) or not SHA256.fullmatch(value):
                raise ManagerError("%s must be a SHA-256 hash" % location)
        lock_path = active_path.parent / "model-policy.lock"
        with _exclusive_lock(lock_path):
            current = _load_model_policy(args.policy_file, routes)
            preview = _policy_preview(current, proposed)
            if args.expected_current_hash != preview["current_policy_hash"]:
                raise ManagerError("model policy compare-and-swap conflict")
            if args.approve_hash != preview["preview_hash"]:
                raise ManagerError("approved hash does not match exact policy preview")
            _atomic_write(Path(args.policy_file), proposed, mode=0o644)
        return {
            "policy": proposed,
            "policy_hash": ROUTER.content_hash(proposed),
            "previous_policy_hash": preview["current_policy_hash"],
            "preview_hash": preview["preview_hash"],
            "schema": "factory-model-policy-apply/v1",
        }
    if args.command == "reviewer-exception-contract":
        return {
            "normal_policy_allowed": False,
            "reason": "ticket-scoped one-use approval integration is not available",
            "schema": "factory-reviewer-exception-contract/v1",
            "supported": False,
            "ticket_scoped": True,
            "one_use": True,
        }
    if args.command == "ticket-status":
        return _ticket_status(
            args.ticket, args.ticket_file, args.ticket_plan,
            catalog, routes, profile_map,
        )
    if args.command == "status":
        active = _load_active(active_path, args.project, profile_map)
        overrides = _load_overrides(overrides_path, args.project, routes)
        now = _now()
        return {
            "active_profile": active,
            "overrides": [
                value for value in overrides["overrides"]
                if _timestamp(value["expires_at"], "override.expires_at") > now
            ],
            "model_policy": model_policy,
            "model_policy_hash": _policy_hash(model_policy),
            "project": args.project,
            "schema": "model-manager-status/v1",
        }
    if args.command == "probe-context":
        active = _load_active(active_path, args.project, profile_map)
        overrides = _load_overrides(overrides_path, args.project, routes)
        profile_id = (
            "project-policy" if model_policy is not None
            else active["profile_id"] if active else DEFAULT_PROFILE
        )
        if profile_id not in profile_map and profile_id != "project-policy":
            raise ManagerError("default profile is absent")
        now = _now()
        disabled = set()
        for override in overrides["overrides"]:
            if _timestamp(override["expires_at"], "override.expires_at") <= now:
                continue
            scope_type = override["scope_type"]
            scope_id = override["scope_id"]
            for route_id, route in routes.items():
                matches = (
                    (scope_type == "route" and route_id == scope_id)
                    or (
                        scope_type == "account-route"
                        and route["account_route_id"] == scope_id
                    )
                    or (
                        scope_type == "provider-family"
                        and route["provider_family"] == scope_id
                    )
                    or (
                        scope_type == "model"
                        and route["selection_id"] == scope_id
                    )
                )
                if matches:
                    disabled.add(route_id)
        return {
            "disabled_route_ids": sorted(disabled),
            "profile_id": profile_id,
            "model_policy_hash": (
                ROUTER.content_hash(model_policy) if model_policy is not None else None
            ),
            "project": args.project,
            "schema": "model-manager-probe-context/v1",
        }
    if args.command == "probe-list":
        profile, _ = _profile_for_plan(
            active_path, args.project, profile_map, routes,
            args.profile, model_policy,
        )
        return ROUTER.probe_list(profile, routes)
    if args.command == "plan":
        profile, effective_policy = _profile_for_plan(
            active_path, args.project, profile_map, routes,
            args.profile, model_policy,
        )
        readiness = _parse_json_argument(args.readiness, "readiness")
        try:
            return ROUTER.resolve_policy(
                catalog, routes, profile, readiness, effective_policy
            )
        except ROUTER.RouterError as exc:
            raise ManagerError(str(exc))
    if args.command == "activate":
        _safe_id(args.profile, "--profile")
        _safe_id(args.approved_by, "--approved-by")
        if not isinstance(args.approve_hash, str) or not SHA256.fullmatch(args.approve_hash):
            raise ManagerError("--approve-hash must be a SHA-256 hash")
        if args.profile not in profile_map:
            raise ManagerError("unknown profile: %s" % args.profile)
        profile = profile_map[args.profile]
        expected_hash = ROUTER.profile_hash(profile)
        if args.approve_hash != expected_hash:
            raise ManagerError("approved hash does not match catalog profile hash")
        value = {
            "approved_at": _format_time(_now()),
            "approved_by": args.approved_by,
            "profile_hash": expected_hash,
            "profile_id": args.profile,
            "profile_version": profile["version"],
            "project": args.project,
            "schema": "model-routing-active/v1",
        }
        _atomic_write(active_path, value)
        return value
    if args.command in ("disable", "enable"):
        _scope_id(args.scope_id, args.scope_type, routes)
        state = _load_overrides(overrides_path, args.project, routes)
        key = (args.scope_type, args.scope_id)
        values = [
            value for value in state["overrides"]
            if (value["scope_type"], value["scope_id"]) != key
        ]
        if args.command == "disable":
            _safe_id(args.operator_id, "--operator-id")
            if (
                isinstance(args.ttl_seconds, bool)
                or args.ttl_seconds < 1
                or args.ttl_seconds > MAX_TTL_SECONDS
            ):
                raise ManagerError(
                    "--ttl-seconds must be between 1 and %d" % MAX_TTL_SECONDS
                )
            created = _now()
            values.append({
                "created_at": _format_time(created),
                "expires_at": _format_time(
                    created + datetime.timedelta(seconds=args.ttl_seconds)
                ),
                "operator_id": args.operator_id,
                "reason": args.reason,
                "scope_id": args.scope_id,
                "scope_type": args.scope_type,
            })
        state["overrides"] = values
        _atomic_write(overrides_path, state)
        return state
    if args.command == "pin":
        if not isinstance(args.ticket, str) or not TICKET.fullmatch(args.ticket):
            raise ManagerError("--ticket must match T-[0-9]+")
        if not isinstance(args.kit_sha, str) or not KIT_SHA.fullmatch(args.kit_sha):
            raise ManagerError("--kit-sha must be 40 lowercase hex characters")
        output = Path(args.output)
        if not output.is_absolute():
            raise ManagerError("--output must be an absolute path")
        existing = _load_secure_json(output, required=False, expected_mode=0o644)
        if existing is not None:
            _validate_pin(existing, catalog, routes, profile_map)
            if existing["ticket"] != args.ticket or existing["kit_sha"] != args.kit_sha:
                raise ManagerError("existing pin ticket or kit SHA mismatch")
            return existing
        if args.resolution_file is not None:
            resolution_path = Path(args.resolution_file)
            if not resolution_path.is_absolute():
                raise ManagerError("--resolution-file must be an absolute path")
            resolution = _load_secure_json(resolution_path)
            try:
                ROUTER.validate_plan(resolution, catalog, routes, profile_map)
            except ROUTER.RouterError as exc:
                raise ManagerError("invalid resolution file: %s" % exc)
        else:
            profile, effective_policy = _profile_for_plan(
                active_path, args.project, profile_map, routes,
                model_policy=model_policy,
            )
            readiness = _parse_json_argument(args.readiness, "readiness")
            try:
                resolution = ROUTER.resolve_policy(
                    catalog, routes, profile, readiness, effective_policy
                )
            except ROUTER.RouterError as exc:
                raise ManagerError(str(exc))
        value = {
            "created_at": _format_time(_now()),
            "kit_sha": args.kit_sha,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": args.ticket,
        }
        _atomic_write(output, value, mode=0o644)
        return value
    if args.command in ("migrate-plan", "migrate"):
        journal = migrate_v1_plan(
            _load_plan_blob(args.ticket_plan),
            args.pin_commit,
            args.kit_sha,
            args.migrated_at,
            catalog,
            routes,
            profile_map,
        )
        preview_hash = ROUTER.content_hash(journal)
        if args.command == "migrate-plan":
            return {
                "journal": journal,
                "preview_hash": preview_hash,
                "schema": "ticket-model-route-migration-preview/v1",
            }
        if args.approve_hash != preview_hash:
            raise ManagerError("approved hash does not match migration preview")
        output = Path(args.output)
        if not output.is_absolute():
            raise ManagerError("--output must be an absolute path")
        existing = _load_secure_json(output, required=False, expected_mode=0o644)
        if existing is not None:
            if existing.get("schema") == "ticket-model-route-journal/v2":
                validate_journal(existing, catalog, routes, profile_map)
                if existing != journal:
                    raise ManagerError("existing route journal differs from migration")
                return existing
            if output.resolve() != Path(args.ticket_plan).resolve():
                raise ManagerError("migration may replace only its exact v1 plan")
        _atomic_write(output, journal, mode=0o644)
        return journal
    if args.command == "fallback-plan":
        journal = _load_secure_json(
            Path(args.ticket_plan), expected_mode=0o644
        )
        validate_journal(journal, catalog, routes, profile_map)
        prior = active_resolution(journal)
        profile = profile_map.get(prior["profile_id"])
        if profile is None:
            raise ManagerError("journal references an unknown profile")
        readiness = _parse_json_argument(args.readiness, "readiness")
        future_roles = _parse_json_argument(args.future_roles, "future roles")
        contributors = _parse_json_argument(args.contributors, "contributors")
        try:
            return ROUTER.resolve_fallback_policy(
                catalog,
                routes,
                profile,
                readiness,
                prior,
                args.failed_role,
                args.failed_route,
                future_roles,
                contributors,
            )
        except ROUTER.RouterError as exc:
            raise ManagerError(str(exc))
    if args.command == "fallback":
        journal_path = Path(args.ticket_plan)
        journal = _load_secure_json(journal_path, expected_mode=0o644)
        validate_journal(journal, catalog, routes, profile_map)
        resolution = _load_secure_json(Path(args.resolution_file))
        approval = _parse_json_argument(args.approval_receipt, "approval receipt")
        result = append_fallback_revision(
            journal,
            resolution,
            args.failed_manifest_digest,
            args.snapshot_digest,
            args.reason,
            approval,
            args.created_at,
            catalog,
            routes,
            profile_map,
        )
        output = Path(args.output)
        if output.resolve() != journal_path.resolve():
            raise ManagerError("fallback output must replace the ticket journal")
        _atomic_write(output, result, mode=0o644)
        return result
    if args.command == "select":
        if args.role not in ROUTER.ROLES:
            raise ManagerError("unknown role: %s" % args.role)
        if not isinstance(args.ticket, str) or not TICKET.fullmatch(args.ticket):
            raise ManagerError("--ticket must match T-[0-9]+")
        value = _load_secure_json(Path(args.ticket_plan), expected_mode=0o644)
        if value.get("schema") == "ticket-model-route-plan/v1":
            _validate_pin(value, catalog, routes, profile_map)
            resolution = value["resolution"]
            ticket = value["ticket"]
            kit_sha = value["kit_sha"]
        elif value.get("schema") == "ticket-model-route-journal/v2":
            validate_journal(value, catalog, routes, profile_map)
            resolution = active_resolution(value)
            ticket = value["ticket"]
            kit_sha = value["kit_sha"]
        else:
            raise ManagerError("unsupported ticket route document schema")
        if ticket != args.ticket:
            raise ManagerError("ticket plan ticket mismatch")
        if kit_sha != args.kit_sha:
            raise ManagerError("ticket plan kit SHA mismatch")
        return resolution["selections"][args.role]
    raise ManagerError("unsupported command")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        output = run(args)
        sys.stdout.write(canonical_json(output) + "\n")
        return 0
    except (ManagerError, ROUTER.RouterError) as exc:
        sys.stderr.write("model-manager: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
