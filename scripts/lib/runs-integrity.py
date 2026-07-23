#!/usr/bin/env python3
"""Snapshot and restore launcher-owned run manifests around an agent process."""

import base64
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys


SNAPSHOT_SCHEMA = 1
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


def snapshot(directory):
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
            not isinstance(expected.get("manifests"), dict)):
        raise ValueError("invalid runs snapshot")


def parse_manifest(content, name):
    values = {}
    for raw in content.decode("utf-8").splitlines():
        if "=" not in raw:
            raise ValueError(f"invalid run manifest row: {name}")
        key, value = raw.split("=", 1)
        if not key or key in values:
            raise ValueError(f"duplicate run manifest field: {name}")
        values[key] = value
    return values


def concurrent_check(directory, active_directory, coordinator, database, expected):
    validate_snapshot(expected)
    runs_descriptor, runs_stat = open_real_directory(directory, "runs root")
    try:
        if not same_identity(runs_stat, expected["directory"]):
            raise ValueError("runs root identity changed")
        own_name = expected.get("own_manifest")
        if (not isinstance(own_name, str) or "/" in own_name or
                own_name not in expected["manifests"] or
                read_manifest(runs_descriptor, own_name) != expected["manifests"][own_name]):
            raise ValueError("owned run manifest changed")
        live = {}
        for name in manifest_names(runs_descriptor):
            content = base64.b64decode(read_manifest(runs_descriptor, name), validate=True)
            values = parse_manifest(content, name)
            if (values.get("accounting_state") == "reserved" and
                    values.get("provider_execution_mode") == "cli-concurrent-v1"):
                key = (values.get("ticket"), values.get("role"))
                if not all(key) or key in live:
                    raise ValueError("concurrent run identity is ambiguous")
                live[key] = values
    finally:
        os.close(runs_descriptor)

    claims_descriptor, _ = open_real_directory(active_directory, "active runs root")
    try:
        claims = sorted(os.listdir(claims_descriptor))
        for name in claims:
            info = os.stat(name, dir_fd=claims_descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("active run claim is unsafe")
            claim_descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=claims_descriptor)
            try:
                if sorted(os.listdir(claim_descriptor)) != ["owner"]:
                    raise ValueError("active run claim has unexpected entries")
                owner = base64.b64decode(read_manifest(claim_descriptor, "owner"), validate=True)
                fields = parse_manifest(owner, name)
                if set(fields) != {"pid", "process_start", "token"}:
                    raise ValueError("active run claim owner is invalid")
                if (not fields["pid"].isdigit() or not fields["process_start"] or
                        len(fields["token"]) != 32 or
                        any(character not in "0123456789abcdef" for character in fields["token"])):
                    raise ValueError("active run claim owner is incomplete")
                pid = int(fields["pid"])
                observed = subprocess.run(
                    ["ps", "-o", "lstart=", "-p", str(pid)], text=True,
                    capture_output=True, check=False, timeout=10,
                ).stdout.strip()
                observed = " ".join(observed.split())
                if observed != fields["process_start"]:
                    raise ValueError("active run claim owner is not live")
            finally:
                os.close(claim_descriptor)
    finally:
        os.close(claims_descriptor)

    result = subprocess.run(
        [sys.executable, str(coordinator), "--db", str(database), "status"],
        text=True, capture_output=True, check=False, timeout=60,
    )
    if result.returncode:
        raise ValueError("provider coordinator status failed")
    status = json.loads(result.stdout)
    attempts = {item["attempt_id"]: item for item in status.get("attempts", [])}
    expected_claims = set()
    for (ticket, role), values in live.items():
        attempt = attempts.get(values.get("provider_attempt_id"))
        if (not attempt or attempt.get("state") not in {"reserved", "GO", "submitted"} or
                attempt.get("ticket_id") != ticket or
                attempt.get("provider_family") != values.get("provider_family") or
                attempt.get("account_route") != values.get("account_route_id") or
                attempt.get("policy_sha256") != values.get("activation_policy_sha256")):
            raise ValueError("concurrent run lacks an authorized provider attempt")
        expected_claims.add(".".join((ticket, role)) + ".lock")
    if set(claims) != expected_claims:
        raise ValueError("active claims do not match authorized concurrent runs")
    return True


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
            restore_manifests(descriptor, expected["manifests"])
            return False
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in {"snapshot", "snapshot-one", "check", "check-concurrent"}:
        raise SystemExit("usage: runs-integrity.py {snapshot|snapshot-one|check|check-concurrent} PATH ...")
    directory = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        print(json.dumps(snapshot(directory), sort_keys=True, separators=(",", ":")))
        return
    if sys.argv[1] == "snapshot-one":
        if len(sys.argv) != 4:
            raise SystemExit("snapshot-one requires RUNS_DIR MANIFEST_NAME")
        value = snapshot(directory)
        name = sys.argv[3]
        value["manifests"] = {name: value["manifests"][name]}
        value["own_manifest"] = name
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return
    expected = json.load(sys.stdin)
    if sys.argv[1] == "check-concurrent":
        if len(sys.argv) != 6:
            raise SystemExit("check-concurrent requires RUNS_DIR ACTIVE_RUNS COORDINATOR DB")
        concurrent_check(directory, Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]), expected)
        return
    if len(sys.argv) != 3:
        raise SystemExit("check requires RUNS_DIR")
    if not check(directory, expected):
        print("role_exit_control_plane_mutation: run manifests changed during provider execution", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"runs-integrity: {error}", file=sys.stderr)
        raise SystemExit(1)
