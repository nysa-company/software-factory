#!/usr/bin/env python3
"""Fixed-argv adapter between the local console and the trusted launcher.

The browser never supplies paths or argument vectors.  A launcher path is
chosen once by the operator at process startup, validated, and then combined
only with command forms declared below.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
SCOPE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
TICKET_RE = re.compile(r"T-[0-9]+")
RUN_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")
SETTING_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}")
VALUE_RE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,6})?")
UTC_RE = re.compile(
    r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
MAX_OUTPUT_BYTES = 1_048_576

# Workflow, envelope, and spend intentionally target one future, fixed
# launcher route.  Until the sealed launcher implements it, callers receive a
# fail-closed "launcher unavailable" result rather than reading product paths.
SNAPSHOT_COMMANDS = {
    "workflow": ("operator-snapshot", "workflow", "--json"),
    "model": ("models", "policy-candidates", "--json"),
    "envelope": ("operator-snapshot", "envelope", "--json"),
    "spend": ("operator-snapshot", "spend", "--json"),
}


class SnapshotError(ValueError):
    """A safe boundary error suitable for a local API response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_project(project: str) -> str:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise SnapshotError("invalid_project", "invalid project selector")
    return project


def validate_launcher(path: Path) -> Path:
    """Resolve one operator-supplied launcher without accepting later paths."""
    if not path.is_absolute():
        raise SnapshotError("invalid_launcher", "launcher path must be absolute")
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        after = resolved.stat()
    except OSError as error:
        raise SnapshotError("invalid_launcher", "launcher is unavailable") from error
    if (
        stat.S_IFMT(before.st_mode) != stat.S_IFREG
        or path.is_symlink()
        or resolved != path
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) & 0o022
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or not os.access(resolved, os.X_OK)
    ):
        raise SnapshotError(
            "invalid_launcher", "launcher must be an owner-controlled executable file"
        )
    return resolved


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value) or value == "auto":
        raise SnapshotError("invalid_action", f"invalid {label}")
    return value


def _scope(scope_type: Any, scope_id: Any) -> tuple[str, str]:
    if scope_type not in {"account-route", "provider-family", "model", "route"}:
        raise SnapshotError("invalid_action", "invalid model scope type")
    if not isinstance(scope_id, str) or not SCOPE_ID_RE.fullmatch(scope_id):
        raise SnapshotError("invalid_action", "invalid model scope identifier")
    if scope_id == "auto" or ".." in scope_id or "//" in scope_id:
        raise SnapshotError("invalid_action", "invalid model scope identifier")
    if scope_type != "model" and not SAFE_ID_RE.fullmatch(scope_id):
        raise SnapshotError("invalid_action", "invalid model scope identifier")
    return scope_type, scope_id


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise SnapshotError("invalid_action", f"invalid {label}")
    return value


def _settings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict) or not value or len(value) > 30:
        raise SnapshotError("invalid_action", "invalid envelope changes")
    result = []
    for key in sorted(value):
        item = value[key]
        if (
            not isinstance(key, str)
            or not SETTING_RE.fullmatch(key)
            or not isinstance(item, str)
            or not VALUE_RE.fullmatch(item)
        ):
            raise SnapshotError("invalid_action", "invalid envelope change")
        result.extend(("--set", f"{key}={item}"))
    return tuple(result)


def _policy(value: Any) -> str:
    if not isinstance(value, dict):
        raise SnapshotError("invalid_action", "model policy must be an object")
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise SnapshotError("invalid_action", "model policy is oversized")
    return encoded


def mutation_command(action: str, payload: dict[str, Any]) -> tuple[str, ...]:
    """Build only launcher forms already allowlisted by the sealed boundary."""
    if action == "model-activate":
        if set(payload) != {"profile", "approve_hash", "approved_by"}:
            raise SnapshotError("invalid_action", "model activation fields mismatch")
        profile = _safe_identifier(payload.get("profile"), "model profile")
        approval = payload.get("approve_hash")
        if not isinstance(approval, str) or not HASH_RE.fullmatch(approval):
            raise SnapshotError("invalid_action", "invalid model approval hash")
        operator = _safe_identifier(payload.get("approved_by"), "model approver")
        return (
            "models", "activate", "--profile", profile, "--approve-hash", approval,
            "--approved-by", operator, "--json",
        )
    if action == "model-disable":
        if set(payload) != {
            "scope_type", "scope_id", "ttl_seconds", "operator_id",
        }:
            raise SnapshotError("invalid_action", "model disable fields mismatch")
        scope_type, scope_id = _scope(
            payload.get("scope_type"), payload.get("scope_id")
        )
        ttl = payload.get("ttl_seconds")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 604800:
            raise SnapshotError("invalid_action", "invalid model disable TTL")
        operator = _safe_identifier(payload.get("operator_id"), "model operator")
        return (
            "models", "disable", "--scope-type", scope_type, "--scope-id", scope_id,
            "--reason", "credits_exhausted", "--ttl-seconds", str(ttl),
            "--operator-id", operator, "--json",
        )
    if action == "model-enable":
        if set(payload) != {"scope_type", "scope_id"}:
            raise SnapshotError("invalid_action", "model enable fields mismatch")
        scope_type, scope_id = _scope(
            payload.get("scope_type"), payload.get("scope_id")
        )
        return (
            "models", "enable", "--scope-type", scope_type, "--scope-id", scope_id,
            "--json",
        )
    if action == "model-policy-preview":
        if set(payload) != {"policy"}:
            raise SnapshotError("invalid_action", "model policy preview fields mismatch")
        return ("models", "policy-preview", "--policy", _policy(payload["policy"]), "--json")
    if action == "model-policy-apply":
        if set(payload) != {"policy", "expected_current_hash", "approve_hash"}:
            raise SnapshotError("invalid_action", "model policy apply fields mismatch")
        return (
            "models", "policy-apply", "--policy", _policy(payload["policy"]),
            "--expected-current-hash",
            _hash(payload["expected_current_hash"], "current policy hash"),
            "--approve-hash", _hash(payload["approve_hash"], "policy approval hash"),
            "--json",
        )
    if action in {"envelope-plan", "envelope-apply"}:
        expected = {"changes"} if action == "envelope-plan" else {"changes", "approve_hash"}
        if set(payload) != expected:
            raise SnapshotError("invalid_action", "envelope fields mismatch")
        command = ["envelope", "plan" if action == "envelope-plan" else "apply"]
        command.extend(_settings(payload["changes"]))
        if action == "envelope-apply":
            command.extend(("--approve-hash", _hash(payload["approve_hash"], "envelope approval hash")))
        command.append("--json")
        return tuple(command)
    if action in {"envelope-override-plan", "envelope-override-apply"}:
        required = {
            "scope", "ticket", "role", "day", "issued_at", "expires_at",
            "operator_id", "reason", "changes",
        }
        if action == "envelope-override-apply":
            required.add("approve_hash")
        if set(payload) != required:
            raise SnapshotError("invalid_action", "envelope override fields mismatch")
        scope = payload["scope"]
        if scope not in {"next-attempt", "ticket", "role", "product-day", "global-day"}:
            raise SnapshotError("invalid_action", "invalid envelope override scope")
        command = [
            "envelope",
            "override-plan" if action.endswith("plan") else "override-apply",
            "--scope", scope,
        ]
        ticket = payload["ticket"]
        role = payload["role"]
        day = payload["day"]
        if ticket is not None:
            if not isinstance(ticket, str) or not TICKET_RE.fullmatch(ticket):
                raise SnapshotError("invalid_action", "invalid override ticket")
            command.extend(("--ticket", ticket))
        if role is not None:
            if role not in {"planner", "builder", "narrator", "spec-linter", "test-author", "reviewer"}:
                raise SnapshotError("invalid_action", "invalid override role")
            command.extend(("--role", role))
        if day is not None:
            if not isinstance(day, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day):
                raise SnapshotError("invalid_action", "invalid override day")
            command.extend(("--day", day))
        for flag, key in (("--issued-at", "issued_at"), ("--expires-at", "expires_at")):
            value = payload[key]
            if not isinstance(value, str) or not UTC_RE.fullmatch(value):
                raise SnapshotError("invalid_action", "invalid override timestamp")
            command.extend((flag, value))
        command.extend(("--operator-id", _safe_identifier(payload["operator_id"], "override operator")))
        if payload["reason"] not in {"budget_exhausted", "operator_requested"}:
            raise SnapshotError("invalid_action", "invalid override reason")
        command.extend(("--reason", payload["reason"]))
        command.extend(_settings(payload["changes"]))
        if action == "envelope-override-apply":
            command.extend(("--approve-hash", _hash(payload["approve_hash"], "override approval hash")))
        command.append("--json")
        return tuple(command)
    if action in {"attempt-cancel-plan", "attempt-cancel"}:
        expected = {"ticket", "run", "reason"}
        if action == "attempt-cancel":
            expected.add("approve_hash")
        if set(payload) != expected:
            raise SnapshotError("invalid_action", "attempt cancellation fields mismatch")
        ticket = payload["ticket"]
        run_id = payload["run"]
        reason = payload["reason"]
        if not isinstance(ticket, str) or not TICKET_RE.fullmatch(ticket):
            raise SnapshotError("invalid_action", "invalid cancellation ticket")
        if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
            raise SnapshotError("invalid_action", "invalid cancellation run")
        if reason not in {"budget_exhausted", "operator_requested"}:
            raise SnapshotError("invalid_action", "invalid cancellation reason")
        command = [
            "attempt", "cancel-plan" if action.endswith("plan") else "cancel",
            "--ticket", ticket, "--run", run_id, "--reason", reason,
        ]
        if action == "attempt-cancel":
            command.extend(("--approve-hash", _hash(payload["approve_hash"], "cancellation approval hash")))
        command.append("--json")
        return tuple(command)
    raise SnapshotError("unknown_action", "unknown operator action")


class LauncherClient:
    """Invoke one validated launcher with bounded output and fixed arguments."""

    def __init__(self, launcher: Path, timeout_seconds: float = 15):
        self.launcher = validate_launcher(launcher)
        metadata = self.launcher.stat()
        self.launcher_identity = (metadata.st_dev, metadata.st_ino)
        self.timeout_seconds = timeout_seconds

    def _invoke(self, project: str, arguments: tuple[str, ...]) -> dict[str, Any]:
        project = validate_project(project)
        current = validate_launcher(self.launcher).stat()
        if (current.st_dev, current.st_ino) != self.launcher_identity:
            raise SnapshotError(
                "launcher_changed", "configured launcher changed after startup"
            )
        environment = {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        try:
            result = subprocess.run(
                [str(self.launcher), project, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SnapshotError(
                "launcher_unavailable", "configured launcher is unavailable"
            ) from error
        if result.returncode:
            raise SnapshotError(
                "launcher_rejected", "configured launcher rejected the fixed command"
            )
        if len(result.stdout) > MAX_OUTPUT_BYTES:
            raise SnapshotError("invalid_output", "launcher output exceeds the limit")
        try:
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SnapshotError("invalid_output", "launcher returned invalid JSON") from error
        if not isinstance(value, dict):
            raise SnapshotError("invalid_output", "launcher must return a JSON object")
        # Every console-facing response must bind itself to the selected
        # project.  This prevents accidental cross-project cache or adapter use.
        if "project" not in value:
            # The validated launcher invocation itself binds the response to
            # this project. Older fixed helpers do not echo that selector.
            value["project"] = project
        elif value.get("project") != project:
            raise SnapshotError(
                "project_mismatch", "launcher response does not match selected project"
            )
        return value

    def snapshot(self, project: str, view: str) -> dict[str, Any]:
        arguments = SNAPSHOT_COMMANDS.get(view)
        if arguments is None:
            raise SnapshotError("unknown_view", "unknown snapshot view")
        return self._invoke(project, arguments)

    def mutate(
        self, project: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._invoke(project, mutation_command(action, payload))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launcher",
        type=Path,
        default=Path.home() / ".factory" / "bin" / "factory-launch",
    )
    parser.add_argument("project")
    parser.add_argument("view", choices=tuple(SNAPSHOT_COMMANDS))
    args = parser.parse_args()
    try:
        value = LauncherClient(args.launcher).snapshot(args.project, args.view)
    except SnapshotError as error:
        print(f"operator-snapshot: {error}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
