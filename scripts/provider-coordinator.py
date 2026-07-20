#!/usr/bin/env python3
"""Owner-local, fail-closed provider admission coordinator."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


SCHEMA = "factory-provider-state/v2"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
OUTPUT_SCHEMA = "factory-provider-coordinator/v1"
APPLICATION_ID = 0x4E595343
ACTIVE_STATES = ("reserved", "GO", "submitted")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
MAX_MONEY = 10**15
MAX_WINDOW = 7 * 24 * 60 * 60
MAX_JSON = 1_000_000


class CoordinatorError(Exception):
    pass


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_id(value, label, operation=False):
    pattern = OPERATION_ID if operation else SAFE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CoordinatorError(f"{label} is invalid")
    return value


def validate_money(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinatorError("reserve_micro_usd must be an integer")
    if value < 0 or value > MAX_MONEY:
        raise CoordinatorError("reserve_micro_usd is out of range")
    return value


def secure_directory(path):
    if not path.is_absolute():
        raise CoordinatorError("database path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CoordinatorError(f"directory is missing: {path}") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise CoordinatorError("database directory is unsafe")
    return path


def secure_regular(path, label, maximum=None, owner_only=False):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CoordinatorError(f"{label} is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (owner_only and info.st_mode & 0o077)
        or (maximum is not None and info.st_size > maximum)
    ):
        raise CoordinatorError(f"{label} is unsafe")
    return info


def secure_read_json(path, label):
    if not path.is_absolute():
        raise CoordinatorError(f"{label} path must be absolute")
    expected = secure_regular(path, label, maximum=MAX_JSON)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise CoordinatorError(f"{label} changed while opening")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def bounded_integer(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise CoordinatorError(f"{label} must be an integer from 1 through {maximum}")
    return value


def validate_limit(value, location):
    if not isinstance(value, dict) or set(value) != {
        "max_concurrent", "max_starts", "window_seconds"
    }:
        raise CoordinatorError(f"{location} must define exactly max_concurrent, max_starts, and window_seconds")
    return {
        "max_concurrent": bounded_integer(value["max_concurrent"], f"{location}.max_concurrent", 6),
        "max_starts": bounded_integer(value["max_starts"], f"{location}.max_starts", 1_000_000),
        "window_seconds": bounded_integer(value["window_seconds"], f"{location}.window_seconds", MAX_WINDOW),
    }


def validate_scope_map(value, location):
    if not isinstance(value, dict):
        raise CoordinatorError(f"{location} must be an object")
    result = {}
    for name, limits in value.items():
        validate_id(name, f"{location} key")
        result[name] = validate_limit(limits, f"{location}.{name}")
    return result


def load_policy(path):
    value = secure_read_json(path, "policy")
    required = {
        "schema", "coupled_max_concurrent", "global",
        "provider_families", "account_routes",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CoordinatorError("policy has unsupported or missing fields")
    if value["schema"] != POLICY_SCHEMA:
        raise CoordinatorError("policy schema is unsupported")
    policy = {
        "schema": POLICY_SCHEMA,
        "coupled_max_concurrent": bounded_integer(
            value["coupled_max_concurrent"], "coupled_max_concurrent", 6
        ),
        "global": validate_limit(value["global"], "global"),
        "provider_families": validate_scope_map(
            value["provider_families"], "provider_families"
        ),
        "account_routes": validate_scope_map(
            value["account_routes"], "account_routes"
        ),
    }
    return policy, digest(policy)


def create_database(path):
    secure_directory(path.parent)
    if path.exists() or path.is_symlink():
        secure_regular(path, "database", owner_only=True)
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    secure_regular(path, "database", owner_only=True)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  provider_family TEXT NOT NULL,
  account_route TEXT NOT NULL,
  reserve_micro_usd INTEGER NOT NULL
    CHECK(reserve_micro_usd >= 0 AND reserve_micro_usd <= 1000000000000000),
  state TEXT NOT NULL
    CHECK(state IN ('prepared','reserved','GO','submitted','terminal')),
  version INTEGER NOT NULL CHECK(version >= 1),
  prepared_at INTEGER NOT NULL,
  admitted_at INTEGER,
  go_at INTEGER,
  submitted_at INTEGER,
  terminal_at INTEGER,
  terminal_result TEXT,
  policy_sha256 TEXT,
  updated_at INTEGER NOT NULL,
  CHECK(state != 'prepared' OR admitted_at IS NULL),
  CHECK(state IN ('prepared','terminal') OR admitted_at IS NOT NULL),
  CHECK((state != 'terminal') = (terminal_at IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS attempts_active
  ON attempts(state, provider_family, account_route);
CREATE INDEX IF NOT EXISTS attempts_starts
  ON attempts(admitted_at, provider_family, account_route);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
) STRICT;
"""


def initialize(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if application_id not in (0, APPLICATION_ID) or user_version not in (0, 2):
            raise CoordinatorError("database identity or version is unsupported")
        if objects and (application_id != APPLICATION_ID or user_version != 2):
            raise CoordinatorError("non-empty database is not provider state-v2")
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema',?)", (SCHEMA,)
        )
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if stored is None or stored[0] != SCHEMA:
            raise CoordinatorError("database schema marker is invalid")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


@contextmanager
def database(path):
    create_database(path)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA trusted_schema=OFF")
        initialize(connection)
        secure_regular(path, "database", owner_only=True)
        yield connection
    finally:
        connection.close()
        secure_regular(path, "database", owner_only=True)


def row_result(row):
    result = dict(row)
    result["schema"] = OUTPUT_SCHEMA
    return result


def attempt(connection, attempt_id):
    row = connection.execute(
        """SELECT attempt_id,provider_family,account_route,reserve_micro_usd,
                  state,version,prepared_at,admitted_at,go_at,submitted_at,
                  terminal_at,terminal_result,policy_sha256,updated_at
           FROM attempts WHERE attempt_id=?""",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise CoordinatorError("attempt does not exist")
    return row


def mutate(connection, operation_id, command, request, function):
    validate_id(operation_id, "operation_id", operation=True)
    request_sha256 = digest(request)
    connection.execute("BEGIN IMMEDIATE")
    try:
        prior = connection.execute(
            "SELECT command,request_sha256,result_json FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if prior is not None:
            if prior["command"] != command or prior["request_sha256"] != request_sha256:
                raise CoordinatorError("operation_id was already used for a different request")
            result = json.loads(prior["result_json"])
            connection.commit()
            return result
        result = function()
        result_json = canonical(result)
        connection.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?)",
            (operation_id, command, request_sha256, result_json, int(time.time())),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def prepare_mutation(connection, values, now):
    existing = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id=?", (values["attempt_id"],)
    ).fetchone()
    if existing is not None:
        expected = (
            existing["provider_family"], existing["account_route"],
            existing["reserve_micro_usd"],
        )
        actual = (
            values["provider_family"], values["account_route"],
            values["reserve_micro_usd"],
        )
        if expected != actual:
            raise CoordinatorError("attempt_id conflicts with an existing attempt")
        return row_result(attempt(connection, values["attempt_id"]))
    connection.execute(
        """INSERT INTO attempts(
             attempt_id,provider_family,account_route,reserve_micro_usd,state,
             version,prepared_at,updated_at)
           VALUES(?,?,?,?,'prepared',1,?,?)""",
        (
            values["attempt_id"], values["provider_family"], values["account_route"],
            values["reserve_micro_usd"], now, now,
        ),
    )
    return row_result(attempt(connection, values["attempt_id"]))


def limit_checks(connection, policy, provider_family, account_route, now):
    scopes = [
        ("coupled", None, None, {"max_concurrent": policy["coupled_max_concurrent"]}),
        ("global", None, None, policy["global"]),
    ]
    if provider_family not in policy["provider_families"]:
        raise CoordinatorError("provider family has no concurrency policy")
    if account_route not in policy["account_routes"]:
        raise CoordinatorError("account route has no concurrency policy")
    scopes.extend([
        ("provider_family", "provider_family", provider_family,
         policy["provider_families"][provider_family]),
        ("account_route", "account_route", account_route,
         policy["account_routes"][account_route]),
    ])
    denials = []
    for name, column, selected, limits in scopes:
        where = "state IN ('reserved','GO','submitted')"
        parameters = []
        if column:
            where += f" AND {column}=?"
            parameters.append(selected)
        active = connection.execute(
            f"SELECT count(*) FROM attempts WHERE {where}", parameters
        ).fetchone()[0]
        if active >= limits["max_concurrent"]:
            denials.append({"limit": "max_concurrent", "scope": name})
        if "max_starts" in limits:
            start_where = "admitted_at IS NOT NULL AND admitted_at>?"
            start_parameters = [now - limits["window_seconds"]]
            if column:
                start_where += f" AND {column}=?"
                start_parameters.append(selected)
            starts = connection.execute(
                f"SELECT count(*) FROM attempts WHERE {start_where}", start_parameters
            ).fetchone()[0]
            if starts >= limits["max_starts"]:
                denials.append({"limit": "max_starts", "scope": name})
    return denials


def admit_mutation(connection, attempt_id, expected_version, policy, policy_hash, now):
    row = attempt(connection, attempt_id)
    if row["state"] != "prepared":
        raise CoordinatorError("only a prepared attempt may be admitted")
    if row["version"] != expected_version:
        raise CoordinatorError("attempt version compare-and-swap failed")
    denials = limit_checks(
        connection, policy, row["provider_family"], row["account_route"], now
    )
    if denials:
        return {
            "admitted": False,
            "attempt": row_result(row),
            "denials": denials,
            "schema": OUTPUT_SCHEMA,
        }
    changed = connection.execute(
        """UPDATE attempts
           SET state='reserved',version=version+1,admitted_at=?,
               policy_sha256=?,updated_at=?
           WHERE attempt_id=? AND state='prepared' AND version=?""",
        (now, policy_hash, now, attempt_id, expected_version),
    ).rowcount
    if changed != 1:
        raise CoordinatorError("attempt changed during admission")
    return {
        "admitted": True,
        "attempt": row_result(attempt(connection, attempt_id)),
        "denials": [],
        "schema": OUTPUT_SCHEMA,
    }


def transition_mutation(connection, attempt_id, expected_version, target, now, result=None):
    row = attempt(connection, attempt_id)
    allowed = {
        "GO": ("reserved",),
        "submitted": ("GO",),
        "terminal": ("prepared", "reserved", "GO", "submitted"),
    }[target]
    if row["version"] != expected_version:
        raise CoordinatorError("attempt version compare-and-swap failed")
    if row["state"] not in allowed:
        raise CoordinatorError(f"attempt cannot transition from {row['state']} to {target}")
    timestamps = {
        "GO": ("go_at",),
        "submitted": ("submitted_at",),
        "terminal": ("terminal_at", "terminal_result"),
    }
    columns = timestamps[target]
    assignments = ["state=?", "version=version+1", "updated_at=?"]
    parameters = [target, now]
    for column in columns:
        assignments.append(f"{column}=?")
        parameters.append(now if column.endswith("_at") else result)
    parameters.extend((attempt_id, expected_version))
    changed = connection.execute(
        f"UPDATE attempts SET {','.join(assignments)} WHERE attempt_id=? AND version=?",
        parameters,
    ).rowcount
    if changed != 1:
        raise CoordinatorError("attempt changed during transition")
    return row_result(attempt(connection, attempt_id))


def common_attempt_values(args):
    return {
        "attempt_id": validate_id(args.attempt_id, "attempt_id"),
        "provider_family": validate_id(args.provider_family, "provider_family"),
        "account_route": validate_id(args.account_route, "account_route"),
        "reserve_micro_usd": validate_money(args.reserve_micro_usd),
    }


def now_value(args):
    if args.now is None:
        return int(time.time())
    if args.now < 0:
        raise CoordinatorError("--now must be a non-negative Unix timestamp")
    return args.now


def prepare_command(connection, args):
    values = common_attempt_values(args)
    now = now_value(args)
    request = dict(values, now=args.now)
    return mutate(
        connection, args.operation_id, "prepare", request,
        lambda: prepare_mutation(connection, values, now),
    )


def admit_command(connection, args):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    policy, policy_hash = load_policy(Path(args.policy))
    now = now_value(args)
    request = {
        "attempt_id": attempt_id, "expected_version": args.expected_version,
        "now": args.now, "policy_sha256": policy_hash,
    }
    return mutate(
        connection, args.operation_id, "admit", request,
        lambda: admit_mutation(
            connection, attempt_id, args.expected_version, policy, policy_hash, now
        ),
    )


def reserve_command(connection, args):
    values = common_attempt_values(args)
    policy, policy_hash = load_policy(Path(args.policy))
    now = now_value(args)
    request = dict(values, now=args.now, policy_sha256=policy_hash)

    def reserve():
        prepared = prepare_mutation(connection, values, now)
        if prepared["state"] != "prepared" or prepared["version"] != 1:
            raise CoordinatorError("reserve replay requires the original prepared attempt")
        return admit_mutation(
            connection, values["attempt_id"], 1, policy, policy_hash, now
        )

    return mutate(connection, args.operation_id, "reserve", request, reserve)


def transition_command(connection, args, target):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    now = now_value(args)
    result = getattr(args, "result", None)
    if result is not None:
        validate_id(result, "terminal result")
    request = {
        "attempt_id": attempt_id, "expected_version": args.expected_version,
        "now": args.now, "target": target, "result": result,
    }
    return mutate(
        connection, args.operation_id, target.lower(), request,
        lambda: transition_mutation(
            connection, attempt_id, args.expected_version, target, now, result
        ),
    )


def status_command(connection, args):
    if args.attempt_id:
        rows = [attempt(connection, validate_id(args.attempt_id, "attempt_id"))]
    else:
        rows = connection.execute(
            """SELECT attempt_id,provider_family,account_route,reserve_micro_usd,
                      state,version,prepared_at,admitted_at,go_at,submitted_at,
                      terminal_at,terminal_result,policy_sha256,updated_at
               FROM attempts ORDER BY attempt_id"""
        ).fetchall()
    counts = dict(
        connection.execute(
            "SELECT state,count(*) FROM attempts GROUP BY state ORDER BY state"
        ).fetchall()
    )
    return {
        "active_reserve_micro_usd": connection.execute(
            "SELECT coalesce(sum(reserve_micro_usd),0) FROM attempts "
            "WHERE state IN ('reserved','GO','submitted')"
        ).fetchone()[0],
        "attempts": [dict(row) for row in rows],
        "counts": counts,
        "schema": OUTPUT_SCHEMA,
    }


def reconcile_command(connection, args):
    observations = secure_read_json(Path(args.input), "reconciliation input")
    if (
        not isinstance(observations, dict)
        or set(observations) != {"schema", "observations"}
        or observations["schema"] != "factory-provider-observations/v1"
        or not isinstance(observations["observations"], list)
    ):
        raise CoordinatorError("reconciliation input schema is invalid")
    normalized = []
    seen = set()
    for item in observations["observations"]:
        if not isinstance(item, dict) or set(item) != {
            "attempt_id", "expected_version", "outcome"
        }:
            raise CoordinatorError("reconciliation observation is invalid")
        attempt_id = validate_id(item["attempt_id"], "attempt_id")
        if attempt_id in seen:
            raise CoordinatorError("reconciliation repeats an attempt")
        seen.add(attempt_id)
        outcome = item["outcome"]
        if outcome not in {"succeeded", "terminal", "cancelled", "failed", "unknown"}:
            raise CoordinatorError("reconciliation outcome is invalid")
        if isinstance(item["expected_version"], bool) or not isinstance(item["expected_version"], int):
            raise CoordinatorError("reconciliation expected_version is invalid")
        normalized.append(item)
    now = now_value(args)
    request = {"input_sha256": digest(observations), "now": args.now}

    def reconcile():
        results = []
        for item in normalized:
            row = attempt(connection, item["attempt_id"])
            if item["outcome"] == "unknown" or (
                item["outcome"] == "failed"
                and row["state"] in {"GO", "submitted"}
            ):
                results.append({
                    "attempt_id": item["attempt_id"],
                    "action": "retained",
                    "state": row["state"],
                })
                continue
            terminal_result = (
                "failed_pre_go"
                if item["outcome"] == "failed"
                else item["outcome"]
            )
            transitioned = transition_mutation(
                connection, item["attempt_id"], item["expected_version"],
                "terminal", now, terminal_result,
            )
            results.append({
                "attempt_id": item["attempt_id"],
                "action": "terminalized",
                "state": transitioned["state"],
            })
        return {"results": results, "schema": OUTPUT_SCHEMA}

    return mutate(connection, args.operation_id, "reconcile", request, reconcile)


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True)
    commands = result.add_subparsers(dest="command", required=True)

    def mutation(name):
        command = commands.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--now", type=int)
        return command

    prepare = mutation("prepare")
    reserve = mutation("reserve")
    for command in (prepare, reserve):
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--provider-family", required=True)
        command.add_argument("--account-route", required=True)
        command.add_argument("--reserve-micro-usd", required=True, type=int)
    prepare.set_defaults(handler=prepare_command)
    reserve.add_argument("--policy", required=True)
    reserve.set_defaults(handler=reserve_command)

    admit = mutation("admit")
    admit.add_argument("--attempt-id", required=True)
    admit.add_argument("--expected-version", required=True, type=int)
    admit.add_argument("--policy", required=True)
    admit.set_defaults(handler=admit_command)

    for name, target in (("mark-go", "GO"), ("mark-submitted", "submitted")):
        command = mutation(name)
        command.add_argument("--attempt-id", required=True)
        command.add_argument("--expected-version", required=True, type=int)
        command.set_defaults(
            handler=lambda connection, args, selected=target:
            transition_command(connection, args, selected)
        )

    terminal = mutation("terminalize")
    terminal.add_argument("--attempt-id", required=True)
    terminal.add_argument("--expected-version", required=True, type=int)
    terminal.add_argument("--result", required=True)
    terminal.set_defaults(
        handler=lambda connection, args:
        transition_command(connection, args, "terminal")
    )

    status = commands.add_parser("status")
    status.add_argument("--attempt-id")
    status.set_defaults(handler=status_command)

    reconcile = mutation("reconcile")
    reconcile.add_argument("--input", required=True)
    reconcile.set_defaults(handler=reconcile_command)
    return result


def main():
    try:
        args = parser().parse_args()
        path = Path(args.db)
        with database(path) as connection:
            output = args.handler(connection, args)
        print(canonical(output))
    except (
        CoordinatorError, OSError, sqlite3.Error, UnicodeError,
        json.JSONDecodeError, ValueError,
    ) as exc:
        print(canonical({"error": str(exc), "schema": OUTPUT_SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
