#!/usr/bin/env python3
"""Owner-local, fail-closed provider admission coordinator."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time


SCHEMA = "factory-provider-state/v2"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
OUTPUT_SCHEMA = "factory-provider-coordinator/v1"
ACCOUNT_SCHEMA = "factory-cursor-account-admission/v1"
ACCOUNT_OUTPUT_SCHEMA = "factory-cursor-account-admission/v1"
ACCOUNT_RECOVERY_SCHEMA = "factory-cursor-account-recovery/v1"
APPLICATION_ID = 0x4E595343
ACCOUNT_APPLICATION_ID = 0x4E594341
ACTIVE_STATES = ("reserved", "GO", "submitted")
TERMINAL_RESULTS = frozenset(
    ("succeeded", "cancelled", "failed_pre_go", "failed", "capacity_denied")
)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
MAX_MONEY = 10**15
MAX_WINDOW = 7 * 24 * 60 * 60
MAX_JSON = 1_000_000
MAX_WAIT_SECONDS = 15 * 60
NON_PRODUCTION_GRACE_MILLISECONDS = 100
ACCOUNT_COMMANDS = frozenset(
    (
        "account-acquire", "account-bind-runtime", "account-validate",
        "account-release", "account-status",
    )
)
ACCOUNT_RECOVERY_COMMANDS = frozenset(
    ("account-recover-preview", "account-recover-apply")
)


class CoordinatorError(Exception):
    pass


@contextmanager
def configuration_guard(path_value):
    if path_value is None:
        yield
        return
    path = Path(path_value)
    if not path.is_absolute():
        raise CoordinatorError("provider configuration lock path must be absolute")
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise CoordinatorError("provider configuration lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


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


def validate_day(value):
    if not isinstance(value, str):
        raise CoordinatorError("budget_day is invalid")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise CoordinatorError("budget_day is invalid") from exc
    if parsed.isoformat() != value:
        raise CoordinatorError("budget_day is invalid")
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


def secure_owner_directory(path, label):
    if not path.is_absolute():
        raise CoordinatorError(f"{label} path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CoordinatorError(f"{label} is missing") from exc
    if (
        resolved != path
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise CoordinatorError(f"{label} is unsafe")
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
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        secure_regular(path, "database", owner_only=True)
        return
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
CREATE TABLE IF NOT EXISTS attempt_budgets (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  product_id TEXT NOT NULL,
  ticket_id TEXT NOT NULL,
  budget_day TEXT NOT NULL,
  product_daily_cap_micro_usd INTEGER NOT NULL,
  ticket_cap_micro_usd INTEGER NOT NULL,
  machine_daily_cap_micro_usd INTEGER NOT NULL,
  charge_micro_usd INTEGER,
  CHECK(length(budget_day) = 10),
  CHECK(product_daily_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(ticket_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(machine_daily_cap_micro_usd BETWEEN 0 AND 1000000000000000),
  CHECK(charge_micro_usd IS NULL OR charge_micro_usd BETWEEN 0 AND 1000000000000000)
) STRICT;
CREATE INDEX IF NOT EXISTS attempt_budgets_scope
  ON attempt_budgets(budget_day, product_id, ticket_id);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS legacy_intervals (
  interval_id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL,
  started_at INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS cancellation_requests (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  requested_at INTEGER NOT NULL,
  reason TEXT NOT NULL
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


ACCOUNT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS account_leases (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  lease_id TEXT NOT NULL UNIQUE,
  account_route TEXT NOT NULL,
  trust_scope TEXT NOT NULL
    CHECK(trust_scope IN (
      'production-certified','qualification-candidate','development-local'
    )),
  owner_pid INTEGER NOT NULL CHECK(owner_pid > 0),
  owner_pgid INTEGER NOT NULL CHECK(owner_pgid > 0),
  owner_start TEXT NOT NULL,
  runtime_pid INTEGER,
  runtime_pgid INTEGER,
  runtime_start TEXT,
  state TEXT NOT NULL CHECK(state IN ('waiting','active')),
  policy_sha256 TEXT NOT NULL,
  max_concurrent INTEGER NOT NULL CHECK(max_concurrent BETWEEN 1 AND 6),
  max_starts INTEGER NOT NULL CHECK(max_starts BETWEEN 1 AND 1000000),
  window_seconds INTEGER NOT NULL CHECK(window_seconds BETWEEN 1 AND 604800),
  requested_at_ms INTEGER NOT NULL,
  admitted_at INTEGER,
  started_at INTEGER,
  CHECK((runtime_pid IS NULL) = (runtime_pgid IS NULL)),
  CHECK((runtime_pid IS NULL) = (runtime_start IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS account_leases_route
  ON account_leases(account_route,state,trust_scope,sequence);
CREATE TABLE IF NOT EXISTS account_starts (
  lease_id TEXT PRIMARY KEY,
  account_route TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  policy_sha256 TEXT NOT NULL,
  max_concurrent INTEGER NOT NULL CHECK(max_concurrent BETWEEN 1 AND 6),
  max_starts INTEGER NOT NULL CHECK(max_starts BETWEEN 1 AND 1000000),
  window_seconds INTEGER NOT NULL CHECK(window_seconds BETWEEN 1 AND 604800)
) STRICT;
CREATE INDEX IF NOT EXISTS account_starts_route
  ON account_starts(account_route,started_at);
"""


def create_account_database(path):
    secure_owner_directory(path.parent, "account admission database directory")
    if path.exists() or path.is_symlink():
        secure_regular(path, "account admission database", owner_only=True)
        return
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        secure_regular(path, "account admission database", owner_only=True)
        return
    os.close(descriptor)
    secure_regular(path, "account admission database", owner_only=True)


def initialize_account_database(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if application_id not in (0, ACCOUNT_APPLICATION_ID) or user_version not in (0, 1):
            raise CoordinatorError("account admission database identity is unsupported")
        if objects and (application_id != ACCOUNT_APPLICATION_ID or user_version != 1):
            raise CoordinatorError("non-empty database is not account admission state-v1")
        for statement in ACCOUNT_SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('schema',?)",
            (ACCOUNT_SCHEMA,),
        )
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if stored is None or stored[0] != ACCOUNT_SCHEMA:
            raise CoordinatorError("account admission schema marker is invalid")
        connection.execute(f"PRAGMA application_id={ACCOUNT_APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


@contextmanager
def account_database(path):
    create_account_database(path)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA trusted_schema=OFF")
        initialize_account_database(connection)
        secure_regular(path, "account admission database", owner_only=True)
        yield connection
    finally:
        connection.close()
        secure_regular(path, "account admission database", owner_only=True)


def account_database_identity(path, connection=None):
    if not path.is_absolute():
        raise CoordinatorError("account admission database path must be absolute")
    try:
        info = secure_regular(path, "account admission database", owner_only=True)
    except CoordinatorError:
        if path.exists() or path.is_symlink():
            raise
        secure_owner_directory(path.parent, "account admission database directory")
        return {"path": str(path), "state": "absent"}
    if connection is not None:
        stored = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != ACCOUNT_APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 1
            or stored is None
            or stored[0] != ACCOUNT_SCHEMA
        ):
            raise CoordinatorError(
                "account admission database identity is unsupported"
            )
    return {
        "application_id": ACCOUNT_APPLICATION_ID,
        "device": info.st_dev,
        "inode": info.st_ino,
        "link_count": info.st_nlink,
        "mode": stat.S_IMODE(info.st_mode),
        "owner_uid": info.st_uid,
        "path": str(path),
        "schema": ACCOUNT_SCHEMA,
        "state": "present",
        "user_version": 1,
    }


@contextmanager
def existing_account_database(path, *, writable):
    identity = account_database_identity(path)
    if identity["state"] == "absent":
        yield None, identity
        return
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(
        f"file:{path}?mode={mode}", uri=True, timeout=10, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if writable:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
        else:
            connection.execute("PRAGMA query_only=ON")
        current = account_database_identity(path, connection)
        if current != identity:
            raise CoordinatorError("account admission database changed while opening")
        yield connection, identity
    finally:
        connection.close()
        if account_database_identity(path) != identity:
            raise CoordinatorError("account admission database changed while open")


def row_result(row):
    result = dict(row)
    result["schema"] = OUTPUT_SCHEMA
    return result


def attempt(connection, attempt_id):
    row = connection.execute(
        """SELECT a.attempt_id,a.provider_family,a.account_route,
                  a.reserve_micro_usd,a.state,a.version,a.prepared_at,
                  a.admitted_at,a.go_at,a.submitted_at,a.terminal_at,
                  a.terminal_result,a.policy_sha256,a.updated_at,
                  b.product_id,b.ticket_id,b.budget_day,
                  b.product_daily_cap_micro_usd,b.ticket_cap_micro_usd,
                  b.machine_daily_cap_micro_usd,b.charge_micro_usd,
                  c.requested_at AS cancellation_requested_at,
                  c.reason AS cancellation_reason
           FROM attempts AS a
           JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
           LEFT JOIN cancellation_requests AS c ON c.attempt_id=a.attempt_id
           WHERE a.attempt_id=?""",
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
        existing_budget = connection.execute(
            "SELECT * FROM attempt_budgets WHERE attempt_id=?",
            (values["attempt_id"],),
        ).fetchone()
        expected_budget = (
            values["product_id"], values["ticket_id"], values["budget_day"],
            values["product_daily_cap_micro_usd"], values["ticket_cap_micro_usd"],
            values["machine_daily_cap_micro_usd"],
        )
        actual_budget = (
            existing_budget["product_id"], existing_budget["ticket_id"],
            existing_budget["budget_day"],
            existing_budget["product_daily_cap_micro_usd"],
            existing_budget["ticket_cap_micro_usd"],
            existing_budget["machine_daily_cap_micro_usd"],
        )
        if expected_budget != actual_budget:
            raise CoordinatorError("attempt_id conflicts with an existing budget binding")
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
    connection.execute(
        """INSERT INTO attempt_budgets(
             attempt_id,product_id,ticket_id,budget_day,
             product_daily_cap_micro_usd,ticket_cap_micro_usd,
             machine_daily_cap_micro_usd)
           VALUES(?,?,?,?,?,?,?)""",
        (
            values["attempt_id"], values["product_id"], values["ticket_id"],
            values["budget_day"], values["product_daily_cap_micro_usd"],
            values["ticket_cap_micro_usd"],
            values["machine_daily_cap_micro_usd"],
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


def budget_checks(connection, row):
    scopes = [
        ("machine_day", "", (), row["machine_daily_cap_micro_usd"]),
        ("product_day", " AND b.product_id=?", (row["product_id"],),
         row["product_daily_cap_micro_usd"]),
        ("ticket", " AND b.product_id=? AND b.ticket_id=?",
         (row["product_id"], row["ticket_id"]), row["ticket_cap_micro_usd"]),
    ]
    denials = []
    for name, predicate, values, cap in scopes:
        spent = connection.execute(
            """SELECT coalesce(sum(
                   CASE WHEN a.state='terminal'
                        THEN b.charge_micro_usd ELSE a.reserve_micro_usd END
                 ),0)
               FROM attempts AS a
               JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
               WHERE b.budget_day=?
                 AND (a.state IN ('reserved','GO','submitted')
                      OR (a.state='terminal' AND b.charge_micro_usd IS NOT NULL))"""
            + predicate,
            (row["budget_day"], *values),
        ).fetchone()[0]
        if spent + row["reserve_micro_usd"] > cap:
            denials.append({"limit": "budget_micro_usd", "scope": name})
    return denials


def admit_mutation(connection, attempt_id, expected_version, policy, policy_hash, now):
    row = attempt(connection, attempt_id)
    if row["state"] != "prepared":
        raise CoordinatorError("only a prepared attempt may be admitted")
    if row["version"] != expected_version:
        raise CoordinatorError("attempt version compare-and-swap failed")
    denials = []
    if connection.execute("SELECT 1 FROM legacy_intervals LIMIT 1").fetchone():
        denials.append({"limit": "legacy_barrier", "scope": "machine"})
    denials.extend(limit_checks(
        connection, policy, row["provider_family"], row["account_route"], now
    ))
    if connection.execute(
        """SELECT count(*) FROM attempts AS a
           JOIN attempt_budgets AS b ON b.attempt_id=a.attempt_id
           WHERE a.state IN ('reserved','GO','submitted')
             AND b.product_id=? AND b.ticket_id=?""",
        (row["product_id"], row["ticket_id"]),
    ).fetchone()[0]:
        denials.append({"limit": "max_concurrent", "scope": "ticket"})
    denials.extend(budget_checks(connection, row))
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


def transition_mutation(
    connection, attempt_id, expected_version, target, now, result=None, charge=None
):
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
    if target == "terminal":
        charge = row["reserve_micro_usd"] if charge is None else validate_money(charge)
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
    if target == "terminal":
        changed = connection.execute(
            """UPDATE attempt_budgets SET charge_micro_usd=?
               WHERE attempt_id=? AND charge_micro_usd IS NULL""",
            (charge, attempt_id),
        ).rowcount
        if changed != 1:
            raise CoordinatorError("attempt charge changed during transition")
    return row_result(attempt(connection, attempt_id))


def common_attempt_values(args):
    return {
        "attempt_id": validate_id(args.attempt_id, "attempt_id"),
        "provider_family": validate_id(args.provider_family, "provider_family"),
        "account_route": validate_id(args.account_route, "account_route"),
        "reserve_micro_usd": validate_money(args.reserve_micro_usd),
        "product_id": validate_id(args.product_id, "product_id"),
        "ticket_id": validate_id(args.ticket_id, "ticket_id"),
        "budget_day": validate_day(args.budget_day),
        "product_daily_cap_micro_usd": validate_money(
            args.product_daily_cap_micro_usd
        ),
        "ticket_cap_micro_usd": validate_money(args.ticket_cap_micro_usd),
        "machine_daily_cap_micro_usd": validate_money(
            args.machine_daily_cap_micro_usd
        ),
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
    with configuration_guard(args.configuration_lock):
        policy, policy_hash = load_policy(Path(args.policy))
        now = now_value(args)
        request = {
            "attempt_id": attempt_id, "expected_version": args.expected_version,
            "now": args.now, "policy_sha256": policy_hash,
        }
        return mutate(
            connection, args.operation_id, "admit", request,
            lambda: admit_mutation(
                connection, attempt_id, args.expected_version, policy,
                policy_hash, now,
            ),
        )


def wait_admit_command(connection, args):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    validate_id(args.operation_id, "operation_id", operation=True)
    if (
        args.expected_policy_sha256 is not None
        and args.configuration_lock is None
    ):
        raise CoordinatorError(
            "activated policy admission requires the configuration lock"
        )
    with configuration_guard(args.configuration_lock):
        policy, policy_hash = load_policy(Path(args.policy))
    if (args.expected_policy_sha256 is not None and
            args.expected_policy_sha256 != policy_hash):
        raise CoordinatorError("provider policy does not match the activated digest")
    if not 1 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        raise CoordinatorError("--wait-seconds is out of range")
    cancel_paths = [Path(value) for value in args.cancel_path]
    if any(not value.is_absolute() for value in cancel_paths):
        raise CoordinatorError("--cancel-path must be absolute")
    request = {
        "attempt_id": attempt_id,
        "expected_version": args.expected_version,
        "policy_sha256": policy_hash,
        "expected_policy_sha256": args.expected_policy_sha256,
        "wait_seconds": args.wait_seconds,
        "cancel_paths": [str(value) for value in cancel_paths],
    }
    deadline = time.monotonic() + args.wait_seconds
    while True:
        with configuration_guard(args.configuration_lock):
            current_policy, current_hash = load_policy(Path(args.policy))
            if current_hash != policy_hash:
                raise CoordinatorError(
                    "provider policy changed during admission wait"
                )
            policy = current_policy
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT command,request_sha256,result_json FROM operations "
                    "WHERE operation_id=?",
                    (args.operation_id,),
                ).fetchone()
                if prior is not None:
                    if (prior["command"] != "wait-admit" or
                            prior["request_sha256"] != digest(request)):
                        raise CoordinatorError(
                            "operation_id was already used for a different request"
                        )
                    result = json.loads(prior["result_json"])
                    connection.commit()
                    return result
                result = admit_mutation(
                    connection, attempt_id, args.expected_version, policy,
                    policy_hash, int(time.time()),
                )
                transient = (
                    result["admitted"] is False
                    and result["denials"]
                    and all(item["limit"] == "max_concurrent"
                            for item in result["denials"])
                )
                stopped = next(
                    (str(value) for value in cancel_paths
                     if value.exists() or value.is_symlink()),
                    None,
                )
                timed_out = time.monotonic() >= deadline
                if result["admitted"] or not transient or stopped or timed_out:
                    result["stopped_by"] = stopped
                    result["timed_out"] = timed_out and not stopped
                    result_json = canonical(result)
                    connection.execute(
                        "INSERT INTO operations VALUES(?,?,?,?,?)",
                        (
                            args.operation_id, "wait-admit", digest(request),
                            result_json, int(time.time()),
                        ),
                    )
                    connection.commit()
                    return result
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        time.sleep(0.1)


def reserve_command(connection, args):
    values = common_attempt_values(args)
    if (
        args.expected_policy_sha256 is not None
        and args.configuration_lock is None
    ):
        raise CoordinatorError(
            "activated policy admission requires the configuration lock"
        )
    with configuration_guard(args.configuration_lock):
        policy, policy_hash = load_policy(Path(args.policy))
        if (args.expected_policy_sha256 is not None and
                args.expected_policy_sha256 != policy_hash):
            raise CoordinatorError(
                "provider policy does not match the activated digest"
            )
        now = now_value(args)
        request = dict(
            values, now=args.now, policy_sha256=policy_hash,
            expected_policy_sha256=args.expected_policy_sha256,
        )

        def reserve():
            prepared = prepare_mutation(connection, values, now)
            if prepared["state"] != "prepared" or prepared["version"] != 1:
                raise CoordinatorError(
                    "reserve replay requires the original prepared attempt"
                )
            return admit_mutation(
                connection, values["attempt_id"], 1, policy, policy_hash, now
            )

        return mutate(connection, args.operation_id, "reserve", request, reserve)


def transition_command(connection, args, target):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    now = now_value(args)
    result = getattr(args, "result", None)
    if result is not None and result not in TERMINAL_RESULTS:
        raise CoordinatorError("terminal result is unsupported")
    charge = getattr(args, "charge_micro_usd", None)
    if charge is not None:
        charge = validate_money(charge)
    request = {
        "attempt_id": attempt_id, "expected_version": args.expected_version,
        "now": args.now, "target": target, "result": result, "charge": charge,
    }
    return mutate(
        connection, args.operation_id, target.lower(), request,
        lambda: transition_mutation(
            connection, attempt_id, args.expected_version, target, now, result,
            charge,
        ),
    )


def request_cancel_command(connection, args):
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    reason = validate_id(args.reason, "reason")
    now = now_value(args)
    request = {
        "attempt_id": attempt_id,
        "expected_version": args.expected_version,
        "now": args.now,
        "reason": reason,
    }

    def persist():
        row = attempt(connection, attempt_id)
        if row["version"] != args.expected_version or row["state"] != "submitted":
            raise CoordinatorError(
                "cancellation request requires the expected submitted attempt"
            )
        existing = connection.execute(
            "SELECT requested_at,reason FROM cancellation_requests WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO cancellation_requests VALUES(?,?,?)",
                (attempt_id, now, reason),
            )
        elif existing["reason"] != reason:
            raise CoordinatorError("cancellation request conflicts")
        return row_result(attempt(connection, attempt_id))

    return mutate(
        connection, args.operation_id, "request-cancel", request, persist
    )


def status_command(connection, args):
    if args.attempt_id:
        rows = [attempt(connection, validate_id(args.attempt_id, "attempt_id"))]
    else:
        rows = [
            attempt(connection, row[0])
            for row in connection.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_id"
            ).fetchall()
        ]
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
        "legacy_intervals": [
            dict(row)
            for row in connection.execute(
                "SELECT interval_id,product_id,started_at "
                "FROM legacy_intervals ORDER BY interval_id"
            ).fetchall()
        ],
        "schema": OUTPUT_SCHEMA,
    }


def legacy_enter_command(connection, args):
    interval_id = validate_id(args.interval_id, "interval_id")
    product_id = validate_id(args.product_id, "product_id")
    now = now_value(args)
    request = {
        "interval_id": interval_id,
        "product_id": product_id,
        "now": args.now,
    }

    def enter():
        existing = connection.execute(
            "SELECT * FROM legacy_intervals WHERE interval_id=?", (interval_id,)
        ).fetchone()
        if existing is not None:
            if existing["product_id"] != product_id:
                raise CoordinatorError("legacy interval identity conflicts")
            return {
                "entered": True,
                "interval": dict(existing),
                "schema": OUTPUT_SCHEMA,
            }
        active = connection.execute(
            "SELECT count(*) FROM attempts WHERE state IN ('reserved','GO','submitted')"
        ).fetchone()[0]
        if active:
            return {
                "denials": [{"limit": "isolated_barrier", "scope": "machine"}],
                "entered": False,
                "schema": OUTPUT_SCHEMA,
            }
        connection.execute(
            "INSERT INTO legacy_intervals VALUES(?,?,?)",
            (interval_id, product_id, now),
        )
        return {
            "entered": True,
            "interval": {
                "interval_id": interval_id,
                "product_id": product_id,
                "started_at": now,
            },
            "schema": OUTPUT_SCHEMA,
        }

    return mutate(
        connection, args.operation_id, "legacy-enter", request, enter
    )


def legacy_exit_command(connection, args):
    interval_id = validate_id(args.interval_id, "interval_id")
    request = {"interval_id": interval_id, "now": args.now}
    now_value(args)

    def leave():
        changed = connection.execute(
            "DELETE FROM legacy_intervals WHERE interval_id=?", (interval_id,)
        ).rowcount
        return {
            "exited": changed == 1,
            "interval_id": interval_id,
            "schema": OUTPUT_SCHEMA,
        }

    return mutate(
        connection, args.operation_id, "legacy-exit", request, leave
    )


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
        if outcome not in {"succeeded", "cancelled", "failed", "unknown"}:
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


def validate_owner_start(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 199
        or "\n" in value
        or "\r" in value
    ):
        raise CoordinatorError("owner process start identity is invalid")
    return value


def process_snapshot():
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,lstart="],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    snapshot = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        snapshot[int(fields[0])] = (int(fields[1]), " ".join(fields[2].split()))
    return snapshot


def owner_state(pid, pgid, started, snapshot):
    if snapshot is None:
        return "unknown"
    observed = snapshot.get(pid)
    if observed is None:
        return "dead"
    observed_pgid, observed_start = observed
    if observed_pgid != pgid:
        return "dead"
    if not observed_start or observed_start != started:
        return "dead"
    return "alive"


def process_group_state(pgid, snapshot):
    if snapshot is None:
        return "unknown"
    return "alive" if any(value[0] == pgid for value in snapshot.values()) else "dead"


def account_lease_is_stale(row, snapshot):
    if owner_state(
        row["owner_pid"], row["owner_pgid"], row["owner_start"], snapshot
    ) != "dead":
        return False
    return row["runtime_pid"] is None or (
        owner_state(
            row["runtime_pid"], row["runtime_pgid"],
            row["runtime_start"], snapshot,
        ) == "dead"
        and process_group_state(row["runtime_pgid"], snapshot) == "dead"
    )


def account_stale_candidates(connection, snapshot=None):
    if connection.in_transaction:
        raise CoordinatorError(
            "account liveness snapshot cannot run inside a writer transaction"
        )
    if snapshot is None:
        snapshot = process_snapshot()
    candidates = {}
    for row in connection.execute(
        """SELECT lease_id,state,owner_pid,owner_pgid,owner_start,
                  runtime_pid,runtime_pgid,runtime_start
           FROM account_leases"""
    ).fetchall():
        if not account_lease_is_stale(row, snapshot):
            continue
        candidates[row["lease_id"]] = (
            row["owner_pid"], row["owner_pgid"], row["owner_start"],
            row["runtime_pid"], row["runtime_pgid"], row["runtime_start"],
        )
    return candidates


def cleanup_stale_account_leases(connection, candidates):
    removed = []
    for lease_id, identity in candidates.items():
        row = connection.execute(
            """SELECT owner_pid,owner_pgid,owner_start,
                      runtime_pid,runtime_pgid,runtime_start
               FROM account_leases WHERE lease_id=?""",
            (lease_id,),
        ).fetchone()
        if row is None or tuple(row) != identity:
            continue
        connection.execute(
            "DELETE FROM account_leases WHERE lease_id=?", (lease_id,)
        )
        removed.append(lease_id)
    return removed


def account_limits(policy, account_route):
    try:
        return policy["account_routes"][account_route]
    except KeyError as exc:
        raise CoordinatorError("account route has no concurrency policy") from exc


def account_lease_result(row):
    return {
        "account_route": row["account_route"],
        "lease_id": row["lease_id"],
        "max_concurrent": row["max_concurrent"],
        "max_starts": row["max_starts"],
        "policy_sha256": row["policy_sha256"],
        "runtime_bound": row["runtime_pid"] is not None,
        "started": row["started_at"] is not None,
        "state": row["state"],
        "trust_scope": row["trust_scope"],
        "window_seconds": row["window_seconds"],
    }


def account_recovery_result(database_sha256, lease_id, lease_sha256):
    return {
        "database_sha256": database_sha256,
        "lease_id": lease_id,
        "lease_sha256": lease_sha256,
        "schema": ACCOUNT_RECOVERY_SCHEMA,
        "status": "absent",
    }


def qualification_recovery_lock_held(path_value, descriptor_value):
    path = Path(path_value)
    try:
        descriptor = int(descriptor_value)
        held = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        raise CoordinatorError(
            "qualification recovery lock capability is invalid"
        ) from exc
    if (
        not path.is_absolute() or not stat.S_ISREG(held.st_mode)
        or held.st_uid != os.geteuid() or held.st_nlink != 1
        or stat.S_IMODE(held.st_mode) & 0o077
        or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise CoordinatorError("qualification recovery lock capability is unsafe")
    probe = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        if (os.fstat(probe).st_dev, os.fstat(probe).st_ino) != (
            held.st_dev, held.st_ino,
        ):
            raise CoordinatorError("qualification recovery lock changed")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(probe, fcntl.LOCK_UN)
        raise CoordinatorError("qualification recovery lock is not held")
    finally:
        os.close(probe)


def require_qualification_recovery_capability():
    names = (
        "FACTORY_CROSS_RELEASE_SOURCE_SHA",
        "FACTORY_CROSS_RELEASE_PRODUCT_ID",
        "FACTORY_DISPATCH_ADMISSION_LOCK",
        "FACTORY_DISPATCH_ADMISSION_LOCK_FD",
        "FACTORY_QUALIFICATION_CONTROLLER_LOCK",
        "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD",
        "FACTORY_KIT_TRUST_SCOPE",
    )
    values = {name: os.environ.get(name, "") for name in names}
    source = values["FACTORY_CROSS_RELEASE_SOURCE_SHA"]
    if (
        not all(values.values())
        or not re.fullmatch(r"[0-9a-f]{40}", source)
        or not validate_id(
            values["FACTORY_CROSS_RELEASE_PRODUCT_ID"], "recovery product_id",
        ).endswith(f":{source}")
        or values["FACTORY_KIT_TRUST_SCOPE"] != "qualification-candidate"
    ):
        raise CoordinatorError("qualification recovery capability is invalid")
    qualification_recovery_lock_held(
        values["FACTORY_DISPATCH_ADMISSION_LOCK"],
        values["FACTORY_DISPATCH_ADMISSION_LOCK_FD"],
    )
    qualification_recovery_lock_held(
        values["FACTORY_QUALIFICATION_CONTROLLER_LOCK"],
        values["FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD"],
    )


def account_recover_preview_command(path, args):
    lease_id = validate_id(args.lease_id, "lease_id")
    with existing_account_database(path, writable=False) as (connection, database):
        database_sha256 = digest(database)
        row = None if connection is None else connection.execute(
            "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            lease_sha256 = digest({"lease_id": lease_id, "state": "absent"})
            return {
                "database": database,
                "database_sha256": database_sha256,
                "lease": None,
                "lease_sha256": lease_sha256,
                "schema": ACCOUNT_RECOVERY_SCHEMA,
                "status": "absent",
            }
        snapshot = process_snapshot()
        if snapshot is None:
            raise CoordinatorError("account recovery liveness is unavailable")
        if not account_lease_is_stale(row, snapshot):
            raise CoordinatorError("account recovery lease is still live")
        lease = dict(row)
        return {
            "database": database,
            "database_sha256": database_sha256,
            "lease": lease,
            "lease_sha256": digest(lease),
            "schema": ACCOUNT_RECOVERY_SCHEMA,
            "status": "planned",
        }


def account_recover_apply_command(path, args):
    require_qualification_recovery_capability()
    lease_id = validate_id(args.lease_id, "lease_id")
    expected_database = args.expected_database_sha256
    expected_lease = args.expected_lease_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", expected_database):
        raise CoordinatorError("account recovery database digest is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_lease):
        raise CoordinatorError("account recovery lease digest is invalid")
    with existing_account_database(path, writable=True) as (connection, database):
        if digest(database) != expected_database:
            raise CoordinatorError("account recovery database identity changed")
        if connection is None:
            return account_recovery_result(
                expected_database, lease_id, expected_lease,
            )
        row = connection.execute(
            "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            return account_recovery_result(
                expected_database, lease_id, expected_lease,
            )
        if row["trust_scope"] != "qualification-candidate":
            raise CoordinatorError("account recovery lease scope is invalid")
        if digest(dict(row)) != expected_lease:
            raise CoordinatorError("account recovery lease identity changed")
        snapshot = process_snapshot()
        if snapshot is None:
            raise CoordinatorError("account recovery liveness is unavailable")
        if not account_lease_is_stale(row, snapshot):
            raise CoordinatorError("account recovery lease is still live")
        expected_identity = tuple(row)
        if account_database_identity(path, connection) != database:
            raise CoordinatorError("account recovery database identity changed")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if current is not None and tuple(current) != expected_identity:
                raise CoordinatorError("account recovery lease changed before release")
            if current is not None:
                connection.execute(
                    "DELETE FROM account_leases WHERE lease_id=?", (lease_id,)
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return account_recovery_result(expected_database, lease_id, expected_lease)


def account_acquire_command(connection, args):
    lease_id = validate_id(args.lease_id, "lease_id")
    account_route = validate_id(args.account_route, "account_route")
    owner_start = validate_owner_start(args.owner_start)
    if args.owner_pid <= 0 or args.owner_pgid <= 0:
        raise CoordinatorError("account admission owner identity is invalid")
    liveness_snapshot = process_snapshot()
    requester_state = owner_state(
        args.owner_pid, args.owner_pgid, owner_start, liveness_snapshot
    )
    if requester_state != "alive":
        raise CoordinatorError("account admission owner is not live")
    if not 1 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        raise CoordinatorError("--wait-seconds is out of range")
    cancel_paths = [Path(value) for value in args.cancel_path]
    if any(not value.is_absolute() for value in cancel_paths):
        raise CoordinatorError("--cancel-path must be absolute")
    if args.expected_policy_sha256 is not None and args.configuration_lock is None:
        raise CoordinatorError(
            "activated policy account admission requires the configuration lock"
        )
    with configuration_guard(args.configuration_lock):
        policy, policy_hash = load_policy(Path(args.policy))
    if (
        args.expected_policy_sha256 is not None
        and args.expected_policy_sha256 != policy_hash
    ):
        raise CoordinatorError("provider policy does not match the activated digest")
    limits = account_limits(policy, account_route)
    requested_at_ms = int(time.time() * 1000)
    deadline = time.monotonic() + args.wait_seconds
    stale_releases = []
    stale_candidates = account_stale_candidates(connection, liveness_snapshot)
    next_stale_check = time.monotonic() + 1
    liveness_fresh = True
    refresh_before_admission = False
    while True:
        if refresh_before_admission or time.monotonic() >= next_stale_check:
            liveness_snapshot = process_snapshot()
            requester_state = owner_state(
                args.owner_pid, args.owner_pgid, owner_start, liveness_snapshot
            )
            stale_candidates = account_stale_candidates(
                connection, liveness_snapshot
            )
            next_stale_check = time.monotonic() + 1
            liveness_fresh = True
            refresh_before_admission = False
        with configuration_guard(args.configuration_lock):
            current_policy, current_hash = load_policy(Path(args.policy))
            if current_hash != policy_hash:
                raise CoordinatorError("provider policy changed during account admission")
            if account_limits(current_policy, account_route) != limits:
                raise CoordinatorError("account route policy changed during admission")
            connection.execute("BEGIN IMMEDIATE")
            try:
                stale = cleanup_stale_account_leases(connection, stale_candidates)
                stale_candidates = {}
                stale_releases.extend(
                    item for item in stale if item not in stale_releases
                )
                existing = connection.execute(
                    "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                if requester_state != "alive":
                    connection.execute(
                        """DELETE FROM account_leases
                           WHERE lease_id=? AND state='waiting'
                             AND owner_pid=? AND owner_pgid=? AND owner_start=?""",
                        (
                            lease_id, args.owner_pid, args.owner_pgid, owner_start,
                        ),
                    )
                    connection.commit()
                    return {
                        "admitted": False,
                        "owner_unavailable": requester_state,
                        "schema": ACCOUNT_OUTPUT_SCHEMA,
                        "stale_releases": stale_releases,
                        "timed_out": False,
                    }
                if existing is None and not liveness_fresh:
                    connection.commit()
                    refresh_before_admission = True
                    continue
                expected = (
                    account_route,
                    args.trust_scope,
                    args.owner_pid,
                    args.owner_pgid,
                    owner_start,
                    policy_hash,
                    limits["max_concurrent"],
                    limits["max_starts"],
                    limits["window_seconds"],
                )
                if existing is None:
                    connection.execute(
                        """INSERT INTO account_leases(
                             lease_id,account_route,trust_scope,owner_pid,
                             owner_pgid,owner_start,state,policy_sha256,
                             max_concurrent,max_starts,window_seconds,
                             requested_at_ms)
                           VALUES(?,?,?,?,?,?,'waiting',?,?,?,?,?)""",
                        (lease_id, *expected, requested_at_ms),
                    )
                    existing = connection.execute(
                        "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
                    ).fetchone()
                else:
                    actual = (
                        existing["account_route"],
                        existing["trust_scope"],
                        existing["owner_pid"],
                        existing["owner_pgid"],
                        existing["owner_start"],
                        existing["policy_sha256"],
                        existing["max_concurrent"],
                        existing["max_starts"],
                        existing["window_seconds"],
                    )
                    if actual != expected:
                        raise CoordinatorError("lease_id conflicts with account admission")
                if existing["state"] == "active":
                    stopped = next(
                        (
                            str(value)
                            for value in cancel_paths
                            if value.exists() or value.is_symlink()
                        ),
                        None,
                    )
                    if stopped is not None:
                        if existing["runtime_pid"] is None:
                            connection.execute(
                                "DELETE FROM account_leases WHERE lease_id=?",
                                (lease_id,),
                            )
                        connection.commit()
                        return {
                            "admitted": False,
                            "lease": account_lease_result(existing),
                            "schema": ACCOUNT_OUTPUT_SCHEMA,
                            "stale_releases": stale_releases,
                            "stopped_by": stopped,
                            "timed_out": False,
                        }
                    connection.commit()
                    return {
                        "admitted": True,
                        "lease": account_lease_result(existing),
                        "schema": ACCOUNT_OUTPUT_SCHEMA,
                        "stale_releases": stale_releases,
                        "timed_out": False,
                    }
                incompatible = connection.execute(
                    """SELECT 1 FROM account_leases
                       WHERE account_route=? AND lease_id!=?
                         AND (max_concurrent!=? OR max_starts!=?
                              OR window_seconds!=?) LIMIT 1""",
                    (
                        account_route,
                        lease_id,
                        limits["max_concurrent"],
                        limits["max_starts"],
                        limits["window_seconds"],
                    ),
                ).fetchone()
                if incompatible is not None:
                    raise CoordinatorError(
                        "live account admission policies disagree across lanes"
                    )
                now = int(time.time())
                connection.execute(
                    "DELETE FROM account_starts WHERE started_at + window_seconds <= ?",
                    (now,),
                )
                incompatible_history = connection.execute(
                    """SELECT 1 FROM account_starts
                       WHERE account_route=?
                         AND (max_concurrent!=? OR max_starts!=?
                              OR window_seconds!=?) LIMIT 1""",
                    (
                        account_route,
                        limits["max_concurrent"],
                        limits["max_starts"],
                        limits["window_seconds"],
                    ),
                ).fetchone()
                if incompatible_history is not None:
                    raise CoordinatorError(
                        "active account start-window policies disagree across lanes"
                    )
                stopped = next(
                    (
                        str(value)
                        for value in cancel_paths
                        if value.exists() or value.is_symlink()
                    ),
                    None,
                )
                if stopped is not None:
                    connection.execute(
                        "DELETE FROM account_leases WHERE lease_id=? AND state='waiting'",
                        (lease_id,),
                    )
                    connection.commit()
                    return {
                        "admitted": False,
                        "lease": account_lease_result(existing),
                        "schema": ACCOUNT_OUTPUT_SCHEMA,
                        "stale_releases": stale_releases,
                        "stopped_by": stopped,
                        "timed_out": False,
                    }
                active = connection.execute(
                    """SELECT count(*) FROM account_leases
                       WHERE account_route=? AND state='active'""",
                    (account_route,),
                ).fetchone()[0]
                starts = connection.execute(
                    """SELECT count(*) FROM account_starts
                       WHERE account_route=? AND started_at>?""",
                    (account_route, now - limits["window_seconds"]),
                ).fetchone()[0]
                pending_starts = connection.execute(
                    """SELECT count(*) FROM account_leases
                       WHERE account_route=? AND state='active'
                         AND started_at IS NULL""",
                    (account_route,),
                ).fetchone()[0]
                first = connection.execute(
                    """SELECT lease_id FROM account_leases
                       WHERE account_route=? AND state='waiting'
                       ORDER BY CASE trust_scope
                                  WHEN 'production-certified' THEN 0
                                  WHEN 'qualification-candidate' THEN 1
                                  ELSE 2 END,
                                sequence LIMIT 1""",
                    (account_route,),
                ).fetchone()
                grace_elapsed = (
                    args.trust_scope == "production-certified"
                    or active < limits["max_concurrent"] - 1
                    or int(time.time() * 1000) - existing["requested_at_ms"]
                    >= NON_PRODUCTION_GRACE_MILLISECONDS
                )
                if (
                    active < limits["max_concurrent"]
                    and starts + pending_starts < limits["max_starts"]
                    and first is not None
                    and first["lease_id"] == lease_id
                    and grace_elapsed
                ):
                    if not liveness_fresh:
                        connection.commit()
                        refresh_before_admission = True
                        continue
                    changed = connection.execute(
                        """UPDATE account_leases
                           SET state='active',admitted_at=?
                           WHERE lease_id=? AND state='waiting'""",
                        (now, lease_id),
                    ).rowcount
                    if changed != 1:
                        raise CoordinatorError("account lease changed during admission")
                    admitted = connection.execute(
                        "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
                    ).fetchone()
                    connection.commit()
                    return {
                        "admitted": True,
                        "lease": account_lease_result(admitted),
                        "schema": ACCOUNT_OUTPUT_SCHEMA,
                        "stale_releases": stale_releases,
                        "timed_out": False,
                    }
                timed_out = time.monotonic() >= deadline
                if timed_out:
                    connection.execute(
                        "DELETE FROM account_leases WHERE lease_id=? AND state='waiting'",
                        (lease_id,),
                    )
                    connection.commit()
                    return {
                        "admitted": False,
                        "lease": account_lease_result(existing),
                        "schema": ACCOUNT_OUTPUT_SCHEMA,
                        "stale_releases": stale_releases,
                        "timed_out": True,
                    }
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        time.sleep(0.05)
        liveness_fresh = False


def account_release_command(connection, args):
    lease_id = validate_id(args.lease_id, "lease_id")
    owner_start = validate_owner_start(args.owner_start)
    observed = connection.execute(
        "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
    ).fetchone()
    if observed is None:
        return {
            "lease_id": lease_id,
            "released": False,
            "schema": ACCOUNT_OUTPUT_SCHEMA,
        }
    if (
        observed["owner_pid"] != args.owner_pid
        or observed["owner_pgid"] != args.owner_pgid
        or observed["owner_start"] != owner_start
    ):
        raise CoordinatorError("account lease ownership changed")
    snapshot = process_snapshot()
    if observed["runtime_pid"] is not None and (
        owner_state(
            observed["runtime_pid"], observed["runtime_pgid"],
            observed["runtime_start"], snapshot,
        )
        != "dead"
        or process_group_state(observed["runtime_pgid"], snapshot) != "dead"
    ):
        raise CoordinatorError(
            "account lease cannot release before its runtime group drains"
        )
    observed_identity = tuple(observed)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            connection.commit()
            return {
                "lease_id": lease_id,
                "released": False,
                "schema": ACCOUNT_OUTPUT_SCHEMA,
            }
        if tuple(row) != observed_identity:
            raise CoordinatorError("account lease changed before release")
        connection.execute("DELETE FROM account_leases WHERE lease_id=?", (lease_id,))
        connection.commit()
        return {
            "lease_id": lease_id,
            "released": True,
            "schema": ACCOUNT_OUTPUT_SCHEMA,
        }
    except Exception:
        connection.rollback()
        raise


def account_bind_runtime_command(connection, args):
    lease_id = validate_id(args.lease_id, "lease_id")
    owner_start = validate_owner_start(args.owner_start)
    runtime_start = validate_owner_start(args.runtime_start)
    snapshot = process_snapshot()
    if (
        args.runtime_pid <= 0
        or args.runtime_pgid <= 0
        or args.runtime_pid != args.runtime_pgid
        or owner_state(
            args.runtime_pid, args.runtime_pgid, runtime_start, snapshot
        ) != "alive"
        or owner_state(
            args.owner_pid, args.owner_pgid, owner_start, snapshot
        ) != "alive"
    ):
        raise CoordinatorError("account admission runtime identity is invalid")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if (
            row is None
            or row["state"] != "active"
            or row["owner_pid"] != args.owner_pid
            or row["owner_pgid"] != args.owner_pgid
            or row["owner_start"] != owner_start
        ):
            raise CoordinatorError("active account admission lease is invalid")
        existing = (
            row["runtime_pid"], row["runtime_pgid"], row["runtime_start"]
        )
        expected = (args.runtime_pid, args.runtime_pgid, runtime_start)
        if existing == (None, None, None):
            connection.execute(
                """UPDATE account_leases
                   SET runtime_pid=?,runtime_pgid=?,runtime_start=?
                   WHERE lease_id=? AND runtime_pid IS NULL""",
                (*expected, lease_id),
            )
        elif existing != expected:
            raise CoordinatorError("account admission runtime binding changed")
        bound = connection.execute(
            "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        connection.commit()
        return {
            "bound": True,
            "lease": account_lease_result(bound),
            "schema": ACCOUNT_OUTPUT_SCHEMA,
        }
    except Exception:
        connection.rollback()
        raise


def account_validate_command(connection, args):
    lease_id = validate_id(args.lease_id, "lease_id")
    account_route = validate_id(args.account_route, "account_route")
    owner_start = validate_owner_start(args.owner_start)
    runtime_start = validate_owner_start(args.runtime_start)
    snapshot = process_snapshot()
    if (
        owner_state(args.owner_pid, args.owner_pgid, owner_start, snapshot)
        != "alive"
        or owner_state(
            args.runtime_pid, args.runtime_pgid, runtime_start, snapshot
        )
        != "alive"
    ):
        raise CoordinatorError("active account admission lease is invalid")
    with configuration_guard(args.configuration_lock):
        policy, policy_hash = load_policy(Path(args.policy))
        if policy_hash != args.expected_policy_sha256:
            raise CoordinatorError("provider policy changed before account start")
        limits = account_limits(policy, account_route)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "active"
                or row["account_route"] != account_route
                or row["trust_scope"] != args.trust_scope
                or row["owner_pid"] != args.owner_pid
                or row["owner_pgid"] != args.owner_pgid
                or row["owner_start"] != owner_start
                or row["runtime_pid"] != args.runtime_pid
                or row["runtime_pgid"] != args.runtime_pgid
                or row["runtime_start"] != runtime_start
                or row["policy_sha256"] != policy_hash
                or limits["max_concurrent"] != row["max_concurrent"]
                or limits["max_starts"] != row["max_starts"]
                or limits["window_seconds"] != row["window_seconds"]
            ):
                raise CoordinatorError("active account admission lease is invalid")

            now = int(time.time())
            connection.execute(
                "DELETE FROM account_starts WHERE started_at + window_seconds <= ?",
                (now,),
            )
            incompatible_history = connection.execute(
                """SELECT 1 FROM account_starts
                   WHERE account_route=?
                     AND (max_concurrent!=? OR max_starts!=?
                          OR window_seconds!=?) LIMIT 1""",
                (
                    account_route,
                    row["max_concurrent"],
                    row["max_starts"],
                    row["window_seconds"],
                ),
            ).fetchone()
            if incompatible_history is not None:
                raise CoordinatorError(
                    "active account start-window policies disagree across lanes"
                )
            starts = connection.execute(
                "SELECT count(*) FROM account_starts WHERE account_route=?",
                (account_route,),
            ).fetchone()[0]
            pending = connection.execute(
                """SELECT count(*) FROM account_leases
                   WHERE account_route=? AND state='active' AND started_at IS NULL""",
                (account_route,),
            ).fetchone()[0]
            if starts + pending > row["max_starts"]:
                raise CoordinatorError("account start reservation capacity is invalid")

            prior_start = connection.execute(
                "SELECT * FROM account_starts WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row["started_at"] is None:
                if prior_start is not None:
                    raise CoordinatorError("account start history is inconsistent")
                connection.execute(
                    """INSERT INTO account_starts(
                         lease_id,account_route,started_at,policy_sha256,
                         max_concurrent,max_starts,window_seconds)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        lease_id,
                        account_route,
                        now,
                        row["policy_sha256"],
                        row["max_concurrent"],
                        row["max_starts"],
                        row["window_seconds"],
                    ),
                )
                connection.execute(
                    "UPDATE account_leases SET started_at=? WHERE lease_id=?",
                    (now, lease_id),
                )
            elif (
                prior_start is None
                or prior_start["account_route"] != account_route
                or prior_start["started_at"] != row["started_at"]
                or prior_start["policy_sha256"] != row["policy_sha256"]
                or prior_start["max_concurrent"] != row["max_concurrent"]
                or prior_start["max_starts"] != row["max_starts"]
                or prior_start["window_seconds"] != row["window_seconds"]
            ):
                raise CoordinatorError("account start history is inconsistent")
            started = connection.execute(
                "SELECT * FROM account_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            connection.commit()
            return {
                "lease": account_lease_result(started),
                "schema": ACCOUNT_OUTPUT_SCHEMA,
                "valid": True,
            }
        except Exception:
            connection.rollback()
            raise


def account_status_command(connection, _args):
    return {
        "leases": [
            account_lease_result(row)
            for row in connection.execute(
                "SELECT * FROM account_leases ORDER BY account_route,sequence"
            ).fetchall()
        ],
        "schema": ACCOUNT_OUTPUT_SCHEMA,
        "starts": [
            dict(row)
            for row in connection.execute(
                "SELECT lease_id,account_route,started_at,window_seconds "
                "     ,policy_sha256,max_concurrent,max_starts "
                "FROM account_starts ORDER BY account_route,started_at,lease_id"
            ).fetchall()
        ],
    }


def parser():
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True)
    result.add_argument("--account-db")
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
        command.add_argument("--product-id", required=True)
        command.add_argument("--ticket-id", required=True)
        command.add_argument("--budget-day", required=True)
        command.add_argument(
            "--product-daily-cap-micro-usd", required=True, type=int
        )
        command.add_argument("--ticket-cap-micro-usd", required=True, type=int)
        command.add_argument(
            "--machine-daily-cap-micro-usd", required=True, type=int
        )
    prepare.set_defaults(handler=prepare_command)
    reserve.add_argument("--policy", required=True)
    reserve.add_argument("--configuration-lock")
    reserve.add_argument("--expected-policy-sha256")
    reserve.set_defaults(handler=reserve_command)

    admit = mutation("admit")
    admit.add_argument("--attempt-id", required=True)
    admit.add_argument("--expected-version", required=True, type=int)
    admit.add_argument("--policy", required=True)
    admit.add_argument("--configuration-lock")
    admit.set_defaults(handler=admit_command)

    wait_admit = mutation("wait-admit")
    wait_admit.add_argument("--attempt-id", required=True)
    wait_admit.add_argument("--expected-version", required=True, type=int)
    wait_admit.add_argument("--policy", required=True)
    wait_admit.add_argument("--configuration-lock")
    wait_admit.add_argument("--expected-policy-sha256")
    wait_admit.add_argument("--wait-seconds", required=True, type=int)
    wait_admit.add_argument("--cancel-path", action="append", default=[])
    wait_admit.set_defaults(handler=wait_admit_command)

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
    terminal.add_argument("--charge-micro-usd", required=True, type=int)
    terminal.set_defaults(
        handler=lambda connection, args:
        transition_command(connection, args, "terminal")
    )

    cancellation = mutation("request-cancel")
    cancellation.add_argument("--attempt-id", required=True)
    cancellation.add_argument("--expected-version", required=True, type=int)
    cancellation.add_argument("--reason", required=True)
    cancellation.set_defaults(handler=request_cancel_command)

    status = commands.add_parser("status")
    status.add_argument("--attempt-id")
    status.set_defaults(handler=status_command)

    legacy_enter = mutation("legacy-enter")
    legacy_enter.add_argument("--interval-id", required=True)
    legacy_enter.add_argument("--product-id", required=True)
    legacy_enter.set_defaults(handler=legacy_enter_command)

    legacy_exit = mutation("legacy-exit")
    legacy_exit.add_argument("--interval-id", required=True)
    legacy_exit.set_defaults(handler=legacy_exit_command)

    reconcile = mutation("reconcile")
    reconcile.add_argument("--input", required=True)
    reconcile.set_defaults(handler=reconcile_command)

    def account_owner(command):
        command.add_argument("--lease-id", required=True)
        command.add_argument("--owner-pid", required=True, type=int)
        command.add_argument("--owner-pgid", required=True, type=int)
        command.add_argument("--owner-start", required=True)

    def account_runtime(command):
        command.add_argument("--runtime-pid", required=True, type=int)
        command.add_argument("--runtime-pgid", required=True, type=int)
        command.add_argument("--runtime-start", required=True)

    account_acquire = commands.add_parser("account-acquire")
    account_owner(account_acquire)
    account_acquire.add_argument("--account-route", required=True)
    account_acquire.add_argument(
        "--trust-scope",
        required=True,
        choices=(
            "production-certified", "qualification-candidate", "development-local"
        ),
    )
    account_acquire.add_argument("--policy", required=True)
    account_acquire.add_argument("--configuration-lock")
    account_acquire.add_argument("--expected-policy-sha256")
    account_acquire.add_argument("--wait-seconds", required=True, type=int)
    account_acquire.add_argument("--cancel-path", action="append", default=[])
    account_acquire.set_defaults(handler=account_acquire_command)

    account_bind = commands.add_parser("account-bind-runtime")
    account_owner(account_bind)
    account_runtime(account_bind)
    account_bind.set_defaults(handler=account_bind_runtime_command)

    account_validate = commands.add_parser("account-validate")
    account_owner(account_validate)
    account_runtime(account_validate)
    account_validate.add_argument("--account-route", required=True)
    account_validate.add_argument("--expected-policy-sha256", required=True)
    account_validate.add_argument("--policy", required=True)
    account_validate.add_argument("--configuration-lock")
    account_validate.add_argument(
        "--trust-scope",
        required=True,
        choices=(
            "production-certified", "qualification-candidate", "development-local"
        ),
    )
    account_validate.set_defaults(handler=account_validate_command)

    account_release = commands.add_parser("account-release")
    account_owner(account_release)
    account_release.set_defaults(handler=account_release_command)

    account_status = commands.add_parser("account-status")
    account_status.set_defaults(handler=account_status_command)

    account_recover_preview = commands.add_parser("account-recover-preview")
    account_recover_preview.add_argument("--lease-id", required=True)
    account_recover_preview.set_defaults(handler=account_recover_preview_command)

    account_recover_apply = commands.add_parser("account-recover-apply")
    account_recover_apply.add_argument("--lease-id", required=True)
    account_recover_apply.add_argument("--expected-database-sha256", required=True)
    account_recover_apply.add_argument("--expected-lease-sha256", required=True)
    account_recover_apply.set_defaults(handler=account_recover_apply_command)
    return result


def main():
    try:
        args = parser().parse_args()
        if args.command in ACCOUNT_RECOVERY_COMMANDS:
            if args.account_db is None:
                raise CoordinatorError("account admission database is required")
            output = args.handler(Path(args.account_db), args)
        else:
            if args.command in ACCOUNT_COMMANDS:
                if args.account_db is None:
                    raise CoordinatorError(
                        "account admission database is required"
                    )
                selected_database = account_database(Path(args.account_db))
            else:
                selected_database = database(Path(args.db))
            with selected_database as connection:
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
