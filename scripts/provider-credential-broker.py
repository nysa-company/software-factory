#!/usr/bin/env python3
"""Issue attempt-bound tokens and proxy approved provider requests."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import stat
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


CONFIG_SCHEMA = "nysa.software-factory.provider-credentials/v1"
OUTPUT_SCHEMA = "nysa.software-factory.provider-credential-broker/v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
HEADER = re.compile(r"^[A-Za-z0-9-]{1,100}$")
MAX_JSON = 1_000_000
MAX_MONEY = 10**15
MAX_TTL = 3600
APPLICATION_ID = 0x4E595342


class BrokerError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise BrokerError(f"{label} is invalid")
    return value


def secure_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BrokerError(f"{label} path must be absolute")
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise BrokerError(f"{label} is unsafe")


def secure_regular(
    path: Path, label: str, *, maximum: int | None = None, owner_only: bool = True
) -> os.stat_result:
    if not path.is_absolute():
        raise BrokerError(f"{label} path must be absolute")
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (owner_only and info.st_mode & 0o077)
        or (maximum is not None and info.st_size > maximum)
    ):
        raise BrokerError(f"{label} is unsafe")
    return info


def read_json(path: Path, label: str) -> dict[str, Any]:
    expected = secure_regular(path, label, maximum=MAX_JSON)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise BrokerError(f"{label} changed while opening")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerError(f"{label} is invalid JSON") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise BrokerError(f"{label} must contain an object")
    return value


def normalized_origin(value: Any, allow_http_loopback: bool) -> str:
    if not isinstance(value, str):
        raise BrokerError("upstream_origin is invalid")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not parsed.hostname
    ):
        raise BrokerError("upstream_origin must be a credential-free origin")
    if parsed.scheme != "https":
        loopback = False
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not (allow_http_loopback and parsed.scheme == "http" and loopback):
            raise BrokerError("upstream_origin must use HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}"


def load_config(path: Path, allow_http_loopback: bool = False) -> dict[str, Any]:
    value = read_json(path, "credential configuration")
    if set(value) != {"schema", "routes"} or value.get("schema") != CONFIG_SCHEMA:
        raise BrokerError("credential configuration schema is unsupported")
    if not isinstance(value["routes"], dict) or not value["routes"]:
        raise BrokerError("credential configuration routes are invalid")
    routes: dict[str, Any] = {}
    for route_id, raw in value["routes"].items():
        validate_id(route_id, "route_id")
        required = {
            "provider_family", "upstream_origin", "credential_header",
            "credential_prefix", "credential_value", "allowed_paths",
            "allowed_models", "forward_headers", "max_request_bytes",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise BrokerError(f"route {route_id} has unsupported or missing fields")
        provider_family = validate_id(raw["provider_family"], "provider_family")
        header = raw["credential_header"]
        if not isinstance(header, str) or not HEADER.fullmatch(header):
            raise BrokerError("credential_header is invalid")
        prefix = raw["credential_prefix"]
        secret = raw["credential_value"]
        if (
            not isinstance(prefix, str)
            or "\r" in prefix
            or "\n" in prefix
            or not isinstance(secret, str)
            or not secret
            or "\r" in secret
            or "\n" in secret
        ):
            raise BrokerError("credential material is invalid")
        paths = raw["allowed_paths"]
        if (
            not isinstance(paths, list)
            or not paths
            or any(
                not isinstance(item, str)
                or not item.startswith("/")
                or item.startswith("//")
                or "?" in item
                or "#" in item
                for item in paths
            )
            or len(set(paths)) != len(paths)
        ):
            raise BrokerError("allowed_paths is invalid")
        models = raw["allowed_models"]
        if (
            not isinstance(models, list)
            or not models
            or any(not isinstance(item, str) or not item for item in models)
            or len(set(models)) != len(models)
        ):
            raise BrokerError("allowed_models is invalid")
        forward = raw["forward_headers"]
        if (
            not isinstance(forward, list)
            or any(not isinstance(item, str) or not HEADER.fullmatch(item) for item in forward)
        ):
            raise BrokerError("forward_headers is invalid")
        forbidden = {"authorization", "proxy-authorization", "x-api-key", header.lower()}
        if any(item.lower() in forbidden for item in forward):
            raise BrokerError("forward_headers includes credential-bearing headers")
        maximum = raw["max_request_bytes"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_JSON:
            raise BrokerError("max_request_bytes is invalid")
        routes[route_id] = {
            **raw,
            "provider_family": provider_family,
            "upstream_origin": normalized_origin(raw["upstream_origin"], allow_http_loopback),
            "allowed_paths": frozenset(paths),
            "allowed_models": frozenset(models),
            "forward_headers": frozenset(item.lower() for item in forward),
        }
    return {"schema": CONFIG_SCHEMA, "routes": routes}


def create_database(path: Path) -> None:
    secure_directory(path.parent, "broker database directory")
    if not path.exists():
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
    secure_regular(path, "broker database")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
  token_sha256 TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  model TEXT NOT NULL,
  reserve_micro_usd INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  max_requests INTEGER NOT NULL,
  used_requests INTEGER NOT NULL DEFAULT 0,
  issued_at INTEGER NOT NULL,
  revoked_at INTEGER
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS one_live_attempt_token
  ON tokens(attempt_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS requests (
  token_sha256 TEXT NOT NULL REFERENCES tokens(token_sha256),
  sequence INTEGER NOT NULL,
  started_at INTEGER NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  status INTEGER,
  completed_at INTEGER,
  PRIMARY KEY(token_sha256, sequence)
) STRICT;
"""


def connect(path: Path) -> sqlite3.Connection:
    create_database(path)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if objects == 0 and application == 0 and version == 0:
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute("PRAGMA user_version = 1")
        elif application != APPLICATION_ID or version != 1:
            raise BrokerError("broker database identity is invalid")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    return connection


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.credentials, args.allow_http_loopback)
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    route_id = validate_id(args.route_id, "route_id")
    route = config["routes"].get(route_id)
    if route is None or args.model not in route["allowed_models"]:
        raise BrokerError("route or model is not approved")
    if not 1 <= args.ttl_seconds <= MAX_TTL:
        raise BrokerError(f"ttl_seconds must be from 1 through {MAX_TTL}")
    if not 1 <= args.max_requests <= 100:
        raise BrokerError("max_requests must be from 1 through 100")
    if not 0 <= args.reserve_micro_usd <= MAX_MONEY:
        raise BrokerError("reserve_micro_usd is out of range")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    connection = connect(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM tokens WHERE attempt_id = ? AND revoked_at IS NULL",
            (attempt_id,),
        ).fetchone()
        if existing:
            raise BrokerError("attempt already has a live token")
        connection.execute(
            """INSERT INTO tokens(
                 token_sha256, attempt_id, route_id, model, reserve_micro_usd,
                 expires_at, max_requests, issued_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_hash(token), attempt_id, route_id, args.model,
                args.reserve_micro_usd, now + args.ttl_seconds, args.max_requests, now,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "attempt_id": attempt_id,
        "broker_token": token,
        "expires_at": now + args.ttl_seconds,
        "model": args.model,
        "reserve_micro_usd": args.reserve_micro_usd,
        "route_id": route_id,
        "schema": OUTPUT_SCHEMA,
        "status": "issued",
    }


def revoke(args: argparse.Namespace) -> dict[str, Any]:
    attempt_id = validate_id(args.attempt_id, "attempt_id")
    now = int(time.time())
    connection = connect(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE tokens SET revoked_at = ? WHERE attempt_id = ? AND revoked_at IS NULL",
            (now, attempt_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "attempt_id": attempt_id,
        "revoked": cursor.rowcount == 1,
        "schema": OUTPUT_SCHEMA,
        "status": "ok",
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    connection = connect(args.db)
    try:
        query = """SELECT attempt_id, route_id, model, reserve_micro_usd, expires_at,
                          max_requests, used_requests, issued_at, revoked_at,
                          EXISTS(
                            SELECT 1 FROM requests
                            WHERE requests.token_sha256=tokens.token_sha256
                              AND requests.completed_at IS NULL
                          ) AS request_in_flight
                   FROM tokens"""
        parameters: tuple[Any, ...] = ()
        if args.attempt_id:
            query += " WHERE attempt_id = ?"
            parameters = (validate_id(args.attempt_id, "attempt_id"),)
        rows = [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()
    now = int(time.time())
    for row in rows:
        row["request_in_flight"] = bool(row["request_in_flight"])
        row["active"] = (
            row["revoked_at"] is None
            and row["expires_at"] > now
            and (row["used_requests"] < row["max_requests"] or row["request_in_flight"])
        )
    return {"schema": OUTPUT_SCHEMA, "status": "ok", "tokens": rows}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BrokerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, handler, *, db: Path, routes: dict[str, Any]):
        super().__init__(address, handler)
        self.db = db
        self.routes = routes
        self.opener = urllib.request.build_opener(NoRedirect())


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server: BrokerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - broker request %s\n" % (self.address_string(), args[1] if len(args) > 1 else "-")
        )

    def error(self, status: int, message: str) -> None:
        body = canonical({"error": message, "schema": OUTPUT_SCHEMA, "status": "error"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            self.proxy()
        except BrokerError as error:
            self.error(403, str(error))
        except (OSError, sqlite3.Error, urllib.error.URLError):
            self.error(502, "approved provider request failed")

    def proxy(self) -> None:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise BrokerError("valid attempt token required")
        token = authorization[7:]
        if not TOKEN.fullmatch(token):
            raise BrokerError("valid attempt token required")
        if "?" in self.path or "#" in self.path or not self.path.startswith("/"):
            raise BrokerError("provider path is not approved")
        length_text = self.headers.get("Content-Length")
        if length_text is None or not length_text.isascii() or not length_text.isdigit():
            raise BrokerError("bounded Content-Length is required")
        length = int(length_text)
        connection = connect(self.server.db)
        now = int(time.time())
        digest = token_hash(token)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_sha256 = ?", (digest,)
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or row["expires_at"] <= now
                or row["used_requests"] >= row["max_requests"]
            ):
                raise BrokerError("attempt token is expired, revoked, or exhausted")
            route = self.server.routes.get(row["route_id"])
            if (
                route is None
                or self.path not in route["allowed_paths"]
                or length > route["max_request_bytes"]
            ):
                raise BrokerError("provider path or request size is not approved")
            body = self.rfile.read(length)
            if len(body) != length:
                raise BrokerError("provider request body is incomplete")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BrokerError("provider request body is invalid JSON") from error
            if not isinstance(payload, dict) or payload.get("model") != row["model"]:
                raise BrokerError("provider request model does not match token binding")
            sequence = row["used_requests"] + 1
            connection.execute(
                "UPDATE tokens SET used_requests = ? WHERE token_sha256 = ?",
                (sequence, digest),
            )
            connection.execute(
                """INSERT INTO requests(
                     token_sha256, sequence, started_at, method, path, body_sha256
                   ) VALUES (?, ?, ?, 'POST', ?, ?)""",
                (digest, sequence, now, self.path, hashlib.sha256(body).hexdigest()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
        connection.close()

        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        for name in route["forward_headers"]:
            if name in self.headers:
                headers[name] = self.headers[name]
        headers[route["credential_header"]] = (
            route["credential_prefix"] + route["credential_value"]
        )
        request = urllib.request.Request(
            route["upstream_origin"] + self.path,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            response = self.server.opener.open(request, timeout=120)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            response_body = response.read(MAX_JSON + 1)
            if len(response_body) > MAX_JSON:
                raise BrokerError("provider response exceeds broker limit")
            response_status = response.status
            response_type = response.headers.get("Content-Type", "application/json")
        completed = int(time.time())
        connection = connect(self.server.db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE requests SET status = ?, completed_at = ?
                   WHERE token_sha256 = ? AND sequence = ?""",
                (response_status, completed, digest, sequence),
            )
            connection.commit()
        finally:
            connection.close()
        self.send_response(response_status)
        self.send_header("Content-Type", response_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


def serve(args: argparse.Namespace) -> None:
    config = load_config(args.credentials, args.allow_http_loopback)
    create_database(args.db)
    server = BrokerServer(
        (args.listen_host, args.listen_port),
        ProxyHandler,
        db=args.db,
        routes=config["routes"],
    )
    if args.tls_cert is None or args.tls_key is None:
        if not args.allow_plaintext_loopback:
            raise BrokerError("TLS certificate and key are required")
        try:
            if not ipaddress.ip_address(args.listen_host).is_loopback:
                raise BrokerError("plaintext broker is restricted to loopback")
        except ValueError as error:
            raise BrokerError("plaintext broker is restricted to loopback") from error
    else:
        secure_regular(args.tls_key, "TLS private key")
        secure_regular(args.tls_cert, "TLS certificate", owner_only=False)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--db", required=True, type=Path)
    value.add_argument("--credentials", required=True, type=Path)
    value.add_argument("--allow-http-loopback", action="store_true", help=argparse.SUPPRESS)
    commands = value.add_subparsers(dest="command", required=True)
    issuance = commands.add_parser("issue")
    issuance.add_argument("--attempt-id", required=True)
    issuance.add_argument("--route-id", required=True)
    issuance.add_argument("--model", required=True)
    issuance.add_argument("--reserve-micro-usd", required=True, type=int)
    issuance.add_argument("--ttl-seconds", type=int, default=900)
    issuance.add_argument("--max-requests", type=int, default=1)
    issuance.set_defaults(handler=issue)
    revocation = commands.add_parser("revoke")
    revocation.add_argument("--attempt-id", required=True)
    revocation.set_defaults(handler=revoke)
    state = commands.add_parser("status")
    state.add_argument("--attempt-id")
    state.set_defaults(handler=status)
    server = commands.add_parser("serve")
    server.add_argument("--listen-host", default="127.0.0.1")
    server.add_argument("--listen-port", type=int, default=8765)
    server.add_argument("--tls-cert", type=Path)
    server.add_argument("--tls-key", type=Path)
    server.add_argument("--allow-plaintext-loopback", action="store_true", help=argparse.SUPPRESS)
    server.set_defaults(handler=serve)
    return value


def main() -> None:
    try:
        args = parser().parse_args()
        result = args.handler(args)
        if result is not None:
            print(canonical(result))
    except (BrokerError, OSError, sqlite3.Error, ssl.SSLError, ValueError) as error:
        print(canonical({"error": str(error), "schema": OUTPUT_SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
