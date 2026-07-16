#!/usr/bin/env python3
"""Snapshot and restore launcher-owned run manifests around an agent process."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys


SNAPSHOT_SCHEMA = 2
DIRECTORY_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                   getattr(os, "O_NOFOLLOW", 0))
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def identity(value):
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "type": stat.S_IFMT(value.st_mode),
    }


def same_identity(value, expected):
    return identity(value) == expected


def real_directory(path, label):
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError(f"{label} must be a real directory")
    return value


def open_real_directory(path, label):
    before = real_directory(path, label)
    descriptor = os.open(path, DIRECTORY_FLAGS)
    after = os.fstat(descriptor)
    if not stat.S_ISDIR(after.st_mode) or identity(before) != identity(after):
        os.close(descriptor)
        raise ValueError(f"{label} changed while opening")
    return descriptor, after


def manifest_names(descriptor):
    return sorted(name for name in os.listdir(descriptor) if name.endswith(".meta"))


def read_manifest(descriptor, name):
    before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"nonregular or multi-link run manifest: {name}")
    file_descriptor = os.open(name, FILE_FLAGS, dir_fd=descriptor)
    try:
        after = os.fstat(file_descriptor)
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or
                identity(before) != identity(after)):
            raise ValueError(f"run manifest changed while opening: {name}")
        with os.fdopen(file_descriptor, "rb") as handle:
            file_descriptor = -1
            return base64.b64encode(handle.read()).decode("ascii")
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def manifests(descriptor):
    return {name: read_manifest(descriptor, name) for name in manifest_names(descriptor)}


def snapshot(directory, owned=None):
    directory = Path(os.path.abspath(directory))
    parent_descriptor, parent_stat = open_real_directory(directory.parent, "runs parent")
    try:
        try:
            directory_stat = os.stat(
                directory.name, dir_fd=parent_descriptor, follow_symlinks=False,
            )
        except FileNotFoundError:
            raise ValueError("runs root is missing")
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("runs root must be a real directory")
        descriptor = os.open(directory.name, DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            if identity(directory_stat) != identity(opened):
                raise ValueError("runs root changed while opening")
            return {
                "schema": SNAPSHOT_SCHEMA,
                "parent": identity(parent_stat),
                "directory": identity(opened),
                "manifests": manifests(descriptor),
                "owned": owned,
            }
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def durable_write(descriptor, name, content):
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
    file_descriptor = -1
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def quarantine(descriptor, name):
    target = f"{name}.rejected-role-mutation-{secrets.token_hex(6)}"
    os.replace(name, target, src_dir_fd=descriptor, dst_dir_fd=descriptor)


def entry_stat(descriptor, name):
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def find_original_directory(parent_descriptor, expected, runs_name):
    found = []
    for name in os.listdir(parent_descriptor):
        if name == runs_name:
            continue
        try:
            value = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(value.st_mode) and same_identity(value, expected):
            found.append(name)
    if len(found) > 1:
        raise ValueError("runs root identity appears more than once")
    return found[0] if found else None


def recover_directory(parent_descriptor, runs_name, expected):
    current = entry_stat(parent_descriptor, runs_name)
    if current is not None:
        quarantine(parent_descriptor, runs_name)
    original = find_original_directory(parent_descriptor, expected, runs_name)
    if original:
        os.replace(
            original, runs_name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
        )
    else:
        os.mkdir(runs_name, 0o700, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def restore_manifests(descriptor, expected):
    for name in manifest_names(descriptor):
        quarantine(descriptor, name)
    for name, encoded in expected.items():
        if not name.endswith(".meta") or "/" in name or name in {".", ".."}:
            raise ValueError(f"invalid manifest name in snapshot: {name}")
        durable_write(descriptor, name, base64.b64decode(encoded, validate=True))
    os.fsync(descriptor)


def validate_snapshot(expected):
    if (not isinstance(expected, dict) or expected.get("schema") != SNAPSHOT_SCHEMA or
            not isinstance(expected.get("parent"), dict) or
            not isinstance(expected.get("directory"), dict) or
            not isinstance(expected.get("manifests"), dict) or
            not isinstance(expected.get("owned"), (str, type(None)))):
        raise ValueError("invalid runs snapshot")


def valid_sibling_transition(name, before, after):
    # ponytail: semantic validation preserves inherited overlap; use a
    # privileged manifest writer if hostile same-UID isolation is required.
    helper = Path(__file__).resolve().parents[1] / "ledger-view.py"
    spec = importlib.util.spec_from_file_location("ledger_view", helper)
    ledger_view = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger_view)

    def values(encoded, accounting=True):
        content = base64.b64decode(encoded, validate=True).decode("utf-8")
        parsed = ledger_view.parse_meta(content, Path(name))
        if accounting and ledger_view.manifest_row(Path(name), parsed) is None:
            raise ValueError(f"invalid accounting manifest: {name}")
        return parsed

    current = values(after)
    if before is None:
        state = current.get("accounting_state")
        phase = current.get("phase")
        return ((state == "reserved" and phase == "spawned") or
                (state in ledger_view.TERMINAL_STATES and
                 phase in {state, "completed", "abandoned"}))

    original = values(before, accounting=False)
    if original == current:
        return True
    mutable = {
        "phase", "accounting_schema", "accounting_state", "terminal_at", "turns", "effective_cost",
        "exit_status", "cost_basis", "role_exit", "updated_at",
    }
    if any(original.get(key) != current.get(key)
           for key in set(original) | set(current) if key not in mutable):
        return False
    if original.get("accounting_schema") != "1":
        return (
            original.get("phase") == "resolved" and
            current.get("accounting_schema") == "1" and
            current.get("phase") in {"launch_void", "abandoned"} and
            current.get("accounting_state") == "launch_void"
        )
    return (
        original.get("phase") == "spawned" and
        original.get("accounting_state") == "reserved" and
        current.get("accounting_state") in ledger_view.TERMINAL_STATES and
        current.get("phase") in {"completed", "abandoned"}
    )


def check(directory, expected):
    validate_snapshot(expected)
    directory = Path(os.path.abspath(directory))
    parent_descriptor, parent_stat = open_real_directory(directory.parent, "runs parent")
    try:
        if not same_identity(parent_stat, expected["parent"]):
            raise ValueError("runs parent identity changed; refusing recovery")

        current = entry_stat(parent_descriptor, directory.name)
        identity_matches = (
            current is not None and stat.S_ISDIR(current.st_mode) and
            same_identity(current, expected["directory"])
        )
        if not identity_matches:
            recover_directory(parent_descriptor, directory.name, expected["directory"])

        descriptor = os.open(directory.name, DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            try:
                actual = manifests(descriptor)
            except (OSError, ValueError):
                actual = None
            if identity_matches and actual == expected["manifests"]:
                return True
            owned = expected["owned"]
            if identity_matches and actual is not None and owned:
                missing = set(expected["manifests"]) - set(actual)
                owned_changed = actual.get(owned) != expected["manifests"].get(owned)
                invalid = [
                    name for name, content in actual.items()
                    if name != owned and not valid_sibling_transition(
                        name, expected["manifests"].get(name), content,
                    )
                ]
                if not missing and not owned_changed and not invalid:
                    return True
                if not missing and not owned_changed:
                    for name in invalid:
                        quarantine(descriptor, name)
                    os.fsync(descriptor)
                    return False
            restore_manifests(descriptor, expected["manifests"])
            return False
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def main():
    if len(sys.argv) not in {3, 4} or sys.argv[1] not in {"snapshot", "check"}:
        raise SystemExit("usage: runs-integrity.py {snapshot|check} RUNS_DIR [OWNED_MANIFEST]")
    directory = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        owned = sys.argv[3] if len(sys.argv) == 4 else None
        print(json.dumps(snapshot(directory, owned), sort_keys=True, separators=(",", ":")))
        return
    expected = json.load(sys.stdin)
    if not check(directory, expected):
        print("role_exit_control_plane_mutation: run manifests changed during provider execution", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"runs-integrity: {error}", file=sys.stderr)
        raise SystemExit(1)
