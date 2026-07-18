#!/usr/bin/env python3
"""Strict per-project operator state for deterministic model routing."""

import argparse
import datetime
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROUTER_PATH = SCRIPT_DIR / "model-router.py"
SPEC = importlib.util.spec_from_file_location("model_router", ROUTER_PATH)
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)

DEFAULT_PROFILE = "legacy-balanced-v1"
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
SCOPE_TYPES = frozenset(("account-route", "provider-family", "model", "route"))


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


def _profile_for_plan(active_path, project, profile_map, requested=None):
    if requested is not None:
        _safe_id(requested, "--profile")
        if requested not in profile_map:
            raise ManagerError("unknown profile: %s" % requested)
        return profile_map[requested]
    active = _load_active(active_path, project, profile_map)
    profile_id = active["profile_id"] if active else DEFAULT_PROFILE
    if profile_id not in profile_map:
        raise ManagerError("default profile is absent")
    return profile_map[profile_id]


def _parse_json_argument(value, location):
    try:
        return json.loads(value, object_pairs_hook=ROUTER._object_no_duplicates)
    except (json.JSONDecodeError, ROUTER.RouterError) as exc:
        raise ManagerError("cannot parse %s JSON: %s" % (location, exc))


def _validate_pin(value, catalog, routes, profile_map):
    _exact_keys(value, PIN_KEYS, "ticket plan")
    if value["schema"] != "ticket-model-route-plan/v1":
        raise ManagerError("unsupported ticket plan schema")
    if not isinstance(value["ticket"], str) or not TICKET.fullmatch(value["ticket"]):
        raise ManagerError("ticket plan has invalid ticket")
    if not isinstance(value["kit_sha"], str) or not KIT_SHA.fullmatch(value["kit_sha"]):
        raise ManagerError("ticket plan has invalid kit SHA")
    _timestamp(value["created_at"], "ticket plan created_at")
    try:
        ROUTER.validate_plan(value["resolution"], catalog, routes, profile_map)
    except ROUTER.RouterError as exc:
        raise ManagerError("invalid embedded resolution: %s" % exc)
    return value


def _common(parser):
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--catalog", default=str(ROUTER.DEFAULT_CATALOG))
    parser.add_argument("--profiles-file", default=str(ROUTER.DEFAULT_PROFILES))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profiles = commands.add_parser("profiles")
    _common(profiles)
    status = commands.add_parser("status")
    _common(status)
    probe_context = commands.add_parser("probe-context")
    _common(probe_context)
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
    return parser


def run(args):
    active_path, overrides_path = _state_paths(args.state_root, args.project)
    try:
        catalog, routes, profiles_doc, profile_map = ROUTER.load_policy(
            args.catalog, args.profiles_file
        )
    except ROUTER.RouterError as exc:
        raise ManagerError(str(exc))

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
            "project": args.project,
            "schema": "model-manager-status/v1",
        }
    if args.command == "probe-context":
        active = _load_active(active_path, args.project, profile_map)
        overrides = _load_overrides(overrides_path, args.project, routes)
        profile_id = active["profile_id"] if active else DEFAULT_PROFILE
        if profile_id not in profile_map:
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
            "project": args.project,
            "schema": "model-manager-probe-context/v1",
        }
    if args.command == "plan":
        profile = _profile_for_plan(active_path, args.project, profile_map, args.profile)
        readiness = _parse_json_argument(args.readiness, "readiness")
        try:
            return ROUTER.resolve_policy(catalog, routes, profile, readiness)
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
            profile = _profile_for_plan(active_path, args.project, profile_map)
            readiness = _parse_json_argument(args.readiness, "readiness")
            try:
                resolution = ROUTER.resolve_policy(catalog, routes, profile, readiness)
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
    if args.command == "select":
        if args.role not in ROUTER.ROLES:
            raise ManagerError("unknown role: %s" % args.role)
        if not isinstance(args.ticket, str) or not TICKET.fullmatch(args.ticket):
            raise ManagerError("--ticket must match T-[0-9]+")
        value = _load_secure_json(Path(args.ticket_plan), expected_mode=0o644)
        _validate_pin(value, catalog, routes, profile_map)
        if value["ticket"] != args.ticket:
            raise ManagerError("ticket plan ticket mismatch")
        if value["kit_sha"] != args.kit_sha:
            raise ManagerError("ticket plan kit SHA mismatch")
        return value["resolution"]["selections"][args.role]
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
